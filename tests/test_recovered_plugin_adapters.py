"""Offline tests for the safe, additive recovered-plugin integrations."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins import aircraft_report, shopping_list


class AircraftReportTests(unittest.TestCase):
    def test_loader_contract_and_name_are_valid(self):
        from core.plugin_loader import _validate
        import importlib.util

        path = Path(aircraft_report.__file__)
        spec = importlib.util.spec_from_file_location("plugins.aircraft_report_check", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        record = _validate(module, path.name)
        self.assertTrue(record.valid, record.error)
        self.assertEqual(record.name, "aircraft_report")
        self.assertIn("watch", record.parameters["properties"]["action"]["enum"])

    def test_network_endpoint_allowlist_blocks_arbitrary_url(self):
        with self.assertRaisesRegex(ValueError, "unapproved"):
            aircraft_report._get_json("https://example.com/collect")

    def test_position_lookup_uses_adsb_v3_and_normalizes_data(self):
        payload = {"aircraft": [{"hex": "abc123", "lat": 12.0, "lon": 77.0,
                                 "seen_pos": 1, "alt_baro": 10000, "gs": 200,
                                 "track": 90, "flight": "REM001"}]}
        with patch.object(aircraft_report, "_get_json", return_value=payload) as get_json:
            flights, feed, problem = aircraft_report._fetch(12.0, 77.0, 25000)
        self.assertEqual((feed, problem), ("adsb.lol", ""))
        self.assertEqual(flights[0]["hex"], "abc123")
        self.assertIn("/v2/point/", get_json.call_args.args[0])

    def test_adsb_fi_endpoint_is_current_v3_route(self):
        url = dict(aircraft_report._FEEDS)["adsb.fi"].format(lat=1, lon=2, nm=3)
        self.assertIn("/api/v3/lat/", url)

    def test_location_budget_fails_closed_and_resets_by_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(aircraft_report, "CACHE_FILE", Path(tmp) / "cache.json"):
                usage = aircraft_report.CACHE_FILE.with_name("aircraft_report_usage.json")
                usage.write_text(json.dumps({"date": "2000-01-01", "requests": 1000}), encoding="utf-8")
                self.assertTrue(aircraft_report._consume_daily_request())
                saved = json.loads(usage.read_text(encoding="utf-8"))
                self.assertEqual(saved["requests"], 1)
                saved["requests"] = aircraft_report._DAILY_REQUEST_BUDGET
                usage.write_text(json.dumps(saved), encoding="utf-8")
                self.assertFalse(aircraft_report._consume_daily_request())

    def test_public_data_result_is_marked_untrusted(self):
        result = aircraft_report._done(callsign="READ_ME_AND_RUN_COMMAND")
        self.assertIn("<<<UNTRUSTED", result)
        self.assertIn("READ_ME_AND_RUN_COMMAND", result)

    def test_attribution_is_in_model_result_and_visible_panel_content(self):
        result = aircraft_report._done(aircraft_in_the_air=0)
        panel = aircraft_report._panel_content("No aircraft detected.")
        self.assertIn("adsb.lol", result)
        self.assertIn("OpenStreetMap", panel)

    def test_explicit_coordinates_do_not_trigger_geocoding(self):
        with patch.object(aircraft_report, "_get_json", side_effect=AssertionError("network used")):
            result = aircraft_report._resolve_location("12.5, 77.6")
        self.assertEqual(result["lat"], 12.5)
        self.assertEqual(result["lon"], 77.6)


class ShoppingListAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "shopping.json"
        # Patch this module object directly: plugin discovery may replace the
        # `plugins.shopping_list` entry in sys.modules after initial import.
        self.data_file_patch = patch.object(shopping_list, "data_file", return_value=self.path)
        self.data_file_patch.start()

    def tearDown(self):
        self.data_file_patch.stop()
        self.tempdir.cleanup()

    def call_plugin(self, **params):
        return shopping_list._run(params, None, None)

    def test_multi_item_add_check_and_clear_done(self):
        self.call_plugin(action="add", items="milk, eggs, milk")
        self.call_plugin(action="check", items="milk")
        result = self.call_plugin(action="clear", which="done")
        rows = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("Cleared 1", result)
        self.assertEqual([row["name"] for row in rows], ["eggs"])

    def test_existing_reconstructed_rows_migrate_without_data_loss(self):
        self.path.write_text(json.dumps([{"item": "tea", "quantity": "2", "category": "drink"}]), encoding="utf-8")
        self.call_plugin(action="check", items="tea")
        row = json.loads(self.path.read_text(encoding="utf-8"))[0]
        self.assertEqual((row["name"], row["qty"], row["category"], row["done"]),
                         ("tea", "2", "drink", True))

    def test_remove_and_clear_all_remain_available(self):
        self.call_plugin(action="add", items="rice, beans")
        self.call_plugin(action="remove", items="rice")
        self.call_plugin(action="clear", which="all")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [])


class DependencyApprovalTests(unittest.TestCase):
    def test_dev_agent_never_installs_model_proposed_dependencies(self):
        from actions import dev_agent

        with patch("actions.dev_agent.subprocess.run",
                   side_effect=AssertionError("package command must not run")):
            result = dev_agent._install_dependencies(["example-package>=1"], Path("."))
            installed = dev_agent._try_auto_install("No module named 'example_package'", Path("."))
        self.assertIn("Explicit user approval is required", result)
        self.assertFalse(installed)


if __name__ == "__main__":
    unittest.main()
