"""
core/orchestrator.py  v11.4 — Orquestador Sonny

BUG-FIXES CRÍTICOS (confirmados en log de prueba):
  - "Código"/"código" como etiqueta de lenguaje → TS2304 en TODOS los archivos
    FIX: añadido a _BARE_LANG_LABELS + segunda pasada en _sanitize_content
  - Angular 17+ standalone: ChatGPT crea app.module.ts → conflicto NG1010
    FIX: _autofix_angular_standalone() lo elimina + reglas de arquitectura en prompts
  - ngModel sin FormsModule → NG8002 persistente
    FIX: FormsModule auto-añadido a componentes standalone con ngModel
  - Anti-loop inefectivo (hash cambia pero mismos error codes)
    FIX v2: rastrea set de error codes entre rondas; si persisten → estrategia diferente

FASE 2 — nuevas mejoras:
  7 → Contexto mejorado: app.config.ts/main.ts/app.routes.ts completos en Turno 3
  8 → Validación semántica heurística tras build exitoso (no bloqueante)
  9 → JSONL logging: log_build_error, log_fix_applied, log_autofix, log_session_end
 11 → Validación de dependencias antes de ejecutar pasos
"""

import os, subprocess, re, shutil, json, hashlib
from pathlib import Path
from datetime import datetime
from core.ai_scraper import ask_ai_multiturn
from core.browser    import AI_SITES
from core.web_log    import (
    log_session_start, log_error,
    log_build_error, log_fix_applied, log_autofix,
    log_dependency_warning, log_session_end,
)

class C:
    CYAN="[96m"; GREEN="[92m"; YELLOW="[93m"; RED="[91m"
    BOLD="[1m";  DIM="[2m";    RESET="[0m";   MAGENTA="[95m"
    BLUE="[94m"

WORKSPACE_ROOT      = Path(__file__).parent.parent / "workspace"
MAX_FIX_ATTEMPTS    = 3
TIMEOUT_CMD         = 120
TIMEOUT_NG_NEW      = 60
TIMEOUT_NPM_INSTALL = 600
CLI_AUTO_ANSWERS    = "y\nN\nCSS\ny\ny\ny\n"
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

# ═══════════════════════════════════════════════════════════════════
#   BUG FIX #1: "Código"/"codigo" añadidos a etiquetas de lenguaje
#   ChatGPT en español escribe "Código\nimport..." sin backticks
# ═══════════════════════════════════════════════════════════════════
_BARE_LANG_LABELS = {
    # Inglés
    "typescript","javascript","python","html","css","scss","sass",
    "json","bash","shell","xml","yaml","sql","java","kotlin","swift",
    "go","rust","ruby","php","ts","js","py","sh","jsx","tsx",
    "text","plaintext","plain","output","console","csharp","c#",
    "c++","cpp","dockerfile","makefile","angular","vue","react",
    # Español — ChatGPT en español usa estas etiquetas sueltas
    "código","codigo",
    # Con dos puntos (ej: "Código:\n...")
    "código:","codigo:","typescript:","javascript:","html:","css:",
    "python:","bash:","json:",
}

_FUNCTIONAL_WARNINGS = {
    "NG8001","NG8002","NG0303",
    "is not a known element",
    "Can't bind to",
    "is not a module",
    "has no exported member",
    "Cannot find module",
    "has no properties in common",
    "is not assignable to type",
}

def _has_functional_warnings(output: str) -> bool:
    return any(w in output for w in _FUNCTIONAL_WARNINGS)

def _extract_error_codes(output: str) -> set:
    """Extrae códigos de error TypeScript/Angular (TS2304, NG1010, etc.)"""
    return set(re.findall(r'(?:TS|NG)\d{4}', output))

def detectar_navegadores() -> list:
    return [n for n, pp in BROWSER_PATHS.items() if any(os.path.exists(p) for p in pp)]

_MAX_LINE = 110
def P(text: str = "", end: str = "\n"):
    if len(text) > _MAX_LINE:
        text = text[:_MAX_LINE-3] + "..."
    print(text, end=end, flush=True)

# ═══════════════════════════════════════════════════════════════════
#   SANITIZACIÓN — BUG FIX #1 integrado
# ═══════════════════════════════════════════════════════════════════
_CONTENT_PREFIXES = {
    "aquí tienes","aquí está","aqui tienes","aqui esta",
    "here's the","here is the","here's","here is",
    "código:","code:","solución:","solution:",
    "el archivo","the file","contenido:","content:",
}

def _is_bare_lang_label(line: str) -> bool:
    stripped = line.strip().lower().rstrip(":")
    return stripped in _BARE_LANG_LABELS or line.strip().lower() in _BARE_LANG_LABELS

def _sanitize_content(content: str) -> str:
    if not content:
        return content
    lines = content.splitlines()
    # 1. Bloque de backticks
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    # 2. Etiqueta de lenguaje (primera pasada — puede ser "TypeScript", "Código", etc.)
    if lines and _is_bare_lang_label(lines[0]):
        lines = lines[1:]
    # 3. Segunda pasada (a veces hay dos etiquetas seguidas)
    if lines and _is_bare_lang_label(lines[0]):
        lines = lines[1:]
    # 4. Frase introductoria de la IA
    if lines:
        first_clean = lines[0].strip().lower().rstrip(":")
        if any(first_clean.startswith(p) for p in _CONTENT_PREFIXES):
            lines = lines[1:]
    # 5. Limpiar vacíos inicio/final
    while lines and not lines[0].strip(): lines = lines[1:]
    while lines and not lines[-1].strip(): lines = lines[:-1]
    return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════════
#   DETECCIÓN DE VERSIÓN ANGULAR
# ═══════════════════════════════════════════════════════════════════
def _get_ng_major(project_dir: Path) -> int:
    pkg_path = project_dir / "package.json"
    if pkg_path.exists():
        try:
            data = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            deps = {**data.get("dependencies",{}), **data.get("devDependencies",{})}
            ver = deps.get("@angular/core","").lstrip("^~>=< ")
            m = re.search(r'^(\d+)', ver)
            if m:
                major = int(m.group(1))
                P(f"  {C.DIM}  Angular v{major} detectado{C.RESET}")
                return major
        except: pass
    if (project_dir / "src/app/app.config.ts").exists():
        return 17
    if list(project_dir.rglob("app.module.ts")):
        return 15
    return 17

# ═══════════════════════════════════════════════════════════════════
#   HASH DE ARCHIVOS
# ═══════════════════════════════════════════════════════════════════
def _get_files_hash(project_dir: Path) -> str:
    hasher = hashlib.md5()
    skip = {"node_modules",".angular","dist","__pycache__"}
    for ext in (".ts",".html",".css",".scss",".json"):
        for f in sorted(project_dir.rglob(f"*{ext}")):
            if set(f.parts) & skip: continue
            try:
                hasher.update(str(f.relative_to(project_dir)).encode())
                hasher.update(f.read_bytes())
            except: pass
    return hasher.hexdigest()

# ═══════════════════════════════════════════════════════════════════
#   FASE 2 — Mejora 7: CONTEXTO DE ARCHIVOS CLAVE
# ═══════════════════════════════════════════════════════════════════
_KEY_CONFIGS_MODERN = [
    "src/app/app.config.ts","src/main.ts","src/app/app.routes.ts","angular.json"
]
_KEY_CONFIGS_LEGACY = [
    "src/app/app.module.ts","src/main.ts","src/app/app-routing.module.ts"
]

