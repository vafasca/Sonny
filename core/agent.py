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
from core.loop_guard import LoopGuard, LoopGuardError
from core.validator import validate_actions, ValidationError
from core.web_log import log_action_blocked, log_error

WORKSPACE_ROOT = Path(__file__).parent.parent / "workspace"
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
TIMEOUT_CMD = 120
MAX_NESTED_LLM_CALLS = 2
MAX_CONTEXT_FILE_BYTES = 3000
MAX_CONTEXT_FILES = 8
MAX_CONTEXT_TOTAL_BYTES = 12000

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
        return self._execute_actions(actions_payload, state, nested_depth=0)

    def _execute_actions(self, actions_payload: dict, state: AgentState, nested_depth: int) -> list[dict[str, Any]]:
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

                if action_type == "llm_call":
                    nested_payload = _parse_nested_actions_payload(result.get("response", ""))
                    if nested_payload:
                        if nested_depth >= MAX_NESTED_LLM_CALLS:
                            raise ExecutorError(
                                f"Se alcanzó max_nested_calls={MAX_NESTED_LLM_CALLS} para llm_call."
                            )
                        validate_actions(nested_payload)
                        nested_results = self._execute_actions(nested_payload, state, nested_depth=nested_depth + 1)
                        result["nested_actions_executed"] = len(nested_payload.get("actions", []))
                        result["nested_results"] = nested_results
                        if any(not r.get("ok", False) for r in nested_results):
                            raise ExecutorError("Fallaron acciones anidadas devueltas por llm_call.")
            except (ValidationError, ExecutorError) as exc:
                log_action_blocked(action_type or "unknown", str(exc))
                log_error("executor", f"Acción bloqueada/fallida ({action_type}): {exc}")
                result = {"ok": False, "error": str(exc)}
            except Exception as exc:
                log_action_blocked(action_type or "unknown", str(exc))
                log_error("executor", f"Acción bloqueada/fallida ({action_type}): {exc}")
                result = {"ok": False, "error": str(exc)}

            is_root_action = nested_depth == 0
            state.register_action(action, result, count_for_phase=is_root_action)
            results.append(result)

            try:
                LoopGuard.check(state)
            except LoopGuardError as exc:
                raise ExecutorError(str(exc)) from exc

        return results


def _parse_nested_actions_payload(raw: str) -> dict | None:
    if not isinstance(raw, str):
        return None

    text = raw.strip()
    if not text:
        return None

    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("actions"), list):
            return payload

    return None


