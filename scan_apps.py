"""
scan_apps.py — Escanea las aplicaciones instaladas en Windows
Ejecuta este script una vez para ver qué apps puede abrir Sonny.
"""
import winreg
import os
import json

# Rutas comunes donde viven los .exe
COMMON_PATHS = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), ""),
]

# Apps conocidas con sus rutas típicas (se usan como fallback)
KNOWN_APPS = {
    "chrome":        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "firefox":       r"C:\Program Files\Mozilla Firefox\firefox.exe",
    "vscode":        os.path.join(os.environ.get("LOCALAPPDATA",""), r"Programs\Microsoft VS Code\Code.exe"),
    "notepad":       "notepad",
    "paint":         "mspaint",
    "calculator":    "calc",
    "explorer":      "explorer",
    "cmd":           "cmd",
    "powershell":    "powershell",
    "github":        os.path.join(os.environ.get("LOCALAPPDATA",""), r"GitHubDesktop\GitHubDesktop.exe"),
    "spotify":       os.path.join(os.environ.get("APPDATA",""), r"Spotify\Spotify.exe"),
    "discord":       os.path.join(os.environ.get("LOCALAPPDATA",""), r"Discord\Update.exe"),
    "steam":         r"C:\Program Files (x86)\Steam\steam.exe",
    "obs":           r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    "vlc":           r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    "word":          r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    "excel":         r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
    "teams":         os.path.join(os.environ.get("LOCALAPPDATA",""), r"Microsoft\Teams\current\Teams.exe"),
    "zoom":          os.path.join(os.environ.get("APPDATA",""), r"Zoom\bin\Zoom.exe"),
    "postman":       os.path.join(os.environ.get("LOCALAPPDATA",""), r"Postman\Postman.exe"),
}

def get_registry_apps():
    """Lee apps instaladas desde el registro de Windows."""
    apps = {}
    keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    hives = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]

    for hive in hives:
        for key_path in keys:
            try:
                key = winreg.OpenKey(hive, key_path)
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                        name = winreg.QueryValueEx(sub, "DisplayName")[0]
                        try:
                            loc = winreg.QueryValueEx(sub, "InstallLocation")[0]
                            if loc:
                                apps[name] = loc
                        except:
                            pass
                    except:
                        pass
            except:
                pass
    return apps

SYSTEM_COMMANDS = {"notepad", "mspaint", "calc", "explorer", "cmd", "powershell"}

def load_custom_apps():
    """Carga apps personalizadas desde custom_apps.json si existe."""
    custom_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "custom_apps.json")
    if os.path.exists(custom_file):
        try:
            with open(custom_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️  Error leyendo custom_apps.json: {e}")
    return {}

def get_available_apps():
    """Devuelve dict de apps disponibles {nombre: ruta_exe}."""
    available = {}

    # 1. Apps conocidas que existen en disco
    for name, path in KNOWN_APPS.items():
        if path in SYSTEM_COMMANDS:
            available[name] = path
        elif os.path.exists(path):
            available[name] = path

    # 2. Apps personalizadas desde custom_apps.json (se confía en el usuario)
    for name, path in load_custom_apps().items():
        available[name] = path  # siempre se agregan, el usuario sabe la ruta

    return available

if __name__ == "__main__":
    print("\n🔍 Escaneando aplicaciones disponibles...\n")
    apps   = get_available_apps()
    custom = load_custom_apps()

    print(f"{'Nombre':<20} {'Disponible':<10} {'Fuente':<12} Ruta")
    print("─" * 80)
    for name, path in sorted(apps.items()):
        fuente = "📌 custom" if name in custom else "builtin"
        print(f"{name:<20} {'✅':<10} {fuente:<12} {path}")

    print(f"\nTotal: {len(apps)} apps encontradas")
    print(f"\n💡 Para agregar más apps edita: custom_apps.json")
    print('   Formato: { "nombre": "C:\\\\ruta\\\\a\\\\app.exe" }')
