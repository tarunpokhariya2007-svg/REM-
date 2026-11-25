import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from actions import dev_agent


class GeneratedProjectExecutionTests(unittest.TestCase):
    def test_only_project_contained_python_scripts_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("print('ok')", encoding="utf-8")
            (root.parent / "outside.py").write_text("print('no')", encoding="utf-8")
            self.assertEqual(dev_agent._project_python_command("python main.py", root)[1],
                             str((root / "main.py").resolve()))
            for command in (
                "python -c print(1)", "powershell.exe -Command whoami",
                "python ../outside.py", "python missing.py", "python main.py; whoami",
            ):
                with self.subTest(command=command), self.assertRaises(ValueError):
                    dev_agent._project_python_command(command, root)

    def test_generated_project_is_not_run_until_confirmation_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "projects"
            calls = {}

            def write_file(file_info, project_dir, **_kwargs):
                target = project_dir / file_info["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("print('safe test')", encoding="utf-8")
                return target.read_text(encoding="utf-8")

            def request(**kwargs):
                calls.update(kwargs)
                return "[CONFIRMATION_PENDING]"

            with (
                patch.object(dev_agent, "PROJECTS_DIR", root),
                patch.object(dev_agent, "_plan_project", return_value={
                    "project_name": "generated", "files": [{"path": "main.py"}],
                    "entry_point": "main.py", "run_command": "python main.py",
                    "dependencies": [],
                }),
                patch.object(dev_agent, "_write_file", side_effect=write_file),
                patch.object(dev_agent, "_open_vscode", return_value=False),
                patch("core.confirm.request", side_effect=request),
                patch.object(dev_agent, "_run_project") as run_project,
            ):
                result = dev_agent._build_project("safe test", "python", "generated", 10)
                run_project.assert_not_called()
                self.assertIn("CONFIRMATION_PENDING", result)
                self.assertIn("python main.py", calls["detail"])
                self.assertIn(str(root / "generated"), calls["detail"])
                self.assertIn("generated Python code", calls["detail"])
                calls["run"]()
                run_project.assert_called_once()

    def test_run_captures_output_and_returns_exit_code_without_echoing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("print('private marker')", encoding="utf-8")
            result = dev_agent._run_project("python main.py", root, timeout=5)
            self.assertIn("exited with code 0", result)
            self.assertIn("captured locally and withheld", result)
            self.assertNotIn("private marker", result)

    def test_timeout_is_bounded_and_process_is_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.py").write_text("import time; time.sleep(5)", encoding="utf-8")
            result = dev_agent._run_project("python main.py", root, timeout=1)
            self.assertIn("timed out after 1s", result)
            self.assertIn("process was stopped", result)


if __name__ == "__main__":
    unittest.main()
