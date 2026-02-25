"""
core/launcher.py — Abre cualquier item del registry.
"""
import os, subprocess
from core.registry import get_all, SYSTEM_CMDS

def launch(name: str) -> tuple[bool, str]:
    """
    Abre el item por nombre.
    Devuelve (ok: bool, mensaje: str).
    """
    apps = get_all()
    ruta = apps.get(name)

    if not ruta:
        return False, f"'{name}' no está en la lista."

    try:
        if ruta in SYSTEM_CMDS:
            subprocess.Popen(ruta)
        elif ruta.lower().endswith(".exe"):
            try:
                subprocess.Popen(ruta)
            except OSError:
                os.startfile(ruta)
        else:
            # .lnk, imágenes, videos, docs → Windows elige la app correcta
            os.startfile(ruta)
        return True, f"Abriendo '{name}'..."

    except FileNotFoundError:
        return False, f"Archivo no encontrado: {ruta}\nActualiza la ruta en app_manager."
    except Exception as e:
        return False, f"Error al abrir '{name}': {e}"
