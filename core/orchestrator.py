"""
core/orchestrator.py — Orquestador Sonny v11.1

CAMBIOS v11.1:
  - Parser _parse_structured: ahora detiene la lectura de FILE al llegar al siguiente PASO
  - Parser: ignora etiquetas de lenguaje sueltas (TypeScript, SCSS, HTML) sin backticks
  - Agrega npm install automático si ng new usó --skip-install
  - _launch con guard para no ejecutarse más de una vez
"""
import os, subprocess, re
from typing import Optional
from pathlib import Path
from datetime import datetime
from core.ai_scraper import ask_ai_multiturn
from core.browser    import AI_SITES
from core.web_log    import log_session_start, log_error

class C:
    CYAN="\033[96m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"
    BOLD="\033[1m";  DIM="\033[2m";    RESET="\033[0m";   MAGENTA="\033[95m"
    BLUE="\033[94m"

WORKSPACE_ROOT   = Path(__file__).parent.parent / "workspace"
MAX_FIX_ATTEMPTS = 3
TIMEOUT_CMD      = 180
CLI_AUTO_ANSWERS = "y\nN\nCSS\ny\ny\ny\n"
CLI_ENV = {
    **os.environ,
    "NG_CLI_ANALYTICS": "false",
    "CI":               "true",
    "npm_config_yes":   "true",
}
BROWSER_PATHS = {
    "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
               r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
    "edge":   [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
               r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
}

# Etiquetas de lenguaje que ChatGPT/Claude escriben SIN backticks
# Ejemplo: después de FILE: ruta.ts ponen "TypeScript" en línea sola
_BARE_LANG_LABELS = {
    "typescript", "javascript", "python", "html", "css", "scss",
    "sass", "json", "bash", "shell", "xml", "yaml", "sql",
    "java", "kotlin", "swift", "go", "rust", "ruby", "php",
    "ts", "js", "py", "sh",
}

def detectar_navegadores() -> list[str]:
    return [n for n, pp in BROWSER_PATHS.items() if any(os.path.exists(p) for p in pp)]

_MAX_LINE = 110

def P(text: str = "", end: str = "\n"):
    if len(text) > _MAX_LINE:
        text = text[:_MAX_LINE - 3] + "..."
    print(text, end=end, flush=True)

# ══════════════════════════════════════════════════════════════════════════════
#   SISTEMA
# ══════════════════════════════════════════════════════════════════════════════

def _run(cmd: str, cwd: Path, timeout: int = TIMEOUT_CMD) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout, input=CLI_AUTO_ANSWERS,
                           env=CLI_ENV, encoding="utf-8", errors="replace")
        out = r.stdout.strip()
        if r.stderr.strip():
            bad = [l for l in r.stderr.strip().splitlines()
                   if not l.startswith("npm warn")
                   and "ExperimentalWarning" not in l
                   and "analytics" not in l.lower()]
            if bad:
                out += ("\n" if out else "") + "[STDERR]\n" + "\n".join(bad)
        return r.returncode == 0, out or "(sin output)"
    except subprocess.TimeoutExpired:
        return False, f"[TIMEOUT] {cmd}"
    except Exception as e:
        return False, f"[ERROR] {e}"

def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def _find_project_root(workspace: Path) -> Path:
    for m in ("angular.json", "package.json"):
        found = [p for p in workspace.rglob(m) if "node_modules" not in str(p)]
        if found: return found[0].parent
    return workspace

