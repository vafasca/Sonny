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

from core.agent import ExecutorError, execute_command, modify_file, write_file
from core.orchestrator import _build_task_workspace
from core.state_manager import AgentState


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

    def test_task_workspace_isolation_unique_folders(self):
        a = _build_task_workspace("desarrolla una landing", None)
        b = _build_task_workspace("desarrolla una landing", None)
        self.assertNotEqual(str(a), str(b))
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())


if __name__ == "__main__":
    unittest.main()
