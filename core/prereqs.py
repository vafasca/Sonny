"""
core/prereqs.py — Verificador y gestor de prerequisitos.
Comprueba Node, npm, nvm, Angular CLI, Java, Python, etc.
Detecta incompatibilidades, sugiere versiones correctas y las instala/cambia.
"""
import subprocess, re, sys, os, json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

# ── Colores ────────────────────────────────────────────────────────────────────
class C:
    CYAN="\033[96m"; GREEN="\033[92m"; YELLOW="\033[93m"
    RED="\033[91m";  BOLD="\033[1m";   DIM="\033[2m"; RESET="\033[0m"

# ── Tabla de compatibilidad por framework ──────────────────────────────────────
# Fuente: documentación oficial de cada framework
COMPATIBILITY: dict[str, dict] = {
    "angular": {
        "versions": {
            "19": {"node": ("18.19.1", "22.x"), "npm": "6.0.0"},
            "18": {"node": ("18.19.1", "20.x"), "npm": "6.0.0"},
            "17": {"node": ("18.13.0", "20.x"), "npm": "6.0.0"},
            "16": {"node": ("16.14.0", "18.x"), "npm": "6.0.0"},
            "15": {"node": ("14.20.0", "18.x"), "npm": "6.0.0"},
        },
        "cli_package":    "@angular/cli",
        "cli_cmd":        "ng",
        "create_cmd":     "ng new {nombre} --routing=false --style=css --skip-git",
        "serve_cmd":      "ng serve --open",
        "latest_version": "19",
    },
    "react": {
        "versions": {
            "18": {"node": ("14.0.0", "20.x"), "npm": "5.6.0"},
            "19": {"node": ("18.0.0", "22.x"), "npm": "8.0.0"},
        },
        "cli_package":    "create-react-app",
        "cli_cmd":        None,
        "create_cmd":     "npx create-react-app {nombre}",
        "serve_cmd":      "npm start",
        "latest_version": "18",
    },
    "vue": {
        "versions": {
            "3": {"node": ("16.0.0", "20.x"), "npm": "7.0.0"},
        },
        "cli_package":    "@vue/cli",
        "cli_cmd":        "vue",
        "create_cmd":     "npm create vue@latest {nombre} -- --no-router --no-vitest",
        "serve_cmd":      "npm run dev",
        "latest_version": "3",
    },
    "nextjs": {
        "versions": {
            "14": {"node": ("18.17.0", "20.x"), "npm": "8.0.0"},
            "15": {"node": ("18.18.0", "22.x"), "npm": "8.0.0"},
        },
        "cli_package":    None,
        "cli_cmd":        None,
        "create_cmd":     "npx create-next-app@latest {nombre} --no-tailwind --no-eslint --no-src-dir --no-app",
        "serve_cmd":      "npm run dev",
        "latest_version": "14",
    },
    "svelte": {
        "versions": {
            "4": {"node": ("16.0.0", "20.x"), "npm": "7.0.0"},
        },
        "cli_package":    None,
        "cli_cmd":        None,
        "create_cmd":     "npx degit sveltejs/template {nombre}",
        "serve_cmd":      "npm run dev",
        "latest_version": "4",
    },
}

# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class ToolInfo:
    name:       str
    installed:  bool
    version:    Optional[str]   = None
    path:       Optional[str]   = None
    error:      Optional[str]   = None

@dataclass
class CompatResult:
    ok:           bool
    issues:       list[str]     = field(default_factory=list)
    suggestions:  list[str]     = field(default_factory=list)
    actions:      list[dict]    = field(default_factory=list)  # acciones a tomar

# ── Utilidades de versión ──────────────────────────────────────────────────────

def _run(cmd: str, timeout: int = 15) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, str(e)

def _parse_version(text: str) -> Optional[tuple[int, ...]]:
    m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', text)
    if m:
        return tuple(int(x) for x in m.groups() if x is not None)
    return None

def _version_str(t: tuple) -> str:
    return ".".join(str(x) for x in t)

def _meets_min(version: str, minimum: str) -> bool:
    v = _parse_version(version)
    m = _parse_version(minimum)
    if not v or not m:
        return False
    # Pad to same length
    max_len = max(len(v), len(m))
    v = v + (0,) * (max_len - len(v))
    m = m + (0,) * (max_len - len(m))
    return v >= m