def _strip_ansi(s: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', s)

# ══════════════════════════════════════════════════════════════════════════════
#   CONSULTA A LA IA
# ══════════════════════════════════════════════════════════════════════════════

def _ask_web(prompt: str, preferred_site: str, objetivo: str) -> str:
    try:
        _, [resp] = ask_ai_multiturn([prompt], preferred_site, objetivo)
        return resp or ""
    except Exception as e:
        log_error(preferred_site or "web", str(e))
        P(f"  {C.RED}  ❌ Error consultando IA web: {e}{C.RESET}")
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#   VERIFICACIÓN DE HERRAMIENTAS
# ══════════════════════════════════════════════════════════════════════════════

_TOOL_VERSION_CMDS = {
    "node":         "node --version",
    "node.js":      "node --version",
    "nodejs":       "node --version",
    "npm":          "npm --version",
    "npx":          "npx --version",
    "angular":      "ng version",
    "angular cli":  "ng version",
    "@angular/cli": "ng version",
    "ng":           "ng version",
    "typescript":   "tsc --version",
    "git":          "git --version",
    "python":       "python --version",
    "java":         "java --version",
    "docker":       "docker --version",
    "yarn":         "yarn --version",
    "pnpm":         "pnpm --version",
}

def _extract_version(raw: str) -> str:
    raw = re.sub(r'\x1b\[[0-9;]*m', '', raw)
    m = re.search(r'v?(\d+\.\d+[\.\d]*)', raw)
    return m.group(1) if m else raw.strip().split("\n")[0][:40]

def _check_tools_from_list(resp_prereq: str) -> dict:
    result = {}
    text_lower = resp_prereq.lower()
    matched: dict[str, str] = {}

    for keyword, cmd in sorted(_TOOL_VERSION_CMDS.items(), key=lambda x: -len(x[0])):
        if keyword in text_lower and cmd not in matched.values():
            display = keyword.title().replace(".Js",".js").replace("@Angular/Cli","Angular CLI")
            matched[display] = cmd

    if not matched:
        matched = {"Node.js": "node --version", "npm": "npm --version"}

    for display, cmd in matched.items():
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=8, encoding="utf-8", errors="replace")
            raw = (r.stdout + r.stderr).strip()

            if "ng version" in cmd:
                ver = ""
                for line in raw.splitlines():
                    lc = re.sub(r'\x1b\[[0-9;]*m', '', line).lower().strip()
                    m2 = re.search(r'angular\s+cli\s*:\s*([\d]+\.[\d]+\.[\d]+)', lc)
                    if m2: ver = m2.group(1); break
                if not ver:
                    m2 = re.search(r'(\d{2,3}\.\d+\.\d+)', raw)
                    ver = m2.group(1) if m2 else _extract_version(raw)
                version = ver
            else:
                version = _extract_version(raw)

            ok = r.returncode == 0 and bool(version)
            result[display] = {"cmd": cmd, "version": version or "instalado", "ok": ok}
        except Exception as e:
            result[display] = {"cmd": cmd, "version": f"error: {e}", "ok": False}

    return result


# ══════════════════════════════════════════════════════════════════════════════
#   ESCANEO DE ESTRUCTURA REAL DEL PROYECTO
# ══════════════════════════════════════════════════════════════════════════════

SKIP_DIRS = {"node_modules", ".git", "dist", ".angular", "__pycache__", ".vscode"}
KEY_EXTS  = {".html", ".css", ".ts", ".scss"}


def _scan_project(project_dir: Path) -> tuple[str, dict]:
    tree_lines = []
    key_files  = {}

    def _walk(path: Path, prefix: str = "", depth: int = 0):
        if depth > 6: return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return
        for i, entry in enumerate(entries):
            if entry.name in SKIP_DIRS: continue
            conn = "└── " if i == len(entries) - 1 else "├── "
            tree_lines.append(f"{prefix}{conn}{entry.name}")
            if entry.is_dir():
                _walk(entry, prefix + ("    " if i == len(entries)-1 else "│   "), depth+1)
            elif entry.suffix in KEY_EXTS and entry.stat().st_size < 6000:
                rel = str(entry.relative_to(project_dir)).replace("\\", "/")
                try:
                    key_files[rel] = entry.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    _walk(project_dir)
    return "\n".join(tree_lines), key_files

# ══════════════════════════════════════════════════════════════════════════════
#   PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

def _p1_prereqs(objetivo: str) -> str:
    obj = objetivo.rstrip(". ")
    return (
        f"Necesito {obj}. Dime ÚNICAMENTE qué debo tener instalado para crear esa aplicación. "
        f"No me des tutoriales ni explicaciones largas, solo la lista de requisitos."
    )

def _p2_steps_create(objetivo: str, verified_tools: dict) -> str:
    tools_str = ", ".join(f"{n} {v}" for n, i in verified_tools.items()
                          if i["ok"] for n, v in [(n, i["version"])])
    return (
        f"Ya tengo instalado: {tools_str}. "
        f"Necesito {objetivo}. "
        f"Dame SOLO el comando para crear el proyecto desde cero (ng new ...). "
        f"Responde ÚNICAMENTE con el comando, sin explicaciones."
    )


