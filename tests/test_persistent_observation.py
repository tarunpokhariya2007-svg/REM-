import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory import config_manager
from core import desktop_control


class PersistentObservationSettingTests(unittest.TestCase):
    def test_local_and_cloud_permissions_are_separate_and_preserve_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "api_keys.json"
            config.write_text(json.dumps({"user_setting": "keep"}), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", config):
                self.assertFalse(config_manager.get_persistent_desktop_observation_enabled())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())
                config_manager.save_persistent_desktop_observation_enabled(True)
                self.assertTrue(config_manager.get_persistent_desktop_observation_enabled())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())
                config_manager.save_cloud_screen_vision_enabled(True)
                self.assertTrue(config_manager.get_cloud_screen_vision_enabled())
                saved = json.loads(config.read_text(encoding="utf-8"))
                self.assertEqual(saved["user_setting"], "keep")
                self.assertIs(saved["persistent_local_observation_enabled"], True)
                self.assertIs(saved["persistent_desktop_observation_enabled"], True)
                self.assertIs(saved["cloud_screen_vision_enabled"], True)

    def test_saved_local_observation_is_not_cloud_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "api_keys.json"
            config.write_text("{}", encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", config):
                config_manager.save_persistent_desktop_observation_enabled(True)
                self.assertTrue(config_manager.get_persistent_desktop_observation_enabled())
                self.assertFalse(config_manager.get_cloud_screen_vision_enabled())

    def test_new_local_observation_key_reads_with_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "api_keys.json"
            config.write_text(json.dumps({
                "persistent_local_observation_enabled": True,
                "persistent_desktop_observation_enabled": False,
            }), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", config):
                self.assertTrue(config_manager.get_persistent_desktop_observation_enabled())

    def test_exclusions_are_normalized_and_saved_without_other_config_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "api_keys.json"
            config.write_text(json.dumps({"api_mode": "local"}), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", config):
                config_manager.save_desktop_observation_exclusions(
                    "Banking App, VS Code, banking app")
                self.assertEqual(config_manager.get_desktop_observation_exclusions(),
                                 ["Banking App", "VS Code"])
                self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["api_mode"],
                                 "local")

    def test_emergency_stop_disables_saved_observation_and_requires_rearming(self):
        old = (desktop_control._persistent_observation, desktop_control._mode,
               desktop_control._paused, dict(desktop_control._state))
        desktop_control._persistent_observation = True
        desktop_control._mode = "LIVE ASSIST"
        desktop_control._paused = False
        desktop_control._stopped.clear()
        try:
            with patch("memory.config_manager.save_persistent_desktop_observation_enabled") as save, \
                 patch("core.screenact.release_all"), \
                 patch.object(desktop_control, "_cancel_inflight"):
                state = desktop_control.emergency_stop()
            self.assertFalse(state["persistent_observation"])
            self.assertFalse(state["screen_access"])
            self.assertEqual(state["mode"], "ON-DEMAND")
            save.assert_called_once_with(False)
        finally:
            desktop_control._persistent_observation, desktop_control._mode, \
                desktop_control._paused, prior_state = old
            desktop_control._state.update(prior_state)
            desktop_control._stopped.clear()
            desktop_control._live_stop.clear()


if __name__ == "__main__":
    unittest.main()