# ── Detección de herramientas ──────────────────────────────────────────────────

def check_node() -> ToolInfo:
    ok, out = _run("node --version")
    if ok and out:
        ver = out.strip().lstrip("v")
        path_ok, path_out = _run("where node" if sys.platform == "win32" else "which node")
        return ToolInfo("node", True, ver, path_out.splitlines()[0] if path_ok else None)
    return ToolInfo("node", False, error="Node.js no instalado")

def check_npm() -> ToolInfo:
    ok, out = _run("npm --version")
    if ok and out:
        return ToolInfo("npm", True, out.strip())
    return ToolInfo("npm", False, error="npm no instalado")

def check_nvm() -> ToolInfo:
    """
    Verifica si nvm-windows está instalado comprobando SOLO la existencia
    del ejecutable. NO lo ejecuta — evita el popup "Terminal Only" en Git Bash.
    """
    import shutil, os
    # shutil.which busca en PATH sin ejecutar nada
    nvm_path = shutil.which("nvm")
    if nvm_path:
        return ToolInfo("nvm", True, "instalado", nvm_path)
    # Rutas fijas de nvm-windows
    user = os.environ.get("USERNAME", "")
    for p in [
        "C:\\ProgramData\\nvm\\nvm.exe",
        f"C:\\Users\\{user}\\AppData\\Roaming\\nvm\\nvm.exe",
        "C:\\nvm\\nvm.exe",
    ]:
        if os.path.exists(p):
            return ToolInfo("nvm", True, "instalado", p)
    return ToolInfo("nvm", False, error="nvm no instalado")


def list_nvm_versions() -> list[str]:
    """
    Lista versiones de Node con nvm, ejecutando via PowerShell para evitar popup.
    Solo se llama si nvm está confirmado instalado.
    """
    # Usar PowerShell que sí puede ejecutar nvm-windows sin popup
    ok, out = _run('powershell -Command "& nvm list"', timeout=10)
    if not ok or not out or "should be run" in out.lower():
        return []
    versions = []
    for line in out.splitlines():
        line = line.strip().lstrip("*").strip()
        if not line or "should be run" in line.lower():
            continue
        v = _parse_version(line)
        if v:
            versions.append(_version_str(v))
    return versions

def check_cli(framework: str) -> ToolInfo:
    cfg = COMPATIBILITY.get(framework, {})
    cmd = cfg.get("cli_cmd")
    if not cmd:
        return ToolInfo(framework + "-cli", False, error="Sin CLI propio")

    ok, out = _run(f"{cmd} version 2>&1")
    if ok or (out and not "not found" in out.lower() and not "is not recognized" in out.lower()):
        # Extraer versión
        ver = None
        for line in out.splitlines():
            v = _parse_version(line)
            if v:
                ver = _version_str(v)
                break
        return ToolInfo(f"{framework}-cli", True, ver)
    return ToolInfo(f"{framework}-cli", False, error=f"{cmd} no instalado")

def check_python() -> ToolInfo:
    for cmd in ("python --version", "python3 --version"):
        ok, out = _run(cmd)
        if ok and "Python" in out:
            ver = out.replace("Python", "").strip()
            return ToolInfo("python", True, ver)
    return ToolInfo("python", False, error="Python no instalado")

def check_java() -> ToolInfo:
    ok, out = _run("java -version 2>&1")
    if ok or "version" in out.lower():
        ver = None
        m = re.search(r'"(\d+[\.\d]*)"', out)
        if m:
            ver = m.group(1)
        return ToolInfo("java", True, ver)
    return ToolInfo("java", False, error="Java no instalado")



# ── Verificador de compatibilidad ──────────────────────────────────────────────

