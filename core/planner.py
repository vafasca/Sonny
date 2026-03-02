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


def _render_project_tree(app_tree: list[str]) -> str:
    if not app_tree:
        return "src/\n └── app/\n     └── (sin archivos detectados)"

    lines = ["src/", " └── app/"]
    app_only = []
    for item in app_tree:
        norm = str(item).replace('\\', '/').strip('/')
        if not norm.startswith('src/app/'):
            continue
        app_only.append(norm.replace('src/app/', ''))

    app_only = sorted(dict.fromkeys(app_only))
    if not app_only:
        return "src/\n └── app/\n     └── (sin archivos detectados)"

    for idx, rel in enumerate(app_only):
        branch = " └──" if idx == len(app_only) - 1 else " ├──"
        lines.append(f"     {branch} {rel}")
    return "\n".join(lines)


def _extra_forbidden_for_standalone(structure: str) -> list[str]:
    if "standalone" not in (structure or "").lower():
        return []
    return [
        "app.module.ts",
        "NgModules",
        "Archivos de bootstrap",
        "loadChildren",
    ]


def get_phase_actions(phase_name: str, context: dict, preferred_site: str | None = None) -> dict:
    existing = _dedupe_keep_order(list(context.get("existing_files", []) or []), 40)
    missing = _dedupe_keep_order(list(context.get("missing_files", []) or []), 40)
    app_tree = _dedupe_keep_order(list(context.get("app_tree", []) or []), 80)
    valid_commands = _dedupe_keep_order(list(context.get("valid_commands", []) or []), 20)
    deprecated = _dedupe_keep_order(list(context.get("deprecated_commands", []) or []), 20)
    angular_rules = _dedupe_keep_order(list(context.get("angular_rules", []) or []), 30)
    runtime = context.get("runtime_env", {})
    structure = context.get("project_structure", "unknown")

    no_create = _dedupe_keep_order(_extra_forbidden_for_standalone(structure), 10)
    tree_view = _render_project_tree(app_tree)

    prompt = (
        f"Genera acciones para la fase en base a mi estructura actual '{phase_name}'.\n"
        "Responde SOLO con JSON válido con este formato:\n"
        "{\n"
        '  "actions": [\n'
        '    {"type": "command", "command": "..."},\n'
        '    {"type": "file_write", "path": "...", "content": "..."},\n'
        '    {"type": "file_modify", "path": "...", "content": "..."},\n'
        '    {"type": "llm_call", "prompt": "..."}\n'
        "  ]\n"
        "}\n\n"
        "────────────────────────────────────\n"
        "CONTEXTO DETECTADO AUTOMÁTICAMENTE\n"
        "────────────────────────────────────\n"
        f"Angular CLI: {context.get('angular_cli_version', 'unknown')}\n"
        f"Angular: {context.get('angular_project_version', 'unknown')}\n"
        f"Node: {runtime.get('node', 'unknown')}\n"
        f"Arquitectura: {structure}\n"
        "\nEstructura actual:\n\n"
        f"{tree_view}\n\n"
        "Archivos existentes (puedes modificar):\n"
        + "\n".join(f"- {f}" for f in existing)
        + "\n\nArchivos faltantes (si los necesitas, usa file_write):\n"
        + "\n".join(f"- {f}" for f in missing)
        + "\n\n"
    )

    if no_create:
        prompt += "NO crear:\n" + "\n".join(f"- {item}" for item in no_create) + "\n\n"

    prompt += (
        "────────────────────────────────────\n"
        f"REGLAS {phase_name}\n"
        "────────────────────────────────────\n"
        f"{_phase_constraints_text(phase_name, context)}\n"
        "────────────────────────────────────\n"
        "LÍMITES DEL PIPELINE\n"
        "────────────────────────────────────\n"
        f"- Máx {MAX_ACTIONS_PER_PHASE} acciones.\n"
        f"- Máx {MAX_FILE_WRITES_WITHOUT_BUILD} escrituras consecutivas sin ng build.\n"
        f"- Máx {MAX_LLM_CALLS_PER_PHASE} llm_call.\n"
        "- Usar rutas relativas.\n"
        "- Incluir ng build al cerrar cambios estructurales.\n\n"
        "────────────────────────────────────\n"
        "REGLAS DEL CAMPO \"content\"\n"
        "────────────────────────────────────\n"
        "- Debe contener código fuente completo y compilable.\n"
        "- No incluir texto descriptivo.\n"
        "- Si archivo no existe, usa file_write en vez de file_modify.\n"
        "- No uses ng serve/npm start en modo automático.\n"
    )

    if valid_commands:
        prompt += "\nComandos válidos:\n" + "\n".join(f"- {c}" for c in valid_commands)
    if deprecated:
        prompt += "\n\nComandos no usar:\n" + "\n".join(f"- {c}" for c in deprecated)
    if angular_rules:
        prompt += "\n\nReglas angular adicionales:\n" + "\n".join(f"- {r}" for r in angular_rules)


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


def get_corrected_phase_actions(
    phase_name: str,
    previous_actions_payload: dict,
    validation_error: str,
    context: dict,
    preferred_site: str | None = None,
) -> dict:
    runtime = context.get("runtime_env", {})
    structure = context.get("project_structure", "unknown")
    existing = _dedupe_keep_order(list(context.get("existing_files", []) or []), 30)
    app_tree = _dedupe_keep_order(list(context.get("app_tree", []) or []), 60)

    prev_json = json.dumps(previous_actions_payload, ensure_ascii=False, indent=2)
    prompt = (
        f"La acción anterior fue bloqueada en la fase '{phase_name}' por esta razón:\n"
        f"- {validation_error}\n\n"
        "Corrige únicamente las acciones inválidas. Mantén el resto si son válidas.\n"
        "No cambies formato JSON ni tipos de acción.\n"
        "No asumas archivos inexistentes. Respeta nombres detectados (ej: app.ts, app.html, app.scss).\n"
        "Si estructura es standalone, no crees app.module.ts ni NgModules.\n"
        "Si tocas app.config.ts en standalone: exporta ApplicationConfig e incluye provideRouter(routes).\n"
        "Si archivo no existe, usa file_write; si existe, file_modify.\n\n"
        "CONTEXTO MÍNIMO:\n"
        f"- Arquitectura: {structure}\n"
        f"- Angular: {context.get('angular_project_version', 'unknown')}\n"
        f"- Node: {runtime.get('node', 'unknown')}\n"
        "- Archivos existentes:\n"
        + "\n".join(f"  • {f}" for f in existing)
        + "\n- Árbol src/app:\n"
        + ("\n".join(f"  • {f}" for f in app_tree) if app_tree else "  • (vacío/no detectado)")
        + "\n\nACCIONES PREVIAS (JSON):\n"
        + prev_json
        + "\n\nResponde SOLO con JSON válido con formato {\"actions\": [...]}"
    )
    return _ask_json(prompt, preferred_site=preferred_site)
