"""Planner de Sonny: obtiene planes/acciones JSON desde el LLM."""

from __future__ import annotations

import json
from core.ai_scraper import call_llm, get_preferred_site, set_preferred_site

MAX_RETRIES = 3


class PlannerError(RuntimeError):
    """Error al obtener JSON estructurado del LLM."""


def _extract_json(raw: str) -> dict:
    text = (raw or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _ask_json(prompt: str, preferred_site: str | None = None) -> dict:
    last_error = ""
    strict_prompt = (
        f"{prompt}\n\n"
        "Responde SOLO con JSON válido. Sin markdown, sin texto extra."
    )

    for _ in range(MAX_RETRIES):
        current = get_preferred_site()
        if preferred_site is not None:
            set_preferred_site(preferred_site)
        try:
            raw = call_llm(strict_prompt)
        finally:
            if preferred_site is not None:
                set_preferred_site(current)
        try:
            return _extract_json(raw)
        except Exception as exc:
            last_error = str(exc)
            strict_prompt = (
                f"Respuesta inválida. Error: {last_error}. "
                "Corrige y responde únicamente JSON válido."
            )

    raise PlannerError(f"No se pudo obtener JSON válido del LLM tras {MAX_RETRIES} intentos: {last_error}")


def get_master_plan(user_request: str, preferred_site: str | None = None) -> dict:
    prompt = (
        "Genera un plan maestro para ejecutar la solicitud del usuario.\n"
        "Formato obligatorio:\n"
        "{\n"
        '  "phases": [\n'
        '    {"name": "...", "description": "...", "depends_on": []}\n'
        "  ]\n"
        "}\n"
        f"Solicitud del usuario: {user_request}"
    )
    return _ask_json(prompt, preferred_site=preferred_site)


def get_phase_actions(phase_name: str, context: dict, preferred_site: str | None = None) -> dict:
    prompt = (
        f"Genera acciones para la fase '{phase_name}'.\n"
        "Formato obligatorio:\n"
        "{\n"
        '  "actions": [\n'
        '    {"type": "command", "command": "..."},\n'
        '    {"type": "file_write", "path": "...", "content": "..."},\n'
        '    {"type": "file_modify", "path": "...", "content": "..."},\n'
        '    {"type": "llm_call", "prompt": "..."}\n'
        "  ]\n"
        "}\n"
        f"Contexto: {json.dumps(context, ensure_ascii=False)}"
    )
    return _ask_json(prompt, preferred_site=preferred_site)