def check_compatibility(framework: str, fw_version: str = None) -> CompatResult:
    """
    Verifica si el entorno actual es compatible con el framework/versión pedidos.
    Devuelve CompatResult con issues, suggestions y actions.
    """
    result = CompatResult(ok=True)
    fw_key = framework.lower().replace(".js", "").replace(" ", "")

    if fw_key not in COMPATIBILITY:
        result.issues.append(f"Framework '{framework}' no tiene tabla de compatibilidad registrada.")
        return result

    cfg        = COMPATIBILITY[fw_key]
    fw_ver     = fw_version or cfg["latest_version"]
    compat_map = cfg["versions"]

    # Buscar versión más cercana si no es exacta
    if fw_ver not in compat_map:
        major = fw_ver.split(".")[0]
        fw_ver = major if major in compat_map else cfg["latest_version"]

    reqs = compat_map.get(fw_ver, {})
    node_range = reqs.get("node", ("14.0.0", "999.x"))
    npm_min    = reqs.get("npm", "6.0.0")
    node_min   = node_range[0]

    # Verificar Node
    node_info = check_node()
    if not node_info.installed:
        result.ok = False
        result.issues.append("Node.js no está instalado.")
        result.suggestions.append(f"Instala Node.js >= {node_min} desde https://nodejs.org")
        result.actions.append({
            "type":    "install",
            "tool":    "node",
            "version": node_min,
            "via":     "nvm" if check_nvm().installed else "manual",
            "cmd":     f"nvm install {node_min.split('.')[0]}" if check_nvm().installed else f"Descarga desde https://nodejs.org/en/download/"
        })
    else:
        if not _meets_min(node_info.version, node_min):
            result.ok = False
            result.issues.append(
                f"Node {node_info.version} instalado, pero {framework} {fw_ver} requiere >= {node_min}."
            )
            nvm = check_nvm()
            nvm_versions = list_nvm_versions() if nvm.installed else []
            # Buscar versión compatible ya instalada
            compatible_installed = [v for v in nvm_versions if _meets_min(v, node_min)]

            if compatible_installed:
                best = sorted(compatible_installed, key=lambda v: _parse_version(v) or (0,))[-1]
                result.suggestions.append(f"Tienes Node {best} instalado en nvm — puedes cambiar a ella.")
                result.actions.append({
                    "type":    "switch",
                    "tool":    "node",
                    "from":    node_info.version,
                    "to":      best,
                    "cmd":     f"nvm use {best}",
                    "auto":    True
                })
            elif nvm.installed:
                result.suggestions.append(f"Instala Node {node_min.split('.')[0]} con nvm.")
                result.actions.append({
                    "type":    "install",
                    "tool":    "node",
                    "version": node_min,
                    "cmd":     f"nvm install {node_min.split('.')[0]} && nvm use {node_min.split('.')[0]}",
                    "auto":    True
                })
            else:
                result.suggestions.append(
                    f"Instala nvm-windows para gestionar versiones de Node fácilmente: "
                    f"https://github.com/coreybutler/nvm-windows/releases"
                )
                result.actions.append({
                    "type":    "manual",
                    "tool":    "node",
                    "version": node_min,
                    "url":     "https://github.com/coreybutler/nvm-windows/releases"
                })

    # Verificar npm
    npm_info = check_npm()
    if npm_info.installed and not _meets_min(npm_info.version, npm_min):
        result.issues.append(f"npm {npm_info.version} instalado, requiere >= {npm_min}.")
        result.suggestions.append("Actualiza npm: npm install -g npm@latest")
        result.actions.append({
            "type": "upgrade", "tool": "npm",
            "cmd": "npm install -g npm@latest", "auto": True
        })

    # Verificar CLI del framework
    cli_info = check_cli(fw_key)
    if cfg.get("cli_cmd") and not cli_info.installed:
        result.issues.append(f"CLI de {framework} no instalada.")
        pkg = cfg.get("cli_package", "")
        result.suggestions.append(f"Instala: npm install -g {pkg}")
        result.actions.append({
            "type": "install", "tool": f"{fw_key}-cli",
            "cmd": f"npm install -g {pkg}", "auto": True
        })
    elif cli_info.installed and cli_info.version:
        cli_major = cli_info.version.split(".")[0]
        if fw_key == "angular" and cli_major != fw_ver:
            result.issues.append(
                f"Angular CLI {cli_info.version} instalada, pero pediste Angular {fw_ver}. "
                f"Pueden ser incompatibles."
            )
            result.suggestions.append(f"Cambia CLI: npm install -g @angular/cli@{fw_ver}")
            result.actions.append({
                "type": "upgrade", "tool": "angular-cli",
                "cmd": f"npm install -g @angular/cli@{fw_ver}", "auto": True
            })

    return result