def _get_key_context_files(project_dir: Path, ng_major: int) -> dict:
    paths = _KEY_CONFIGS_MODERN if ng_major >= 17 else _KEY_CONFIGS_LEGACY
    result, total = {}, 0
    for rel in paths:
        fp = project_dir / rel
        if not fp.exists(): continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
            if total + len(content) > 8000:
                remaining = 8000 - total
                if remaining > 200:
                    result[rel] = content[:remaining] + "\n// ... (truncado)"
                break
            result[rel] = content
            total += len(content)
        except: pass
    return result

# ═══════════════════════════════════════════════════════════════════
#   FASE 2 — Mejora 8: VALIDACIÓN SEMÁNTICA
# ═══════════════════════════════════════════════════════════════════
_SEMANTIC_MAP = [
    (r'\brojo\b',       ["red","rojo","#f","danger","#e"]),
    (r'\bazul\b',       ["blue","azul","#0","primary","#2","#3"]),
    (r'\bverde\b',      ["green","verde","success","#4","#2a"]),
    (r'\blogin\b',      ["login","LoginComponent","sign-in","auth"]),
    (r'\btabla\b',      ["<table","mat-table","ngFor","@for","table"]),
    (r'\bcalcul',        ["calculate","calc","result","compute"]),
    (r'\bformulario\b', ["<form","FormGroup","FormControl","NgForm"]),
    (r'\bdashboard\b',  ["dashboard","DashboardComponent","panel"]),
    (r'\bgráfica|grafica\b', ["chart","Chart","graph","canvas","recharts","d3"]),
]

def _semantic_validation_warning(objetivo: str, project_dir: Path):
    low_obj = objetivo.lower()
    matched = [(p,t) for p,t in _SEMANTIC_MAP if re.search(p, low_obj)]
    if not matched: return
    all_content = ""
    for ext in (".ts",".html",".css",".scss"):
        for f in project_dir.rglob(f"*{ext}"):
            if "node_modules" not in str(f) and ".angular" not in str(f):
                try: all_content += f.read_text(encoding="utf-8", errors="replace").lower()
                except: pass
    missing = []
    for pat, terms in matched:
        if not any(t.lower() in all_content for t in terms):
            concept = re.sub(r'\\b|\\|\'', '', pat).strip()
            missing.append(concept)
    if missing:
        P(f"\n  {C.YELLOW}  ⚠️  Validación semántica — posibles ausencias:{C.RESET}")
        for c in missing:
            P(f"  {C.YELLOW}      • '{c}' no encontrado en el código{C.RESET}")
        P(f"  {C.DIM}    (puede ser falso positivo — revisar manualmente){C.RESET}")

# ═══════════════════════════════════════════════════════════════════
#   FASE 2 — Mejora 11: VALIDACIÓN DE DEPENDENCIAS
# ═══════════════════════════════════════════════════════════════════
_BUILTIN_PKGS = {
    "path","fs","os","http","https","url","crypto","events","stream",
    "@angular/core","@angular/common","@angular/forms","@angular/router",
    "@angular/platform-browser","@angular/platform-browser/animations",
    "@angular/animations","@angular/cdk","@angular/material",
    "rxjs","zone.js","tslib",
}

def _validate_dependencies(project_dir: Path, steps: list) -> list:
    pkg_path = project_dir / "package.json"
    if not pkg_path.exists(): return []
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        all_deps = (set(pkg.get("dependencies",{}).keys()) |
                    set(pkg.get("devDependencies",{}).keys()))
    except: return []
    warnings, seen = [], set()
    for step in steps:
        for fi in step.get("files",[]):
            if not fi.get("path","").endswith(".ts"): continue
            for m in re.finditer(r"""from\s+[\'\"](@?[\w][\w/_-]*)[\'\"]""", fi.get("content","")):
                pkg_name = m.group(1)
                scope = "/".join(pkg_name.split("/")[:2]) if pkg_name.startswith("@") else pkg_name.split("/")[0]
                if pkg_name.startswith(".") or scope in _BUILTIN_PKGS: continue
                if scope not in all_deps:
                    w = f"'{scope}' usado en {fi['path']} pero no está en package.json"
                    if w not in seen: seen.add(w); warnings.append(w)
    return warnings

# ═══════════════════════════════════════════════════════════════════
#   BUG FIX #2+3: AUTO-FIX ANGULAR STANDALONE
#   Elimina app.module.ts conflictivo, añade FormsModule, RouterLink, CommonModule
# ═══════════════════════════════════════════════════════════════════
def _inject_module_standalone(ts_content: str, module_name: str, from_pkg: str) -> str:
    """Inyecta módulo(s) en imports[] de @Component standalone."""
    first_mod = module_name.split(",")[0].strip()
    # 1. Añadir import statement si el primer módulo no está
    if first_mod not in ts_content:
        last_pos = 0
        for m in re.finditer(r'^import .+ from', ts_content, re.MULTILINE):
            last_pos = m.start()
        if last_pos:
            insert = ts_content.find('\n', last_pos) + 1
            ts_content = (ts_content[:insert]
                         + f"import {{ {module_name} }} from '{from_pkg}';\n"
                         + ts_content[insert:])
        else:
            ts_content = f"import {{ {module_name} }} from '{from_pkg}';\n" + ts_content
    # 2. Añadir al array imports[]
    def add_to_arr(m):
        arr = m.group(1)
        mods = [mod.strip() for mod in module_name.split(",")]
        new = [mod for mod in mods if mod not in arr]
        if new:
            prefix = ", ".join(new) + (", " if arr.strip() else "")
            return m.group(0).replace(arr, prefix + arr.lstrip())
        return m.group(0)
    ts_content = re.sub(r'imports\s*:\s*\[([^\]]*)\]', add_to_arr, ts_content, count=1)
    # 3. Si no hay imports[], añadirlo
    if not re.search(r'imports\s*:', ts_content):
        ts_content = re.sub(r'(@Component\s*\(\s*\{)',
                            rf'\1\n  imports: [{module_name}],',
                            ts_content, count=1)
    return ts_content

