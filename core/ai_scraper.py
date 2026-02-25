"""
core/ai_scraper.py — Habla con IAs web y parsea sus respuestas como pasos ejecutables.
Gestiona múltiples sitios con fallback automático.
"""
import asyncio, re, json
from pathlib import Path
from core.browser import BrowserSession, AI_SITES, check_playwright, install_playwright, C

# ── Orden de prioridad de IAs web ─────────────────────────────────────────────
SITE_PRIORITY = ["claude", "chatgpt", "gemini", "qwen"]

# ── Prompt base que le enviamos a la IA web ────────────────────────────────────
def build_prompt(objetivo: str) -> str:
    """
    Prompt simple en lenguaje natural. Sin exigir JSON.
    La IA responde con comandos y pasos que el parser de texto maneja.
    """
    return (
        f"Necesito crear lo siguiente: {objetivo}\n\n"
        "Dame los comandos exactos para Windows CMD, uno por línea, numerados. "
        "Solo los comandos necesarios para crear el proyecto. "
        "No incluyas: ng serve, npm start, cd, verificaciones de versión ni explicaciones largas. "
        "Solo los comandos de instalación y creación del proyecto."
    )


def build_prompt_fix(objetivo: str, errores: list) -> str:
    """Prompt para corregir errores, también en lenguaje natural."""
    err_text = "\n".join(f"- {e.get('cmd','')}: {e.get('output','')[:200]}" for e in errores)
    return (
        f"Estaba creando: {objetivo}\n"
        f"Estos comandos fallaron:\n{err_text}\n\n"
        "Dame solo los comandos corregidos para Windows CMD, uno por línea."
    )


def parse_steps(response: str) -> list[dict]:
    """
    Parser simple: extrae comandos de una respuesta en lenguaje natural.
    Busca líneas que parezcan comandos de terminal.
    """
    steps = []
    seen = set()

    CMD_STARTS = (
        "npm ", "npx ", "ng ", "pip ", "python ", "node ",
        "mkdir ", "git ", "mvn ", "gradle ", "yarn ", "pnpm ",
        "vue ", "react-", "dotnet ", "cargo ", "go ",
    )
    SKIP = ("ng serve", "npm start", "npm run dev", "npm run serve",
            "cd ", "node -v", "npm -v", "ng version", "node --version")

    for line in response.splitlines():
        # Limpiar numeración y símbolos comunes
        line = line.strip()
        line = re.sub(r'^[\d]+[.):\-\s]+', '', line).strip()
        line = line.lstrip('`$>').strip()

        if not line or len(line) < 5:
            continue
        if any(line.lower().startswith(s) for s in SKIP):
            continue
        if any(line.startswith(s) for s in CMD_STARTS):
            if line not in seen:
                seen.add(line)
                steps.append({"type": "cmd", "value": line})

    return steps



def _parse_json_steps(data: dict) -> list[dict]:
    """
    Convierte JSON de la IA en pasos ejecutables.
    Maneja múltiples estructuras que distintas IAs pueden devolver.
    """
    steps = []
    meta  = data.get("meta", {})

    if meta:
        fw   = meta.get("framework", "")
        proj = meta.get("nombre_proyecto", "")
        if fw or proj:
            print(f"  {C.DIM}  Framework: {fw} | Proyecto: {proj}{C.RESET}")

    # ── Detectar qué estructura usó la IA ─────────────────────────────────
    # Estructura 1: {"flujo_creacion_app": [...]}  ← nuestro formato
    # Estructura 2: {"tarea": {"pasos": [...]}}    ← ChatGPT a veces usa esto
    # Estructura 3: {"pasos": [...]}               ← simplificado
    # Estructura 4: {"steps": [...]}               ← en inglés
    # Estructura 5: [...]                          ← array directo

    # ── Estructura especial: {"code": {"archivo": "contenido"}} ──────────
    # ChatGPT a veces devuelve los archivos directamente en "code"
    if "code" in data and isinstance(data["code"], dict):
        file_steps = []
        for filepath, file_content in data["code"].items():
            if isinstance(file_content, str) and file_content.strip():
                file_steps.append({
                    "type": "file",
                    "path": filepath,
                    "value": file_content,
                    "_accion": f"Crear {filepath}"
                })
        if file_steps:
            # Anteponer el comando ng new si hay archivos de Angular
            fw = data.get("meta", {}).get("framework", "").lower()
            proj = data.get("meta", {}).get("nombre_proyecto", "mi-app")
            if "angular" in fw:
                return [{"type": "cmd", "value": f"ng new {proj} --routing --style=css --no-analytics",
                         "_accion": "Crear proyecto Angular"}] + file_steps
            return file_steps

    flujo = (
        data.get("flujo_creacion_app") or
        data.get("pasos") or
        data.get("steps") or
        data.get("tarea", {}).get("pasos") or
        data.get("tarea", {}).get("steps") or
        (data if isinstance(data, list) else None) or
        []
    )

    # Normalizar cada item independientemente del formato
    for item in flujo:
        if not isinstance(item, dict):
            continue

        # Extraer campos con múltiples nombres posibles
        comando = (item.get("comando") or item.get("command") or
                   item.get("cmd") or "").strip()
        archivo = (item.get("archivo") or item.get("file") or
                   item.get("path") or "").strip()
        contenido = (item.get("contenido") or item.get("content") or
                     item.get("code") or "").strip()
        accion = (item.get("accion") or item.get("action") or
                  item.get("titulo") or item.get("title") or
                  item.get("descripcion") or item.get("description") or "").strip()
        detalle = (item.get("detalle") or item.get("detail") or "").strip()

        # Ignorar comandos que el sistema maneja internamente
        SKIP_CMDS = ["ng serve", "npm start", "npm run dev", "npm run serve"]
        if any(skip in comando for skip in SKIP_CMDS):
            continue
        # Ignorar cd — el sistema rastrea directorios
        if comando.lower().startswith("cd "):
            continue

        # Paso con comando
        if comando and comando.lower() != "null":
            steps.append({
                "type": "cmd", "value": comando,
                "_accion": accion,
                "_si_falla": item.get("si_falla") or item.get("on_error")
            })

        # Paso con archivo
        if archivo and archivo.lower() != "null" and contenido and contenido.lower() != "null":
            steps.append({
                "type": "file", "path": archivo, "value": contenido,
                "_accion": accion
            })

        # Solo informativo
        if not comando and not archivo and (accion or detalle):
            steps.append({"type": "info", "value": f"{accion}: {detalle}".strip(": ")})

    return steps


