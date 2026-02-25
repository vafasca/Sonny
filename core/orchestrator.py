"""
core/orchestrator.py — Orquestador principal de Sonny.
Flujo completo: entiende la tarea → consulta IA web → ejecuta pasos → corrige errores.
"""
import asyncio, os, subprocess, sys, re, json
from typing import Optional
from pathlib import Path
from datetime import datetime
from core.ai_scraper import ask_ai_web_sync, parse_steps, build_prompt
from core.browser    import AI_SITES, C
from core.prereqs    import scan_and_fix_prereqs, COMPATIBILITY

# ── Configuración ──────────────────────────────────────────────────────────────
WORKSPACE_ROOT  = Path(__file__).parent.parent / "workspace"
MAX_FIX_ATTEMPTS = 3    # cuántas veces pedirle a la IA que corrija un error
TIMEOUT_CMD      = 120  # segundos para comandos lentos (npm install, etc.)

# ── Detección de navegadores instalados ───────────────────────────────────────

BROWSER_PATHS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "edge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "firefox": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
    "brave": [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
}

def detectar_navegadores() -> list[str]:
    """Devuelve lista de navegadores instalados."""
    encontrados = []
    for name, paths in BROWSER_PATHS.items():
        for path in paths:
            if os.path.exists(path):
                encontrados.append(name)
                break
    return encontrados

# ── Ejecutor de pasos ──────────────────────────────────────────────────────────

# Respuestas automáticas para prompts interactivos de CLIs
# Angular, npm, etc. preguntan cosas durante la instalación
CLI_AUTO_ANSWERS = "y\nN\nCSS\ny\ny\ny\n"

# Variables de entorno para silenciar prompts interactivos de CLIs
CLI_ENV = {
    **os.environ,
    "NG_CLI_ANALYTICS": "false",      # Angular: no preguntar analytics
    "CI": "true",                      # Modo CI: desactiva prompts interactivos
    "npm_config_yes": "true",          # npm: auto-yes
}

