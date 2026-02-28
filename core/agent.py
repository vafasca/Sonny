"""Executor determinístico de Sonny.

Este módulo NO planifica. Solo ejecuta acciones validadas.
"""

from __future__ import annotations

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


def execute_command(action: dict, context: dict) -> dict:
    cmd = action["command"]
    workspace = Path(context["workspace"])
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_CMD,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output.strip()}


def write_file(action: dict, context: dict) -> dict:
    workspace = Path(context["workspace"])
    path = workspace / action["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(action.get("content", ""), encoding="utf-8")
    return {"ok": True, "path": str(path)}


def modify_file(action: dict, context: dict) -> dict:
    workspace = Path(context["workspace"])
    path = workspace / action["path"]
    if not path.exists():
        raise ExecutorError(f"No existe archivo para modificar: {action['path']}")
    path.write_text(action.get("content", ""), encoding="utf-8")
    return {"ok": True, "path": str(path)}


def planner_call(action: dict, context: dict) -> dict:
    prompt = action.get("prompt", "")
    response = call_llm(prompt)
    return {"ok": True, "response": response}


# Compatibilidad con sonny.py: mantiene entrypoint pero delega a orchestrator.
def run_agent(user_request: str):
    from core.orchestrator import run_orchestrator

    return run_orchestrator(user_request)