def _parse_text_steps(response: str) -> list[dict]:
    """Parser de texto plano como fallback si la IA no devuelve JSON."""
    steps = []
    lines = response.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        if not line or re.match(r'^\d+\.\s*$', line):
            i += 1; continue
        line = re.sub(r'^\d+[\.\)]\s*', '', line)

        if line.upper().startswith("CMD:"):
            steps.append({"type": "cmd", "value": line[4:].strip()})
        elif line.upper().startswith("FILE:"):
            file_path = line[5:].strip()
            content_lines = []
            i += 1
            if i < len(lines) and lines[i].strip().startswith("```"):
                i += 1
            while i < len(lines):
                if lines[i].strip() == "```": break
                content_lines.append(lines[i]); i += 1
            steps.append({"type": "file", "path": file_path,
                          "value": "\n".join(content_lines)})
        elif line.upper().startswith("INFO:"):
            steps.append({"type": "info", "value": line[5:].strip()})
        elif any(line.startswith(x) for x in
                 ("npm ", "npx ", "ng ", "pip ", "python ", "node ", "mkdir ", "git ")):
            steps.append({"type": "cmd", "value": line})
        i += 1

    return steps

# ── Función principal ──────────────────────────────────────────────────────────

async def ask_ai_web(objetivo: str,
                     preferred_site: str = None) -> tuple[str, list[dict]]:
    """
    Envía el objetivo a la mejor IA web disponible.
    Devuelve (respuesta_cruda, pasos_parseados).
    """
    if not check_playwright():
        install_playwright()

    sites_to_try = []
    if preferred_site and preferred_site in AI_SITES:
        sites_to_try = [preferred_site] + [s for s in SITE_PRIORITY if s != preferred_site]
    else:
        sites_to_try = SITE_PRIORITY

    for site_key in sites_to_try:
        print(f"\n  {C.CYAN}🌐 Intentando con {AI_SITES[site_key]['name']}...{C.RESET}")
        try:
            async with BrowserSession(site_key) as session:

                # Verificar login
                if await session.needs_login():
                    await session.wait_for_login()
                    # Recargar después del login
                    await session._page.goto(
                        AI_SITES[site_key]["url"],
                        wait_until="domcontentloaded",
                        timeout=30000
                    )

                # Construir y enviar prompt
                prompt = build_prompt(objetivo)
                print(f"  {C.DIM}  Enviando prompt a {AI_SITES[site_key]['name']}...{C.RESET}")
                response = await session.send_prompt(prompt)

                if not response or len(response) < 50:
                    print(f"  {C.YELLOW}  Respuesta muy corta, probando siguiente...{C.RESET}")
                    continue

                print(f"  {C.GREEN}  ✅ Respuesta recibida ({len(response)} caracteres){C.RESET}")

                # Parsear pasos
                steps = parse_steps(response)
                print(f"  {C.DIM}  {len(steps)} pasos detectados{C.RESET}")

                if steps:
                    return response, steps

                # Sin pasos detectados — continuar con siguiente IA
                print(f"  {C.YELLOW}  No se detectaron comandos en la respuesta{C.RESET}")
                continue

        except Exception as e:
            print(f"  {C.RED}  ❌ {AI_SITES[site_key]['name']} falló: {e}{C.RESET}")
            continue

    raise RuntimeError("Ninguna IA web estuvo disponible.")


def ask_ai_web_sync(objetivo: str, preferred_site: str = None) -> tuple[str, list[dict]]:
    """Versión síncrona para llamar desde código no-async."""
    return asyncio.run(ask_ai_web(objetivo, preferred_site))