def _collect_project_file_context(prompt: str, state: AgentState | None) -> str:
    if state is None or not state.project_root:
        return ""

    project_root = Path(state.project_root).resolve()
    if not project_root.exists() or not project_root.is_dir():
        return ""

    low = (prompt or "").lower()

    keyword_map: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
        (
            ("html", "formulario", "form", "accesibilidad", "aria", "label", "input", "reemplazar"),
            (".html",),
        ),
        (
            ("scss", "css", "estilo", "color", "variable", "tipografía", "margin", "padding", "consistencia"),
            (".scss", ".css"),
        ),
        (
            ("ts", "typescript", "componente", "import", "standalone", "service", "compilación", "error"),
            (".ts",),
        ),
    ]

    selected_exts: set[str] = set()
    for keywords, exts in keyword_map:
        if any(kw in low for kw in keywords):
            selected_exts.update(exts)

    candidate_rel_paths: list[str] = []
    if selected_exts:
        for file in sorted(project_root.rglob("*")):
            if not file.is_file():
                continue
            if file.suffix.lower() not in selected_exts:
                continue
            try:
                rel = file.relative_to(project_root)
            except ValueError:
                continue
            candidate_rel_paths.append(str(rel).replace("\\", "/"))

    # Si el prompt menciona rutas explícitas, priorizarlas.
    explicit_paths = re.findall(r"(?:src/[\w\-./]+\.(?:ts|html|scss|css|md))", prompt or "", flags=re.IGNORECASE)
    candidate_rel_paths.extend(explicit_paths)

    selected: list[str] = []
    seen = set()
    for rel in candidate_rel_paths:
        norm = rel.replace("\\", "/")
        if norm in seen:
            continue
        seen.add(norm)
        selected.append(norm)
        if len(selected) >= MAX_CONTEXT_FILES:
            break

    blocks: list[str] = []
    total_context_bytes = 0
    for rel in selected:
        full = (project_root / rel).resolve()
        try:
            if not full.exists() or not full.is_file():
                continue
            if project_root not in full.parents and full != project_root:
                continue

            full_text = full.read_text(encoding="utf-8", errors="replace")
            allowed_for_file = min(MAX_CONTEXT_FILE_BYTES, MAX_CONTEXT_TOTAL_BYTES - total_context_bytes)
            if allowed_for_file <= 0:
                break

            content = full_text[:allowed_for_file]
            consumed = len(content)
            if consumed <= 0:
                continue

            blocks.append(f"[ARCHIVO REAL EN DISCO: {rel}]\n```\n{content}\n```")
            total_context_bytes += consumed
        except Exception:
            continue

    if not blocks:
        app_dir = project_root / "src" / "app"
        if app_dir.exists():
            tree = [
                str(p.relative_to(project_root)).replace("\\", "/")
                for p in sorted(app_dir.rglob("*"))
                if p.is_file()
            ]
            if tree:
                blocks.append(
                    "ÁRBOL REAL DEL PROYECTO (sin contenido — especifica qué archivos necesitas):\n"
                    + "\n".join(f"• {f}" for f in tree[:60])
                )

    if not blocks:
        return ""

    divider = "═" * 60
    return (
        f"{divider}\n"
        "CONTENIDO REAL DEL PROYECTO EN DISCO\n"
        "(usa EXACTAMENTE estas estructuras, no inventes nuevas):\n"
        f"{divider}\n"
        + "\n\n".join(blocks)
    )


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
    angular_files = list(task_workspace.rglob("angular.json"))
    if not angular_files:
        return None
    # Política explícita: elegir el angular.json más reciente.
    angular_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return angular_files[0].parent.resolve()