def run_cmd(cmd: str, cwd: Path) -> tuple[bool, str]:
    """Ejecuta un comando de shell. Devuelve (ok, output)."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=str(cwd),
            capture_output=True, text=True,
            timeout=TIMEOUT_CMD,
            input=CLI_AUTO_ANSWERS,
            env=CLI_ENV,               # env sin prompts interactivos
            encoding="utf-8", errors="replace"
        )
        output = ""
        if result.stdout.strip():
            output += result.stdout.strip()
        if result.stderr.strip():
            stderr_lines = [l for l in result.stderr.strip().splitlines()
                           if not l.startswith("npm warn") and "ExperimentalWarning" not in l
                           and "analytics" not in l.lower()]
            if stderr_lines:
                output += ("\n" if output else "") + "[STDERR]\n" + "\n".join(stderr_lines)
        return result.returncode == 0, output or "(sin output)"
    except subprocess.TimeoutExpired:
        return False, f"[TIMEOUT] Comando tardó más de {TIMEOUT_CMD}s: {cmd}"
    except Exception as e:
        return False, f"[ERROR] {e}"

def write_file(path: Path, content: str):
    """Crea un archivo, generando las carpetas necesarias."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def _clean_angular_defaults(project_dir: Path, objetivo: str = ""):
    """
    Angular 19+: usa app.ts / app.html (sin 'component' en el nombre)
    Angular 18-: usa app.component.ts / app.component.html
    """
    app_dir = project_dir / "src" / "app"
    if not app_dir.exists():
        print(f"  {C.YELLOW}      -> src/app no encontrado{C.RESET}")
        return

    is_new = (app_dir / "app.ts").exists()

    html_file = app_dir / ("app.html"           if is_new else "app.component.html")
    ts_file   = app_dir / ("app.ts"             if is_new else "app.component.ts")
    css_file  = app_dir / ("app.css"            if is_new else "app.component.css")
    tmpl      = "./app.html"                     if is_new else "./app.component.html"
    style_url = "./app.css"                      if is_new else "./app.component.css"
    cls_name  = "App"                            if is_new else "AppComponent"

    # Remove wrongly-created app.component.* if we're in new-style Angular
    if is_new:
        for bad in ("app.component.html", "app.component.ts", "app.component.css"):
            bad_path = app_dir / bad
            if bad_path.exists():
                bad_path.unlink()

    # 1. Root HTML -> only router-outlet
    html_file.write_text("<router-outlet />\n", encoding="utf-8")

    # 2. Root TS -> minimal component
    ts_content = (
        "import { Component } from '@angular/core';\n"
        "import { RouterOutlet } from '@angular/router';\n\n"
        "@Component({\n"
        "  selector: 'app-root',\n"
        "  standalone: true,\n"
        "  imports: [RouterOutlet],\n"
        f"  templateUrl: '{tmpl}',\n"
        f"  styleUrl: '{style_url}'\n"
        "})\n"
        f"export class {cls_name} {{}}\n"
    )
    ts_file.write_text(ts_content, encoding="utf-8")

    # 3. Root CSS -> empty
    css_file.write_text("", encoding="utf-8")

    # 4. Extract display text from objetivo
    content_text = "Hola Mundo"
    if objetivo:
        for phrase in ["diga ", "que diga ", "muestre ", "que muestre ", "con texto "]:
            if phrase in objetivo.lower():
                idx = objetivo.lower().index(phrase) + len(phrase)
                content_text = objetivo[idx:].strip().capitalize()
                break

    # 5. Create hola-mundo component
    comp_dir = app_dir / "hola-mundo"
    comp_dir.mkdir(exist_ok=True)

    (comp_dir / "hola-mundo.component.html").write_text(
        f'<div class="container">\n  <h1>{content_text}</h1>\n</div>\n',
        encoding="utf-8"
    )
    (comp_dir / "hola-mundo.component.css").write_text(
        ".container {\n"
        "  display: flex;\n  justify-content: center;\n"
        "  align-items: center;\n  height: 100vh;\n"
        "  font-family: Arial, sans-serif;\n}\n"
        "h1 { color: #333; font-size: 2.5rem; }\n",
        encoding="utf-8"
    )
    (comp_dir / "hola-mundo.component.ts").write_text(
        "import { Component } from '@angular/core';\n"
        "import { CommonModule } from '@angular/common';\n\n"
        "@Component({\n"
        "  selector: 'app-hola-mundo',\n"
        "  standalone: true,\n"
        "  imports: [CommonModule],\n"
        "  templateUrl: './hola-mundo.component.html',\n"
        "  styleUrl: './hola-mundo.component.css'\n"
        "})\n"
        "export class HolaMundoComponent {}\n",
        encoding="utf-8"
    )

    # 6. app.routes.ts
    (app_dir / "app.routes.ts").write_text(
        "import { Routes } from '@angular/router';\n"
        "import { HolaMundoComponent } from './hola-mundo/hola-mundo.component';\n\n"
        "export const routes: Routes = [\n"
        "  { path: '', component: HolaMundoComponent },\n"
        "  { path: '**', redirectTo: '' }\n"
        "];\n",
        encoding="utf-8"
    )

    # 7. app.config.ts
    (app_dir / "app.config.ts").write_text(
        "import { ApplicationConfig } from '@angular/core';\n"
        "import { provideRouter } from '@angular/router';\n"
        "import { routes } from './app.routes';\n\n"
        "export const appConfig: ApplicationConfig = {\n"
        "  providers: [provideRouter(routes)]\n"
        "};\n",
        encoding="utf-8"
    )

    style_label = "Angular 19+ (app.ts)" if is_new else "Angular 18- (app.component.ts)"
    print(f"  {C.GREEN}      -> Patron router-outlet [{style_label}] OK{C.RESET}")
    print(f"  {C.DIM}         {html_file.name} -> <router-outlet />{C.RESET}")
    print(f"  {C.DIM}         hola-mundo/ -> HolaMundoComponent{C.RESET}")
    print(f"  {C.DIM}         app.routes.ts -> / -> HolaMundoComponent{C.RESET}")


def _find_project_root(workspace: Path) -> Path:
    """
    Busca la carpeta raíz del proyecto generado (donde está package.json o angular.json).
    Si hay una subcarpeta de proyecto, la devuelve. Si no, devuelve workspace.
    """
    # Buscar angular.json o package.json que no esté en node_modules
    for marker in ("angular.json", "package.json"):
        found = [p for p in workspace.rglob(marker) if "node_modules" not in str(p)]
        if found:
            return found[0].parent
    return workspace