def _autofix_angular_standalone(project_dir: Path, ng_major: int) -> list:
    fixes = []
    # Fix 1: Eliminar app.module.ts si coexiste con app.config.ts (Angular 17+)
    if ng_major >= 17:
        mod_file = project_dir / "src/app/app.module.ts"
        cfg_file = project_dir / "src/app/app.config.ts"
        if mod_file.exists() and cfg_file.exists():
            mod_file.unlink()
            fixes.append("app.module.ts eliminado")
            P(f"  {C.YELLOW}  🗑  app.module.ts eliminado (conflicto standalone){C.RESET}")
            try: log_autofix("delete_ngmodule", ["src/app/app.module.ts"], ng_major)
            except: pass
    # Fix 2-4: por cada componente
    for ts_file in sorted(project_dir.rglob("*.component.ts")):
        if "node_modules" in str(ts_file) or ".angular" in str(ts_file): continue
        try: ts_content = ts_file.read_text(encoding="utf-8", errors="replace")
        except: continue
        # Template HTML
        html_file = ts_file.with_suffix(".html")
        if not html_file.exists():
            html_file = ts_file.parent / (ts_file.stem + ".html")
        html = ""
        if html_file.exists():
            try: html = html_file.read_text(encoding="utf-8", errors="replace")
            except: pass
        changed = False; comp = ts_file.name
        # Fix 2: FormsModule para [(ngModel)]
        if ng_major >= 17 and re.search(r'\[\(ngModel\)\]|ngModel', html):
            if "FormsModule" not in ts_content:
                ts_content = _inject_module_standalone(ts_content, "FormsModule", "@angular/forms")
                fixes.append(f"{comp}: FormsModule")
                P(f"  {C.YELLOW}  🔧 FormsModule añadido → {comp}{C.RESET}")
                try: log_autofix("add_formsmodule", [comp], ng_major)
                except: pass
                changed = True
        # Fix 3: RouterLink/RouterOutlet
        if ng_major >= 17:
            need_link   = bool(re.search(r'routerLink\b|\[routerLink\]', html))
            need_outlet = bool(re.search(r'<router-outlet', html or ts_content))
            has_router  = "RouterLink" in ts_content or "RouterOutlet" in ts_content
            if (need_link or need_outlet) and not has_router:
                mods = []
                if need_link:   mods.append("RouterLink")
                if need_outlet: mods.append("RouterOutlet")
                mod_str = ", ".join(mods)
                ts_content = _inject_module_standalone(ts_content, mod_str, "@angular/router")
                fixes.append(f"{comp}: {mod_str}")
                P(f"  {C.YELLOW}  🔧 {mod_str} añadido → {comp}{C.RESET}")
                try: log_autofix("add_router", [comp], ng_major)
                except: pass
                changed = True
        # Fix 4: CommonModule para *ngIf/*ngFor
        if re.search(r'\*ng(If|For|Switch|Class|Style)\b', html):
            if "CommonModule" not in ts_content and "NgIf" not in ts_content:
                if ng_major >= 17:
                    ts_content = _inject_module_standalone(ts_content, "CommonModule", "@angular/common")
                    fixes.append(f"{comp}: CommonModule")
                    P(f"  {C.YELLOW}  🔧 CommonModule añadido → {comp}{C.RESET}")
                    try: log_autofix("add_commonmodule", [comp], ng_major)
                    except: pass
                    changed = True
        if changed:
            try: ts_file.write_text(ts_content, encoding="utf-8")
            except Exception as e:
                P(f"  {C.RED}  Error escribiendo {comp}: {e}{C.RESET}")
    if fixes:
        P(f"  {C.GREEN}  ✅ Auto-fix standalone: {len(fixes)} corrección(es){C.RESET}")
    return fixes

# ═══════════════════════════════════════════════════════════════════
#   SISTEMA
# ═══════════════════════════════════════════════════════════════════
def _run(cmd: str, cwd: Path, timeout: int = TIMEOUT_CMD) -> tuple:
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
            if bad: out += ("\n" if out else "") + "[STDERR]\n" + "\n".join(bad)
        return r.returncode == 0, out or "(sin output)"
    except subprocess.TimeoutExpired:
        return False, f"[TIMEOUT] {cmd} superó {timeout}s."
    except Exception as e:
        return False, f"[ERROR] {e}"

def _run_npm_install(cwd: Path) -> tuple:
    P(f"\n  {C.CYAN}  📦 npm install (puede tardar 1-3 min)...{C.RESET}")
    ok, out = _run("npm install", cwd, timeout=TIMEOUT_NPM_INSTALL)
    if ok: P(f"  {C.GREEN}  ✅ npm install completado{C.RESET}")
    else:
        P(f"  {C.RED}  ❌ npm install falló:{C.RESET}")
        for l in out.splitlines()[:15]: P(f"  {C.DIM}    {l}{C.RESET}")
    return ok, out

def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_sanitize_content(content), encoding="utf-8")

def _find_project_root(workspace: Path) -> Path:
    for m in ("angular.json","package.json"):
        found = [p for p in workspace.rglob(m) if "node_modules" not in str(p)]
        if found: return found[0].parent
    return workspace

def _force_skip_install(cmd: str) -> str:
    if "ng new" in cmd.lower() and "--skip-install" not in cmd:
        cmd = cmd.rstrip() + " --skip-install"
    return cmd

def _show_error_block(title: str, error: str):
    P(f"\n  {C.RED}{'─'*54}{C.RESET}")
    P(f"  {C.RED}{C.BOLD}  ❌ {title}{C.RESET}")
    P(f"  {C.RED}{'─'*54}{C.RESET}")
    lines = error.splitlines()
    for l in lines[:30]: P(f"  {C.DIM}    {l}{C.RESET}")
    if len(lines) > 30: P(f"  {C.DIM}    ... (+{len(lines)-30} líneas más){C.RESET}")
    P(f"  {C.RED}{'─'*54}{C.RESET}\n")

# ═══════════════════════════════════════════════════════════════════
#   CONSULTA A LA IA
# ═══════════════════════════════════════════════════════════════════
def _ask_web(prompt: str, preferred_site: str, objetivo: str) -> str:
    try:
        _, [resp] = ask_ai_multiturn([prompt], preferred_site, objetivo)
        return resp or ""
    except Exception as e:
        log_error(preferred_site or "web", str(e))
        P(f"  {C.RED}  ❌ Error consultando IA: {e}{C.RESET}")
        return ""

# ═══════════════════════════════════════════════════════════════════
#   VERIFICACIÓN DE HERRAMIENTAS
# ═══════════════════════════════════════════════════════════════════
_TOOL_VERSION_CMDS = {
    "node":"node --version","node.js":"node --version","nodejs":"node --version",
    "npm":"npm --version","npx":"npx --version",
    "angular":"ng version","angular cli":"ng version","@angular/cli":"ng version","ng":"ng version",
    "typescript":"tsc --version","git":"git --version","python":"python --version",
    "java":"java --version","docker":"docker --version","yarn":"yarn --version","pnpm":"pnpm --version",
}

def _extract_version(raw: str) -> str:
    raw = re.sub(r'\x1b\[[0-9;]*m','',raw)
    m = re.search(r'v?(\d+\.\d+[\.\d]*)',raw)
    return m.group(1) if m else raw.strip().split("\n")[0][:40]

def _check_tools_from_list(resp_prereq: str) -> dict:
    result, text_lower, matched = {}, resp_prereq.lower(), {}
    for keyword, cmd in sorted(_TOOL_VERSION_CMDS.items(), key=lambda x: -len(x[0])):
        if keyword in text_lower and cmd not in matched.values():
            display = keyword.title().replace(".Js",".js").replace("@Angular/Cli","Angular CLI")
            matched[display] = cmd
    if not matched: matched = {"Node.js":"node --version","npm":"npm --version"}
    for display, cmd in matched.items():
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=8, encoding="utf-8", errors="replace")
            raw = (r.stdout + r.stderr).strip()
            if "ng version" in cmd:
                ver = ""
                for line in raw.splitlines():
                    lc = re.sub(r'\x1b\[[0-9;]*m','',line).lower().strip()
                    m2 = re.search(r'angular\s+cli\s*:\s*([\d]+\.[\d]+\.[\d]+)',lc)
                    if m2: ver = m2.group(1); break
                if not ver:
                    m2 = re.search(r'(\d{2,3}\.\d+\.\d+)',raw)
                    ver = m2.group(1) if m2 else _extract_version(raw)
                version = ver
            else:
                version = _extract_version(raw)
            result[display] = {"cmd":cmd,"version":version or "instalado","ok": r.returncode==0 and bool(version)}
        except Exception as e:
            result[display] = {"cmd":cmd,"version":f"error: {e}","ok":False}
    return result

# ═══════════════════════════════════════════════════════════════════
#   ESCANEO DE ESTRUCTURA
# ═══════════════════════════════════════════════════════════════════
SKIP_DIRS = {"node_modules",".git","dist",".angular","__pycache__",".vscode"}
KEY_EXTS  = {".html",".css",".ts",".scss"}

