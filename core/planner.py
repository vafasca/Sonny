"""Planner de Sonny: obtiene planes/acciones JSON desde el LLM."""

from __future__ import annotations

import json
import re
from core.ai_scraper import call_llm, get_preferred_site, set_preferred_site
from core.pipeline_config import MAX_ACTIONS_PER_PHASE, MAX_LLM_CALLS_PER_PHASE, MAX_FILE_WRITES_WITHOUT_BUILD

MAX_RETRIES = 3


def _dedupe_keep_order(items: list[str], limit: int) -> list[str]:
    seen = set()
    out = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= limit:
            break
    return out


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
        "Genera un plan maestro para ejecutar la solicitud del usuario con fases rígidas y validación incremental.\n"
        "Incluye exactamente estas fases (puedes agregar subfases internas si hace falta):\n"
        "1) FASE 1 — ESTRUCTURA ARQUITECTÓNICA\n"
        "2) FASE 2 — IMPLEMENTACIÓN PROGRESIVA\n"
        "3) FASE 3 — ACCESIBILIDAD Y SEO\n"
        "4) FASE 4 — OPTIMIZACIÓN\n"
        "5) FASE 5 — QUALITY CHECK FINAL\n"
        "Formato obligatorio:\n"
        "{\n"
        '  "phases": [\n'
        '    {"name": "...", "description": "...", "depends_on": []}\n'
        "  ]\n"
        "}\n"
        f"Solicitud del usuario: {user_request}"
    )
    return _ask_json(prompt, preferred_site=preferred_site)


def _phase_constraints_text(phase_name: str, context: dict) -> str:
    low = (phase_name or "").lower()
    generic = (
        "LÍMITES OBLIGATORIOS DEL PIPELINE:\n"
        f"- Máximo {MAX_ACTIONS_PER_PHASE} acciones por fase/subfase.\n"
        f"- Máximo {MAX_LLM_CALLS_PER_PHASE} llm_call por fase/subfase.\n"
        f"- Máximo {MAX_FILE_WRITES_WITHOUT_BUILD} escrituras consecutivas (file_write/file_modify) sin build intermedio.\n"
        "- Debes evaluar el estado actual del proyecto antes de proponer nuevas acciones.\n"
        "- Prioriza arquitectura antes que diseño visual.\n"
        "- Si la fase es grande, divídela en subfases controladas.\n"
    )

    if "fase 1" in low or "estructura" in low:
        return generic + (
            "REGLAS FASE 1 — ESTRUCTURA ARQUITECTÓNICA:\n"
            "- Solo crear o validar estructura base.\n"
            "- NO generar diseño visual.\n"
            "- Para Angular standalone: crear componentes standalone vacíos; prohibido NgModule/app.module.ts/loadChildren.\n"
            "- Incluye ng build al cerrar la fase.\n"
        )

    if "fase 2" in low or "implementación" in low:
        return generic + (
            "REGLAS FASE 2 — IMPLEMENTACIÓN PROGRESIVA:\n"
            "- Modificar máximo 2 componentes por subfase.\n"
            "- No modificar más de 3 archivos antes de compilar.\n"
            "- Después de cada subfase, incluir ng build.\n"
            "- Prohibido más de una llm_call anidada.\n"
        )

    if "fase 3" in low or "accesibilidad" in low or "seo" in low:
        return generic + (
            "REGLAS FASE 3 — ACCESIBILIDAD Y SEO:\n"
            "- Solo tocar index.html, atributos semánticos, meta tags, aria-label y alt.\n"
            "- No modificar arquitectura base.\n"
            "- Incluye ng build al finalizar.\n"
        )

    if "fase 4" in low or "optimización" in low:
        return generic + (
            "REGLAS FASE 4 — OPTIMIZACIÓN:\n"
            "- Solo optimizar si existen rutas reales detectadas.\n"
            "- Evita complejidad innecesaria.\n"
            "- Incluye ng build al finalizar.\n"
        )

    if "fase 5" in low or "quality" in low:
        return generic + (
            "REGLAS FASE 5 — QUALITY CHECK FINAL:\n"
            "- Prioriza ng build --configuration production.\n"
            "- Reporta warnings y evita comandos prohibidos.\n"
        )

    return generic