def execute_steps(steps: list[dict], workspace: Path, objetivo: str = '') -> list[dict]:
    """
    Ejecuta la lista de pasos en el workspace.
    Devuelve lista de resultados con errores si los hay.
    """
    results     = []
    project_dir = workspace   # se actualiza dinámicamente cuando se crea el proyecto

    for i, step in enumerate(steps, 1):
        stype = step.get("type")
        value = step.get("value", "")

        if stype == "cmd":
            # Ignorar "cd" — manejamos directorios nosotros
            if value.strip().lower().startswith("cd "):
                print(f"  {C.DIM}  [{i}] CD (ignorado — manejamos rutas interno){C.RESET}")
                # Actualizar project_dir si existe esa carpeta
                target = value.strip()[3:].strip()
                candidate = workspace / target
                if candidate.is_dir():
                    project_dir = candidate
                results.append({"step": i, "type": stype, "cmd": value,
                                "ok": True, "output": "(cd interno)"})
                continue

            print(f"  {C.CYAN}  [{i}] CMD: {value}{C.RESET}")
            ok, output = run_cmd(value, project_dir)
            if ok:
                print(f"  {C.GREEN}      ✅{C.RESET} {C.DIM}{output[:120]}{C.RESET}")
                # Después de ng new / create-react-app, buscar el proyecto creado
                if any(x in value for x in ("ng new", "create-react-app", "npx create-")):
                    new_root = _find_project_root(workspace)
                    if new_root != workspace:
                        project_dir = new_root
                        print(f"  {C.DIM}      → proyecto en: {project_dir.name}{C.RESET}")
                    # Deshabilitar analytics desde el inicio (evita el prompt en ng serve)
                    if "ng new" in value:
                        subprocess.run("ng analytics disable --global", shell=True,
                                       cwd=str(project_dir), capture_output=True, env=CLI_ENV)
                        subprocess.run("ng analytics disable", shell=True,
                                       cwd=str(project_dir), capture_output=True, env=CLI_ENV)
                    # Implementar patrón router-outlet + componente
                    _clean_angular_defaults(project_dir, objetivo)
            else:
                print(f"  {C.RED}      ❌ Error:{C.RESET}")
                for line in output.splitlines()[:8]:
                    print(f"  {C.DIM}      {line}{C.RESET}")
            results.append({"step": i, "type": stype, "cmd": value,
                            "ok": ok, "output": output})

        elif stype == "file":
            rel_path  = step.get("path", f"archivo_{i}.txt")
            # Rutas relativas cortas como "src/app/app.component.html"
            # deben resolverse dentro del project_dir, no del workspace raíz
            fpath = project_dir / rel_path
            write_file(fpath, value)
            rel_display = fpath.relative_to(workspace) if fpath.is_relative_to(workspace) else fpath
            print(f"  {C.GREEN}  [{i}] FILE: {rel_display} ✅{C.RESET}")
            results.append({"step": i, "type": stype,
                            "path": str(fpath), "ok": True, "output": ""})

        elif stype == "info":
            print(f"  {C.YELLOW}  [{i}] INFO: {value[:100]}{C.RESET}")
            results.append({"step": i, "type": stype,
                            "ok": True, "output": value})

    return results

def has_errors(results: list[dict]) -> list[dict]:
    """Filtra solo los pasos que fallaron."""
    return [r for r in results if not r.get("ok")]

# ── Prompt de corrección de errores ───────────────────────────────────────────

def build_fix_prompt(objetivo: str, error_results: list[dict]) -> str:
    errors_json = json.dumps([
        {"paso": r.get("step"), "cmd": r.get("cmd",""), "error": r.get("output","")}
        for r in error_results
    ], ensure_ascii=False, indent=2)

    return f"""Eres un agente de desarrollo. Estaba ejecutando esta tarea: {objetivo}

Los siguientes pasos fallaron:
{errors_json}

Analiza los errores y devuelve SOLO JSON con los pasos corregidos:
{{
  "diagnostico": "<qué causó los errores>",
  "flujo_creacion_app": [
    {{
      "paso": 1,
      "accion": "<título>",
      "detalle": "<qué hace este paso>",
      "comando": "<comando exacto o null>",
      "archivo": "<ruta/archivo o null>",
      "contenido": "<contenido completo del archivo o null>",
      "herramienta": "terminal | editor",
      "si_falla": "<alternativa o null>"
    }}
  ]
}}

Solo incluye los pasos necesarios para corregir. No repitas los que ya funcionaron.
Los comandos deben funcionar en Windows."""



def _detect_framework_from_text(text: str) -> Optional[str]:
    """Detecta el framework mencionado en el texto del usuario."""
    low = text.lower()
    fw_map = {
        "angular": "angular", "react": "react", "vue": "vue",
        "next.js": "nextjs", "nextjs": "nextjs", "next ": "nextjs",
        "svelte": "svelte",
    }
    for kw, fw in fw_map.items():
        if kw in low:
            return fw
    return None