def _scan_project(project_dir: Path) -> tuple:
    tree_lines, key_files = [], {}
    def _walk(path: Path, prefix: str="", depth: int=0):
        if depth > 6: return
        try: entries = sorted(path.iterdir(), key=lambda p:(p.is_file(), p.name))
        except PermissionError: return
        for i, entry in enumerate(entries):
            if entry.name in SKIP_DIRS: continue
            conn = "└── " if i==len(entries)-1 else "├── "
            tree_lines.append(f"{prefix}{conn}{entry.name}")
            if entry.is_dir():
                _walk(entry, prefix+("    " if i==len(entries)-1 else "│   "), depth+1)
            elif entry.suffix in KEY_EXTS and entry.stat().st_size < 6000:
                rel = str(entry.relative_to(project_dir)).replace("\\","/")
                try: key_files[rel] = entry.read_text(encoding="utf-8", errors="replace")
                except: pass
    _walk(project_dir)
    return "\n".join(tree_lines), key_files

# ═══════════════════════════════════════════════════════════════════
#   BUG FIX #2: REGLAS DE ARQUITECTURA ANGULAR EN PROMPTS
# ═══════════════════════════════════════════════════════════════════
def _ng_arch_rules(ng_major: int) -> str:
    if ng_major >= 17:
        return f"""
⚠️  ARQUITECTURA Angular v{ng_major} — STANDALONE OBLIGATORIO:
❌ NO crear app.module.ts — este proyecto USA app.config.ts (standalone)
❌ NO usar @NgModule ni NgModule en ningún archivo
❌ NO poner FormsModule/RouterModule en un NgModule (no existe en este proyecto)
✅ Cada @Component DEBE tener: standalone: true
✅ FormsModule en imports[] del @Component si usas [(ngModel)]
✅ RouterLink/RouterOutlet en imports[] del @Component si usas routing
✅ CommonModule en imports[] del @Component si usas *ngIf/*ngFor
   (alternativa moderna: @if/@for — sintaxis Angular {ng_major}+)
✅ La configuración global está SOLO en app.config.ts
✅ En Angular 17+ el componente raíz puede llamarse app.ts (no app.component.ts)
"""
    return f"Angular v{ng_major} (NgModule clásico — app.module.ts existe y es correcto)"

# ═══════════════════════════════════════════════════════════════════
#   PROMPTS
# ═══════════════════════════════════════════════════════════════════
def _p1_prereqs(objetivo: str) -> str:
    return (
        f"Necesito {objetivo.rstrip('. ')}. "
        f"Dime ÚNICAMENTE qué debo tener instalado. "
        f"Solo la lista de requisitos, sin tutoriales."
    )

def _p2_steps_create(objetivo: str, verified_tools: dict) -> str:
    tools_str = ", ".join(f"{n} {i['version']}" for n,i in verified_tools.items() if i["ok"])
    return (
        f"Ya tengo: {tools_str}. Necesito {objetivo}. "
        f"Dame SOLO el comando ng new. NO incluyas --skip-install. Solo el comando."
    )

def _p_fix_ng_new(objetivo: str, cmd: str, error: str, verified_tools: dict) -> str:
    tools_str = ", ".join(f"{n} {i['version']}" for n,i in verified_tools.items() if i["ok"])
    return (
        f"Comando fallido: '{cmd}'\nTengo: {tools_str}\nError: {error[:600]}\n\n"
        f"Dame SOLO el comando ng new corregido. Sin --skip-install."
    )

def _p2_steps(objetivo: str, verified_tools: dict, ng_major: int=17,
              tree: str="", key_files: dict=None, key_config: dict=None) -> str:
    tools_str = ", ".join(f"{n} {i['version']}" for n,i in verified_tools.items() if i["ok"])
    files_ctx = ""
    if key_files:
        for rel,content in list(key_files.items())[:8]:
            snippet = content.strip()[:80].replace("\n"," ")
            files_ctx += f"  {rel}: {snippet}\n"
    config_ctx = ""
    if key_config:
        for rel,content in key_config.items():
            config_ctx += f"\n=== {rel} (COMPLETO) ===\n{content}\n"
    arch = _ng_arch_rules(ng_major)
    return (
        f"Tengo: {tools_str}. Angular v{ng_major}.\n"
        f"Proyecto ya creado con ng new + npm install completado.\n\n"
        f"{arch}\n"
        f"ESTRUCTURA REAL DEL PROYECTO:\n```\n{tree}\n```\n\n"
        f"ARCHIVOS DE CONFIGURACIÓN ACTUALES (COMPLETOS):\n{config_ctx}\n"
        f"OTROS ARCHIVOS CLAVE (extracto):\n{files_ctx}\n"
        f"TAREA: {objetivo}\n\n"
        f"Dame los pasos para modificar los archivos y lograr la tarea.\n"
        f"USA EXACTAMENTE este formato:\n\n"
        f"PASO 1: descripción corta\n"
        f"CMD: comando exacto (o NINGUNO)\n"
        f"FILE: ruta/exacta/del/archivo.ext\n"
        f"```\ncontenido completo del archivo\n```\n\n"
        f"REGLAS CRÍTICAS:\n"
        f"1. Usa SOLO rutas que existen en la estructura real\n"
        f"2. NO incluyas: ng new, ng serve, cd, npm install\n"
        f"3. Cada FILE DEBE tener el contenido COMPLETO\n"
        f"4. El contenido empieza DIRECTAMENTE con código (sin 'Código:', backticks sueltos ni frases)\n"
        f"5. NO crear app.module.ts en Angular 17+\n"
        f"6. Solo pasos. Sin introducciones ni conclusiones."
    )

def _p_fix_step(objetivo: str, paso_desc: str, cmd_ej: str,
                error: str, verified_tools: dict, ng_major: int=17) -> str:
    tools_str = ", ".join(f"{n} {i['version']}" for n,i in verified_tools.items() if i["ok"])
    arch = _ng_arch_rules(ng_major)
    return (
        f"Estoy creando: {objetivo}\n{arch}\n"
        f"Falló '{paso_desc}' con: {cmd_ej}\n"
        f"Error: {error[:600]}\n\n"
        f"Dame pasos corregidos en formato PASO/CMD/FILE. Sin introducciones.\n"
        f"Contenido de cada FILE empieza directamente con código."
    )

def _p_fix_serve(objetivo: str, errors: str,
                 project_dir: Path, tools_str: str, ng_major: int=17) -> str:
    _, key_files = _scan_project(project_dir)
    key_config = _get_key_context_files(project_dir, ng_major)
    config_ctx = ""
    for rel, content in key_config.items():
        config_ctx += f"\n--- {rel} (COMPLETO) ---\n{content}\n"
    files_ctx = ""
    for rel, content in list(key_files.items())[:6]:
        files_ctx += f"\n--- {rel} ---\n{content[:400]}\n"
    arch = _ng_arch_rules(ng_major)
    return (
        f"App Angular ({tools_str}) v{ng_major} con errores.\n\n"
        f"{arch}\n"
        f"ERRORES:\n```\n{errors}\n```\n\n"
        f"ARCHIVOS CONFIGURACIÓN ACTUALES:\n{config_ctx}\n"
        f"OTROS ARCHIVOS:\n{files_ctx}\n"
        f"TAREA ORIGINAL: {objetivo}\n\n"
        f"Corrige TODOS los errores. Formato:\n"
        f"PASO 1: descripción\nCMD: (o NINGUNO)\nFILE: ruta\n```\ncontenido COMPLETO\n```\n\n"
        f"El contenido empieza directamente con código. Sin introducciones."
    )

