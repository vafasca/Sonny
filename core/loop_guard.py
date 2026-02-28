"""Protecciones contra loops en ejecución de fases/acciones."""

from __future__ import annotations

from core.state_manager import AgentState


class LoopGuardError(RuntimeError):
    """Error controlado cuando se detecta loop."""


class LoopGuard:
    MAX_GLOBAL_ITERATIONS = 25
    MAX_ACTIONS_PER_PHASE = 10
    MAX_SAME_ACTION_STREAK = 5
    MAX_PHASE_REPETITIONS = 3

    @classmethod
    def check(cls, state: AgentState) -> None:
        if state.iteration_count > cls.MAX_GLOBAL_ITERATIONS:
            raise LoopGuardError("Loop detectado: se superó el máximo global de iteraciones (25).")

        if state.phase_action_count > cls.MAX_ACTIONS_PER_PHASE:
            raise LoopGuardError(
                f"Loop detectado: la fase '{state.current_phase}' superó 10 acciones."
            )

        if state.current_phase and state.phase_repetition_count[state.current_phase] > cls.MAX_PHASE_REPETITIONS:
            raise LoopGuardError(
                f"Loop detectado: la fase '{state.current_phase}' se repitió más de 3 veces."
            )

        if len(state.action_history) >= cls.MAX_SAME_ACTION_STREAK:
            last_actions = [e.get("action", {}).get("type") for e in state.action_history[-cls.MAX_SAME_ACTION_STREAK:]]
            if len(set(last_actions)) == 1:
                raise LoopGuardError(
                    f"Loop detectado: {cls.MAX_SAME_ACTION_STREAK} acciones consecutivas iguales ({last_actions[0]})."
                )
