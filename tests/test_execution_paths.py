import re
import sys
import tempfile
import types
import unittest
from pathlib import Path


ai_scraper_stub = types.ModuleType("core.ai_scraper")
ai_scraper_stub.call_llm = lambda prompt: "{}"
ai_scraper_stub.get_preferred_site = lambda: None
ai_scraper_stub.set_preferred_site = lambda site: None
ai_scraper_stub.available_sites = lambda: ["chatgpt", "claude", "gemini", "qwen"]
sys.modules["core.ai_scraper"] = ai_scraper_stub

from core.agent import ExecutorError, execute_command, modify_file, write_file, _block_interactive_commands, ActionExecutor
from core.orchestrator import _build_task_workspace, _parse_angular_cli_version, _snapshot_project_files, _strip_ansi, _build_angular_rules, _validate_action_consistency, _sanitize_project_name, _build_ng_new_command, _project_has_lint_target
from core.state_manager import AgentState
from core import planner as planner_mod
import core.agent as agent_mod
import core.orchestrator as orch_mod
from core.validator import validate_actions, ValidationError


class TestExecutionPaths(unittest.TestCase):
    def test_end_to_end_project_root_write_location(self):
        """Inicio vacío -> detección de raíz -> escritura en subcarpeta del proyecto."""
        base = Path(tempfile.mkdtemp(prefix="sonny_e2e_"))
        task = base / "task1"
        task.mkdir(parents=True, exist_ok=True)

        project = task / "demo-app"
        (project / "src" / "app").mkdir(parents=True, exist_ok=True)
        (project / "angular.json").write_text("{}", encoding="utf-8")

        state = AgentState()
        state.set_task_workspace(task)
        state.set_project_root(project)

        result = write_file(
            {
                "type": "file_write",
                "path": "src/app/header/header.component.html",
                "content": "<h1>Header</h1>",
            },
            {"workspace": task, "state": state},
        )

        expected = project / "src" / "app" / "header" / "header.component.html"
        self.assertTrue(expected.exists())
        self.assertEqual(result["path"], str(expected))
        self.assertFalse((task / "src" / "app" / "header" / "header.component.html").exists())

    def test_path_traversal_blocked(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_traversal_"))
        task = base / "task2"
        task.mkdir(parents=True, exist_ok=True)

        state = AgentState()
        state.set_task_workspace(task)

        with self.assertRaises(Exception):
            write_file(
                {"type": "file_write", "path": "../../escape.txt", "content": "x"},
                {"workspace": task, "state": state},
            )

    def test_write_file_unescapes_literal_newlines(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_escape_write_"))
        task = base / "task_escape_w"
        task.mkdir(parents=True, exist_ok=True)

        state = AgentState()
        state.set_task_workspace(task)

        write_file(
            {
                "type": "file_write",
                "path": "src/app/app.ts",
                "content": "import { A } from 'a';\nexport class App {}\n",
            },
            {"workspace": task, "state": state},
        )

        content = (task / "src" / "app" / "app.ts").read_text(encoding="utf-8")
        self.assertIn("\nexport class App", content)
        self.assertNotIn("\\n", content)

    def test_modify_file_unescapes_literal_newlines(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_escape_modify_"))
        task = base / "task_escape_m"
        task.mkdir(parents=True, exist_ok=True)

        state = AgentState()
        state.set_task_workspace(task)

        modify_file(
            {
                "type": "file_modify",
                "path": "src/app/app.html",
                "content": "<main>\n  <h1>Título</h1>\n</main>\n",
            },
            {"workspace": task, "state": state},
        )

        content = (task / "src" / "app" / "app.html").read_text(encoding="utf-8")
        self.assertIn("\n  <h1>", content)
        self.assertNotIn("\\n", content)

    def test_modify_fallback_to_write(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_modify_"))
        task = base / "task3"
        task.mkdir(parents=True, exist_ok=True)
        state = AgentState()
        state.set_task_workspace(task)

        result = modify_file(
            {"type": "file_modify", "path": "src/app/new.component.ts", "content": "export const x=1;"},
            {"workspace": task, "state": state},
        )
        self.assertTrue((task / "src" / "app" / "new.component.ts").exists())
        self.assertIn("warning", result)


    def test_block_interactive_commands(self):
        with self.assertRaises(ExecutorError):
            _block_interactive_commands("ng serve --open")

    def test_ng_lint_requires_target(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_lint_"))
        task = base / "task4"
        project = task / "proj"
        project.mkdir(parents=True, exist_ok=True)
        (project / "angular.json").write_text('{"projects":{"a":{"architect":{}}}}', encoding="utf-8")

        state = AgentState()
        state.set_task_workspace(task)
        state.set_project_root(project)

        with self.assertRaises(ExecutorError):
            execute_command({"type": "command", "command": "ng lint"}, {"workspace": task, "state": state})


    def test_parse_angular_cli_version_variants(self):
        sample = """
Angular CLI       : 21.1.5
Node.js           : 20.19.0
Package Manager   : npm 11.10.1
"""
        self.assertEqual(_parse_angular_cli_version(sample), "21.1.5")
        self.assertEqual(_parse_angular_cli_version("Angular CLI 19.2.3"), "19.2.3")
        self.assertEqual(_parse_angular_cli_version("random output"), "unknown")


    def test_snapshot_scans_real_src_app_tree(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_snapshot_"))
        project = base / "proj"
        (project / "src" / "app" / "nested").mkdir(parents=True, exist_ok=True)
        (project / "src" / "app" / "app.ts").write_text("export const x=1", encoding="utf-8")
        (project / "src" / "app" / "nested" / "cmp.html").write_text("<p>x</p>", encoding="utf-8")
        snap = _snapshot_project_files(base, project)
        self.assertIn("src/app/app.ts", snap["existing"])
        self.assertIn("src/app/nested/cmp.html", snap["app_tree"])

    def test_planner_injects_project_context_block(self):
        captured = {"prompt": ""}

        def fake_call(prompt: str) -> str:
            captured["prompt"] = prompt
            return '{"actions": [{"type": "llm_call", "prompt": "ok"}]}'

        planner_mod.call_llm = fake_call
        payload = planner_mod.get_phase_actions(
            "fase-demo",
            {
                "task_workspace": "/tmp/task",
                "project_root": "/tmp/task/proj",
                "current_workdir": "/tmp/task/proj",
                "angular_cli_version": "21.1.5",
                "angular_project_version": "21.1.5",
                "runtime_env": {"node": "v20", "npm": "10", "os": "Windows"},
                "project_structure": "standalone_components (NO NgModules)",
                "existing_files": ["src/app/app.ts"],
                "missing_files": ["src/app/app.module.ts"],
                "app_tree": ["src/app/app.ts"],
                "valid_commands": ["ng build --configuration production (NO --prod)"],
                "deprecated_commands": ["ng build --prod"],
                "angular_rules": ["Usa app.ts y app.html"],
            },
        )
        self.assertIn("CONTEXTO DEL PROYECTO ANGULAR", captured["prompt"])
        self.assertEqual(captured["prompt"].count("CONTEXTO DEL PROYECTO ANGULAR"), 1)
        self.assertIn("ARCHIVOS QUE EXISTEN", captured["prompt"])
        self.assertIn("COMANDOS VÁLIDOS", captured["prompt"])
        self.assertNotIn("Contexto JSON:", captured["prompt"])
        self.assertIn('REGLAS CRÍTICAS PARA EL CAMPO "content"', captured["prompt"])
        self.assertIn('PROHIBIDO en "content" para .scss, .ts y .html', captured["prompt"])
        self.assertEqual(payload["actions"][0]["type"], "llm_call")


    def test_planner_prompt_dedupes_repeated_project_lists(self):
        captured = {"prompt": ""}

        def fake_call(prompt: str) -> str:
            captured["prompt"] = prompt
            return '{"actions": [{"type": "llm_call", "prompt": "ok"}]}'

        planner_mod.call_llm = fake_call
        planner_mod.get_phase_actions(
            "fase-dup",
            {
                "existing_files": ["src/app/app.ts", "src/app/app.ts"],
                "missing_files": ["src/app/a.ts", "src/app/a.ts"],
                "app_tree": ["src/app/app.ts", "src/app/app.ts"],
                "valid_commands": ["ng build --configuration production", "ng build --configuration production"],
                "deprecated_commands": ["ng build --prod", "ng build --prod"],
                "angular_rules": ["Usa standalone", "Usa standalone"],
            },
        )

        self.assertEqual(captured["prompt"].count("• src/app/app.ts"), 2)  # existing + app_tree
        self.assertEqual(captured["prompt"].count("• ng build --configuration production"), 1)

    def test_planner_prompt_includes_forbidden_commands_from_context(self):
        captured = {"prompt": ""}

        def fake_call(prompt: str) -> str:
            captured["prompt"] = prompt
            return '{"actions": [{"type": "llm_call", "prompt": "ok"}]}'

        planner_mod.call_llm = fake_call
        planner_mod.get_phase_actions(
            "fase-autofix",
            {
                "forbidden_commands": ["ng lint", "ng serve"],
            },
        )

        self.assertIn("COMANDOS PROHIBIDOS (NO los uses bajo ninguna circunstancia)", captured["prompt"])
        self.assertIn("• ng lint", captured["prompt"])
        self.assertIn("• ng serve", captured["prompt"])

    def test_planner_prompt_includes_accumulated_quality_failures(self):
        captured = {"prompt": ""}

        def fake_call(prompt: str) -> str:
            captured["prompt"] = prompt
            return '{"actions": [{"type": "llm_call", "prompt": "ok"}]}'

        planner_mod.call_llm = fake_call
        planner_mod.get_phase_actions(
            "fase-autofix",
            {
                "accumulated_quality_failures": [
                    {
                        "command": "ng build --configuration production",
                        "exit_code": 1,
                        "output": "ERROR in src/app/app.ts: Type 'x' is not assignable",
                    },
                    {
                        "command": "ng test --no-watch --browsers=ChromeHeadless",
                        "exit_code": 1,
                        "output": "FAILED: AppComponent should render title",
                    },
                ],
            },
        )

        self.assertIn("FALLOS ACUMULADOS EN RONDAS ANTERIORES DE CALIDAD", captured["prompt"])
        self.assertIn("• ng build --configuration production → exit 1", captured["prompt"])
        self.assertIn("• ng test --no-watch --browsers=ChromeHeadless → exit 1", captured["prompt"])

    def test_strip_ansi_from_cli_output(self):
        raw = "\x1b[36mAngular CLI\x1b[39m : \x1b[33m21.1.5\x1b[39m"
        cleaned = _strip_ansi(raw)
        self.assertIn("Angular CLI", cleaned)
        self.assertIn("21.1.5", cleaned)
        self.assertNotIn("\x1b", cleaned)

    def test_dynamic_angular_rules_by_structure(self):
        standalone = _build_angular_rules("standalone_components (NO NgModules)", "21.1.5")
        ngmodules = _build_angular_rules("ngmodules", "14.2.0")
        self.assertTrue(any("NO NgModules" in r or "standalone" in r.lower() for r in standalone))
        self.assertTrue(any("app.module.ts" in r for r in ngmodules))

    def test_validator_accepts_phase_short_dependencies(self):
        from core.validator import validate_plan

        plan = {
            "phases": [
                {"name": "Fase 1: Planificación y Diseño", "description": "a", "depends_on": []},
                {"name": "Fase 2: Configuración", "description": "b", "depends_on": ["Fase 1"]},
                {"name": "Fase 3: Desarrollo", "description": "c", "depends_on": ["Fase 2"]},
            ]
        }

        validate_plan(plan)

    def test_validator_allows_nested_app_config(self):
        payload = {
            "actions": [
                {"type": "file_write", "path": "src/app/app.config.ts", "content": "export const appConfig = {};"}
            ]
        }
        validate_actions(payload)

    def test_action_consistency_blocks_app_module_in_standalone(self):
        context = {
            "project_structure": "standalone_components (NO NgModules)",
            "existing_files": ["src/app/app.ts", "src/app/app.config.ts"],
        }
        payload = {
            "actions": [
                {"type": "file_write", "path": "src/app/app.module.ts", "content": "x"}
            ]
        }
        with self.assertRaises(ValidationError):
            _validate_action_consistency(payload, context)

    def test_build_ng_new_command_fast_init_uses_skip_install(self):
        cmd = _build_ng_new_command("hospital-landing", fast_init=True)
        self.assertIn("--skip-install", cmd)

    def test_build_ng_new_command_full_init_without_skip_install(self):
        cmd = _build_ng_new_command("hospital-landing", fast_init=False)
        self.assertNotIn("--skip-install", cmd)

    def test_sanitize_project_name_keeps_ng_constraints(self):
        self.assertEqual(_sanitize_project_name("123__Hospital App!!!"), "app-123-hospital-app")

    def test_validator_allows_typescript_with_header_comment_and_real_code(self):
        payload = {
            "actions": [
                {
                    "type": "file_write",
                    "path": "src/app/app.component.ts",
                    "content": """// Componente principal
import { Component } from '@angular/core';

@Component({selector: 'app-root', standalone: true, template: '<div></div>'})
export class AppComponent {}""",
                }
            ]
        }
        validate_actions(payload)

    def test_validator_blocks_placeholder_typescript_content(self):
        payload = {
            "actions": [
                {
                    "type": "file_write",
                    "path": "src/app/contact.component.ts",
                    "content": "// Este archivo se encargará del formulario de contacto",
                }
            ]
        }
        with self.assertRaises(ValidationError):
            validate_actions(payload)

    def test_validator_blocks_placeholder_html_content(self):
        payload = {
            "actions": [
                {
                    "type": "file_write",
                    "path": "src/app/app.component.html",
                    "content": "<!-- Este archivo contendrá el HTML básico -->",
                }
            ]
        }
        with self.assertRaises(ValidationError):
            validate_actions(payload)

    def test_llm_call_executes_nested_actions(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_nested_llm_"))
        task = base / "task_nested"
        task.mkdir(parents=True, exist_ok=True)

        state = AgentState()
        state.set_task_workspace(task)

        original = agent_mod.call_llm
        agent_mod.call_llm = lambda prompt: '{"actions":[{"type":"file_write","path":"src/app/nested.service.ts","content":"export const nested = true;"}]}'
        try:
            executor = ActionExecutor(workspace=task)
            results = executor.execute_actions({"actions": [{"type": "llm_call", "prompt": "hazlo"}]}, state)
        finally:
            agent_mod.call_llm = original

        self.assertTrue((task / "src" / "app" / "nested.service.ts").exists())
        self.assertEqual(results[0].get("nested_actions_executed"), 1)

    def test_planner_call_enriches_prompt_with_real_project_files(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_planner_ctx_"))
        task = base / "task_ctx"
        project = task / "proj"
        (project / "src" / "app" / "lead-form").mkdir(parents=True, exist_ok=True)
        (project / "src" / "app" / "lead-form" / "lead-form.component.html").write_text(
            '<input [(ngModel)]="form.nombre" />',
            encoding="utf-8",
        )

        state = AgentState()
        state.set_task_workspace(task)
        state.set_project_root(project)

        captured = {"prompt": ""}
        original = agent_mod.call_llm
        agent_mod.call_llm = lambda prompt: captured.__setitem__("prompt", prompt) or "ok"
        try:
            result = agent_mod.planner_call(
                {
                    "type": "llm_call",
                    "prompt": "Analiza formularios html y accesibilidad aria para reemplazar",
                },
                {"workspace": task, "state": state},
            )
        finally:
            agent_mod.call_llm = original

        self.assertTrue(result["ok"])
        self.assertTrue(result.get("prompt_context_files"))
        self.assertIn("CONTENIDO REAL DEL PROYECTO EN DISCO", captured["prompt"])
        self.assertIn("[ARCHIVO REAL EN DISCO: src/app/lead-form/lead-form.component.html]", captured["prompt"])
        self.assertIn("[(ngModel)]", captured["prompt"])

    def test_planner_call_limits_total_context_size_before_calling_llm(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_planner_ctx_limit_"))
        task = base / "task_ctx_limit"
        project = task / "proj"
        (project / "src" / "app").mkdir(parents=True, exist_ok=True)

        big_a = "A" * (agent_mod.MAX_CONTEXT_TOTAL_BYTES // 2 + 300)
        big_b = "B" * (agent_mod.MAX_CONTEXT_TOTAL_BYTES // 2 + 300)
        big_c = "C" * (agent_mod.MAX_CONTEXT_TOTAL_BYTES // 2 + 300)
        (project / "src" / "app" / "one.component.html").write_text(big_a, encoding="utf-8")
        (project / "src" / "app" / "two.component.html").write_text(big_b, encoding="utf-8")
        (project / "src" / "app" / "three.component.html").write_text(big_c, encoding="utf-8")

        state = AgentState()
        state.set_task_workspace(task)
        state.set_project_root(project)

        captured = {"prompt": ""}
        original = agent_mod.call_llm
        agent_mod.call_llm = lambda prompt: captured.__setitem__("prompt", prompt) or "ok"
        try:
            agent_mod.planner_call(
                {
                    "type": "llm_call",
                    "prompt": "Analiza html y accesibilidad del formulario",
                },
                {"workspace": task, "state": state},
            )
        finally:
            agent_mod.call_llm = original

        injected = captured["prompt"].split("CONTENIDO REAL DEL PROYECTO EN DISCO", 1)[-1]
        extracted_payload = re.findall(r"```\n([\s\S]*?)\n```", injected)
        total_injected_chars = sum(len(chunk) for chunk in extracted_payload)
        self.assertLessEqual(total_injected_chars, agent_mod.MAX_CONTEXT_TOTAL_BYTES)

    def test_planner_call_fallbacks_to_tree_when_no_keyword_match(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_planner_tree_"))
        task = base / "task_tree"
        project = task / "proj"
        (project / "src" / "app").mkdir(parents=True, exist_ok=True)
        (project / "src" / "app" / "app.component.ts").write_text("export class AppComponent {}", encoding="utf-8")

        state = AgentState()
        state.set_task_workspace(task)
        state.set_project_root(project)

        captured = {"prompt": ""}
        original = agent_mod.call_llm
        agent_mod.call_llm = lambda prompt: captured.__setitem__("prompt", prompt) or "ok"
        try:
            agent_mod.planner_call(
                {
                    "type": "llm_call",
                    "prompt": "Resume el estado actual del proyecto",
                },
                {"workspace": task, "state": state},
            )
        finally:
            agent_mod.call_llm = original

        self.assertIn("ÁRBOL REAL DEL PROYECTO", captured["prompt"])
        self.assertIn("src/app/app.component.ts", captured["prompt"])



    def test_run_quality_checks_skips_lint_without_target(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_quality_no_lint_"))
        project = base / "proj"
        project.mkdir(parents=True, exist_ok=True)
        (project / "angular.json").write_text('{"projects":{"app":{"architect":{}}}}', encoding="utf-8")

        called_commands: list[str] = []
        original = orch_mod._run_cmd_utf8
        orch_mod._run_cmd_utf8 = lambda cmd, cwd=None, timeout=30: (called_commands.append(cmd) or (0, "ok"))
        try:
            failures, reports = orch_mod._run_quality_checks(project)
        finally:
            orch_mod._run_cmd_utf8 = original

        self.assertEqual(failures, [])
        self.assertEqual(len(reports), 3)
        self.assertNotIn("ng lint", called_commands)

    def test_run_quality_checks_includes_lint_with_target(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_quality_with_lint_"))
        project = base / "proj"
        project.mkdir(parents=True, exist_ok=True)
        (project / "angular.json").write_text('{"projects":{"app":{"architect":{"lint":{}}}}}', encoding="utf-8")

        called_commands: list[str] = []
        original = orch_mod._run_cmd_utf8
        orch_mod._run_cmd_utf8 = lambda cmd, cwd=None, timeout=30: (called_commands.append(cmd) or (0, "ok"))
        try:
            failures, reports = orch_mod._run_quality_checks(project)
        finally:
            orch_mod._run_cmd_utf8 = original

        self.assertEqual(failures, [])
        self.assertEqual(len(reports), 4)
        self.assertIn("ng lint", called_commands)

    def test_autofix_context_accumulates_quality_failures(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_autofix_ctx_"))
        task = base / "task"
        project = task / "proj"
        (project / "src" / "app").mkdir(parents=True, exist_ok=True)
        (project / "angular.json").write_text('{"projects":{"app":{"architect":{}}}}', encoding="utf-8")

        captured_contexts: list[dict] = []
        round_counter = {"n": 0}

        original_build_task = orch_mod._build_task_workspace
        original_detect_cli = orch_mod.detect_angular_cli_version
        original_detect_env = orch_mod.detect_node_npm_os
        original_ensure = orch_mod._ensure_angular_project_initialized
        original_get_master = orch_mod.get_master_plan
        original_get_actions = orch_mod.get_phase_actions
        original_quality = orch_mod._run_quality_checks
        original_autofix = orch_mod._autofix_with_llm

        orch_mod._build_task_workspace = lambda user_request, workspace=None: task
        orch_mod.detect_angular_cli_version = lambda: "21.1.5"
        orch_mod.detect_node_npm_os = lambda: {"node": "v20", "npm": "10", "os": "Linux"}

        def fake_ensure(task_workspace, state, user_request):
            state.set_project_root(project)
            state.angular_project_version = "21.1.5"

        orch_mod._ensure_angular_project_initialized = fake_ensure
        orch_mod.get_master_plan = lambda user_request, preferred_site=None: {
            "phases": [{"name": "Desarrollo de componentes", "description": "x", "depends_on": []}]
        }
        orch_mod.get_phase_actions = lambda phase_name, context, preferred_site=None: {
            "actions": [{"type": "file_write", "path": "src/app/app.component.ts", "content": "export class AppComponent {}"}]
        }

        def fake_quality(project_root):
            round_counter["n"] += 1
            if round_counter["n"] == 1:
                f = [{"command": "ng build --configuration production", "exit_code": 1, "ok": False, "type": "build", "output": "e1"}]
                return f, f
            if round_counter["n"] == 2:
                f = [{"command": "ng test --no-watch --browsers=ChromeHeadless", "exit_code": 1, "ok": False, "type": "test", "output": "e2"}]
                return f, f
            return [], [{"command": "ok", "exit_code": 0, "ok": True, "type": "x", "output": ""}]

        orch_mod._run_quality_checks = fake_quality

        def fake_autofix(fix_context, executor, state, preferred_site):
            captured_contexts.append(fix_context)
            return []

        orch_mod._autofix_with_llm = fake_autofix

        try:
            result = orch_mod.run_orchestrator("demo request")
        finally:
            orch_mod._build_task_workspace = original_build_task
            orch_mod.detect_angular_cli_version = original_detect_cli
            orch_mod.detect_node_npm_os = original_detect_env
            orch_mod._ensure_angular_project_initialized = original_ensure
            orch_mod.get_master_plan = original_get_master
            orch_mod.get_phase_actions = original_get_actions
            orch_mod._run_quality_checks = original_quality
            orch_mod._autofix_with_llm = original_autofix

        self.assertTrue(result["ok"])
        self.assertEqual(len(captured_contexts), 2)
        self.assertEqual(len(captured_contexts[0]["accumulated_quality_failures"]), 1)
        self.assertEqual(len(captured_contexts[1]["accumulated_quality_failures"]), 2)
        self.assertIn("ng build --configuration production", captured_contexts[1]["forbidden_commands"])
        self.assertIn("ng test --no-watch --browsers=ChromeHeadless", captured_contexts[1]["forbidden_commands"])
        self.assertTrue(all(
            "ng build --configuration production" not in cmd
            for cmd in captured_contexts[1]["valid_commands"]
        ))
        self.assertTrue(all(
            "ng test --no-watch --browsers=ChromeHeadless" not in cmd
            for cmd in captured_contexts[1]["valid_commands"]
        ))

    def test_nested_actions_do_not_increment_phase_action_count(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_nested_count_"))
        task = base / "task_nested_count"
        task.mkdir(parents=True, exist_ok=True)

        state = AgentState()
        state.set_task_workspace(task)
        state.set_phase("Desarrollo de componentes")

        nested_payload = {
            "actions": [
                {"type": "file_write", "path": "src/app/a.ts", "content": "export const a = 1;"},
                {"type": "file_write", "path": "src/app/b.ts", "content": "export const b = 1;"},
                {"type": "file_write", "path": "src/app/c.ts", "content": "export const c = 1;"},
            ]
        }

        original = agent_mod.call_llm
        agent_mod.call_llm = lambda prompt: __import__("json").dumps(nested_payload)
        try:
            executor = ActionExecutor(workspace=task)
            executor.execute_actions({"actions": [{"type": "llm_call", "prompt": "genera tests"}]}, state)
        finally:
            agent_mod.call_llm = original

        self.assertEqual(state.phase_action_count, 1)
        self.assertEqual(len(state.action_history), 4)

    def test_execute_actions_checks_loop_guard_per_action(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_action_limit_"))
        task = base / "task_action_limit"
        task.mkdir(parents=True, exist_ok=True)

        state = AgentState()
        state.set_task_workspace(task)
        state.set_phase("Fase extensa")

        actions = [
            {"type": "file_write", "path": f"src/app/file_{idx}.ts", "content": "export const x = 1;"}
            for idx in range(11)
        ]

        executor = ActionExecutor(workspace=task)
        with self.assertRaises(ExecutorError):
            executor.execute_actions({"actions": actions}, state)

        self.assertEqual(state.phase_action_count, 11)

    def test_project_has_lint_target_detection(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_lint_target_"))
        project = base / "proj"
        project.mkdir(parents=True, exist_ok=True)

        (project / "angular.json").write_text('{"projects":{"app":{"architect":{"lint":{}}}}}', encoding="utf-8")
        self.assertTrue(_project_has_lint_target(project))

        (project / "angular.json").write_text('{"projects":{"app":{"architect":{}}}}', encoding="utf-8")
        self.assertFalse(_project_has_lint_target(project))

    def test_llm_call_nested_depth_limit(self):
        base = Path(tempfile.mkdtemp(prefix="sonny_nested_depth_"))
        task = base / "task_nested_depth"
        task.mkdir(parents=True, exist_ok=True)

        state = AgentState()
        state.set_task_workspace(task)

        original = agent_mod.call_llm
        agent_mod.call_llm = lambda prompt: '{"actions":[{"type":"llm_call","prompt":"again"}]}'
        try:
            executor = ActionExecutor(workspace=task)
            results = executor.execute_actions({"actions": [{"type": "llm_call", "prompt": "start"}]}, state)
        finally:
            agent_mod.call_llm = original

        self.assertFalse(results[0]["ok"])
        self.assertIn("anidadas", results[0]["error"])

    def test_task_workspace_isolation_unique_folders(self):
        a = _build_task_workspace("desarrolla una landing", None)
        b = _build_task_workspace("desarrolla una landing", None)
        self.assertNotEqual(str(a), str(b))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())


if __name__ == "__main__":
    unittest.main()
