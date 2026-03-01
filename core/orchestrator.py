"""Orquestador determinístico de Sonny."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
import platform
import re
import subprocess
from pathlib import Path

from core.agent import ActionExecutor
from core.loop_guard import LoopGuard, LoopGuardError
from core.planner import PlannerError, get_master_plan, get_phase_actions
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


def _run_quality_checks(project_root: Path) -> tuple[list[dict], list[dict]]:
    checks = [
        ("ng build --configuration production", "Prueba de Compilación (AOT)", 300),
        ("ng lint", "Análisis Estático", 120),
        ("ng test --no-watch --browsers=ChromeHeadless", "Pruebas Unitarias", 300),
        ("ng e2e", "Pruebas de Extremo a Extremo", 300),
    ]

    failures: list[dict] = []
    reports: list[dict] = []

    for cmd, check_type, timeout in checks:
        code, out = _run_cmd_utf8(cmd, cwd=project_root, timeout=timeout)
        report = {
            "command": cmd,
            "type": check_type,
            "ok": code == 0,
            "exit_code": code,
            "output": out[-4000:],
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
            "app.config.ts es central y puede modificarse si es necesario.",
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
        "ng lint requiere target lint (si falta, primero instala/configura angular-eslint).",
    ]
    return rules

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

    phase_results: list[dict] = []
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
                "ng build --configuration production (NO --prod)",
                "ng test --no-watch --browsers=ChromeHeadless",
                "ng lint (requiere angular-eslint)",
                "ng e2e",
            ],
            "deprecated_commands": ["ng build --prod"],
            "angular_rules": _build_angular_rules(snap["structure"], state.angular_project_version),
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
        if state.project_root and _phase_generates_code(actions_payload):
            print(f"  {C.CYAN}▶ Verificando calidad tras fase: {phase_name}{C.RESET}")
            max_fix_rounds = 3
            for round_num in range(1, max_fix_rounds + 1):
                failures, reports = _run_quality_checks(Path(state.project_root))
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

                if round_num == max_fix_rounds:
                    print(f"  {C.RED}❌ Fase '{phase_name}' no se pudo estabilizar tras {max_fix_rounds} intentos.{C.RESET}")
                    break

                print(f"  {C.YELLOW}⚠️ Corrigiendo errores detectados ({len(failures)}) con LLM...{C.RESET}")
                fix_context = {
                    **context,
                    "phase": {
                        "name": f"Corrección automática ({phase_name})",
                        "description": "Corrige errores de build/lint/test/e2e sin usar comandos interactivos.",
                        "depends_on": [phase_name],
                    },
                    "failed_checks": failures,
                    "errores_compilacion": failures,
                    "forbidden_commands": ["ng serve", "npm start", "npm run start"],
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