def _p2_steps(objetivo: str, verified_tools: dict,
              tree: str = "", key_files: dict = None) -> str:
    tools_str = ", ".join(f"{n} {i['version']}" for n, i in verified_tools.items() if i["ok"])

    files_ctx = ""
    if key_files:
        for rel, content in list(key_files.items())[:8]:
            snippet = content.strip()[:80].replace("\n", " ")
            files_ctx += f"  {rel}: {snippet}\n"

    return (
        f"Ya tengo instalado: {tools_str}. "
        f"El proyecto ya fue creado con ng new. "
        f"Esta es la estructura REAL de archivos:\n"
        f"```\n{tree}\n```\n\n"
        f"Contenido actual de los archivos clave:\n{files_ctx}\n"
        f"TAREA: {objetivo}\n\n"
        f"Dame los pasos para modificar los archivos y lograr la tarea.\n"
        f"USA EXACTAMENTE este formato para cada paso, sin cambiar nada:\n\n"
        f"PASO 1: descripción corta\n"
        f"CMD: comando exacto (o NINGUNO si no hay comando)\n"
        f"FILE: ruta/exacta/del/archivo.ext\n"
        f"```\n"
        f"contenido completo del archivo\n"
        f"```\n\n"
        f"REGLAS:\n"
        f"1. Usa SOLO las rutas que aparecen en la estructura de arriba\n"
        f"2. NO incluyas: ng new, ng serve, cd\n"
        f"3. Cada FILE debe tener el contenido COMPLETO\n"
        f"4. Solo los pasos. Sin introducciones ni conclusiones."
    )


def _p_fix(objetivo: str, paso_desc: str, cmd_ejecutado: str,
           error: str, verified_tools: dict) -> str:
    tools_str = ", ".join(f"{n} {i['version']}" for n, i in verified_tools.items() if i["ok"])
    err_clean = error[:400].replace('\n', ' ').strip()
    return (
        f"Estoy creando {objetivo} con {tools_str}. "
        f"Falló el paso '{paso_desc}' al ejecutar '{cmd_ejecutado}' con el error: {err_clean}. "
        f"Dame los pasos corregidos para solucionar este error. Solo pasos y comandos, sin explicaciones."
    )

# ══════════════════════════════════════════════════════════════════════════════
#   PARSER — VERSIÓN CORREGIDA
# ══════════════════════════════════════════════════════════════════════════════

def _parse_plan(response: str) -> list[dict]:
    steps = _parse_structured(response)
    if not steps:
        steps = _parse_natural(response)

    BLOCK = ("npm install -g",)
    result = []
    for step in steps:
        cmd = step.get("cmd", "") or ""
        if any(cmd.lower().startswith(b) for b in BLOCK):
            step["cmd"] = None
        if cmd.lower().startswith("ng serve") or cmd.lower().startswith("npm start"):
            step["_is_serve"] = True
        if step.get("cmd") or step.get("files"):
            result.append(step)
    return result


def _is_bare_lang_label(line: str) -> bool:
    """
    Detecta si una línea es solo una etiqueta de lenguaje suelta que ChatGPT
    a veces escribe sin backticks después de FILE:
    Ejemplos: 'TypeScript', 'SCSS', 'HTML', 'JavaScript'
    """
    stripped = line.strip()
    return stripped.lower() in _BARE_LANG_LABELS


