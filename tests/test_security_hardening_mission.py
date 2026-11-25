import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch


class SecurityHardeningMissionTests(unittest.TestCase):
    def test_negated_desktop_action_is_denied(self):
        from core.desktop_control import authorize_user_request
        allowed, reason = authorize_user_request(
            "Don't click anything", "desktop_ui",
            {"action": "click", "target": "Save"}, source="voice")
        self.assertFalse(allowed)
        self.assertIn("forbids", reason)

    def test_read_only_observation_can_follow_a_negated_click(self):
        from core.desktop_control import authorize_user_request
        allowed, _ = authorize_user_request(
            "Don't click anything, just check my screen", "screen_process",
            {"angle": "screen"}, source="voice")
        self.assertTrue(allowed)

    def test_cloud_image_gate_requires_opt_in_before_preflight(self):
        from core import screen_observation
        with patch("memory.config_manager.get_cloud_screen_vision_enabled", return_value=False), \
             patch("core.screenview.assert_observation_allowed") as preflight:
            with self.assertRaises(PermissionError):
                screen_observation.assert_cloud_image_egress_allowed()
        preflight.assert_not_called()

    def test_exclusion_configuration_error_fails_closed(self):
        from core.screenview import observation_exclusion
        with patch("memory.config_manager.get_desktop_observation_exclusions",
                   side_effect=RuntimeError("invalid config")):
            with self.assertRaises(RuntimeError):
                observation_exclusion("editor.exe", "Project")

    def test_absolute_and_traversal_dev_agent_paths_are_rejected(self):
        from actions.dev_agent import _safe_rel
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for unsafe in ("C:\\outside.txt", "\\\\server\\share\\file", "/etc/passwd",
                           "../outside.txt", "folder/../../outside.txt"):
                with self.subTest(path=unsafe):
                    self.assertIsNone(_safe_rel(root, unsafe))
            self.assertEqual(_safe_rel(root, "src/main.py"), "src/main.py")

    def test_occlusion_mismatch_prevents_physical_click(self):
        from core import screenact
        target = screenact.Point(50, 60, label="Save", origin="uia")
        with patch.object(screenact, "available", return_value=(True, "")), \
             patch.object(screenact, "_resolve", return_value=(target, None)), \
             patch.object(screenact, "_move_to"), \
             patch.object(screenact, "_lands_on_self", return_value=False), \
             patch.object(screenact, "_occluding_process", return_value="other.exe"), \
             patch.object(screenact, "_differs", return_value=True), \
             patch.object(screenact.pyautogui, "click") as click:
            result = screenact.click(target, expect_process="editor.exe")
        self.assertFalse(result.ok)
        self.assertIn("refused", result.error)
        click.assert_not_called()

    def test_corrupt_memory_is_preserved_and_not_overwritten(self):
        from memory import memory_manager
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "long_term.json"
            original = "{broken memory"
            path.write_text(original, encoding="utf-8")
            with patch.object(memory_manager, "MEMORY_PATH", path):
                with self.assertRaises(RuntimeError):
                    memory_manager.update_memory({"notes": {"new": "value"}})
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_memory_saves_atomically_as_valid_json(self):
        from memory import memory_manager
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "long_term.json"
            with patch.object(memory_manager, "MEMORY_PATH", path):
                memory_manager.save_memory({"notes": {"entry": {"value": "safe"}}})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["notes"]["entry"]["value"],
                             "safe")
            self.assertEqual(list(path.parent.glob("long_term.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