# ── Lanzador inteligente de proyectos ─────────────────────────────────────────

def _detect_project_type(workspace: Path, steps: list[dict]) -> str:
    """Detecta el tipo de proyecto por archivos y comandos usados."""
    cmds = " ".join(s.get("value","") for s in steps if s.get("type") == "cmd").lower()
    files = [str(f) for f in workspace.rglob("*")]

    if "ng serve" in cmds or "angular.json" in str(files):
        return "angular"
    if "npm start" in cmds or "react" in cmds or any("package.json" in f for f in files):
        return "node"
    if "flask" in cmds or "django" in cmds or "uvicorn" in cmds:
        return "python_server"
    if any(f.endswith("index.html") and "node_modules" not in f for f in files):
        return "static_html"
    return "unknown"

def _launch_project(workspace: Path, steps: list[dict]):
    """Lanza el proyecto de la forma correcta según su tipo."""
    ptype = _detect_project_type(workspace, steps)

    # Abrir carpeta siempre
    print(f"\n  {C.DIM}Abriendo carpeta del proyecto...{C.RESET}")
    try:
        os.startfile(str(workspace))
    except Exception:
        pass

    if ptype == "angular":
        angular_roots = [p.parent for p in workspace.rglob("angular.json")]
        project_dir   = angular_roots[0] if angular_roots else workspace

        # Deshabilitar analytics globalmente y en el proyecto (silencia el prompt)
        subprocess.run("ng analytics disable --global", shell=True,
                       cwd=str(project_dir), capture_output=True, env=CLI_ENV)
        subprocess.run("ng analytics disable", shell=True,
                       cwd=str(project_dir), capture_output=True, env=CLI_ENV)

        print(f"\n  {C.GREEN}🔺 Proyecto Angular detectado — levantando servidor...{C.RESET}")
        print(f"  {C.DIM}  El navegador se abrirá en http://localhost:4200{C.RESET}")
        print(f"  {C.DIM}  Presiona Ctrl+C para detener.{C.RESET}\n")
        try:
            subprocess.run("ng serve --open", shell=True, cwd=str(project_dir), env=CLI_ENV)
        except KeyboardInterrupt:
            print(f"\n  {C.YELLOW}Servidor detenido.{C.RESET}")

    elif ptype == "node":
        # Detectar carpeta con package.json
        pkg_files = [p for p in workspace.rglob("package.json")
                     if "node_modules" not in str(p)]
        project_dir = pkg_files[0].parent if pkg_files else workspace
        print(f"\n  {C.GREEN}📦 Proyecto Node detectado{C.RESET}")
        print(f"  {C.YELLOW}¿Levantar servidor? {C.DIM}(npm start){C.RESET}")
        resp = input(f"  {C.CYAN}tú > {C.RESET}").strip().lower()
        if resp and resp[0] in ("s","y"):
            try:
                subprocess.run("npm start", shell=True, cwd=str(project_dir))
            except KeyboardInterrupt:
                print(f"\n  {C.YELLOW}Servidor detenido.{C.RESET}")

    elif ptype == "python_server":
        # Detectar el comando de servidor en los pasos
        server_cmd = next(
            (s["value"] for s in steps
             if s.get("type") == "cmd" and
             any(x in s["value"] for x in ("flask", "uvicorn", "python app", "python manage"))),
            None
        )
        if server_cmd:
            print(f"\n  {C.GREEN}🐍 Servidor Python detectado{C.RESET}")
            print(f"  {C.YELLOW}¿Levantar servidor? {C.DIM}({server_cmd}){C.RESET}")
            resp = input(f"  {C.CYAN}tú > {C.RESET}").strip().lower()
            if resp and resp[0] in ("s","y"):
                try:
                    subprocess.run(server_cmd, shell=True, cwd=str(workspace))
                except KeyboardInterrupt:
                    print(f"\n  {C.YELLOW}Servidor detenido.{C.RESET}")

    elif ptype == "static_html":
        # HTML estático — abrir directamente en navegador
        html_files = [p for p in workspace.rglob("index.html")
                      if "node_modules" not in str(p)]
        if not html_files:
            html_files = [p for p in workspace.rglob("*.html")
                          if "node_modules" not in str(p)]
        if html_files:
            print(f"\n  {C.GREEN}🌐 Abriendo en el navegador...{C.RESET}")
            try:
                os.startfile(str(html_files[0]))
            except Exception:
                pass
    else:
        print(f"  {C.DIM}Proyecto en: {workspace}{C.RESET}")