def _parse_structured(response: str) -> list[dict]:
    """
    Parser principal. Entiende el formato:
      PASO N: descripción
      CMD: comando (o NINGUNO)
      FILE: ruta/archivo.ext
      ```
      contenido
      ```

    FIXES v11.1:
    - Detiene la lectura de FILE al encontrar el siguiente PASO (no solo en ```)
    - Ignora etiquetas de lenguaje sueltas sin backticks (TypeScript, SCSS, etc.)
    - Soporta contenido de FILE tanto con backticks como sin ellos
    """
    steps, lines, i = [], response.splitlines(), 0

    while i < len(lines):
        m = re.match(r'^PASO\s+\d+\s*[:\-]\s*(.+)', lines[i].strip(), re.IGNORECASE)
        if not m:
            i += 1
            continue

        step = {"desc": m.group(1).strip(), "cmd": None, "files": [], "_is_serve": False}
        i += 1

        while i < len(lines):
            l = lines[i].strip()

            # ── Siguiente PASO → terminar este step ──────────────────────────
            if re.match(r'^PASO\s+\d+\s*[:\-]', l, re.IGNORECASE):
                break

            # ── CMD: ─────────────────────────────────────────────────────────
            mc = re.match(r'^CMD\s*:\s*(.+)', l, re.IGNORECASE)
            if mc:
                v = mc.group(1).strip()
                if v.upper() not in ("NINGUNO", "NONE", "N/A", ""):
                    step["cmd"] = v
                i += 1
                continue

            # ── FILE: ────────────────────────────────────────────────────────
            mf = re.match(r'^FILE\s*:\s*(.+)', l, re.IGNORECASE)
            if mf:
                fpath = mf.group(1).strip()
                i += 1
                cl = []

                # Saltar línea en blanco después de FILE:
                if i < len(lines) and lines[i].strip() == "":
                    i += 1

                # Saltar etiqueta de lenguaje suelta (TypeScript, HTML, etc.)
                if i < len(lines) and _is_bare_lang_label(lines[i]):
                    i += 1

                # Detectar si usa backticks o no
                uses_backticks = i < len(lines) and lines[i].strip().startswith("```")
                if uses_backticks:
                    i += 1  # saltar la línea de apertura ```

                # Leer contenido hasta:
                #   - ``` de cierre (si usa backticks)
                #   - Siguiente PASO (siempre)
                #   - Siguiente FILE o CMD (sin backticks)
                while i < len(lines):
                    current = lines[i]
                    stripped_current = current.strip()

                    # Cierre de bloque con backticks
                    if uses_backticks and stripped_current.startswith("```"):
                        i += 1
                        break

                    # Siguiente PASO → terminar lectura del archivo
                    if re.match(r'^PASO\s+\d+\s*[:\-]', stripped_current, re.IGNORECASE):
                        break

                    # Si no usamos backticks: FILE: o CMD: también terminan el contenido
                    if not uses_backticks:
                        if re.match(r'^FILE\s*:', stripped_current, re.IGNORECASE):
                            break
                        if re.match(r'^CMD\s*:', stripped_current, re.IGNORECASE):
                            break

                    cl.append(current)
                    i += 1

                # Limpiar líneas vacías al final del contenido
                while cl and cl[-1].strip() == "":
                    cl.pop()

                if fpath and cl:
                    step["files"].append({"path": fpath, "content": "\n".join(cl)})
                continue

            i += 1

        if step["cmd"] or step["files"]:
            steps.append(step)

    return steps


_SRC_RE  = re.compile(r'\bsrc/[\w/.\-]+\.\w{1,5}\b')
_ITEM_RE = re.compile(r'^[*\-]\s+(?:[Aa]rchivo\s*[:\-]\s*)?[`\'""]?(\S+\.\w{1,5})[`\'""]?')
_STEP_RE = re.compile(
    r'^(?:#{1,3}\s*)?(?:\d+[️⃣°]?\s*)?(?:paso|step)\s*[\d️⃣°]*\s*[:\-]?\s*(.*)',
    re.IGNORECASE
)
_CMD_OK   = ("ng ", "npm install", "npx ", "git ")
_CMD_SKIP = ("npm install -g",)
_LANG_DEF = {
    "html": "src/app/app.component.html",
    "css":  "src/app/app.component.css",
    "typescript": "src/app/app.component.ts",
    "ts":   "src/app/app.component.ts",
}


def _parse_natural(response: str) -> list[dict]:
    steps, lines, i = [], response.splitlines(), 0
    cur: dict | None = None
    last_path = ""

    def _flush():
        nonlocal cur
        if cur and (cur["cmd"] or cur["files"]): steps.append(cur)
        cur = None

    while i < len(lines):
        line = lines[i].strip(); i += 1
        pm = _STEP_RE.match(line)
        if pm:
            _flush()
            cur = {"desc": pm.group(1).strip() or line, "cmd": None,
                   "files": [], "_is_serve": False}
            continue
        am = _ITEM_RE.match(line)
        if am: last_path = am.group(1).strip()
        sm = _SRC_RE.search(line)
        if sm: last_path = sm.group(0)
        clean = re.sub(r'^[`$>\s]+', '', line)
        if any(clean.lower().startswith(s) for s in _CMD_OK):
            if not any(clean.lower().startswith(s) for s in _CMD_SKIP):
                if cur is None:
                    cur = {"desc": clean[:60], "cmd": None, "files": [], "_is_serve": False}
                if not cur["cmd"]: cur["cmd"] = clean
        fm = re.match(r'^```(\w*)', line)
        if fm:
            lang = fm.group(1).lower()
            cl   = []
            while i < len(lines):
                if lines[i].strip().startswith("```"): i += 1; break
                cl.append(lines[i]); i += 1
            content = "\n".join(cl).strip()
            if not content: continue
            fpath = last_path or _LANG_DEF.get(lang, "")
            if fpath:
                if cur is None:
                    cur = {"desc": f"Editar {fpath}", "cmd": None,
                           "files": [], "_is_serve": False}
                if fpath not in [f["path"] for f in cur["files"]]:
                    cur["files"].append({"path": fpath, "content": content})
                last_path = ""
    _flush()
    return steps

