"""Executor determinístico de Sonny.

Este módulo NO planifica. Solo ejecuta acciones validadas.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from core.action_registry import ALLOWED_ACTIONS
from core.ai_scraper import call_llm
from core.state_manager import AgentState
from core.web_log import log_action_blocked, log_error

WORKSPACE_ROOT = Path(__file__).parent.parent / "workspace"
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
TIMEOUT_CMD = 120

TRIGGERS_AGENTE = [
    "desarrolla", "crea", "construye", "programa", "escribe",
    "make", "build", "create", "develop", "script",
]


class ExecutorError(RuntimeError):
    """Error controlado del ejecutor."""


def es_tarea_agente(texto: str) -> bool:
    low = (texto or "").lower()
    return any(trigger in low for trigger in TRIGGERS_AGENTE)


class ActionExecutor:
    def __init__(self, workspace: Path | None = None):
        self.workspace = (workspace or WORKSPACE_ROOT).resolve()

    def execute_actions(self, actions_payload: dict, state: AgentState) -> list[dict[str, Any]]:
        actions = actions_payload.get("actions", [])
        results: list[dict[str, Any]] = []

        for action in actions:
            action_type = action.get("type")
            handler = ALLOWED_ACTIONS.get(action_type)
            if handler is None:
                raise ExecutorError(f"Acción no registrada: '{action_type}'.")

            try:
                result = handler(action, {"workspace": self.workspace, "state": state})
                if not isinstance(result, dict):
                    result = {"ok": True, "output": str(result)}
            except Exception as exc:
                log_action_blocked(action_type or "unknown", str(exc))
                log_error("executor", f"Acción bloqueada/fallida ({action_type}): {exc}")
                result = {"ok": False, "error": str(exc)}

            state.register_action(action, result)
            results.append(result)

        return results


def _state_workspace(state: AgentState | None, fallback: Path) -> Path:
    if state and state.task_workspace:
        return Path(state.task_workspace).resolve()
    return fallback.resolve()


def _ensure_inside_workspace(path: Path, workspace: Path) -> Path:
    resolved = path.resolve()
    ws = workspace.resolve()
    if resolved != ws and ws not in resolved.parents:
        raise ExecutorError(f"Ruta fuera del workspace de tarea: {resolved}")
    return resolved


def _resolve_base_dir(context: dict) -> Path:
    state: AgentState | None = context.get("state")
    workspace = Path(context["workspace"]).resolve()
    task_workspace = _state_workspace(state, workspace)

    base = Path(state.current_workdir).resolve() if state and state.current_workdir else task_workspace
    return _ensure_inside_workspace(base, task_workspace)


def _safe_join(base: Path, rel_path: str, context: dict) -> Path:
    if not rel_path or not isinstance(rel_path, str):
        raise ExecutorError("Ruta relativa inválida.")

    target = Path(rel_path)
    if target.is_absolute():
        raise ExecutorError(f"No se permiten rutas absolutas: {rel_path}")

    state: AgentState | None = context.get("state")
    workspace = Path(context["workspace"]).resolve()
    task_workspace = _state_workspace(state, workspace)
    return _ensure_inside_workspace((base / target).resolve(), task_workspace)


def _find_angular_root(task_workspace: Path) -> Path | None:
    candidates = sorted(task_workspace.rglob("angular.json"), key=lambda p: len(p.parts))
    if not candidates:
        return None
    return candidates[0].parent.resolve()


def _sync_state_with_disk(state: AgentState | None, workspace: Path) -> None:
    if state is None:
        return

    task_ws = _state_workspace(state, workspace)

    if not state.current_workdir or not Path(state.current_workdir).exists():
        state.set_current_workdir(task_ws)

    if state.current_workdir:
        try:
            state.set_current_workdir(_ensure_inside_workspace(Path(state.current_workdir), task_ws))
        except ExecutorError:
            state.set_current_workdir(task_ws)

    if state.project_root and not Path(state.project_root).exists():
        state.project_root = None

    detected = _find_angular_root(task_ws)
    if detected:
        state.set_project_root(detected)


def _extract_cd_target(cmd: str) -> str | None:
    m = re.match(r"^\s*cd\s+([^&;]+)", cmd.strip(), flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def _extract_ng_new_target(cmd: str) -> str | None:
    m = re.search(r"\bng\s+new\s+([\w\-.]+)", cmd, flags=re.IGNORECASE)
    return m.group(1) if m else None


def _guard_angular_command(cmd: str, cwd: Path, state: AgentState | None, workspace: Path) -> Path:
    if not re.match(r"^\s*ng\b", cmd, flags=re.IGNORECASE):
        return cwd

    if re.search(r"\bng\s+new\b", cmd, flags=re.IGNORECASE):
        return cwd

    if (cwd / "angular.json").exists():
        return cwd

    if state and state.project_root and (Path(state.project_root) / "angular.json").exists():
        return _ensure_inside_workspace(Path(state.project_root).resolve(), _state_workspace(state, workspace))

    raise ExecutorError(
        "Comando Angular bloqueado: no hay angular.json en el directorio actual ni project_root detectado."
    )


def execute_command(action: dict, context: dict) -> dict:
    cmd = action["command"]
    state: AgentState | None = context.get("state")
    workspace = Path(context["workspace"]).resolve()

    _sync_state_with_disk(state, workspace)
    cwd = _resolve_base_dir(context)
    cwd = _guard_angular_command(cmd, cwd, state, workspace)

    ng_new_target = _extract_ng_new_target(cmd)
    if state and ng_new_target:
        # actualización inmediata optimista; se valida luego con filesystem
        optimistic = _safe_join(cwd, ng_new_target, context)
        state.set_project_root(optimistic)

    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_CMD,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")

    created_project_root = ""
    if state and proc.returncode == 0:
        cd_target = _extract_cd_target(cmd)
        if cd_target:
            next_dir = _safe_join(cwd, cd_target, context)
            if not next_dir.exists() or not next_dir.is_dir():
                raise ExecutorError(f"cd inválido: destino no existe ({next_dir})")
            state.set_current_workdir(next_dir)

        detected = _find_angular_root(_state_workspace(state, workspace))
        if detected:
            state.set_project_root(detected)
            created_project_root = str(detected)

    result = {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output.strip()}
    if state:
        result["cwd"] = str(state.current_workdir or cwd)
        if state.project_root:
            result["project_root"] = str(state.project_root)
        if created_project_root:
            result["created_project_root"] = created_project_root
    return result


def write_file(action: dict, context: dict) -> dict:
    state: AgentState | None = context.get("state")
    workspace = Path(context["workspace"]).resolve()
    _sync_state_with_disk(state, workspace)

    base = _resolve_base_dir(context)
    path = _safe_join(base, action["path"], context)
    path.parent.mkdir(parents=True, exist_ok=True)

    content = action.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path)}


def modify_file(action: dict, context: dict) -> dict:
    state: AgentState | None = context.get("state")
    workspace = Path(context["workspace"]).resolve()
    _sync_state_with_disk(state, workspace)

    base = _resolve_base_dir(context)
    path = _safe_join(base, action["path"], context)
    if not path.exists():
        raise ExecutorError(f"No existe archivo para modificar: {action['path']}")

    content = action.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path)}


def planner_call(action: dict, context: dict) -> dict:
    prompt = action.get("prompt", "")
    response = call_llm(prompt)
    return {"ok": True, "response": response}


# Compatibilidad con sonny.py: mantiene entrypoint pero delega a orchestrator.
def run_agent(user_request: str):
    from core.orchestrator import run_orchestrator

    return run_orchestrator(user_request)
