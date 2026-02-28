"""Gestión de estado determinístico del agente Sonny."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter
from typing import Any


@dataclass
class AgentState:
    current_phase: str | None = None
    completed_phases: list[str] = field(default_factory=list)
    action_history: list[dict[str, Any]] = field(default_factory=list)
    iteration_count: int = 0
    tool_usage_count: Counter = field(default_factory=Counter)
    phase_action_count: int = 0
    phase_repetition_count: Counter = field(default_factory=Counter)

    def set_phase(self, phase_name: str) -> None:
        if self.current_phase == phase_name:
            self.phase_repetition_count[phase_name] += 1
        else:
            self.current_phase = phase_name
            self.phase_action_count = 0
            self.phase_repetition_count[phase_name] += 1

    def complete_phase(self, phase_name: str) -> None:
        if phase_name not in self.completed_phases:
            self.completed_phases.append(phase_name)

    def register_action(self, action: dict[str, Any], result: dict[str, Any]) -> None:
        action_type = action.get("type", "unknown")
        self.action_history.append(
            {
                "phase": self.current_phase,
                "action": action,
                "result": result,
            }
        )
        self.phase_action_count += 1
        self.tool_usage_count[action_type] += 1

    def increment_iteration(self) -> None:
        self.iteration_count += 1

    def reset_phase(self) -> None:
        self.phase_action_count = 0
