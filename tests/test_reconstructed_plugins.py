import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.action_loader import discover_actions
from core.plugin_loader import discover_plugins


ROOT = Path(__file__).resolve().parents[1]


class ReconstructedPluginTests(unittest.TestCase):
    def test_complete_suite_discovers_without_collisions(self):
        actions = discover_actions(ROOT / "actions", logger=lambda _m: None)
        registry = discover_plugins(ROOT / "plugins", actions.names(), logger=lambda _m: None)
        rows = registry.list_for_ui()
        self.assertEqual(len(rows), 27)
        self.assertTrue(all(row["valid"] for row in rows), rows)
        names = [row["name"] for row in rows]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(set(names).isdisjoint(actions.names()))

    def test_unconfigured_integration_responses_are_safe(self):
        from plugins import gmail_control, home_assistant, obsidian_vault, printer_control
        with patch("plugins.gmail_control.service", side_effect=RuntimeError("Configure OAuth")):
            self.assertIn("Configure OAuth", gmail_control.run({"action": "read", "query": "id"}))
        with patch("plugins.home_assistant.config", return_value={}):
            self.assertIn("Configure", home_assistant.run({"action": "list"}))
        with patch("plugins.obsidian_vault.config", return_value={}):
            self.assertIn("Configure", obsidian_vault.run({"action": "search", "query": "note"}))
        with patch("plugins.printer_control.config", return_value={}):
            self.assertIn("Configure", printer_control.run({"action": "status"}))

    def test_gmail_send_is_gated_before_api_send(self):
        from plugins import gmail_control
        class Messages:
            def send(self, **kwargs): raise AssertionError("must wait for confirmation")
        class Users:
            def messages(self): return Messages()
        class Service:
            def users(self): return Users()
        with patch.object(gmail_control, "service", return_value=Service()), \
             patch("core.confirm.pending_title", return_value=""), \
             patch("core.confirm.request", return_value="confirmation pending") as request:
            result = gmail_control.run({"action": "send", "to": "a@example.com", "subject": "Hello", "body": "Text"})
        self.assertEqual(result, "confirmation pending")
        request.assert_called_once()

    def test_telegram_ignores_unpaired_chat(self):
        from plugins import telegram_remote
        class Player:
            def on_text_command(self, text): raise AssertionError("unpaired message reached REM")
        with patch.object(telegram_remote, "_cfg", return_value={"allowed_chat_ids": "123"}), \
             patch.object(telegram_remote, "_player", Player()):
            telegram_remote._message({"update_id": 10, "message": {"chat": {"id": 999}, "text": "/status"}})

    def test_whatsapp_driver_refuses_wrong_contact(self):
        from plugins._whatsapp_core import WhatsAppDriver
        from types import SimpleNamespace
        from core import screenview
        edit = SimpleNamespace(eid=1, label="Search", role=screenview.ROLE_EDIT,
                               interactable=True, enabled=True, protected=False)
        search_view = SimpleNamespace(degraded=False, app="WhatsApp.exe", title="WhatsApp", elements=[edit])
        results_view = SimpleNamespace(degraded=False, app="WhatsApp.exe", title="WhatsApp", elements=[])
        with patch("core.desktop_control.should_stop", return_value=False), \
             patch("core.screenact.focus_app", return_value=SimpleNamespace(ok=True)), \
             patch("core.screenview.look", side_effect=[search_view, results_view]), \
             patch("core.screenact.type_text", return_value=SimpleNamespace(ok=True)), \
             patch("core.screenact.click") as click, patch("core.screenact.press") as press:
            sent, reason = WhatsAppDriver().send_message_to("Alice", "hello")
        self.assertFalse(sent)
        self.assertIn("exactly matching chat", reason)
        click.assert_not_called()
        press.assert_not_called()

    def test_screen_agent_reports_missing_accessibility_tree(self):
        from actions import screen_agent as implementation
        from core.screenview import View
        view = View(source="none", degraded=True, reason="UI Automation unavailable")
        with patch.object(implementation, "_view", return_value=view):
            result = implementation.screen_agent({"action": "observe"})
        self.assertIn("UI Automation unavailable", result)
        self.assertIn("degraded", result)

    def test_screen_agent_refuses_ambiguous_targets(self):
        from actions import screen_agent as implementation
        from core.screenview import Element, View, ROLE_BUTTON
        view = View(source="uia", degraded=False, elements=[
            Element(1, ROLE_BUTTON, "Save", "", 0, 0, 40, 20),
            Element(2, ROLE_BUTTON, "Save", "", 50, 0, 40, 20),
        ])
        target, reason = implementation._resolve(view, "Save")
        self.assertIsNone(target)
        self.assertIn("multiple possible controls", reason)

    def test_screen_agent_verification_reobserves_after_state_change(self):
        from actions import screen_agent as implementation
        from core.screenview import View
        before = View(source="uia", degraded=False, app="app", title="Window", digest="before")
        after = View(source="uia", degraded=False, app="app", title="Window", digest="after")
        with patch.object(implementation, "_view", return_value=after), patch("actions.screen_agent.time.sleep"):
            observed, result = implementation._verified_after(before)
        self.assertIs(observed, after)
        self.assertIn("verified", result)

    def test_sequence_stops_after_failed_action(self):
        from plugins import screen_agent
        with patch.object(screen_agent, "_one", return_value="click FAILED: target disappeared") as action:
            result = screen_agent.run({"action": "task", "steps": [
                {"action": "click", "target": "Save"}, {"action": "observe"}
            ]})
        self.assertEqual(action.call_count, 1)
        self.assertIn("FAILED", result)

    def test_home_assistant_refuses_security_domains(self):
        from plugins import home_assistant
        class Response:
            def raise_for_status(self): pass
            def json(self): return {"state": "locked"}
        with patch("plugins.home_assistant.config", return_value={"url": "https://ha.invalid", "token": "secret"}), \
             patch("requests.get", return_value=Response()), patch("requests.post") as post:
            result = home_assistant.run({"action": "turn_off", "entity_id": "lock.front_door"})
        self.assertIn("security-sensitive", result)
        post.assert_not_called()

    def test_obsidian_does_not_overwrite_existing_note(self):
        from plugins import obsidian_vault
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "existing.md").write_text("keep", encoding="utf-8")
            with patch("plugins.obsidian_vault.config", return_value={"vault_path": temp}):
                result = obsidian_vault.run({"action": "create", "query": "existing", "content": "replace"})
            self.assertIn("already exists", result)
            self.assertEqual((root / "existing.md").read_text(encoding="utf-8"), "keep")

    def test_obsidian_update_waits_for_confirmation(self):
        from plugins import obsidian_vault
        with tempfile.TemporaryDirectory() as temp:
            note=Path(temp)/"existing.md"; note.write_text("keep",encoding="utf-8")
            with patch("plugins.obsidian_vault.config",return_value={"vault_path":temp}), \
                 patch("core.confirm.pending_title",return_value=""), \
                 patch("core.confirm.request",return_value="confirmation pending") as request:
                result=obsidian_vault.run({"action":"update","query":"existing","content":"replace"})
            self.assertEqual(result,"confirmation pending"); request.assert_called_once()
            self.assertEqual(note.read_text(encoding="utf-8"),"keep")

    def test_excel_never_overwrites_output(self):
        from plugins import excel_writer
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "existing.xlsx"; target.write_bytes(b"keep")
            result = excel_writer.run({"path": str(target), "headers": ["A"]})
            self.assertIn("not overwritten", result)
            self.assertEqual(target.read_bytes(), b"keep")

    def test_excel_edit_writes_a_copy_and_preserves_source(self):
        try: from openpyxl import Workbook, load_workbook
        except ImportError: self.skipTest("openpyxl is optional")
        from plugins import excel_writer
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/"source.xlsx"; output=Path(temp)/"edited.xlsx"
            wb=Workbook(); ws=wb.active; ws.append(["Item"]); ws.append(["Original"]); wb.save(source)
            result=excel_writer.run({"action":"edit","source_path":str(source),"path":str(output),"rows":[["Added"]]})
            self.assertIn("Created and verified",result)
            self.assertTrue(output.is_file())
            check=load_workbook(source,read_only=True); self.assertEqual(check.active.max_row,2); check.close()

    def test_calendar_update_waits_for_confirmation(self):
        from plugins import calendar_control
        class Request:
            def execute(self): return {"id":"event1","summary":"Old title"}
        class Events:
            def get(self,**kwargs): return Request()
            def patch(self,**kwargs): raise AssertionError("must wait for confirmation")
        class Calendar:
            def events(self): return Events()
        with patch.object(calendar_control,"service",return_value=Calendar()), \
             patch("core.confirm.pending_title",return_value=""), \
             patch("core.confirm.request",return_value="confirmation pending") as request:
            result=calendar_control.run({"action":"update","event_id":"event1","summary":"New title"})
        self.assertEqual(result,"confirmation pending"); request.assert_called_once()

    def test_camera_is_not_opened_before_confirmation(self):
        from plugins import calorie_counter, pushup_counter
        with patch("core.confirm.pending_title", return_value=""), \
             patch("core.confirm.request", return_value="pending") as request:
            self.assertEqual(calorie_counter.run({"use_camera": True}), "pending")
            self.assertEqual(pushup_counter.run({"action": "count", "seconds": 5}), "pending")
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