# ══════════════════════════════════════════════════════════════════════════════
#   EJECUTOR DE PASOS
# ══════════════════════════════════════════════════════════════════════════════

def _exec_step(step: dict, project_dir: Path,
               step_num: int, total: int) -> tuple[bool, str]:
    desc  = step.get("desc", "")
    cmd   = step.get("cmd")
    files = step.get("files", [])

    P(f"\n  {C.BOLD}{C.BLUE}┌─ Paso {step_num}/{total}: {desc}{C.RESET}")

    if cmd:
        P(f"  {C.CYAN}│  🖥  {cmd}{C.RESET}")
        ok, out = _run(cmd, project_dir)
        if ok:
            for l in [x for x in out.splitlines() if x.strip()][:5]:
                P(f"  {C.DIM}│    {l}{C.RESET}")
            P(f"  {C.GREEN}│  ✅ OK{C.RESET}")
            new_root = _find_project_root(project_dir)
            if new_root != project_dir and new_root.exists():
                step["_new_dir"] = new_root
                P(f"  {C.DIM}│  → Proyecto en: {new_root.name}{C.RESET}")
        else:
            P(f"  {C.RED}│  ❌ Error:{C.RESET}")
            for l in out.splitlines()[:10]:
                P(f"  {C.DIM}│    {l}{C.RESET}")
            return False, out

    for fi in files:
        rel     = fi.get("path", "").strip()
        content = fi.get("content", "")
        if not rel: continue
        _write(project_dir / rel, content)
        P(f"  {C.GREEN}│  📝 {rel} ({len(content)} chars){C.RESET}")

    P(f"  {C.GREEN}└─ ✅ Completado{C.RESET}")
    return True, ""

# ══════════════════════════════════════════════════════════════════════════════
#   LANZADOR — con guard para no ejecutarse más de una vez
# ══════════════════════════════════════════════════════════════════════════════

_launched = False   # guard global

def _launch(workspace: Path, project_dir: Path):
    global _launched
    if _launched:
        return
    _launched = True

    P(f"\n  {C.DIM}Abriendo carpeta del proyecto...{C.RESET}")
    try:
        os.startfile(str(workspace))
    except Exception:
        pass

    ng_jsons = list(project_dir.rglob("angular.json"))
    if ng_jsons:
        pdir = ng_jsons[0].parent
        subprocess.run("ng analytics disable --global", shell=True,
                       cwd=str(pdir), capture_output=True, env=CLI_ENV)
        P(f"\n  {C.GREEN}{C.BOLD}🚀 Angular listo — http://localhost:4200{C.RESET}")
        P(f"  {C.DIM}  Ctrl+C para detener{C.RESET}\n")
        try:
            subprocess.run("ng serve --open", shell=True, cwd=str(pdir), env=CLI_ENV)
        except KeyboardInterrupt:
            P(f"\n  {C.YELLOW}  Servidor detenido.{C.RESET}")
        return

    pkgs = [p for p in project_dir.rglob("package.json") if "node_modules" not in str(p)]
    if pkgs:
        pdir = pkgs[0].parent
        r = input(f"  {C.YELLOW}¿Levantar servidor? (npm start) (s/n) > {C.RESET}").strip().lower()
        if r and r[0] in ("s", "y"):
            try:
                subprocess.run("npm start", shell=True, cwd=str(pdir))
            except KeyboardInterrupt:
                pass


def _detect_framework(text: str) -> Optional[str]:
    low = text.lower()
    for kw, fw in [("angular","angular"),("react","react"),("vue","vue"),
                   ("next.js","nextjs"),("nextjs","nextjs"),("svelte","svelte")]:
        if kw in low: return fw
    return None