def _p_fix_serve_strategy_change(objetivo: str, errors: str,
                                  project_dir: Path, tools_str: str,
                                  ng_major: int, attempt: int) -> str:
    _, key_files = _scan_project(project_dir)
    key_config = _get_key_context_files(project_dir, ng_major)
    config_ctx = ""
    for rel, content in key_config.items():
        config_ctx += f"\n--- {rel} ---\n{content}\n"
    arch = _ng_arch_rules(ng_major)
    return (
        f"INTENTO {attempt}: Los fixes anteriores NO resolvieron. MISMOS errores persisten.\n"
        f"Necesito estrategia COMPLETAMENTE DIFERENTE.\n\n"
        f"{arch}\n"
        f"ERRORES PERSISTENTES:\n```\n{errors}\n```\n\n"
        f"ARCHIVOS ACTUALES:\n{config_ctx}\n\n"
        f"TAREA: {objetivo}\n\n"
        f"IMPORTANTE — aplica una estrategia diferente:\n"
        f"- Si hay app.module.ts → ELIMINARLO y usar solo app.config.ts (standalone)\n"
        f"- Si hay 'Código' al inicio de archivos → es una etiqueta, no código\n"
        f"- Si usas [(ngModel)] → añadir FormsModule a imports[] del @Component\n"
        f"- Si faltan imports → añadirlos todos explícitamente en el @Component\n"
        f"- Reconstruye los archivos desde cero si es necesario\n\n"
        f"Formato: PASO/CMD/FILE con contenido COMPLETO. El contenido empieza con código directo."
    )

# ═══════════════════════════════════════════════════════════════════
#   PARSER
# ═══════════════════════════════════════════════════════════════════
_CONTAMINATED_PLAN_MARKERS = (
    "Isolated Segment",
    "window.__CF$cv$params",
    "ARCHIVOS DE CONFIGURACIÓN ACTUALES (COMPLETOS):",
    "REGLAS CRÍTICAS:",
)


def _response_is_contaminated(response: str) -> bool:
    txt = response or ""
    return any(m in txt for m in _CONTAMINATED_PLAN_MARKERS)


def _parse_plan(response: str) -> list:
    if _response_is_contaminated(response):
        return []

    steps = _parse_structured(response)
    if not steps: steps = _parse_natural(response)
    BLOCK = ("npm install -g",)
    result = []
    for step in steps:
        cmd = step.get("cmd","") or ""
        if any(cmd.lower().startswith(b) for b in BLOCK): step["cmd"] = None
        if cmd.lower().startswith("ng serve") or cmd.lower().startswith("npm start"):
            step["_is_serve"] = True
        if step.get("cmd") or step.get("files"): result.append(step)
    return result

def _parse_structured(response: str) -> list:
    steps, lines, i = [], response.splitlines(), 0
    while i < len(lines):
        m = re.match(r'^PASO\s+\d+\s*[:\-]\s*(.+)', lines[i].strip(), re.IGNORECASE)
        if not m: i+=1; continue
        step = {"desc": m.group(1).strip(), "cmd": None, "files": [], "_is_serve": False}
        i += 1
        while i < len(lines):
            l = lines[i].strip()
            if re.match(r'^PASO\s+\d+\s*[:\-]', l, re.IGNORECASE): break
            mc = re.match(r'^CMD\s*:\s*(.+)', l, re.IGNORECASE)
            if mc:
                v = mc.group(1).strip()
                if v.upper() not in ("NINGUNO","NONE","N/A",""): step["cmd"] = v
                i+=1; continue
            mf = re.match(r'^FILE\s*:\s*(.+)', l, re.IGNORECASE)
            if mf:
                fpath = mf.group(1).strip(); i+=1; cl = []
                if i < len(lines) and lines[i].strip() == "": i+=1
                # BUG FIX: saltar etiquetas de lenguaje incluyendo "Código"
                while i < len(lines) and _is_bare_lang_label(lines[i]): i+=1
                uses_backticks = i < len(lines) and lines[i].strip().startswith("```")
                if uses_backticks: i+=1
                while i < len(lines):
                    cur = lines[i]; curs = cur.strip()
                    if uses_backticks and curs.startswith("```"): i+=1; break
                    if re.match(r'^PASO\s+\d+\s*[:\-]', curs, re.IGNORECASE): break
                    if not uses_backticks:
                        if re.match(r'^FILE\s*:', curs, re.IGNORECASE): break
                        if re.match(r'^CMD\s*:', curs, re.IGNORECASE): break
                    cl.append(cur); i+=1
                while cl and cl[-1].strip() == "": cl.pop()
                if fpath and cl: step["files"].append({"path":fpath,"content":"\n".join(cl)})
                continue
            i+=1
        if step["cmd"] or step["files"]: steps.append(step)
    return steps

_SRC_RE  = re.compile(r'\bsrc/[\w/.\-]+\.\w{1,5}\b')
_ITEM_RE = re.compile(r'^[*\-]\s+(?:[Aa]rchivo\s*[:\-]\s*)?[`\'"]?(\S+\.\w{1,5})[`\'"]?')
_STEP_RE = re.compile(r'^(?:#{1,3}\s*)?(?:\d+[️⃣°]?\s*)?(?:paso|step)\s*[\d️⃣°]*\s*[:\-]?\s*(.*)', re.IGNORECASE)
_CMD_OK   = ("ng ","npm install","npx ","git ")
_CMD_SKIP = ("npm install -g",)
_LANG_DEF = {"html":"src/app/app.component.html","css":"src/app/app.component.css",
             "typescript":"src/app/app.component.ts","ts":"src/app/app.component.ts"}

def _parse_natural(response: str) -> list:
    steps, lines, i = [], response.splitlines(), 0
    cur, last_path = None, ""
    def _flush():
        nonlocal cur
        if cur and (cur["cmd"] or cur["files"]): steps.append(cur)
        cur = None
    while i < len(lines):
        line = lines[i].strip(); i+=1
        pm = _STEP_RE.match(line)
        if pm:
            _flush()
            cur = {"desc": pm.group(1).strip() or line, "cmd": None, "files": [], "_is_serve": False}
            continue
        am = _ITEM_RE.match(line)
        if am: last_path = am.group(1).strip()
        sm = _SRC_RE.search(line)
        if sm: last_path = sm.group(0)
        clean = re.sub(r'^[`$>\s]+','',line)
        if any(clean.lower().startswith(s) for s in _CMD_OK):
            if not any(clean.lower().startswith(s) for s in _CMD_SKIP):
                if cur is None:
                    cur = {"desc": clean[:60], "cmd": None, "files": [], "_is_serve": False}
                if not cur["cmd"]: cur["cmd"] = clean
        fm = re.match(r'^```(\w*)', line)
        if fm:
            lang = fm.group(1).lower(); cl = []
            while i < len(lines):
                if lines[i].strip().startswith("```"): i+=1; break
                cl.append(lines[i]); i+=1
            content = "\n".join(cl).strip()
            if not content: continue
            fpath = last_path or _LANG_DEF.get(lang,"")
            if fpath:
                if cur is None:
                    cur = {"desc": f"Editar {fpath}", "cmd": None, "files": [], "_is_serve": False}
                if fpath not in [f["path"] for f in cur["files"]]:
                    cur["files"].append({"path": fpath, "content": content})
                last_path = ""
    _flush()
    return steps