def _fallback_steps(fw_key: str, objetivo: str) -> list[dict]:
    """
    Pasos mínimos predefinidos cuando la IA no responde bien.
    El orquestador ya maneja la limpieza/configuración de archivos.
    """
    # Extraer nombre del proyecto del objetivo o usar default
    nombre = "mi-app"
    for word in objetivo.lower().split():
        if len(word) > 3 and word not in ("una", "app", "aplicacion", "proyecto",
                                           "hola", "mundo", "angular", "react", "vue"):
            nombre = word.replace("á","a").replace("é","e").replace("í","i")
            break

    if fw_key == "angular":
        return [
            {"type": "cmd", "value": f"ng new {nombre} --routing --style=css --no-analytics",
             "_accion": "Crear proyecto Angular"}
        ]
    elif fw_key == "react":
        return [
            {"type": "cmd", "value": f"npx create-react-app {nombre}",
             "_accion": "Crear proyecto React"}
        ]
    elif fw_key == "vue":
        return [
            {"type": "cmd", "value": f"npm create vue@latest {nombre} -- --no-router --no-vitest",
             "_accion": "Crear proyecto Vue"}
        ]
    elif fw_key == "nextjs":
        return [
            {"type": "cmd",
             "value": f"npx create-next-app@latest {nombre} --no-tailwind --no-eslint",
             "_accion": "Crear proyecto Next.js"}
        ]
    return []

# ── Orquestador principal ──────────────────────────────────────────────────────

