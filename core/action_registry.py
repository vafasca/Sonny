"""Registro de acciones permitidas para el executor."""

from __future__ import annotations


def _execute_command(action: dict, context: dict):
    from core.agent import execute_command

    return execute_command(action, context)


def _write_file(action: dict, context: dict):
    from core.agent import write_file

    return write_file(action, context)


def _modify_file(action: dict, context: dict):
    from core.agent import modify_file

    return modify_file(action, context)


def _planner_call(action: dict, context: dict):
    from core.agent import planner_call

    return planner_call(action, context)


ALLOWED_ACTIONS = {
    "command": _execute_command,
    "file_write": _write_file,
    "file_modify": _modify_file,
    "llm_call": _planner_call,
}