# ═══════════════════════════════════════════════════════════════════
#   EJECUTOR DE PASOS
# ═══════════════════════════════════════════════════════════════════
def _exec_step(step: dict, project_dir: Path, step_num: int, total: int) -> tuple:
    desc  = step.get("desc","")
    cmd   = step.get("cmd")
    files = step.get("files",[])
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
        else:
            _show_error_block(f"Error paso {step_num}: {cmd}", out)
            return False, out
    for fi in files:
        rel     = fi.get("path","").strip()
        content = fi.get("content","")
        if not rel: continue
        _write(project_dir / rel, content)
        P(f"  {C.GREEN}│  📝 {rel} ({len(content)} chars){C.RESET}")
    P(f"  {C.GREEN}└─ ✅ Paso {step_num}/{total} completado{C.RESET}")
    return True, ""

# ═══════════════════════════════════════════════════════════════════
#   LANZADOR
# ═══════════════════════════════════════════════════════════════════
_launched = False

def _launch(workspace: Path, project_dir: Path,
            objetivo: str="", preferred_site: str=None,
            verified_tools: dict=None, ng_major: int=17):
    global _launched
    if _launched: return
    _launched = True
    P(f"\n  {C.DIM}Abriendo carpeta del proyecto...{C.RESET}")
    try: os.startfile(str(workspace))
    except: pass
    ng_jsons = list(project_dir.rglob("angular.json"))
    if ng_jsons:
        pdir = ng_jsons[0].parent
        _serve_and_fix(project_dir=pdir, objetivo=objetivo, preferred_site=preferred_site,
                       verified_tools=verified_tools or {}, ng_major=ng_major)
        return
    pkgs = [p for p in project_dir.rglob("package.json") if "node_modules" not in str(p)]
    if pkgs:
        pdir = pkgs[0].parent
        r = input(f"  {C.YELLOW}¿Levantar servidor? (s/n) > {C.RESET}").strip().lower()
        if r and r[0] in ("s","y"):
            try: subprocess.run("npm start", shell=True, cwd=str(pdir))
            except KeyboardInterrupt: pass

