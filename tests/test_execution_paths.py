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

from core.agent import ExecutorError, execute_command, modify_file, write_file, _block_interactive_commands
from core.orchestrator import _build_task_workspace, _parse_angular_cli_version, _snapshot_project_files, _strip_ansi, _build_angular_rules, _validate_action_consistency, _sanitize_project_name
from core.state_manager import AgentState
from core import planner as planner_mod
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
        self.assertIn("ARCHIVOS QUE EXISTEN", captured["prompt"])
        self.assertIn("COMANDOS VÁLIDOS", captured["prompt"])
        self.assertEqual(payload["actions"][0]["type"], "llm_call")


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
                {"type": "file_write", "path": "src/app/app.config.ts", "content": "x"}
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

    def test_sanitize_project_name_keeps_ng_constraints(self):
        self.assertEqual(_sanitize_project_name("123__Hospital App!!!"), "app-123-hospital-app")

    def test_task_workspace_isolation_unique_folders(self):
        a = _build_task_workspace("desarrolla una landing", None)
        b = _build_task_workspace("desarrolla una landing", None)
        self.assertNotEqual(str(a), str(b))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())


if __name__ == "__main__":
    unittest.main()