def get_phase_actions(phase_name: str, context: dict, preferred_site: str | None = None) -> dict:
    existing = _dedupe_keep_order(list(context.get("existing_files", []) or []), 40)
    missing = _dedupe_keep_order(list(context.get("missing_files", []) or []), 40)
    app_tree = _dedupe_keep_order(list(context.get("app_tree", []) or []), 80)
    valid_commands = _dedupe_keep_order(list(context.get("valid_commands", []) or []), 20)
    deprecated = _dedupe_keep_order(list(context.get("deprecated_commands", []) or []), 20)
    angular_rules = _dedupe_keep_order(list(context.get("angular_rules", []) or []), 30)
    runtime = context.get("runtime_env", {})

    divider = "─" * 70
    project_block = (
        f"\n{divider}\n"
        "CONTEXTO DEL PROYECTO ANGULAR:\n"
        f"{divider}\n"
        f"Angular CLI global: {context.get('angular_cli_version', 'unknown')}\n"
        f"Angular del proyecto: {context.get('angular_project_version', 'unknown')}\n"
        f"Node: {runtime.get('node', 'unknown')} / npm: {runtime.get('npm', 'unknown')} / SO: {runtime.get('os', 'unknown')}\n"
        f"Estructura: {context.get('project_structure', 'unknown')}\n"
        f"Task workspace: {context.get('task_workspace', '')}\n"
        f"Project root: {context.get('project_root', '')}\n"
        f"Current workdir: {context.get('current_workdir', '')}\n"
        "\nARCHIVOS QUE EXISTEN (puedes modificar):\n"
        + "\n".join(f"• {f}" for f in existing)
        + "\n\nARCHIVOS QUE NO EXISTEN (NO intentes modificar, usa file_write):\n"
        + "\n".join(f"• {f}" for f in missing)
        + "\n\nÁRBOL REAL src/app (escaneado):\n"
        + ("\n".join(f"• {f}" for f in app_tree) if app_tree else "• (vacío/no detectado)")
        + "\n\nCOMANDOS VÁLIDOS:\n"
        + "\n".join(f"• {c}" for c in valid_commands)
        + "\n\nCOMANDOS DEPRECADOS/NO USAR:\n"
        + "\n".join(f"• {c}" for c in deprecated)
        + "\n\nREGLAS ANGULAR:\n"
        + "\n".join(f"• {r}" for r in angular_rules)
        + f"\n{divider}\n"
    )

    exec_rules = (
        "Reglas de ejecución:\n"
        "- Usa rutas RELATIVAS lógicas al proyecto (ej: src/app/app.ts).\n"
        "- NO uses rutas absolutas.\n"
        "- El executor resolverá rutas absolutas de forma segura.\n"
        "- Si archivo no existe, usa file_write en vez de file_modify.\n"
        "- No uses ng serve/npm start en modo automático.\n"
        "\nREGLAS CRÍTICAS PARA EL CAMPO \"content\":\n"
        "- SIEMPRE debe contener código fuente COMPLETO listo para escribirse en disco.\n"
        "- Si es .scss → selectores CSS reales. Ejemplo: body { margin: 0; }\n"
        "- Si es .ts   → código TypeScript Angular compilable.\n"
        "- Si es .html → markup HTML/Angular válido.\n"
        "- Si es .md   → texto y markdown libremente.\n"
        "\nPROHIBIDO en \"content\" para .scss, .ts y .html:\n"
        "- Texto descriptivo (\"Agregar X, hacer Y...\").\n"
        "- Instrucciones en lenguaje natural.\n"
        "- Comentarios sin código real.\n"
        "\n❌ MAL: \"content\": \"Agregar focus-visible a botones...\"\n"
        "✅ BIEN: \"content\": \":focus-visible { outline: 3px solid #667EEA; }\"\n"
    )

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
        f"{project_block}\n"
        f"{_phase_constraints_text(phase_name, context)}\n"
        f"{exec_rules}"
    )

    forbidden = _dedupe_keep_order(list(context.get("forbidden_commands", []) or []), 30)
    if forbidden:
        prompt += (
            "\n\nCOMANDOS PROHIBIDOS (NO los uses bajo ninguna circunstancia):\n"
            + "\n".join(f"• {c}" for c in forbidden)
        )

    accum = list(context.get("accumulated_quality_failures", []) or [])
    if accum:
        prompt += (
            "\n\nFALLOS ACUMULADOS EN RONDAS ANTERIORES DE CALIDAD:\n"
            + "\n".join(
                f"• {f.get('command', '?')} → exit {f.get('exit_code', '?')}: {str(f.get('output', ''))[:120]}"
                for f in accum
            )
        )

    return _ask_json(prompt, preferred_site=preferred_site)