# ═══════════════════════════════════════════════════════════════════
#   ORQUESTADOR PRINCIPAL
# ═══════════════════════════════════════════════════════════════════
def run_orchestrator(objetivo: str, preferred_site: str=None) -> bool:
    global _launched
    _launched = False
    site_name = AI_SITES.get(preferred_site,{}).get("name","IA automática") if preferred_site else "IA automática"

    P(f"\n{C.CYAN}{C.BOLD}  ╔══════════════════════════════════════╗")
    P(f"  ║   🤖 ORQUESTADOR SONNY  v11.4       ║")
    P(f"  ╚══════════════════════════════════════╝{C.RESET}")
    P(f"  {C.DIM}Objetivo : {objetivo}{C.RESET}")
    P(f"  {C.DIM}Cerebro  : {site_name}{C.RESET}\n")

    navs = detectar_navegadores()
    P(f"  {C.GREEN}🌐 Navegadores: {', '.join(navs) or 'Chromium interno'}{C.RESET}")

    safe = re.sub(r'[^\w\-]','_', objetivo.lower())[:35]
    ts   = datetime.now().strftime("%H%M%S")
    workspace = WORKSPACE_ROOT / f"{safe}_{ts}"
    workspace.mkdir(parents=True, exist_ok=True)
    P(f"  {C.DIM}Workspace: {workspace}{C.RESET}\n")

    log_session_start(objetivo)
    verified_tools, ng_major = {}, 17

    # ── TURNO 1 ─────────────────────────────────────────────────────
    P(f"  {C.BOLD}{C.MAGENTA}━━━ TURNO 1 → {site_name}: herramientas ━━━{C.RESET}\n")
    P(f"  {C.DIM}  Abriendo navegador...{C.RESET}\n")
    resp_prereq = _ask_web(_p1_prereqs(objetivo), preferred_site, objetivo)
    if not resp_prereq:
        P(f"  {C.RED}  ❌ Sin respuesta.{C.RESET}")
        try: log_session_end(objetivo, success=False, total_rounds=0, ng_major=0)
        except: pass
        return False
    P(f"\n  {C.CYAN}  💬 {site_name} dice:{C.RESET}")
    for l in resp_prereq.strip().splitlines()[:10]:
        if l.strip(): P(f"  {C.DIM}    {l.strip()}{C.RESET}")
    P("")

    # ── SONNY verifica herramientas ──────────────────────────────────
    P(f"  {C.BOLD}{C.MAGENTA}━━━ SONNY verifica herramientas ━━━{C.RESET}\n")
    verified_tools = _check_tools_from_list(resp_prereq)
    all_ok = True
    for tname, info in verified_tools.items():
        icon = f"{C.GREEN}  ✅{C.RESET}" if info["ok"] else f"{C.RED}  ❌{C.RESET}"
        P(f"  {icon} {tname:<22} {info['version']}")
        if not info["ok"]: all_ok = False
    if not all_ok:
        P(f"\n  {C.YELLOW}  ⚠️  Herramientas faltantes.{C.RESET}")
        r = input(f"  {C.YELLOW}  ¿Continuar? (s/n) > {C.RESET}").strip().lower()
        if r and r[0] not in ("s","y"):
            try: log_session_end(objetivo, success=False, total_rounds=0, ng_major=0)
            except: pass
            return False
    else:
        P(f"\n  {C.GREEN}  ✅ Todo instalado.{C.RESET}\n")

    # ── TURNO 2 ─────────────────────────────────────────────────────
    P(f"  {C.BOLD}{C.MAGENTA}━━━ TURNO 2 → {site_name}: comando ng new ━━━{C.RESET}\n")
    resp_create = _ask_web(_p2_steps_create(objetivo, verified_tools), preferred_site, objetivo)
    if not resp_create:
        P(f"  {C.RED}  ❌ Sin respuesta Turno 2.{C.RESET}")
        return False
    P(f"  {C.CYAN}  💬 Comando recibido:{C.RESET}")
    P(f"  {C.DIM}    {resp_create.strip()[:100]}{C.RESET}\n")

    create_cmd = ""
    for line in resp_create.splitlines():
        clean = line.strip().lstrip("`$> ").strip()
        if _is_bare_lang_label(clean): continue
        if clean.lower().startswith("ng new"): create_cmd = clean; break
    if not create_cmd:
        create_cmd = "ng new mi-app --style=css --skip-git --defaults"
        P(f"  {C.YELLOW}  ⚠️  ng new no detectado — usando default{C.RESET}")
    create_cmd = _force_skip_install(create_cmd)
    P(f"  {C.DIM}  (--skip-install añadido){C.RESET}")

    # ── SONNY ejecuta ng new ─────────────────────────────────────────
    P(f"  {C.BOLD}{C.MAGENTA}━━━ SONNY crea el proyecto ━━━{C.RESET}\n")
    project_dir = None
    for attempt in range(1, MAX_FIX_ATTEMPTS+1):
        P(f"  {C.CYAN}  🖥  {create_cmd}{C.RESET}")
        ok, err = _run(create_cmd, workspace, timeout=TIMEOUT_NG_NEW)
        if ok:
            project_dir = _find_project_root(workspace)
            P(f"  {C.GREEN}  ✅ Proyecto creado: {project_dir.name}{C.RESET}")
            subprocess.run("ng analytics disable --global", shell=True,
                           cwd=str(project_dir), capture_output=True, env=CLI_ENV)
            break
        else:
            _show_error_block(f"ng new falló (intento {attempt}/{MAX_FIX_ATTEMPTS})", err)
            if attempt >= MAX_FIX_ATTEMPTS:
                P(f"  {C.RED}  ❌ ng new falló {MAX_FIX_ATTEMPTS} veces.{C.RESET}")
                return False
            P(f"  {C.YELLOW}  Consultando {site_name} para corregir...{C.RESET}\n")
            fix_resp = _ask_web(_p_fix_ng_new(objetivo, create_cmd, err, verified_tools), preferred_site, objetivo)
            if fix_resp:
                for line in fix_resp.splitlines():
                    clean = line.strip().lstrip("`$> ").strip()
                    if _is_bare_lang_label(clean): continue
                    if clean.lower().startswith("ng new"):
                        create_cmd = _force_skip_install(clean)
                        for item in workspace.iterdir():
                            if item.is_dir(): shutil.rmtree(item, ignore_errors=True)
                            else: item.unlink(missing_ok=True)
                        break

    if project_dir is None: return False

    # ── npm install ──────────────────────────────────────────────────
    _run_npm_install(project_dir)

    # ── Detectar versión Angular ─────────────────────────────────────
    ng_major = _get_ng_major(project_dir)
    P(f"\n  {C.DIM}  Angular major: v{ng_major}{C.RESET}")

    # ── Escaneo estructura real ──────────────────────────────────────
    P(f"\n  {C.BOLD}{C.MAGENTA}━━━ SONNY escanea estructura real ━━━{C.RESET}\n")
    tree, key_files = _scan_project(project_dir)
    P(f"  {C.DIM}  Archivos detectados:{C.RESET}")
    for f in list(key_files.keys())[:10]: P(f"  {C.DIM}    📄 {f}{C.RESET}")
    if len(key_files) > 10: P(f"  {C.DIM}    ... y {len(key_files)-10} más{C.RESET}")

    # MEJORA 7: obtener archivos de configuración completos
    key_config = _get_key_context_files(project_dir, ng_major)

    # ── TURNO 3 ─────────────────────────────────────────────────────
    P(f"\n  {C.BOLD}{C.MAGENTA}━━━ TURNO 3 → {site_name}: pasos con estructura real ━━━{C.RESET}\n")
    P(f"  {C.DIM}  Enviando estructura real + archivos config completos...{C.RESET}\n")
    p3 = _p2_steps(objetivo, verified_tools, ng_major, tree, key_files, key_config)
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

    # MEJORA 11: validación de dependencias
    dep_warnings = _validate_dependencies(project_dir, steps)
    if dep_warnings:
        P(f"\n  {C.YELLOW}  ⚠️  Advertencias de dependencias:{C.RESET}")
        for w in dep_warnings: P(f"  {C.YELLOW}      • {w}{C.RESET}")
        try: log_dependency_warning(dep_warnings)
        except: pass

    serve_steps = [s for s in steps if s.get("_is_serve")]
    exec_steps  = [s for s in steps if not s.get("_is_serve")]
    P(f"  {C.GREEN}  ✅ Plan: {len(exec_steps)} paso(s) + {len(serve_steps)} de inicio{C.RESET}\n")
    P(f"  {C.BOLD}  📋 Resumen:{C.RESET}")
    for idx, s in enumerate(exec_steps, 1):
        files_str = ", ".join(f["path"] for f in s.get("files",[]))
        P(f"  {C.CYAN}    {idx}. {s['desc'][:65]}{C.RESET}")
        if s.get("cmd"): P(f"  {C.DIM}       CMD:  {s['cmd'][:65]}{C.RESET}")
        if files_str:    P(f"  {C.DIM}       FILE: {files_str[:65]}{C.RESET}")

    # ── SONNY ejecuta pasos ──────────────────────────────────────────
    P(f"\n  {C.BOLD}{C.MAGENTA}━━━ SONNY ejecuta {len(exec_steps)} paso(s) ━━━{C.RESET}")
    total = len(exec_steps)
    for step_num, step in enumerate(exec_steps, 1):
        ok, error_out = _exec_step(step, project_dir, step_num, total)
        if step.get("_new_dir"):
            project_dir = step["_new_dir"]
            tree, key_files = _scan_project(project_dir)
        if not ok:
            fixed = False
            for attempt in range(1, MAX_FIX_ATTEMPTS+1):
                P(f"\n  {C.YELLOW}  ⚠️  Error paso {step_num}. Consultando {site_name} ({attempt}/{MAX_FIX_ATTEMPTS})...{C.RESET}\n")
                ejecutado = step.get("cmd") or str([f["path"] for f in step.get("files",[])])
                fix_p = _p_fix_step(objetivo, step["desc"], ejecutado, error_out, verified_tools, ng_major)
                fix_resp = _ask_web(fix_p, preferred_site, objetivo)
                if not fix_resp: break
                fix_steps = [s for s in _parse_plan(fix_resp) if not s.get("_is_serve")]
                if not fix_steps: break
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
                    P(f"  {C.RED}  Detenido.{C.RESET}")
                    return False

    # ── Auto-fix standalone (BUG FIX #2+3) ──────────────────────────
    P(f"\n  {C.DIM}  Aplicando auto-fix standalone (ngModel, RouterLink, CommonModule)...{C.RESET}")
    _autofix_angular_standalone(project_dir, ng_major)

    # ── COMPLETADO ───────────────────────────────────────────────────
    P(f"\n  {'─'*54}")
    P(f"  {C.GREEN}{C.BOLD}  ✅ COMPLETADO{C.RESET}")
    P(f"  {C.DIM}  Proyecto en: {project_dir}{C.RESET}")

    files_only = [f for f in workspace.rglob("*")
                  if f.is_file() and "node_modules" not in str(f) and ".angular" not in str(f)]
    if files_only:
        P(f"\n  {C.DIM}Archivos del proyecto:{C.RESET}")
        for f in sorted(files_only)[:20]:
            P(f"  {C.GREEN}    📄 {f.relative_to(workspace)}{C.RESET}")
        if len(files_only) > 20:
            P(f"  {C.DIM}    ... y {len(files_only)-20} más{C.RESET}")

    _launch(workspace, project_dir, objetivo=objetivo, preferred_site=preferred_site,
            verified_tools=verified_tools, ng_major=ng_major)
    return True


def run_orchestrator_with_site(objetivo: str) -> bool:
    P(f"\n  {C.BOLD}¿Qué IA quieres consultar?{C.RESET}")
    options = list(AI_SITES.keys())
    for i, key in enumerate(options, 1):
        P(f"  {C.CYAN}  {i}. {AI_SITES[key]['name']}{C.RESET}")
    P(f"  {C.DIM}  0. Automático{C.RESET}")
    resp = input(f"  {C.CYAN}tú > {C.RESET}").strip()
    try:
        idx  = int(resp)
        site = options[idx-1] if 1 <= idx <= len(options) else None
    except (ValueError, IndexError):
        site = None
    return run_orchestrator(objetivo, preferred_site=site)

