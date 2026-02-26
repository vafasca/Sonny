"""
core/web_log.py — Registro de interacciones con IAs web.
Guarda cada prompt enviado y cada respuesta recibida en logs/web_interactions.log
"""
import os
from pathlib import Path
from datetime import datetime

# ── Carpeta de logs ────────────────────────────────────────────────────────────
LOGS_DIR = Path(__file__).parent.parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)

LOG_FILE = LOGS_DIR / "web_interactions.log"

# ── Separador visual ───────────────────────────────────────────────────────────
_SEP = "═" * 70


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_prompt(site_name: str, objetivo: str, prompt: str):
    """
    Registra el prompt que el agente envió a una IA web.

    Parámetros:
        site_name  — Nombre del sitio (ej: "ChatGPT", "Claude.ai")
        objetivo   — El objetivo original del usuario
        prompt     — El texto exacto enviado a la IA
    """
    lineas = [
        f"\n{_SEP}",
        f"[{_timestamp()}]  ▶ PROMPT ENVIADO → {site_name}",
        f"{_SEP}",
        f"OBJETIVO USUARIO : {objetivo}",
        f"{'─' * 70}",
        f"PROMPT ENVIADO   :",
        prompt,
        "",
    ]
    _write(lineas)


def log_response(site_name: str, response: str, steps_count: int = 0):
    """
    Registra la respuesta recibida de una IA web.

    Parámetros:
        site_name   — Nombre del sitio (ej: "ChatGPT", "Claude.ai")
        response    — Texto completo de la respuesta
        steps_count — Cantidad de pasos/comandos detectados (opcional)
    """
    lineas = [
        f"[{_timestamp()}]  ◀ RESPUESTA RECIBIDA ← {site_name}",
        f"{'─' * 70}",
        f"CARACTERES: {len(response)} | PASOS DETECTADOS: {steps_count}",
        f"RESPUESTA  :",
        response,
        f"{_SEP}\n",
    ]
    _write(lineas)


def log_error(site_name: str, error: str):
    """Registra un error al intentar comunicarse con una IA web."""
    lineas = [
        f"\n{'─' * 70}",
        f"[{_timestamp()}]  ✗ ERROR en {site_name}: {error}",
        f"{'─' * 70}\n",
    ]
    _write(lineas)


def log_session_start(objetivo: str):
    """Registra el inicio de una sesión del orquestador web."""
    lineas = [
        f"\n{'#' * 70}",
        f"# NUEVA SESIÓN ORQUESTADOR WEB — {_timestamp()}",
        f"# Objetivo: {objetivo}",
        f"{'#' * 70}",
    ]
    _write(lineas)


def _write(lineas: list[str]):
    """Escribe las líneas en el archivo de log con codificación UTF-8."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lineas) + "\n")
    except Exception as e:
        # No interrumpir la ejecución si el log falla
        print(f"\033[2m  [web_log] No se pudo escribir en el log: {e}\033[0m")