def _extract_angular_project_version(project_root: Path | None) -> str:
    if not project_root:
        return "unknown"
    package_file = project_root / "package.json"
    if not package_file.exists():
        return "unknown"
    try:
        data = json.loads(package_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return "unknown"

    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    version = str(deps.get("@angular/core", "")).strip()
    return version or "unknown"


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
        state.angular_project_version = _extract_angular_project_version(detected)


def _extract_cd_target(cmd: str) -> str | None:
    m = re.match(r"^\s*cd\s+([^&;]+)", cmd.strip(), flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'")


def _extract_ng_new_target(cmd: str) -> str | None:
    m = re.search(r"\bng\s+new\s+([\w\-.]+)", cmd, flags=re.IGNORECASE)
    return m.group(1) if m else None


def _validate_lint_target(cmd: str, cwd: Path) -> None:
    if not re.match(r"^\s*ng\s+lint\b", cmd, flags=re.IGNORECASE):
        return
    angular_file = cwd / "angular.json"
    if not angular_file.exists():
        raise ExecutorError("ng lint bloqueado: angular.json no existe en el directorio actual.")
    content = angular_file.read_text(encoding="utf-8", errors="replace")
    if '"lint"' not in content:
        raise ExecutorError("ng lint bloqueado: el target lint no está definido en angular.json.")


def _guard_angular_command(cmd: str, cwd: Path, state: AgentState | None, workspace: Path) -> Path:
    if not re.match(r"^\s*ng\b", cmd, flags=re.IGNORECASE):
        return cwd

    if re.search(r"\bng\s+new\b", cmd, flags=re.IGNORECASE):
        return cwd

    if (cwd / "angular.json").exists():
        _validate_lint_target(cmd, cwd)
        return cwd

    if state and state.project_root and (Path(state.project_root) / "angular.json").exists():
        fixed = _ensure_inside_workspace(Path(state.project_root).resolve(), _state_workspace(state, workspace))
        _validate_lint_target(cmd, fixed)
        return fixed

    raise ExecutorError(
        "Comando Angular bloqueado: no hay angular.json en el directorio actual ni project_root detectado."
    )




def _block_interactive_commands(cmd: str) -> None:
    low = cmd.strip().lower()
    blocked_prefixes = (
        'ng serve',
        'npm start',
        'npm run start',
    )
    if any(low.startswith(prefix) for prefix in blocked_prefixes):
        raise ExecutorError(
            f"Comando interactivo bloqueado en modo automático: '{cmd}'. Usa ng build/ng test/ng e2e en su lugar."
        )


def execute_command(action: dict, context: dict) -> dict:
    cmd = action["command"]
    _block_interactive_commands(cmd)
    state: AgentState | None = context.get("state")
    workspace = Path(context["workspace"]).resolve()

    _sync_state_with_disk(state, workspace)
    cwd = _resolve_base_dir(context)
    cwd = _guard_angular_command(cmd, cwd, state, workspace)

    ng_new_target = _extract_ng_new_target(cmd)
    if state and ng_new_target:
        optimistic = _safe_join(cwd, ng_new_target, context)
        state.set_project_root(optimistic)

    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_CMD,
        encoding="utf-8",
        errors="replace",
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
            state.angular_project_version = _extract_angular_project_version(detected)
            created_project_root = str(detected)

    result = {"ok": proc.returncode == 0, "returncode": proc.returncode, "output": output.strip()}
    if state:
        result["cwd"] = str(state.current_workdir or cwd)
        if state.project_root:
            result["project_root"] = str(state.project_root)
        if created_project_root:
            result["created_project_root"] = created_project_root
    return result


def _normalize_file_content(content: Any) -> str:
    if not isinstance(content, str):
        return json.dumps(content, ensure_ascii=False, indent=2)

    normalized = content
    # Caso frecuente: el LLM devuelve texto doble-escapado ("\\n" literal)
    # y termina escribiéndose en una sola línea.
    if "\\n" in normalized and "\n" not in normalized:
        normalized = normalized.replace("\\r\\n", "\n").replace("\\n", "\n")
    if "\\t" in normalized and "\t" not in normalized:
        normalized = normalized.replace("\\t", "\t")

    return normalized


def write_file(action: dict, context: dict) -> dict:
    state: AgentState | None = context.get("state")
    workspace = Path(context["workspace"]).resolve()
    _sync_state_with_disk(state, workspace)

    base = _resolve_base_dir(context)
    path = _safe_join(base, action["path"], context)
    path.parent.mkdir(parents=True, exist_ok=True)

    content = _normalize_file_content(action.get("content", ""))
    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path)}


def modify_file(action: dict, context: dict) -> dict:
    state: AgentState | None = context.get("state")
    workspace = Path(context["workspace"]).resolve()
    _sync_state_with_disk(state, workspace)

    base = _resolve_base_dir(context)
    path = _safe_join(base, action["path"], context)

    content = _normalize_file_content(action.get("content", ""))

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "path": str(path),
            "warning": f"file_modify fallback a file_write: archivo no existía ({action['path']})",
        }

    path.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(path)}


def planner_call(action: dict, context: dict) -> dict:
    prompt = action.get("prompt", "")
    state: AgentState | None = context.get("state")

    file_context = _collect_project_file_context(prompt, state)
    enriched_prompt = f"{prompt}\n\n{file_context}" if file_context else prompt

    response = call_llm(enriched_prompt)
    result = {"ok": True, "response": response}
    if file_context:
        result["prompt_context_files"] = True
    return result


# Compatibilidad con sonny.py: mantiene entrypoint pero delega a orchestrator.
def run_agent(user_request: str):
    from core.orchestrator import run_orchestrator

    return run_orchestrator(user_request)