# ═══════════════════════════════════════════════════════════════════
#   SERVE + FIX LOOP — BUG FIX #4: Anti-loop v2 (error codes)
# ═══════════════════════════════════════════════════════════════════
def _extract_build_errors(output: str) -> str:
    lines = output.splitlines()
    error_lines = []
    for line in lines:
        if re.search(r'(^\s*[X▲✖]\s+\[(?:ERROR|WARNING)\]|Application bundle generation failed|\[ERROR\]|\[WARNING\]|Error occurs in|Cannot find module|TS\d{4}:|NG\d{4}:)', line):
            error_lines.append(line)
        elif error_lines and re.search(r'^\s+\d+\s*[│╵]', line):
            error_lines.append(line)
        elif error_lines and re.search(r'src/.*:\d+:\d+', line):
            error_lines.append(line)
    for line in lines:
        if any(w in line for w in _FUNCTIONAL_WARNINGS) and line not in error_lines:
            error_lines.append(line)
    return "\n".join(error_lines[:60]) if error_lines else output[:1000]

def _has_build_errors(output: str) -> bool:
    patterns = [
        r'Application bundle generation failed',
        r'X \[ERROR\]',
        r'\[ERROR\].*\[plugin angular-compiler\]',
        r'TS\d{4}:',
        r'NG\d{4}:.*(?:ERROR|error)',
    ]
    if any(re.search(p, output) for p in patterns): return True
    return _has_functional_warnings(output)

def _serve_and_fix(project_dir: Path, objetivo: str, preferred_site: str,
                   verified_tools: dict, ng_major: int=17, max_fix_rounds: int=3):
    """
    Ejecuta ng build, detecta errores, corrige con IA y reintenta.
    BUG FIX #4: Anti-loop v2 — rastrea error codes entre rondas.
      Si los MISMOS códigos (TS2304, NG8002, etc.) persisten 2+ rondas
      → fuerza estrategia diferente aunque el hash de archivos cambie.
    """
    tools_str = ", ".join(f"{n} {i['version']}" for n,i in verified_tools.items() if i["ok"])
    subprocess.run("ng analytics disable --global", shell=True,
                   cwd=str(project_dir), capture_output=True, env=CLI_ENV)

    last_hash = ""
    error_codes_history = []  # BUG FIX #4: historial de sets de error codes

    for fix_round in range(max_fix_rounds + 1):
        if fix_round == 0:
            P(f"\n  {C.GREEN}{C.BOLD}🚀 Iniciando ng serve...{C.RESET}")
        else:
            P(f"\n  {C.CYAN}  🔄 Reintentando ng serve (ronda {fix_round}/{max_fix_rounds})...{C.RESET}")

        # Auto-fix antes de compilar
        if fix_round > 0:
            _autofix_angular_standalone(project_dir, ng_major)

        P(f"  {C.DIM}  Compilando proyecto para detectar errores...{C.RESET}")
        ok_build, build_out = _run("ng build --configuration=development",
                                   project_dir, timeout=120)

        if _has_build_errors(build_out) or not ok_build:
            errors = _extract_build_errors(build_out)

            # BUG FIX #4: extraer y registrar error codes de esta ronda
            curr_codes = _extract_error_codes(build_out)
            error_codes_history.append(curr_codes)

            _show_error_block(f"Errores (ronda {fix_round+1}/{max_fix_rounds})", errors)

            # MEJORA 9: log de error de build
            try: log_build_error(fix_round, list(curr_codes), errors[:500], ng_major)
            except: pass

            if fix_round >= max_fix_rounds:
                P(f"  {C.RED}  ❌ Se agotaron los intentos.{C.RESET}")
                P(f"  {C.YELLOW}  El proyecto está en: {project_dir}{C.RESET}")
                P(f"  {C.YELLOW}  Puedes abrirlo manualmente con: ng serve{C.RESET}")
                try: log_session_end(objetivo, success=False, total_rounds=fix_round, ng_major=ng_major)
                except: pass
                break

            # BUG FIX #4: detectar si los MISMOS error codes persisten
            current_hash = _get_files_hash(project_dir)
            hash_changed = current_hash != last_hash
            last_hash = current_hash

            # Si hay al menos 2 rondas y los mismos codes se repiten → estrategia diferente
            persistent_codes = set()
            if len(error_codes_history) >= 2:
                persistent_codes = error_codes_history[-1] & error_codes_history[-2]

            site_name = AI_SITES.get(preferred_site,{}).get("name","IA") if preferred_site else "IA"
            P(f"\n  {C.MAGENTA}{C.BOLD}  🤖 Consultando {site_name}...{C.RESET}\n")

            use_strategy_change = (
                (persistent_codes and fix_round > 0) or
                (not hash_changed and fix_round > 0)
            )

            if use_strategy_change:
                reason = f"códigos persistentes: {persistent_codes}" if persistent_codes else "estado sin cambios"
                P(f"  {C.YELLOW}  ⚠️  {reason} → cambiando estrategia{C.RESET}")
                fix_prompt = _p_fix_serve_strategy_change(
                    objetivo, errors, project_dir, tools_str, ng_major, fix_round+1
                )
            else:
                fix_prompt = _p_fix_serve(objetivo, errors, project_dir, tools_str, ng_major)

            fix_resp = _ask_web(fix_prompt, preferred_site, objetivo)
            if not fix_resp:
                P(f"  {C.RED}  Sin respuesta de la IA.{C.RESET}")
                break

            P(f"  {C.CYAN}  💬 {site_name} — corrección:{C.RESET}")
            for l in fix_resp.strip().splitlines()[:6]:
                P(f"  {C.DIM}    {l.strip()[:100]}{C.RESET}")
            P(f"  {C.DIM}    ...{C.RESET}\n")

            fix_steps = [s for s in _parse_plan(fix_resp) if not s.get("_is_serve")]
            if not fix_steps:
                P(f"  {C.YELLOW}  ⚠️  No se encontraron pasos.{C.RESET}")
                break

            P(f"  {C.BOLD}  📋 Aplicando {len(fix_steps)} corrección(es):{C.RESET}")
            files_changed = []
            all_ok = True
            for fi, fs in enumerate(fix_steps, 1):
                fok, ferr = _exec_step(fs, project_dir, fi, len(fix_steps))
                files_changed += [f["path"] for f in fs.get("files",[])]
                if not fok:
                    _show_error_block(f"Fix {fi} falló", ferr)
                    all_ok = False; break

            # MEJORA 9: log del fix aplicado
            try:
                strategy = "strategy_change" if use_strategy_change else "normal"
                log_fix_applied(fix_round, files_changed, strategy)
            except: pass

            if not all_ok:
                P(f"  {C.YELLOW}  ⚠️  Algún fix falló, reintentando...{C.RESET}")

            continue

        else:
            # ✅ Compilación exitosa
            P(f"  {C.GREEN}  ✅ Compilación exitosa — lanzando servidor...{C.RESET}")
            # MEJORA 8: validación semántica
            _semantic_validation_warning(objetivo, project_dir)
            # MEJORA 9: log de sesión exitosa
            try: log_session_end(objetivo, success=True, total_rounds=fix_round, ng_major=ng_major)
            except: pass
            P(f"\n  {C.GREEN}{C.BOLD}🚀 Angular listo — http://localhost:4200{C.RESET}")
            P(f"  {C.DIM}  Ctrl+C para detener{C.RESET}\n")
            try:
                subprocess.run("ng serve --open", shell=True,
                               cwd=str(project_dir), env=CLI_ENV)
            except KeyboardInterrupt:
                P(f"\n  {C.YELLOW}  Servidor detenido.{C.RESET}")
            return

    P(f"\n  {C.YELLOW}  Puedes intentar 'ng serve' manualmente en:{C.RESET}")
    P(f"  {C.DIM}  {project_dir}{C.RESET}")