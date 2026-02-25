"""
core/registry.py — Fuente única de verdad para los items de Sonny.
Combina apps builtin + custom_apps.json.
Soporta: .exe, .lnk, imágenes, videos, audio, documentos.
"""
import os, json

# ── Rutas ──────────────────────────────────────────────────────────────────────
_BASE        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM_FILE  = os.path.join(_BASE, "data", "custom_apps.json")

_LOCAL = os.environ.get("LOCALAPPDATA", "")
_ROAM  = os.environ.get("APPDATA", "")

SYSTEM_CMDS = {"notepad", "mspaint", "calc", "explorer", "cmd", "powershell"}

# Apps builtin conocidas
BUILTIN: dict[str, str] = {
    "chrome":      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "firefox":     r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "vscode":      os.path.join(_LOCAL, r"Programs\Microsoft VS Code\Code.exe"),
    "notepad":     "notepad",
    "paint":       "mspaint",
    "calculator":  "calc",
    "explorer":    "explorer",
    "cmd":         "cmd",
    "powershell":  "powershell",
    "github":      os.path.join(_LOCAL, r"GitHubDesktop\GitHubDesktop.exe"),
    "spotify":     os.path.join(_ROAM,  r"Spotify\Spotify.exe"),
    "discord":     os.path.join(_LOCAL, r"Discord\Update.exe"),
    "steam":       r"C:\Program Files (x86)\Steam\steam.exe",
    "obs":         r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    "vlc":         r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    "postman":     os.path.join(_LOCAL, r"Postman\Postman.exe"),
    "zoom":        os.path.join(_ROAM,  r"Zoom\bin\Zoom.exe"),
}

# Tipos por extensión (para mostrar en UI/lista)
EXT_TYPE: dict[str, str] = {
    ".exe": "app",   ".lnk": "acceso",
    ".jpg": "img",   ".jpeg": "img",  ".png": "img",  ".gif": "img", ".webp": "img",
    ".mp4": "video", ".mkv": "video", ".avi": "video", ".mov": "video",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio",
    ".pdf": "doc",   ".docx": "doc",  ".xlsx": "doc",  ".txt": "doc",
}

def item_type(path: str) -> str:
    if path in SYSTEM_CMDS:
        return "sistema"
    return EXT_TYPE.get(os.path.splitext(path)[1].lower(), "archivo")

def item_exists(path: str) -> bool:
    return path in SYSTEM_CMDS or os.path.exists(path)

# ── Persistencia custom ────────────────────────────────────────────────────────

def load_custom() -> dict[str, str]:
    os.makedirs(os.path.dirname(CUSTOM_FILE), exist_ok=True)
    if os.path.exists(CUSTOM_FILE):
        try:
            with open(CUSTOM_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_custom(data: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(CUSTOM_FILE), exist_ok=True)
    with open(CUSTOM_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── API pública ────────────────────────────────────────────────────────────────

def get_all() -> dict[str, str]:
    """Devuelve todos los items: builtin (si existen) + custom (siempre)."""
    result = {}
    for name, path in BUILTIN.items():
        if item_exists(path):
            result[name] = path
    result.update(load_custom())   # custom tiene prioridad y siempre se incluye
    return result
