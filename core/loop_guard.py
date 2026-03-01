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
            last = state.action_history[-cls.MAX_SAME_ACTION_STREAK:]
            last_types = [e.get("action", {}).get("type") for e in last]
            if len(set(last_types)) == 1:
                repeated_type = last_types[0]
                # Evita falsos positivos: en desarrollo es normal tener varios file_write seguidos
                if repeated_type in {"file_write", "file_modify"}:
                    targets = [
                        (e.get("action", {}).get("path") or "").strip().lower()
                        for e in last
                    ]
                    if len(set(targets)) == 1 and targets[0]:
                        raise LoopGuardError(
                            f"Loop detectado: {cls.MAX_SAME_ACTION_STREAK} escrituras consecutivas sobre el mismo archivo ({targets[0]})."
                        )
                else:
                    raise LoopGuardError(
                        f"Loop detectado: {cls.MAX_SAME_ACTION_STREAK} acciones consecutivas iguales ({repeated_type})."
                    )