# ══════════════════════════════════════════════════════════════════════════════
#   ORQUESTADOR PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def run_orchestrator(objetivo: str, preferred_site: str = None) -> bool:
    global _launched
    _launched = False   # reset guard por si se llama varias veces en la misma sesión

    site_name = (AI_SITES.get(preferred_site, {}).get("name", "IA automática")
                 if preferred_site else "IA automática")

    P(f"\n{C.CYAN}{C.BOLD}  ╔══════════════════════════════════════╗")
    P(f"  ║   🤖 ORQUESTADOR SONNY  v11.1       ║")
    P(f"  ╚══════════════════════════════════════╝{C.RESET}")
    P(f"  {C.DIM}Objetivo : {objetivo}{C.RESET}")
    P(f"  {C.DIM}Cerebro  : {site_name} (navegador web){C.RESET}")
    P(f"  {C.DIM}Cuerpo   : Sonny (ejecuta comandos){C.RESET}\n")

    navs = detectar_navegadores()
    P(f"  {C.GREEN}🌐 Navegadores: {', '.join(navs) or 'Chromium interno'}{C.RESET}")

    safe = re.sub(r'[^\w\-]', '_', objetivo.lower())[:35]
    ts   = datetime.now().strftime("%H%M%S")
    workspace = WORKSPACE_ROOT / f"{safe}_{ts}"
    workspace.mkdir(parents=True, exist_ok=True)
    P(f"  {C.DIM}Workspace: {workspace}{C.RESET}\n")

    log_session_start(objetivo)
    verified_tools = {}

    # ══════════════════════════════════════════════════════════════════════
    #  TURNO 1 — ¿qué herramientas necesito?
    # ══════════════════════════════════════════════════════════════════════
    P(f"  {C.BOLD}{C.MAGENTA}━━━ TURNO 1 → {site_name}: herramientas necesarias ━━━{C.RESET}\n")
    P(f"  {C.DIM}  Abriendo navegador...{C.RESET}\n")

    p1 = _p1_prereqs(objetivo)
    resp_prereq = _ask_web(p1, preferred_site, objetivo)

    if not resp_prereq:
        P(f"  {C.RED}  ❌ Sin respuesta. Verifica el navegador.{C.RESET}")
        return False

    P(f"\n  {C.CYAN}  💬 {site_name} dice:{C.RESET}")
    for l in resp_prereq.strip().splitlines()[:10]:
        if l.strip(): P(f"  {C.DIM}    {l.strip()}{C.RESET}")
    P("")

    # ══════════════════════════════════════════════════════════════════════
    #  SONNY verifica herramientas
    # ══════════════════════════════════════════════════════════════════════
    P(f"  {C.BOLD}{C.MAGENTA}━━━ SONNY verifica herramientas en tu sistema ━━━{C.RESET}\n")

    verified_tools = _check_tools_from_list(resp_prereq)
    all_ok = True
    for tool_name, info in verified_tools.items():
        icon = f"{C.GREEN}  ✅{C.RESET}" if info["ok"] else f"{C.RED}  ❌{C.RESET}"
        P(f"  {icon} {tool_name:<22} {info['version']}")
        if not info["ok"]: all_ok = False

    if not all_ok:
        P(f"\n  {C.YELLOW}  ⚠️  Hay herramientas faltantes.{C.RESET}")
        r = input(f"  {C.YELLOW}  ¿Continuar de todas formas? (s/n) > {C.RESET}").strip().lower()
        if r and r[0] not in ("s","y"): return False
    else:
        P(f"\n  {C.GREEN}  ✅ Todo instalado. Listo para continuar.{C.RESET}\n")

    # ══════════════════════════════════════════════════════════════════════
    #  TURNO 2 — comando de creación
    # ══════════════════════════════════════════════════════════════════════
    P(f"  {C.BOLD}{C.MAGENTA}━━━ TURNO 2 → {site_name}: comando de creación ━━━{C.RESET}\n")

    p2_create = _p2_steps_create(objetivo, verified_tools)
    resp_create = _ask_web(p2_create, preferred_site, objetivo)

    if not resp_create:
        P(f"  {C.RED}  ❌ Sin respuesta del Turno 2.{C.RESET}")
        return False

    P(f"  {C.CYAN}  💬 {site_name} — comando recibido:{C.RESET}")
    P(f"  {C.DIM}    {resp_create.strip()[:100]}{C.RESET}\n")

    # Extraer el ng new del response
    create_cmd = ""
    for line in resp_create.splitlines():
        clean = line.strip().lstrip("`$ ").lstrip("Bash").lstrip("bash").strip()
        if clean.lower().startswith("ng new"):
            create_cmd = clean
            break
    if not create_cmd:
        create_cmd = "ng new mi-app --style=css --skip-git --defaults"
        P(f"  {C.YELLOW}  ⚠️  No se detectó ng new — usando: {create_cmd}{C.RESET}")

    # Detectar si la IA usó --skip-install para correr npm install después
    used_skip_install = "--skip-install" in create_cmd

    # ══════════════════════════════════════════════════════════════════════
    #  SONNY ejecuta ng new
    # ══════════════════════════════════════════════════════════════════════
    P(f"  {C.BOLD}{C.MAGENTA}━━━ SONNY crea el proyecto ━━━{C.RESET}\n")
    P(f"  {C.CYAN}  🖥  {create_cmd}{C.RESET}")

    ok, err = _run(create_cmd, workspace)
    if not ok:
        P(f"  {C.RED}  ❌ ng new falló:{C.RESET}")
        for l in err.splitlines()[:8]: P(f"  {C.DIM}    {l}{C.RESET}")
        return False

    project_dir = _find_project_root(workspace)
    P(f"  {C.GREEN}  ✅ Proyecto creado: {project_dir.name}{C.RESET}")
    subprocess.run("ng analytics disable --global", shell=True,
                   cwd=str(project_dir), capture_output=True, env=CLI_ENV)

    # ── npm install si se usó --skip-install ──────────────────────────────
    if used_skip_install:
        P(f"\n  {C.CYAN}  📦 Instalando dependencias (npm install)...{C.RESET}")
        ok_npm, out_npm = _run("npm install", project_dir, timeout=300)
        if ok_npm:
            P(f"  {C.GREEN}  ✅ Dependencias instaladas{C.RESET}")
        else:
            P(f"  {C.YELLOW}  ⚠️  npm install tuvo advertencias (puede continuar):{C.RESET}")
            for l in out_npm.splitlines()[:5]:
                P(f"  {C.DIM}    {l}{C.RESET}")

    # ══════════════════════════════════════════════════════════════════════
    #  SONNY escanea estructura real
    # ══════════════════════════════════════════════════════════════════════
    P(f"\n  {C.BOLD}{C.MAGENTA}━━━ SONNY escanea estructura real ━━━{C.RESET}\n")
    tree, key_files = _scan_project(project_dir)
    P(f"  {C.DIM}  Archivos detectados:{C.RESET}")
    for f in list(key_files.keys())[:10]:
        P(f"  {C.DIM}    📄 {f}{C.RESET}")
    if len(key_files) > 10:
        P(f"  {C.DIM}    ... y {len(key_files)-10} más{C.RESET}")

    # ══════════════════════════════════════════════════════════════════════
    #  TURNO 3 — pasos con estructura real
    # ══════════════════════════════════════════════════════════════════════
    P(f"\n  {C.BOLD}{C.MAGENTA}━━━ TURNO 3 → {site_name}: pasos con estructura real ━━━{C.RESET}\n")
    P(f"  {C.DIM}  Enviando estructura real del proyecto...{C.RESET}\n")

    p3 = _p2_steps(objetivo, verified_tools, tree, key_files)
    resp_steps = _ask_web(p3, preferred_site, objetivo)

    if not resp_steps:
        P(f"  {C.RED}  ❌ Sin plan del Turno 3.{C.RESET}")
        return False

    P(f"  {C.CYAN}  💬 {site_name} — plan recibido:{C.RESET}")
    for l in resp_steps.strip().splitlines()[:8]:
        P(f"  {C.DIM}    {l.strip()[:100]}{C.RESET}")
    P(f"  {C.DIM}    ...{C.RESET}\n")

    steps = _parse_plan(resp_steps)

    if not steps:
        P(f"  {C.YELLOW}  ⚠️  No se encontraron pasos ejecutables.{C.RESET}")
        log_error(site_name, f"No steps parsed: {resp_steps[:300]}")
        return False

    serve_steps = [s for s in steps if s.get("_is_serve")]
    exec_steps  = [s for s in steps if not s.get("_is_serve")]

    P(f"  {C.GREEN}  ✅ Plan: {len(exec_steps)} paso(s) + {len(serve_steps)} de inicio{C.RESET}\n")
    P(f"  {C.BOLD}  📋 Resumen de pasos a ejecutar:{C.RESET}")
    for idx, s in enumerate(exec_steps, 1):
        P(f"  {C.CYAN}    {idx}. {s['desc'][:70]}{C.RESET}")
        if s.get("cmd"):
            P(f"  {C.DIM}       CMD:  {s['cmd'][:65]}{C.RESET}")
        if s.get("files"):
            paths = ', '.join(f["path"] for f in s["files"])[:65]
            P(f"  {C.DIM}       FILE: {paths}{C.RESET}")

    # ══════════════════════════════════════════════════════════════════════
    #  SONNY ejecuta los pasos uno por uno
    # ══════════════════════════════════════════════════════════════════════
    P(f"\n  {C.BOLD}{C.MAGENTA}━━━ SONNY ejecuta {len(exec_steps)} paso(s) ━━━{C.RESET}")

    total = len(exec_steps)
    for step_num, step in enumerate(exec_steps, 1):
        ok, error_out = _exec_step(step, project_dir, step_num, total)

        if step.get("_new_dir"):
            project_dir = step["_new_dir"]
            tree, key_files = _scan_project(project_dir)

        if not ok:
            fixed = False
            for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
                P(f"\n  {C.YELLOW}  ⚠️  Error paso {step_num}. "
                  f"Consultando {site_name} ({attempt}/{MAX_FIX_ATTEMPTS})...{C.RESET}\n")

                ejecutado = step.get("cmd") or str([f["path"] for f in step.get("files",[])])
                fix_p = _p_fix(objetivo, step["desc"], ejecutado, error_out, verified_tools)
                fix_resp = _ask_web(fix_p, preferred_site, objetivo)
                if not fix_resp: break

                P(f"  {C.CYAN}  💬 {site_name} — corrección:{C.RESET}")
                for l in fix_resp.strip().splitlines()[:6]:
                    P(f"  {C.DIM}    {l.strip()[:100]}{C.RESET}")

                fix_steps = [s for s in _parse_plan(fix_resp) if not s.get("_is_serve")]
                if not fix_steps:
                    P(f"  {C.YELLOW}  Sin pasos de corrección.{C.RESET}"); break

                all_fix_ok = True
                for fi, fs in enumerate(fix_steps, 1):
                    fok, ferr = _exec_step(fs, project_dir, fi, len(fix_steps))
                    if fs.get("_new_dir"): project_dir = fs["_new_dir"]
                    if not fok: all_fix_ok = False; error_out = ferr; break

                if all_fix_ok:
                    P(f"  {C.GREEN}  ✅ Corrección exitosa{C.RESET}")
                    fixed = True; break

            if not fixed:
                P(f"\n  {C.RED}  ❌ Paso {step_num} sin resolver.{C.RESET}")
                r = input(f"  {C.YELLOW}¿Continuar? (s/n) > {C.RESET}").strip().lower()
                if not r or r[0] not in ("s","y"):
                    P(f"  {C.RED}  Detenido.{C.RESET}"); return False

    # ══════════════════════════════════════════════════════════════════════
    #  FIN
    # ══════════════════════════════════════════════════════════════════════
    P(f"\n  {'─'*54}")
    P(f"  {C.GREEN}{C.BOLD}  ✅ COMPLETADO{C.RESET}")
    P(f"  {C.DIM}  Proyecto en: {project_dir}{C.RESET}")

    files_only = [f for f in workspace.rglob("*")
                  if f.is_file()
                  and "node_modules" not in str(f)
                  and ".angular" not in str(f)]
    if files_only:
        P(f"\n  {C.DIM}Archivos del proyecto:{C.RESET}")
        for f in sorted(files_only)[:20]:
            P(f"  {C.GREEN}    📄 {f.relative_to(workspace)}{C.RESET}")
        if len(files_only) > 20:
            P(f"  {C.DIM}    ... y {len(files_only)-20} más{C.RESET}")

    _launch(workspace, project_dir)
    return True


def run_orchestrator_with_site(objetivo: str) -> bool:
    P(f"\n  {C.BOLD}¿Qué IA quieres consultar?{C.RESET}")
    options = list(AI_SITES.keys())
    for i, key in enumerate(options, 1):
        P(f"  {C.CYAN}  {i}. {AI_SITES[key]['name']}{C.RESET}")
    P(f"  {C.DIM}  0. Automático (primer disponible){C.RESET}")
    resp = input(f"  {C.CYAN}tú > {C.RESET}").strip()
    try:
        idx  = int(resp)
        site = options[idx-1] if 1 <= idx <= len(options) else None
    except (ValueError, IndexError):
        site = None
    return run_orchestrator(objetivo, preferred_site=site)