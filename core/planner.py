"""Planner de Sonny: obtiene planes/acciones JSON desde el LLM."""

from __future__ import annotations

import json
import re
from core.ai_scraper import call_llm, get_preferred_site, set_preferred_site

MAX_RETRIES = 3


def _extract_braced_json(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise json.JSONDecodeError("No JSON object found", text, 0)
    return text[start:end + 1]


def _repair_unescaped_content_quotes(text: str) -> str:
    """Repara el caso típico: "content": "{"k":"v"}" (comillas internas sin escape)."""
    src = text
    key_pat = '"content"'
    i = 0
    out = []
    changed = False

    while i < len(src):
        k = src.find(key_pat, i)
        if k == -1:
            out.append(src[i:])
            break

        out.append(src[i:k])
        out.append(key_pat)
        j = k + len(key_pat)

        while j < len(src) and src[j].isspace():
            out.append(src[j]); j += 1
        if j < len(src) and src[j] == ':':
            out.append(':'); j += 1
        while j < len(src) and src[j].isspace():
            out.append(src[j]); j += 1

        if j >= len(src) or src[j] != '"':
            i = j
            continue

        out.append('"')
        j += 1
        value_start = j

        end_q = -1
        if j < len(src) and src[j] in '{[':
            # Si content empieza con objeto/lista serializado dentro del string,
            # buscamos el cierre balanceado y luego la comilla del string.
            opener = src[j]
            closer = '}' if opener == '{' else ']'
            balance = 0
            scan = j
            while scan < len(src):
                ch = src[scan]
                if ch == opener:
                    balance += 1
                elif ch == closer:
                    balance -= 1
                    if balance == 0:
                        nq = scan + 1
                        while nq < len(src) and src[nq].isspace():
                            nq += 1
                        if nq < len(src) and src[nq] == '"':
                            end_q = nq
                            break
                scan += 1
        else:
            # fallback general
            scan = j
            while scan < len(src):
                if src[scan] == '"':
                    nxt = scan + 1
                    while nxt < len(src) and src[nxt].isspace():
                        nxt += 1
                    if nxt >= len(src) or src[nxt] in ',}':
                        end_q = scan
                        break
                scan += 1

        if end_q == -1:
            out.append(src[value_start:])
            break

        raw_val = src[value_start:end_q]
        escaped = raw_val.replace("\\", "\\\\").replace("\"", "\\\"")
        if escaped != raw_val:
            changed = True
        out.append(escaped)
        out.append('"')
        i = end_q + 1

    return ''.join(out) if changed else text


class PlannerError(RuntimeError):
    """Error al obtener JSON estructurado del LLM."""


def _extract_json(raw: str) -> dict:
    text = (raw or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        candidate = _extract_braced_json(text)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            repaired = _repair_unescaped_content_quotes(candidate)
            return json.loads(repaired)


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
                "Corrige y responde únicamente JSON válido. "
                "IMPORTANTE: si el campo content incluye JSON interno, debe ir escapado, "
                "por ejemplo: {\"a\":1} dentro del string."
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