def run_orchestrator(objetivo: str, preferred_site: str = None) -> bool:
    """
    Flujo completo:
    1. Detectar navegadores
    2. Consultar IA web
    3. Ejecutar pasos
    4. Corregir errores si los hay
    5. Abrir resultado
    """
    print(f"\n{C.CYAN}{C.BOLD}  ╔══════════════════════════════════════╗")
    print(f"  ║   🤖 ORQUESTADOR SONNY              ║")
    print(f"  ╚══════════════════════════════════════╝{C.RESET}")
    print(f"  {C.DIM}Objetivo: {objetivo}{C.RESET}\n")

    # ── Paso 1: Detectar navegadores ──────────────────────────────────────
    navegadores = detectar_navegadores()
    if navegadores:
        print(f"  {C.GREEN}🌐 Navegadores disponibles: {', '.join(navegadores)}{C.RESET}")
    else:
        print(f"  {C.YELLOW}⚠️  No se detectó Chrome/Edge/Firefox. Playwright usará Chromium propio.{C.RESET}")

    # ── Paso 2: Crear workspace ────────────────────────────────────────────
    safe = re.sub(r'[^\w\-]', '_', objetivo.lower())[:35]
    ts   = datetime.now().strftime("%H%M%S")
    workspace = WORKSPACE_ROOT / f"{safe}_{ts}"
    workspace.mkdir(parents=True, exist_ok=True)
    print(f"  {C.DIM}Workspace: {workspace}{C.RESET}\n")

    # ── Paso 3: Prerequisitos ─────────────────────────────────────────────
    # Detectar el framework del objetivo para escanear prerequisitos
    fw_key = _detect_framework_from_text(objetivo)
    if fw_key:
        prereq_result = scan_and_fix_prereqs(fw_key)
        if prereq_result.get("actions_manual"):
            print(f"  {C.RED}  ⚠️  Hay prerequisitos que requieren instalación manual.{C.RESET}")
            print(f"  {C.YELLOW}  ¿Continuar de todas formas? {C.DIM}(s/n){C.RESET}")
            resp = input(f"  {C.CYAN}tú > {C.RESET}").strip().lower()
            if resp and resp[0] not in ("s", "y"):
                return False

    # ── Paso 4: Consultar IA web ───────────────────────────────────────────
    print(f"  {C.BOLD}📡 Consultando IA web...{C.RESET}")
    try:
        response, steps = ask_ai_web_sync(objetivo, preferred_site)
    except Exception as e:
        print(f"  {C.RED}❌ No se pudo obtener respuesta de la IA web: {e}{C.RESET}")
        return False

    if not steps:
        # Fallback: generar pasos básicos según el framework detectado
        fw_key = _detect_framework_from_text(objetivo)
        print(f"  {C.YELLOW}⚠️  No se parsearon pasos de la IA — usando pasos predefinidos para {fw_key}{C.RESET}")
        steps = _fallback_steps(fw_key, objetivo)
        if not steps:
            print(f"  {C.RED}❌ No hay pasos para ejecutar.{C.RESET}")
            return False

    # Mostrar resumen de pasos
    print(f"\n  {C.BOLD}📋 Plan de ejecución ({len(steps)} pasos):{C.RESET}")
    for i, step in enumerate(steps, 1):
        stype  = step.get("type", "?")
        accion = step.get("_accion", "")
        label  = f" [{accion}]" if accion else ""
        if stype == "cmd":
            print(f"  {C.DIM}  {i}.{label} CMD: {step['value'][:65]}{C.RESET}")
        elif stype == "file":
            print(f"  {C.DIM}  {i}.{label} FILE: {step.get('path','?')}{C.RESET}")
        elif stype == "info":
            print(f"  {C.DIM}  {i}.{label} INFO: {step['value'][:65]}{C.RESET}")

    # ── Paso 4: Ejecutar automáticamente ────────────────────────────────
    print(f"\n  {C.BOLD}⚡ Ejecutando pasos...{C.RESET}\n")
    results = execute_steps(steps, workspace, objetivo)
    errors  = has_errors(results)

    # ── Paso 5: Corregir errores ──────────────────────────────────────────
    attempt = 0
    while errors and attempt < MAX_FIX_ATTEMPTS:
        attempt += 1
        print(f"\n  {C.YELLOW}⚠️  {len(errors)} paso(s) con error. Consultando IA para corrección ({attempt}/{MAX_FIX_ATTEMPTS})...{C.RESET}")

        try:
            fix_prompt_text = build_fix_prompt(objetivo, errors)
            # Reusar la misma sesión pidiendo la corrección
            fix_response, fix_steps = ask_ai_web_sync(fix_prompt_text, preferred_site)

            if fix_steps:
                print(f"  {C.CYAN}  Aplicando {len(fix_steps)} correcciones...{C.RESET}\n")
                fix_results = execute_steps(fix_steps, workspace)
                errors = has_errors(fix_results)
            else:
                print(f"  {C.DIM}  Sin pasos de corrección.{C.RESET}")
                break

        except Exception as e:
            print(f"  {C.RED}  Error en corrección: {e}{C.RESET}")
            break

    # ── Resultado final ────────────────────────────────────────────────────
    print(f"\n  {'─'*50}")
    if not errors:
        print(f"\n  {C.GREEN}{C.BOLD}  ✅ TAREA COMPLETADA SIN ERRORES{C.RESET}")
    else:
        print(f"\n  {C.YELLOW}{C.BOLD}  ⚠️  COMPLETADO CON {len(errors)} ERROR(ES) SIN RESOLVER{C.RESET}")

    print(f"  {C.DIM}Archivos en: {workspace}{C.RESET}")

    # Listar archivos creados
    all_files = list(workspace.rglob("*"))
    files_only = [f for f in all_files if f.is_file()]
    if files_only:
        print(f"\n  {C.DIM}Archivos generados:{C.RESET}")
        for f in files_only[:15]:
            rel = f.relative_to(workspace)
            print(f"  {C.GREEN}    📄 {rel}{C.RESET}")
        if len(files_only) > 15:
            print(f"  {C.DIM}    ... y {len(files_only)-15} más{C.RESET}")

    # ── Detectar tipo de proyecto y lanzar correctamente ─────────────────
    _launch_project(workspace, steps)
    print()
    return not errors


def run_orchestrator_with_site(objetivo: str) -> bool:
    """
    Versión que pregunta al usuario qué IA usar si hay varias.
    """
    print(f"\n  {C.BOLD}¿Qué IA quieres consultar?{C.RESET}")
    options = list(AI_SITES.keys())
    for i, key in enumerate(options, 1):
        name = AI_SITES[key]["name"]
        print(f"  {C.CYAN}  {i}. {name}{C.RESET}")
    print(f"  {C.DIM}  0. Automático (primer disponible){C.RESET}")

    resp = input(f"  {C.CYAN}tú > {C.RESET}").strip()
    try:
        idx = int(resp)
        site = options[idx - 1] if 1 <= idx <= len(options) else None
    except (ValueError, IndexError):
        site = None

    return run_orchestrator(objetivo, preferred_site=site)