# ── Ejecutor de acciones de prerequisitos ──────────────────────────────────────

def apply_prereq_actions(actions: list[dict]) -> list[dict]:
    """
    Ejecuta automáticamente las acciones de prerequisitos marcadas con auto=True.
    Devuelve lista de resultados.
    """
    results = []
    for action in actions:
        if not action.get("auto"):
            continue
        cmd  = action.get("cmd")
        tool = action.get("tool", "?")
        tipo = action.get("type", "?")

        if not cmd:
            continue

        print(f"  {C.CYAN}  🔧 {tipo.upper()} {tool}: {cmd}{C.RESET}")
        ok, out = _run(cmd, timeout=120)
        if ok:
            print(f"  {C.GREEN}      ✅ OK{C.RESET}")
        else:
            print(f"  {C.RED}      ❌ {out[:100]}{C.RESET}")
        results.append({"action": action, "ok": ok, "output": out})

    return results

# ── Función principal exportable ───────────────────────────────────────────────

def scan_and_fix_prereqs(framework: str, fw_version: str = None) -> dict:
    """
    Escanea el entorno, reporta problemas y aplica correcciones automáticas.
    Devuelve un resumen JSON-serializable.
    """
    fw_key = framework.lower().replace(".js","").replace(" ","")
    print(f"\n  {C.BOLD}🔍 Escaneando prerequisitos para {framework}...{C.RESET}\n")

    # ── Inventario completo ────────────────────────────────────────────────
    tools = {
        "node":   check_node(),
        "npm":    check_npm(),
        "nvm":    check_nvm(),
        "python": check_python(),
    }
    if fw_key in COMPATIBILITY and COMPATIBILITY[fw_key].get("cli_cmd"):
        tools[f"{fw_key}_cli"] = check_cli(fw_key)

    # Mostrar inventario
    print(f"  {C.BOLD}  📦 Herramientas detectadas:{C.RESET}")
    for name, info in tools.items():
        if info.installed:
            print(f"  {C.GREEN}    ✅ {name:<15} {info.version or 'instalado'}{C.RESET}")
        else:
            print(f"  {C.RED}    ❌ {name:<15} {info.error or 'no encontrado'}{C.RESET}")

    # nvm: listar versiones de Node disponibles
    nvm = tools["nvm"]
    if nvm.installed:
        nvm_vers = list_nvm_versions()
        if nvm_vers:
            print(f"\n  {C.DIM}  Versiones Node en nvm: {', '.join(nvm_vers)}{C.RESET}")

    # ── Compatibilidad ─────────────────────────────────────────────────────
    compat = check_compatibility(fw_key, fw_version)

    if compat.ok:
        print(f"\n  {C.GREEN}  ✅ Entorno compatible con {framework}{' '+fw_version if fw_version else ''}{C.RESET}\n")
    else:
        print(f"\n  {C.YELLOW}  ⚠️  Problemas de compatibilidad encontrados:{C.RESET}")
        for issue in compat.issues:
            print(f"  {C.RED}    • {issue}{C.RESET}")
        for sug in compat.suggestions:
            print(f"  {C.YELLOW}    💡 {sug}{C.RESET}")

        # Aplicar correcciones automáticas
        auto_actions = [a for a in compat.actions if a.get("auto")]
        manual_actions = [a for a in compat.actions if not a.get("auto")]

        if auto_actions:
            print(f"\n  {C.CYAN}  🔧 Aplicando correcciones automáticas...{C.RESET}")
            apply_prereq_actions(auto_actions)

        if manual_actions:
            print(f"\n  {C.YELLOW}  ⚠️  Acciones manuales requeridas:{C.RESET}")
            for a in manual_actions:
                url = a.get("url", "")
                print(f"  {C.YELLOW}    → Instala {a['tool']} {a.get('version','')} desde {url}{C.RESET}")

    # Resultado serializable
    return {
        "framework":  framework,
        "fw_version": fw_version,
        "tools":      {k: {"installed": v.installed, "version": v.version}
                       for k, v in tools.items()},
        "compatible": compat.ok,
        "issues":     compat.issues,
        "actions_taken": [a for a in compat.actions if a.get("auto")],
        "actions_manual": [a for a in compat.actions if not a.get("auto")],
    }