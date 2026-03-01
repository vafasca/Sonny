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
    "desarrolla",
    "crea",
    "construye",
    "programa",
    "escribe",
    "make",
    "build",
    "create",
    "develop",
    "script",
]


def es_tarea_agente(texto: str) -> bool:
    low = (texto or "").lower()
    return any(trigger in low for trigger in TRIGGERS_AGENTE)


class ExecutorError(RuntimeError):
    """Error controlado del ejecutor."""


class ActionExecutor:
    def __init__(self, workspace: Path | None = None):
        self.workspace = workspace or WORKSPACE_ROOT

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




def _resolve_base_dir(context: dict) -> Path:
    state: AgentState | None = context.get("state")
    if state and state.current_workdir:
        return Path(state.current_workdir)
    return Path(context["workspace"])


def _extract_cd_target(cmd: str) -> str | None:
    m = re.match(r"^\s*cd\s+([^&;]+)", cmd.strip(), flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def _detect_created_project_dir(cmd: str, base_dir: Path) -> Path | None:
    m = re.search(r"\bng\s+new\s+([\w\-.]+)", cmd, flags=re.IGNORECASE)
    if not m:
        return None
    candidate = (base_dir / m.group(1)).resolve()
    return candidate if candidate.exists() else None


def execute_command(action: dict, context: dict) -> dict:
    cmd = action["command"]
    workspace = _resolve_base_dir(context)
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_CMD,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")

    state: AgentState | None = context.get("state")
    created: Path | None = None
    if state and proc.returncode == 0:
        created = _detect_created_project_dir(cmd, workspace)
        if created:
            state.set_project_root(created)
        elif (workspace / "angular.json").exists():
            state.set_project_root(workspace)
        cd_target = _extract_cd_target(cmd)
        if cd_target:
            next_dir = Path(cd_target)
            if not next_dir.is_absolute():
                next_dir = (workspace / next_dir).resolve()
            if next_dir.exists() and next_dir.is_dir():
                state.set_current_workdir(next_dir)

    result = {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output.strip()}
    if state:
        result["cwd"] = str(state.current_workdir or workspace)
        if state.project_root:
            result["project_root"] = str(state.project_root)
        if created:
            result["created_project_root"] = str(created)
    return result


def write_file(action: dict, context: dict) -> dict:
    workspace = _resolve_base_dir(context)
    path = workspace / action["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    content = action.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2)
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path)}


def modify_file(action: dict, context: dict) -> dict:
    workspace = _resolve_base_dir(context)
    path = workspace / action["path"]
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
