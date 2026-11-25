"""Tests for REM AUTONOMOUS MODE, task-scoped authorization, and safety boundaries."""
import contextvars
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from actions.background_tasks import background_tasks
from core import autonomous_mode, confirm, desktop_control
from core.request_context import RequestContext
from core.tasks.engine import TaskEngine
from core.tasks.tools import TaskTools


class AutonomousModeTests(unittest.TestCase):
    def setUp(self):
        self._sess_tmp = tempfile.TemporaryDirectory()
        self._old_sessions_dir = autonomous_mode._SESSIONS_DIR
        autonomous_mode._SESSIONS_DIR = Path(self._sess_tmp.name)
        autonomous_mode.disable("test setup reset")
        desktop_control.resume()

    def tearDown(self):
        autonomous_mode.emergency_stop("test teardown")
        desktop_control.resume()
        autonomous_mode._SESSIONS_DIR = self._old_sessions_dir
        self._sess_tmp.cleanup()

    def test_enable_and_disable_mode(self):
        self.assertEqual(autonomous_mode.get_mode(), autonomous_mode.DISABLED)
        snap = autonomous_mode.enable()
        self.assertTrue(snap["enabled"])
        self.assertEqual(autonomous_mode.get_mode(), autonomous_mode.ENABLED)
        snap2 = autonomous_mode.disable()
        self.assertFalse(snap2["enabled"])
        self.assertEqual(autonomous_mode.get_mode(), autonomous_mode.DISABLED)

    def test_task_scoped_authorization_allows_routine_tools_within_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "ProjectREM"
            project_dir.mkdir()
            autonomous_mode.enable()
            sess = autonomous_mode.start_session(
                "Inspect Project REM, fix avatar expressions, run tests, and write report.",
                source="voice",
                approved_dirs=[project_dir],
                permitted_apps=["chrome", "code", "python"],
                internet_access=True,
            )
            ctx = RequestContext.create(
                sess.objective, sess.source,
                ttl=3600.0, autonomous_session_id=sess.session_id,
            )

            # Routine browser, local screen observation, desktop UI, and open_app pass
            for tool, args in (
                ("browser_control", {"action": "go_to", "url": "https://docs.python.org"}),
                ("browser_control", {"action": "search", "query": "PyQt6 QPainter"}),
                ("screen_process", {"angle": "screen", "text": "Inspect the active window"}),
                ("desktop_ui", {"action": "observe"}),
                ("desktop_ui", {"action": "click", "target": "Run Tests"}),
                ("open_app", {"app_name": "Chrome"}),
            ):
                with self.subTest(tool=tool, args=args):
                    ok, reason = desktop_control.authorize_user_request(
                        sess.objective, tool, args, source="voice", auth_context=ctx,
                    )
                    self.assertTrue(ok, reason)

            # Unapproved app is denied even in autonomous mode
            ok, _ = desktop_control.authorize_user_request(
                sess.objective, "open_app", {"app_name": "Steam"},
                source="voice", auth_context=ctx,
            )
            self.assertFalse(ok)

    def test_local_screenshot_never_grants_cloud_image_sharing(self):
        with tempfile.TemporaryDirectory() as tmp:
            autonomous_mode.enable()
            sess = autonomous_mode.start_session(
                "Observe screen locally", source="typed", approved_dirs=[tmp],
            )
            self.assertIn(autonomous_mode.CAT_LOCAL_SCREEN, sess.allowed_operations)
            self.assertNotIn(autonomous_mode.CAT_CLOUD_IMAGE, sess.allowed_operations)

            # Even if global setting is flipped on, local session without cloud opt-in stays local
            with patch("memory.config_manager.get_cloud_screen_vision_enabled", return_value=True):
                rem = main.RemLive.__new__(main.RemLive)
                rem._request_context_lock = threading.Lock()
                rem._dispatch_context_local = contextvars.ContextVar(
                    f"auto-cloud-test-{id(rem)}", default=None)
                rem._action_registry = SimpleNamespace(
                    has=lambda n: n == "desktop_ui",
                    run=lambda n, a, c: "local UIA observation",
                )
                rem.ui = SimpleNamespace(muted=False, set_state=lambda *_: None)
                ctx = RequestContext.create(
                    sess.objective, "typed", ttl=3600.0, autonomous_session_id=sess.session_id,
                )
                coro = rem._execute_tool(
                    SimpleNamespace(name="screen_process", args={"angle": "screen", "text": "Inspect"}, id="c1"),
                    ctx,
                )
                try:
                    coro.send(None)
                except StopIteration as done:
                    resp = done.value
                    self.assertIn("local UIA observation", str(resp.response))

    def test_nested_tool_calls_with_bound_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            autonomous_mode.enable()
            sess = autonomous_mode.start_session(
                "Check docs in browser", source="typed", approved_dirs=[tmp],
            )
            ctx = RequestContext.create(
                sess.objective, "typed", ttl=3600.0, autonomous_session_id=sess.session_id,
            )
            rem = main.RemLive.__new__(main.RemLive)
            rem._request_context_lock = threading.Lock()
            rem._dispatch_context_local = contextvars.ContextVar(
                f"nested-auto-{id(rem)}", default=None)
            calls = []
            rem._action_registry = SimpleNamespace(
                has=lambda n: n == "browser_control",
                run=lambda n, a, c: calls.append((n, a)) or "status=done action=browser_control verified=true",
            )
            rem._plugin_registry = SimpleNamespace(has=lambda *_: False)
            rem.ui = SimpleNamespace()
            res = rem._run_with_request_context(
                ctx,
                lambda: rem._run_tool_for_plugin("browser_control", {"action": "go_to", "url": "https://example.com"}),
            )
            self.assertIn("status=done", res)
            self.assertEqual(len(calls), 1)

    def test_project_file_access_and_command_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            outside = Path(tmp) / "secret.txt"
            root.mkdir()
            (root / "app.py").write_text("x = 1\n", encoding="utf-8")
            outside.write_text("TOP_SECRET", encoding="utf-8")

            autonomous_mode.enable()
            sess = autonomous_mode.start_session(
                "Edit repo and run tests", source="typed", approved_dirs=[root],
            )
            tools = TaskTools(Path(tmp) / "work", ["notes"], autonomous_session_id=sess.session_id)

            read_ok = tools.execute("read_project_file", {"path": "app.py"})
            self.assertIn("x = 1", read_ok)

            edit_ok = tools.execute("edit_project_file", {"path": "app.py", "target": "x = 1", "replacement": "x = 2"})
            self.assertIn("status=done", edit_ok)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "x = 2\n")
            self.assertIn(str((root / "app.py").resolve()), sess.files_modified)

            # Outside approved_dirs is blocked and recorded as pending approval (not executed)
            outside_res = tools.execute("read_project_file", {"path": str(outside)})
            self.assertIn("APPROVAL_REQUIRED_QUEUED", outside_res)
            self.assertNotIn("TOP_SECRET", outside_res)

            cmd_ok = tools.execute("run_project_command", {"command": "python -c \"print('ok')\"", "is_test": True})
            self.assertIn("returncode=0", cmd_ok)
            self.assertEqual(len(sess.tests_executed), 1)
            self.assertTrue(sess.tests_executed[0]["passed"])

    def test_sensitive_actions_record_pending_and_silence_never_approves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            autonomous_mode.enable()
            sess = autonomous_mode.start_session(
                "Update code, avoid unapproved external sends", source="typed", approved_dirs=[root],
            )
            # Confirm gate in autonomous mode records pending approval without executing
            executed = []
            confirm_res = confirm.request(
                "send-email", "Send status email?", "Body text",
                lambda: executed.append("sent") or "sent",
            )
            self.assertIn("APPROVAL_REQUIRED_QUEUED", confirm_res)
            self.assertEqual(executed, [])
            self.assertEqual(len(sess.pending_approvals), 1)

            # Software install command is blocked & queued; independent work still runs
            tools = TaskTools(Path(tmp) / "work", ["notes"], autonomous_session_id=sess.session_id)
            install_res = tools.execute("run_project_command", {"command": "pip install untrusted-pkg"})
            self.assertIn("APPROVAL_REQUIRED_QUEUED", install_res)
            self.assertGreaterEqual(len(sess.pending_approvals), 2)

            # Independent permitted step still works cleanly
            write_res = tools.execute("write_project_file", {"path": "ok.txt", "content": "continued"})
            self.assertIn("status=done", write_res)

    def test_revocation_and_emergency_stop_immediately_block_tools(self):
        from unittest.mock import patch
        from memory import config_manager

        old_dc = (
            desktop_control._persistent_observation,
            desktop_control._mode,
            desktop_control._paused,
            dict(desktop_control._state),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            tmp_cfg = Path(tmp) / "api_keys.json"
            with patch.object(config_manager, "CONFIG_FILE", tmp_cfg):
                try:
                    autonomous_mode.enable()
                    sess = autonomous_mode.start_session("Work on repo", source="typed", approved_dirs=[root])
                    ctx = RequestContext.create(sess.objective, "typed", ttl=3600.0, autonomous_session_id=sess.session_id)

                    autonomous_mode.revoke_session(sess.session_id, reason="user revoked", disable_mode=True)
                    ok, reason = desktop_control.authorize_user_request(
                        sess.objective, "desktop_ui", {"action": "observe"}, source="typed", auth_context=ctx,
                    )
                    self.assertFalse(ok)
                    self.assertIn("revoked", reason.lower())

                    # Emergency stop also stops autonomous mode and desktop control
                    autonomous_mode.enable()
                    sess2 = autonomous_mode.start_session("Work on repo 2", source="typed", approved_dirs=[root])
                    desktop_control.emergency_stop()
                    self.assertEqual(autonomous_mode.get_mode(), autonomous_mode.DISABLED)
                    self.assertFalse(sess2.is_valid()[0])
                finally:
                    (desktop_control._persistent_observation,
                     desktop_control._mode,
                     desktop_control._paused,
                     prior_state) = old_dc
                    desktop_control._state.update(prior_state)
                    desktop_control._stopped.clear()
                    desktop_control._live_stop.clear()

    def test_checkpoint_recovery_and_completion_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            reports = Path(tmp) / "reports"
            tasks_root = Path(tmp) / "tasks"
            autonomous_mode.enable()
            sess = autonomous_mode.start_session("Plan and complete task", source="typed", approved_dirs=[root])
            autonomous_mode.set_steps(sess, ["Inspect files", "Run tests", "Send email"])
            autonomous_mode.update_step(sess, "step-1", "done", verification="Inspected 3 files")
            autonomous_mode.update_step(sess, "step-3", "blocked_approval", note="Awaiting user approval")
            autonomous_mode.record_pending_approval(
                sess, category="external_communication", title="Send email", detail="Needs user approval",
            )
            autonomous_mode.record_file_modified(sess, root / "core.py")
            autonomous_mode.record_test_run(sess, "unittest", True, "5 passed")

            # Verify persisted checkpoint file exists and retains step status
            saved = sess.save(Path(tmp) / "sessions")
            loaded = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual(loaded["steps"][0]["status"], "done")
            self.assertEqual(loaded["steps"][2]["status"], "blocked_approval")

            report_path = autonomous_mode.save_completion_report(
                sess, final_summary="Completed step 1 and tests; step 3 waiting for approval.",
                reports_dir=reports,
            )
            text = report_path.read_text(encoding="utf-8")
            for heading in (
                "## Original Task",
                "## Actions Performed",
                "## Files Modified",
                "## Tests Executed",
                "## Actual Results",
                "## Failures",
                "## Pending Approvals",
                "## Remaining Work",
                "## Output Locations",
            ):
                self.assertIn(heading, text)

            # TaskEngine start + completion report integration
            round_calls = []

            def fake_ask(system, turns, decls):
                round_calls.append(len(turns))
                return {"text": "Verified and finished.", "calls": []}

            eng = TaskEngine(
                tasks_root, reports, [Path("agents")],
                ask_fn=fake_ask, autostart=False,
                tools_factory=lambda t, p: TaskTools(
                    tasks_root / t["id"], p.tools,
                    autonomous_session_id=str(t.get("autonomous_session_id") or ""),
                ),
            )
            t = eng.start(
                "Autonomous test task",
                extra={"autonomous_session_id": sess.session_id},
            )
            eng._flags[t["id"]] = {"cancel": False, "finish": False, "halt": False}
            eng._run(t["id"])
            finished = eng.get(t["id"])
            self.assertEqual(finished["status"], "done")
            self.assertTrue(Path(finished["report"]).exists())

    def test_background_tasks_refuses_untrusted_source_for_autonomous_mode(self):
        autonomous_mode.enable()
        res = background_tasks(
            {"action": "start", "task": "Inspect files", "autonomous": True},
            session_memory={"source": "telegram"},
        )
        self.assertIn("failed", res.lower())


if __name__ == "__main__":
    unittest.main()
