"""Orquestador determinístico de Sonny."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import os
import platform
import re
import subprocess
from pathlib import Path

from core.agent import ActionExecutor
from core.loop_guard import LoopGuard, LoopGuardError
from core.planner import PlannerError, get_master_plan, get_phase_actions
from core.pipeline_config import (
    MAX_ACTIONS_PER_PHASE,
    MAX_LLM_CALLS_PER_PHASE,
    REQUIRE_PRECHECK,
)
from core.ai_scraper import available_sites, set_preferred_site
from core.state_manager import AgentState
from core.validator import ValidationError, validate_actions, validate_plan
from core.web_log import log_error, log_validation_failed, log_action_blocked, log_loop_detected, log_phase_event


class C:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text or "")


def _run_cmd_utf8(cmd: str, cwd: Path | None = None, timeout: int = 30) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        shell=True,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, _strip_ansi(out).strip()


def _parse_angular_cli_version(output: str) -> str:
    txt = (output or "").strip()
    if not txt:
        return "unknown"

    for line in txt.splitlines():
        low = line.lower()
        if "angular cli" in low and ":" in line:
            return line.split(":", 1)[1].strip() or "unknown"

    m = re.search(r"angular\s+cli\s*:?\s*v?(\d+(?:\.\d+){1,3})", txt, flags=re.IGNORECASE)
    if m:
        return m.group(1)

    return "unknown"


def detect_angular_cli_version() -> str:
    attempts = ["ng version --no-interactive", "ng version", "ng v"]
    saw_any_output = False
    for cmd in attempts:
        code, out = _run_cmd_utf8(cmd)
        if out:
            saw_any_output = True
            parsed = _parse_angular_cli_version(out)
            if parsed != "unknown":
                return parsed
        if code == 0 and out:
            continue
    return "unknown" if saw_any_output else "not_installed"


def detect_node_npm_os() -> dict[str, str]:
    _, node = _run_cmd_utf8("node -v")
    _, npm = _run_cmd_utf8("npm -v")
    return {
        "node": (node or "unknown").splitlines()[0].strip(),
        "npm": (npm or "unknown").splitlines()[0].strip(),
        "os": platform.system() or "unknown",
    }




RIGID_PHASES = [
    "FASE 1 — ESTRUCTURA ARQUITECTÓNICA",
    "FASE 2 — IMPLEMENTACIÓN PROGRESIVA",
    "FASE 3 — ACCESIBILIDAD Y SEO",
    "FASE 4 — OPTIMIZACIÓN",
    "FASE 5 — QUALITY CHECK FINAL",
]


def _normalize_phase_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _is_rigid_phase(name: str, rigid_name: str) -> bool:
    norm = _normalize_phase_name(name)
    rigid = _normalize_phase_name(rigid_name)
    return rigid in norm or norm in rigid


def _enforce_rigid_pipeline(plan: dict) -> dict:
    phases = list(plan.get("phases", []))
    if not phases:
        return {"phases": []}

    selected = []
    for rigid in RIGID_PHASES:
        found = next((p for p in phases if _is_rigid_phase(p.get("name", ""), rigid)), None)
        if found:
            selected.append({
                "name": rigid,
                "description": found.get("description", rigid),
                "depends_on": [selected[-1]["name"]] if selected else [],
            })
            continue
        selected.append({
            "name": rigid,
            "description": rigid,
            "depends_on": [selected[-1]["name"]] if selected else [],
        })

    return {"phases": selected}


def _validate_phase_action_limits(actions_payload: dict, phase_name: str) -> None:
    actions = list(actions_payload.get("actions", []))
    if len(actions) > MAX_ACTIONS_PER_PHASE:
        raise ValidationError(
            f"La fase '{phase_name}' excede máximo de acciones ({MAX_ACTIONS_PER_PHASE})."
        )

    llm_calls = sum(1 for a in actions if a.get("type") == "llm_call")
    if llm_calls > MAX_LLM_CALLS_PER_PHASE:
        raise ValidationError(
            f"La fase '{phase_name}' excede máximo de llm_call ({MAX_LLM_CALLS_PER_PHASE})."
        )


def _run_precheck_phase(
    state: AgentState,
    executor: ActionExecutor,
    preferred_site: str | None,
    runtime_env: dict[str, str],
    phase_results: list[dict],
) -> None:
    if not REQUIRE_PRECHECK:
        return

    if not state.project_root:
        raise RuntimeError("FASE 0 requiere project_root Angular detectado.")

    project_root = Path(state.project_root)
    required = ["angular.json", "package.json", "src/main.ts"]
    missing = [rel for rel in required if not (project_root / rel).exists()]
    if missing:
        raise RuntimeError(f"FASE 0 abortada: faltan archivos obligatorios: {', '.join(missing)}")

    state.set_phase("FASE 0 — PRE-CHECK")
    precheck_actions = {
        "actions": [
            {"type": "command", "command": "npm install"},
            {"type": "command", "command": "ng build"},
        ]
    }
    results = executor.execute_actions(precheck_actions, state)
    phase_results.append(
        {
            "phase": "FASE 0 — PRE-CHECK",
            "results": results,
            "cwd": str(state.current_workdir or project_root),
            "project_root": str(project_root),
        }
    )

    has_build_failure = any(
        isinstance(r, dict) and r.get("ok") is False and "returncode" in r
        for r in results[-1:]
    )

    if not has_build_failure:
        state.complete_phase("FASE 0 — PRE-CHECK")
        state.reset_phase()
        return

    fix_context = {
        "user_request": "Autocorregir fallo de pre-check de build",
        "phase": {"name": "Corrección FASE 0", "description": "Corrige build inicial.", "depends_on": []},
        "completed_phases": state.completed_phases,
        "action_history": state.action_history[-20:],
        "failed_actions": _failed_action_summaries(state),
        "errores_compilacion": [results[-1]],
        "task_workspace": str(state.task_workspace) if state.task_workspace else "",
        "current_workdir": str(state.current_workdir) if state.current_workdir else "",
        "project_root": str(state.project_root),
        "angular_cli_version": state.angular_cli_version,
        "angular_project_version": state.angular_project_version,
        "runtime_env": runtime_env,
        "project_structure": _snapshot_project_files(Path(state.task_workspace), Path(state.project_root))["structure"],
        "existing_files": _snapshot_project_files(Path(state.task_workspace), Path(state.project_root))["existing"],
        "missing_files": _snapshot_project_files(Path(state.task_workspace), Path(state.project_root))["missing"],
        "app_tree": _snapshot_project_files(Path(state.task_workspace), Path(state.project_root))["app_tree"],
        "valid_commands": ["ng build", "npm install"],
        "deprecated_commands": ["ng build --prod"],
        "angular_rules": _build_angular_rules(
            _snapshot_project_files(Path(state.task_workspace), Path(state.project_root))["structure"],
            state.angular_project_version,
        ),
        "forbidden_commands": ["ng serve", "npm start", "npm run start"],
    }
    fix_results = _autofix_with_llm(fix_context, executor, state, preferred_site)
    phase_results.append(
        {
            "phase": "quality_autofix_FASE_0",
            "results": fix_results,
            "cwd": str(state.current_workdir or project_root),
            "project_root": str(project_root),
        }
    )

    retry = executor.execute_actions({"actions": [{"type": "command", "command": "ng build"}]}, state)
    phase_results.append(
        {
            "phase": "FASE 0 — PRE-CHECK retry",
            "results": retry,
            "cwd": str(state.current_workdir or project_root),
            "project_root": str(project_root),
        }
    )
    if not retry or retry[-1].get("ok") is not True:
        raise RuntimeError("FASE 0 abortada: ng build sigue fallando tras autocorrección.")

    state.complete_phase("FASE 0 — PRE-CHECK")
    state.reset_phase()

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


def _is_angular_request(user_request: str) -> bool:
    low = (user_request or "").lower()
    tokens = ("angular", "ng ", "ngnew", "landing page", "landing")
    return any(token in low for token in tokens)


def _sanitize_project_name(seed: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", (seed or "hospital-landing").lower()).strip("-")
    if not base:
        base = "hospital-landing"
    if not re.match(r"^[a-z]", base):
        base = f"app-{base}"
    return base[:40].rstrip("-") or "hospital-landing"




def _build_ng_new_command(project_name: str, fast_init: bool = True) -> str:
    parts = [
        "ng",
        "new",
        project_name,
        "--routing",
        "--style=scss",
        "--skip-git",
        "--defaults",
        "--interactive=false",
    ]
    if fast_init:
        parts.append("--skip-install")
    return " ".join(parts)


def _ensure_angular_project_initialized(task_workspace: Path, state: AgentState, user_request: str) -> Path | None:
    existing_root = _find_angular_root(task_workspace)
    if existing_root:
        state.set_project_root(existing_root)
        state.angular_project_version = _angular_project_version(existing_root)
        state.set_current_workdir(existing_root)
        return existing_root

    if not _is_angular_request(user_request):
        return None

    if (state.angular_cli_version or "").strip() in {"unknown", "not_installed", ""}:
        print(f"  {C.YELLOW}⚠️ Angular CLI no disponible; no se puede inicializar proyecto automáticamente.{C.RESET}")
        return None

    project_name = _sanitize_project_name(_slugify_request(user_request))
    # Por defecto hacemos init rápido para no bloquear UX en npm install largo.
    fast_init = os.getenv("SONNY_ANGULAR_FAST_INIT", "1").strip().lower() not in {"0", "false", "no"}
    cmd = _build_ng_new_command(project_name, fast_init=fast_init)
    print(f"  {C.CYAN}▶ Inicializando proyecto Angular base: {project_name}{C.RESET}")
    if fast_init:
        print(f"  {C.DIM}Init rápido activo (--skip-install). Puedes instalar deps luego con npm install.{C.RESET}")
    code, out = _run_cmd_utf8(cmd, cwd=task_workspace, timeout=300 if fast_init else 900)
    if code != 0:
        raise RuntimeError(f"No se pudo inicializar proyecto Angular base con ng new: {out[-1000:]}")

    project_root = (task_workspace / project_name).resolve()
    state.set_project_root(project_root)
    state.angular_project_version = _angular_project_version(project_root)
    state.set_current_workdir(project_root)
    return project_root


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


def _find_angular_root(task_workspace: Path) -> Path | None:
    angular_files = list(task_workspace.rglob("angular.json"))
    if not angular_files:
        return None
    angular_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return angular_files[0].parent.resolve()


def _angular_project_version(project_root: Path | None) -> str:
    if not project_root:
        return "unknown"
    package = project_root / "package.json"
    if not package.exists():
        return "unknown"
    try:
        data = json.loads(package.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return "unknown"
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    return str(deps.get("@angular/core", "unknown"))


def _snapshot_project_files(task_workspace: Path, project_root: Path | None) -> dict:
    target = project_root or task_workspace
    if not target.exists():
        return {"existing": [], "missing": [], "structure": "unknown", "app_tree": []}

    app_dir = target / "src" / "app"
    app_tree: list[str] = []
    if app_dir.exists():
        app_tree = [str(p.relative_to(target)).replace("\\", "/") for p in sorted(app_dir.rglob("*")) if p.is_file()]

    key_files = [
        "src/app/app.ts",
        "src/app/app.html",
        "src/app/app.config.ts",
        "src/app/app.routes.ts",
        "src/styles.scss",
        "src/app/app.component.ts",
        "src/app/app.component.html",
        "src/app/app.module.ts",
        "src/environments/environment.prod.ts",
    ]

    existing, missing = [], []
    for rel in key_files:
        candidate = target / rel
        if candidate.exists():
            existing.append(rel)
        else:
            missing.append(rel)

    structure = "standalone_components (NO NgModules)" if "src/app/app.config.ts" in existing else "ngmodules"
    return {"existing": existing, "missing": missing, "structure": structure, "app_tree": app_tree}


def _failed_action_summaries(state: AgentState) -> list[dict]:
    failed = []
    for entry in state.action_history[-20:]:
        result = entry.get("result", {})
        if result.get("ok") is False:
            failed.append(
                {
                    "phase": entry.get("phase"),
                    "type": entry.get("action", {}).get("type"),
                    "error": result.get("error", "unknown"),
                }
            )
    return failed[-6:]


def _sync_state_before_phase(state: AgentState, task_workspace: Path) -> None:
    if not state.current_workdir or not Path(state.current_workdir).exists():
        state.set_current_workdir(task_workspace)

    project = _find_angular_root(task_workspace)
    if project:
        state.set_project_root(project)
        state.angular_project_version = _angular_project_version(project)


def _project_has_lint_target(project_root: Path | None) -> bool:
    if not project_root:
        return False

    angular_file = Path(project_root) / "angular.json"
    if not angular_file.exists():
        return False

    try:
        content = angular_file.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False

    return '"lint"' in content


def _phase_generates_code(actions_payload: dict) -> bool:
    actions = actions_payload.get("actions", [])
    for action in actions:
        t = action.get("type")
        if t in {"file_write", "file_modify"}:
            return True
        if t == "command":
            cmd = (action.get("command") or "").lower()
            if any(k in cmd for k in ("ng new", "ng g", "ng generate", "npm install", "ng add")):
                return True
    return False



MAX_CHECK_OUTPUT_CHARS = 20000


def _compact_output(text: str, limit: int = MAX_CHECK_OUTPUT_CHARS) -> str:
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    head = raw[: limit // 2]
    tail = raw[-(limit // 2):]
    return f"{head}\n...<output truncated>...\n{tail}"


def _extract_error_signature(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        low = line.lower()
        if any(tok in low for tok in ("error", "ts", "cannot", "failed", "x [error]", "✘")):
            normalized = re.sub(r"\s+", " ", line).strip()
            if normalized:
                lines.append(normalized)
        if len(lines) >= 8:
            break
    if not lines:
        return ""
    return " | ".join(lines)[:1200]


def _quality_signature(failures: list[dict]) -> str:
    if not failures:
        return ""
    chunks = []
    for item in failures:
        cmd = str(item.get("command", "")).strip()
        sig = str(item.get("error_signature", "")).strip()
        chunks.append(f"{cmd}:{sig}")
    return " || ".join(chunks)

def _run_quality_checks(project_root: Path) -> tuple[list[dict], list[dict]]:
    checks = [
        ("ng build --configuration production", "Prueba de Compilación (AOT)", 300),
        ("ng test --no-watch --browsers=ChromeHeadless", "Pruebas Unitarias", 300),
        ("ng e2e", "Pruebas de Extremo a Extremo", 300),
    ]
    if _project_has_lint_target(project_root):
        checks.insert(1, ("ng lint", "Análisis Estático", 120))

    failures: list[dict] = []
    reports: list[dict] = []

    for cmd, check_type, timeout in checks:
        code, out = _run_cmd_utf8(cmd, cwd=project_root, timeout=timeout)
        report = {
            "command": cmd,
            "type": check_type,
            "ok": code == 0,
            "exit_code": code,
            "output": _compact_output(out),
            "error_signature": _extract_error_signature(out),
        }
        reports.append(report)
        if code != 0:
            failures.append(report)

    return failures, reports


def _print_checklist(reports: list[dict], round_num: int) -> None:
    print(f"  {C.CYAN}Checklist calidad (ronda {round_num}):{C.RESET}")
    for item in reports:
        icon = "✅" if item.get("ok") else "❌"
        print(f"    {icon} {item['command']} [{item['type']}]")
        if not item.get("ok") and item.get("error_signature"):
            print(f"      {C.DIM}{item.get('error_signature','')[:240]}{C.RESET}")


def _autofix_with_llm(
    fix_context: dict,
    executor: ActionExecutor,
    state: AgentState,
    preferred_site: str | None,
) -> list[dict]:
    actions_payload = get_phase_actions("Corrección automática de calidad", fix_context, preferred_site=preferred_site)
    validate_actions(actions_payload)
    return executor.execute_actions(actions_payload, state)




def _version_major(version_text: str) -> int | None:
    m = re.search(r"(\d+)", str(version_text or ""))
    return int(m.group(1)) if m else None


def _build_angular_rules(project_structure: str, project_version: str) -> list[str]:
    rules = [
        "Genera acciones válidas para la estructura detectada; no mezcles arquitecturas.",
        "Usa rutas relativas y evita tocar archivos fuera del proyecto.",
    ]

    major = _version_major(project_version)
    if project_structure == "ngmodules":
        rules += [
            "NO uses ni modifiques app.config.ts.",
            "Usa app.module.ts como configuración central.",
        ]
    elif project_structure == "standalone_components (NO NgModules)":
        rules += [
            "Usa componentes standalone (NO NgModules).",
            "app.config.ts debe mantener ApplicationConfig y providers con provideRouter(routes).",
            "NO cambies app.config.ts a objeto custom sin providers Angular.",
            "No crees app.module.ts salvo que exista explícitamente en el árbol real.",
        ]
    else:
        if major is not None and major >= 17:
            rules += [
                "Estructura unknown: asume standalone por Angular >= 17.",
                "Prefiere app.ts/app.html/app.config.ts.",
            ]
        elif major is not None and major < 15:
            rules += [
                "Estructura unknown: asume NgModule por Angular < 15.",
                "Prefiere app.module.ts y evita app.config.ts.",
            ]
        else:
            rules += [
                "Estructura unknown en Angular 15-16: NO asumas arquitectura.",
                "Consulta el árbol real; si falta evidencia, crea con file_write sin reemplazar estructura base.",
            ]

    rules += [
        "Usa ng build --configuration production (NO --prod).",
        "Ejecuta ng lint solo si angular.json define el target lint.",
        "Ejecuta ng e2e solo si angular.json define un target e2e.",
    ]
    return rules

def _validate_action_consistency(actions_payload: dict, context: dict) -> None:
    structure = context.get("project_structure", "unknown")
    existing = set(context.get("existing_files", []) or [])

    for action in actions_payload.get("actions", []):
        if action.get("type") not in {"file_write", "file_modify"}:
            continue
        path = str(action.get("path", "")).replace("\\", "/")
        if structure == "standalone_components (NO NgModules)" and path.endswith("src/app/app.module.ts") and "src/app/app.module.ts" not in existing:
            raise ValidationError(
                "Acción inválida para standalone: no crees app.module.ts si no existe en el árbol real."
            )

        if structure == "standalone_components (NO NgModules)" and path.endswith("src/app/app.ts"):
            content = str(action.get("content", ""))
            if "bootstrapApplication" in content:
                raise ValidationError("Acción inválida para standalone: bootstrapApplication debe permanecer en src/main.ts, no en src/app/app.ts.")

        if structure == "standalone_components (NO NgModules)" and path.endswith("src/app/app.config.ts"):
            content = str(action.get("content", ""))
            low_content = content.lower()
            if "applicationconfig" not in low_content or "providerouter" not in low_content:
                raise ValidationError(
                    "Acción inválida para standalone: app.config.ts debe exportar ApplicationConfig e incluir provideRouter(routes)."
                )

        if structure == "standalone_components (NO NgModules)" and path.endswith("src/app/app.routes.ts"):
            content = str(action.get("content", ""))
            if "export const routes" not in content:
                raise ValidationError(
                    "Acción inválida para standalone: app.routes.ts debe exportar 'routes' para mantener compatibilidad con app.config.ts."
                )


def run_orchestrator(
    user_request: str,
    workspace: Path | None = None,
    preferred_site: str | None = None,
    angular_cli_version_hint: str | None = None,
) -> dict:
    task_workspace = _build_task_workspace(user_request, workspace)
    state = AgentState()
    state.set_task_workspace(task_workspace)
    state.angular_cli_version = angular_cli_version_hint or detect_angular_cli_version()
    runtime_env = detect_node_npm_os()

    executor = ActionExecutor(workspace=task_workspace)
    print(f"  {C.DIM}Workspace de tarea: {task_workspace}{C.RESET}")
    print(f"  {C.DIM}Angular CLI global: {state.angular_cli_version}{C.RESET}")

    _ensure_angular_project_initialized(task_workspace, state, user_request)

    master_plan: dict | None = None
    for _ in range(2):
        master_plan = get_master_plan(user_request, preferred_site=preferred_site)
        try:
            validate_plan(master_plan)
            break
        except ValidationError as exc:
            log_validation_failed("plan", str(exc))
            user_request = f"Corrige estructuralmente el plan previo. Error: {exc}. Solicitud original: {user_request}"
    else:
        raise PlannerError("No se pudo obtener un plan maestro estructuralmente válido.")

    master_plan = _enforce_rigid_pipeline(master_plan)

    phase_results: list[dict] = []
    _run_precheck_phase(state, executor, preferred_site, runtime_env, phase_results)

    ordered_phases = _sort_phases(master_plan)
    print(f"  {C.CYAN}Plan validado: {len(ordered_phases)} fase(s){C.RESET}")

    for phase in ordered_phases:
        phase_name = phase["name"]
        state.set_phase(phase_name)
        _sync_state_before_phase(state, task_workspace)

        snap = _snapshot_project_files(task_workspace, state.project_root)
        context = {
            "user_request": user_request,
            "phase": phase,
            "completed_phases": state.completed_phases,
            "action_history": state.action_history[-10:],
            "failed_actions": _failed_action_summaries(state),
            "errores_compilacion": [],
            "task_workspace": str(state.task_workspace) if state.task_workspace else "",
            "current_workdir": str(state.current_workdir) if state.current_workdir else "",
            "project_root": str(state.project_root) if state.project_root else "",
            "angular_cli_version": state.angular_cli_version,
            "angular_project_version": state.angular_project_version,
            "runtime_env": runtime_env,
            "project_structure": snap["structure"],
            "existing_files": snap["existing"],
            "missing_files": snap["missing"],
            "app_tree": snap["app_tree"],
            "valid_commands": [
                "ng build",
                "ng build --configuration production (NO --prod)",
                "ng test --no-watch --browsers=ChromeHeadless",
                *(["ng lint (requiere angular-eslint)"] if _project_has_lint_target(state.project_root) else []),
                "ng e2e",
            ],
            "deprecated_commands": ["ng build --prod"],
            "angular_rules": _build_angular_rules(snap["structure"], state.angular_project_version),
        }

        try:
            actions_payload = get_phase_actions(phase_name, context, preferred_site=preferred_site)
            validate_actions(actions_payload)
            _validate_phase_action_limits(actions_payload, phase_name)
            _validate_action_consistency(actions_payload, context)
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
            _sync_state_before_phase(state, task_workspace)
            state.increment_iteration()
            LoopGuard.check(state)
            state.complete_phase(phase_name)
            state.reset_phase()
            phase_results.append(
                {
                    "phase": phase_name,
                    "results": results,
                    "cwd": str(state.current_workdir or task_workspace),
                    "project_root": str(state.project_root) if state.project_root else "",
                }
            )
            log_phase_event(phase_name, "completed", f"actions={len(results)}")
        except LoopGuardError as exc:
            log_loop_detected(phase_name, str(exc))
            raise LoopGuardError(f"{exc} | fase={phase_name} | iteraciones={state.iteration_count}")
        except Exception as exc:
            log_error("orchestrator", f"Error en fase '{phase_name}': {exc}")
            raise

        # Validación post-fase con auto-corrección (máx 3 intentos)
        if state.project_root and (_phase_generates_code(actions_payload) or "fase 5" in phase_name.lower()):
            print(f"  {C.CYAN}▶ Verificando calidad tras fase: {phase_name}{C.RESET}")
            max_fix_rounds = 3
            accumulated_quality_failures: list[dict] = []
            seen_quality_signatures: set[str] = set()
            unchanged_signature_streak = 0
            for round_num in range(1, max_fix_rounds + 1):
                failures, reports = _run_quality_checks(Path(state.project_root))
                accumulated_quality_failures.extend(failures)
                _print_checklist(reports, round_num)

                phase_results.append(
                    {
                        "phase": f"quality_checks_{phase_name}_round_{round_num}",
                        "results": reports,
                        "cwd": str(state.project_root),
                        "project_root": str(state.project_root),
                    }
                )

                if not failures:
                    print(f"  {C.GREEN}✅ Fase '{phase_name}' verificada y compilable.{C.RESET}")
                    break

                signature = _quality_signature(failures)
                if signature and signature in seen_quality_signatures:
                    unchanged_signature_streak += 1
                else:
                    unchanged_signature_streak = 0
                if signature:
                    seen_quality_signatures.add(signature)

                if unchanged_signature_streak >= 1:
                    print(f"  {C.RED}❌ Error de calidad sin cambios entre rondas; abortando autofix para evitar loop.{C.RESET}")
                    log_loop_detected(phase_name, f"quality_error_unchanged: {signature[:500]}")
                    break

                if round_num == max_fix_rounds:
                    print(f"  {C.RED}❌ Fase '{phase_name}' no se pudo estabilizar tras {max_fix_rounds} intentos.{C.RESET}")
                    break

                print(f"  {C.YELLOW}⚠️ Corrigiendo errores detectados ({len(failures)}) con LLM...{C.RESET}")
                blocked_quality_commands = list(dict.fromkeys([
                    str(f.get("command", "")).strip()
                    for f in accumulated_quality_failures
                    if f.get("exit_code") != 0 and str(f.get("command", "")).strip()
                ]))
                forbidden_commands = [
                    "ng serve",
                    "npm start",
                    "npm run start",
                    *blocked_quality_commands,
                ]
                filtered_valid_commands = []
                for cmd in list(context.get("valid_commands", []) or []):
                    normalized = str(cmd).strip()
                    is_blocked = any(
                        normalized == blocked
                        or normalized.startswith(f"{blocked} ")
                        or blocked.startswith(f"{normalized} ")
                        for blocked in forbidden_commands
                    )
                    if not is_blocked:
                        filtered_valid_commands.append(cmd)

                fix_context = {
                    **context,
                    "phase": {
                        "name": f"Corrección automática ({phase_name})",
                        "description": "Corrige errores de build/lint/test/e2e sin usar comandos interactivos.",
                        "depends_on": [phase_name],
                    },
                    "failed_checks": failures,
                    "accumulated_quality_failures": accumulated_quality_failures[-20:],
                    "errores_compilacion": failures,
                    "forbidden_commands": forbidden_commands,
                    "valid_commands": filtered_valid_commands,
                    "action_history": state.action_history[-20:],
                    "failed_actions": _failed_action_summaries(state),
                }

                try:
                    fix_results = _autofix_with_llm(fix_context, executor, state, preferred_site)
                    phase_results.append(
                        {
                            "phase": f"quality_autofix_{phase_name}_round_{round_num}",
                            "results": fix_results,
                            "cwd": str(state.current_workdir or state.project_root),
                            "project_root": str(state.project_root) if state.project_root else "",
                        }
                    )
                except Exception as exc:
                    log_error("orchestrator", f"Auto-fix de calidad falló en fase '{phase_name}': {exc}")
                    break

    print(f"  {C.GREEN}✅ Orquestación finalizada.{C.RESET}")
    return {
        "ok": True,
        "plan": master_plan,
        "completed_phases": state.completed_phases,
        "iterations": state.iteration_count,
        "phase_results": phase_results,
        "task_workspace": str(task_workspace),
        "project_root": str(state.project_root) if state.project_root else "",
        "angular_cli_version": state.angular_cli_version,
        "angular_project_version": state.angular_project_version,
        "runtime_env": runtime_env,
    }


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


def run_orchestrator_with_site(
    user_request: str,
    preferred_site: str | None = None,
    angular_cli_version_hint: str | None = None,
):
    selected = preferred_site or _extract_site_from_request(user_request)
    if selected and selected in available_sites():
        set_preferred_site(selected)
    else:
        selected = None
        set_preferred_site(None)
    return run_orchestrator(user_request, preferred_site=selected, angular_cli_version_hint=angular_cli_version_hint)
