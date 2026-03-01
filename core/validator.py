"""Validación determinística de planes y acciones."""

from __future__ import annotations

from collections import defaultdict
import re


class ValidationError(ValueError):
    """Error estructural en plan o acciones."""


ALLOWED_ACTION_TYPES = {"command", "file_write", "file_modify", "llm_call"}
DANGEROUS_COMMAND_PATTERNS = [
    r"rm\s+-rf",
    r"dd\s+if=",
    r"mkfs",
    r"shutdown",
    r"\bformat\b",
    r"del\s+/f\s+/s\s+/q",
    r"curl\s+[^\n|]*\|\s*sh",
]
PROTECTED_FILES = {
    "angular.json",
    "package.json",
    "main.ts",
    "index.html",
}


def validate_plan(plan: dict) -> None:
    phases = plan.get("phases")
    if not isinstance(phases, list) or not phases:
        raise ValidationError("El plan está vacío o no contiene fases válidas.")

    seen_names: set[str] = set()
    normalized_names: set[str] = set()
    aliases: dict[str, str] = {}
    graph: dict[str, list[str]] = defaultdict(list)

    for phase in phases:
        if not isinstance(phase, dict):
            raise ValidationError("Cada fase debe ser un objeto JSON.")

        for required in ("name", "description", "depends_on"):
            if required not in phase:
                raise ValidationError(f"Fase inválida: falta '{required}'.")

        name = phase["name"]
        if name in seen_names:
            raise ValidationError(f"Fase duplicada detectada: '{name}'.")
        seen_names.add(name)

        normalized_name = _normalize_phase_ref(name)
        if normalized_name in normalized_names:
            raise ValidationError(f"Fase ambigua detectada por normalización: '{name}'.")
        normalized_names.add(normalized_name)

        short_ref = _extract_phase_short_ref(name)
        if short_ref:
            aliases[short_ref] = name

        deps = phase.get("depends_on") or []
        if not isinstance(deps, list):
            raise ValidationError(f"depends_on debe ser lista en fase '{name}'.")

        for dep in deps:
            graph[name].append(dep)

    resolved_graph: dict[str, list[str]] = defaultdict(list)

    for name, deps in graph.items():
        for dep in deps:
            resolved_dep = _resolve_phase_dependency(dep, seen_names, aliases)
            if resolved_dep is None:
                raise ValidationError(f"Dependencia desconocida '{dep}' en fase '{name}'.")
            resolved_graph[name].append(resolved_dep)

    _assert_acyclic(resolved_graph)


def _normalize_phase_ref(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _extract_phase_short_ref(name: str) -> str | None:
    normalized = re.sub(r"\s+", " ", name.strip())
    # Permite que `depends_on: ["Fase 1"]` haga match con
    # `name: "Fase 1: Configuración ..."`.
    match = re.match(r"^(fase|phase)\s+(\d+)(?=\s*[:\-–—]|\s*$)", normalized, flags=re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1).casefold()} {match.group(2)}"


def _resolve_phase_dependency(dep: str, names: set[str], aliases: dict[str, str]) -> str | None:
    if dep in names:
        return dep

    normalized_dep = _normalize_phase_ref(dep)
    by_normalized = { _normalize_phase_ref(name): name for name in names }
    if normalized_dep in by_normalized:
        return by_normalized[normalized_dep]

    short_dep = _extract_phase_short_ref(dep)
    if short_dep and short_dep in aliases:
        return aliases[short_dep]

    return None


def _assert_acyclic(graph: dict[str, list[str]]) -> None:
    visited: set[str] = set()
    visiting: set[str] = set()

    def dfs(node: str) -> None:
        if node in visiting:
            raise ValidationError(f"Dependencia circular detectada en '{node}'.")
        if node in visited:
            return

        visiting.add(node)
        for neighbor in graph.get(node, []):
            dfs(neighbor)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        dfs(node)


def validate_actions(actions_payload: dict) -> None:
    actions = actions_payload.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValidationError("La fase no contiene acciones válidas.")

    for action in actions:
        if not isinstance(action, dict):
            raise ValidationError("Cada acción debe ser un objeto JSON.")

        action_type = action.get("type")
        if action_type not in ALLOWED_ACTION_TYPES:
            raise ValidationError(f"Tipo de acción no permitido: '{action_type}'.")

        if action_type == "command":
            cmd = action.get("command", "")
            _validate_command(cmd)

        if action_type in {"file_write", "file_modify"}:
            path = action.get("path", "")
            _validate_path(path)


def _validate_command(command: str) -> None:
    if not command or not isinstance(command, str):
        raise ValidationError("Acción command sin campo 'command' válido.")

    lower = command.lower()
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, lower):
            raise ValidationError(f"Comando bloqueado por seguridad: '{command}'.")


def _validate_path(path: str) -> None:
    if not path or not isinstance(path, str):
        raise ValidationError("Acción de archivo sin path válido.")

    cleaned = path.replace("\\", "/")
    if cleaned.startswith("/") or re.match(r"^[a-zA-Z]:/", cleaned):
        raise ValidationError(f"Ruta absoluta no permitida: '{path}'.")
    if ".." in cleaned.split("/"):
        raise ValidationError(f"Path traversal bloqueado: '{path}'.")

    normalized = cleaned.split("/")[-1].lower()
    is_top_level = "/" not in cleaned
    if is_top_level and normalized in PROTECTED_FILES:
        raise ValidationError(f"Archivo protegido bloqueado: '{path}'.")
