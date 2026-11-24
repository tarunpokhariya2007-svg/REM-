import unittest
from unittest.mock import patch

from plugins import screen_agent


class ScreenAgentPluginTests(unittest.TestCase):
    def test_declaration_and_loader_validation(self):
        from core.plugin_loader import _validate
        from types import SimpleNamespace

        record = _validate(SimpleNamespace(PLUGIN=screen_agent.PLUGIN,
                                           run=screen_agent.run), "screen_agent.py")
        self.assertTrue(record.valid, record.error)
        self.assertEqual(record.name, "screen_agent")

    def test_tool_route_reports_unavailable_without_execution(self):
        class Player:
            def tool_catalog(self):
                return []

            def run_tool(self, name, parameters):
                raise AssertionError("unavailable tool must not run")

        result = screen_agent.run({"action": "tool", "tool_name": "open_app"}, Player())
        self.assertIn("not available", result)

    def test_desktop_dispatch_and_bounded_task(self):
        with patch.object(screen_agent, "_one", return_value="observed") as call:
            self.assertEqual(screen_agent.run({"action": "observe"}), "observed")
            result = screen_agent.run({"action": "task", "steps": [
                {"action": "observe"}, {"action": "find", "target": "search box"}
            ]})
        self.assertIn("Step 2: observed", result)
        self.assertEqual(call.call_count, 3)

    def test_sequence_stops_on_confirmation_and_rejects_invalid_steps(self):
        with patch.object(screen_agent, "_one", return_value="[CONFIRMATION_PENDING]") as call:
            result = screen_agent.run({"action": "task", "steps": [
                {"action": "click", "target": "Submit"}, {"action": "observe"}
            ]})
        self.assertEqual(call.call_count, 1)
        self.assertIn("CONFIRMATION_PENDING", result)
        invalid = screen_agent.run({"action": "task", "steps": [{"action": "shell"}]})
        self.assertIn("stopped safely", invalid)

    def test_task_can_route_existing_tools(self):
        class Player:
            def tool_catalog(self): return [{"name": "browser_control"}]
            def run_tool(self, name, parameters): return "status=done action=browser_control verified=true"
        with patch.object(screen_agent, "_one", return_value="verified observation"):
            result = screen_agent.run({"action": "task", "steps": [
                {"action": "tool", "tool_name": "browser_control", "tool_parameters": {"action": "search", "query": "REM"}},
                {"action": "observe"},
            ]}, Player())
        self.assertIn("Step 2: verified observation", result)


if __name__ == "__main__":
    unittest.main()
