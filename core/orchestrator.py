"""Orquestador determinístico de Sonny.

Flujo:
1) Obtener plan maestro
2) Validar plan
3) Ejecutar fases en orden válido
4) Validar acciones por fase y ejecutar con executor
5) Aplicar loop guard y actualizar estado
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import re
from pathlib import Path

from core.agent import ActionExecutor
from core.loop_guard import LoopGuard, LoopGuardError
from core.planner import PlannerError, get_master_plan, get_phase_actions
from core.ai_scraper import available_sites, set_preferred_site
from core.state_manager import AgentState
from core.validator import ValidationError, validate_actions, validate_plan
from core.web_log import (
    log_error,
    log_validation_failed,
    log_action_blocked,
    log_loop_detected,
    log_phase_event,
)


class C:
    CYAN="\033[96m"; GREEN="\033[92m"; YELLOW="\033[93m"; RED="\033[91m"
    DIM="\033[2m"; RESET="\033[0m"


def _sort_phases(plan: dict) -> list[dict]:
    phases = plan.get("phases", [])
    by_name = {p["name"]: p for p in phases}
    indegree = {p["name"]: 0 for p in phases}

    for phase in phases:
        for dep in phase.get("depends_on", []):
            indegree[phase["name"]] += 1

    q = deque([name for name, d in indegree.items() if d == 0])
    ordered: list[dict] = []

    while q:
        name = q.popleft()
        ordered.append(by_name[name])
        for other in phases:
            if name in other.get("depends_on", []):
                indegree[other["name"]] -= 1
                if indegree[other["name"]] == 0:
                    q.append(other["name"])

    if len(ordered) != len(phases):
        raise ValidationError("No fue posible ordenar las fases por dependencias.")

    return ordered




def _slugify_request(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "task").strip().lower()).strip("_")
    return cleaned[:48] or "task"


def _build_task_workspace(user_request: str, workspace: Path | None) -> Path:
    if workspace is not None:
        root = Path(workspace)
        root.mkdir(parents=True, exist_ok=True)
        return root

    base = Path(__file__).parent.parent / "workspace"
    base.mkdir(parents=True, exist_ok=True)
    task_dir = base / f"{_slugify_request(user_request)}_{datetime.now().strftime('%H%M%S_%f')}"
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def run_orchestrator(
    user_request: str,
    workspace: Path | None = None,
    preferred_site: str | None = None,
) -> dict:
    task_workspace = _build_task_workspace(user_request, workspace)
    state = AgentState()
    state.set_task_workspace(task_workspace)
    executor = ActionExecutor(workspace=task_workspace)
    print(f"  {C.DIM}Workspace de tarea: {task_workspace}{C.RESET}")

    master_plan: dict | None = None
    for _ in range(2):
        master_plan = get_master_plan(user_request, preferred_site=preferred_site)
        try:
            validate_plan(master_plan)
            break
        except ValidationError as exc:
            log_validation_failed("plan", str(exc))
            user_request = (
                f"Corrige estructuralmente el plan previo. Error: {exc}. "
                f"Solicitud original: {user_request}"
            )
    else:
        raise PlannerError("No se pudo obtener un plan maestro estructuralmente válido.")

    phase_results: list[dict] = []
    ordered_phases = _sort_phases(master_plan)
    print(f"  {C.CYAN}Plan validado: {len(ordered_phases)} fase(s){C.RESET}")

    for phase in ordered_phases:
        phase_name = phase["name"]
        state.set_phase(phase_name)

        context = {
            "user_request": user_request,
            "phase": phase,
            "completed_phases": state.completed_phases,
            "action_history": state.action_history[-10:],
            "task_workspace": str(state.task_workspace) if state.task_workspace else "",
            "current_workdir": str(state.current_workdir) if state.current_workdir else "",
            "project_root": str(state.project_root) if state.project_root else "",
        }

        try:
            actions_payload = get_phase_actions(phase_name, context, preferred_site=preferred_site)
            validate_actions(actions_payload)
        except ValidationError as exc:
            log_validation_failed(f"acciones:{phase_name}", str(exc))
            log_action_blocked("phase_actions", str(exc))
            raise

        log_phase_event(phase_name, "start")
        actions = actions_payload.get("actions", [])
        print(f"  {C.CYAN}▶ Fase: {phase_name}{C.RESET} {C.DIM}({len(actions)} acciones){C.RESET}")
        for idx, action in enumerate(actions, 1):
            atype = action.get("type", "unknown")
            detail = action.get("path") or action.get("command") or action.get("prompt", "")[:60]
            print(f"    {C.DIM}- [{idx}/{len(actions)}] {atype}: {detail}{C.RESET}")

        try:
            results = executor.execute_actions(actions_payload, state)
            state.increment_iteration()
            LoopGuard.check(state)
            state.complete_phase(phase_name)
            state.reset_phase()
            phase_results.append({"phase": phase_name, "results": results, "cwd": str(state.current_workdir or task_workspace), "project_root": str(state.project_root) if state.project_root else ""})
            log_phase_event(phase_name, "completed", f"actions={len(results)}")
        except LoopGuardError as exc:
            log_loop_detected(phase_name, str(exc))
            raise LoopGuardError(f"{exc} | fase={phase_name} | iteraciones={state.iteration_count}")
        except Exception as exc:
            log_error("orchestrator", f"Error en fase '{phase_name}': {exc}")
            raise

    return {
        "ok": True,
        "plan": master_plan,
        "completed_phases": state.completed_phases,
        "iterations": state.iteration_count,
        "phase_results": phase_results,
        "task_workspace": str(task_workspace),
        "project_root": str(state.project_root) if state.project_root else "",
    }


# Compatibilidad con sonny.py

def detectar_navegadores():
    return []


def _extract_site_from_request(user_request: str) -> str | None:
    low = (user_request or "").lower()
    aliases = {
        "claude": "claude",
        "chatgpt": "chatgpt",
        "chat gpt": "chatgpt",
        "gpt": "chatgpt",
        "gemini": "gemini",
        "qwen": "qwen",
    }
    for token, key in aliases.items():
        if token in low:
            return key
    return None


def run_orchestrator_with_site(user_request: str, preferred_site: str | None = None):
    selected = preferred_site or _extract_site_from_request(user_request)
    if selected and selected in available_sites():
        set_preferred_site(selected)
    else:
        selected = None
        set_preferred_site(None)
    return run_orchestrator(user_request, preferred_site=selected)
