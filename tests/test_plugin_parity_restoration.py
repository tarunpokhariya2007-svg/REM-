import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class RecoveredFeatureParityTests(unittest.TestCase):
    def test_water_goal_and_today_total_are_additive_and_preserve_entries(self):
        from plugins import water_reminder
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "water.json"
            today = datetime.now(timezone.utc)
            yesterday = today - timedelta(days=1)
            original_entries = [
                {"amount_ml": 250, "at": today.isoformat()},
                {"amount_ml": 300, "at": yesterday.isoformat()},
            ]
            path.write_text(json.dumps({"enabled": False, "entries": original_entries}),
                            encoding="utf-8")
            with patch.object(water_reminder, "data_file", return_value=path):
                result = water_reminder.run({"action": "goal", "goal_ml": 2600})
                self.assertIn("2600 ml", result)
                result = water_reminder.run({"action": "status"})
                self.assertIn("250 ml", result)
                water_reminder.run({"action": "record", "amount_ml": 400})
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["daily_goal_ml"], 2600)
            self.assertEqual(len(saved["entries"]), 3)
            self.assertEqual(saved["entries"][0], original_entries[0])

    def test_quiz_accepts_supplied_questions_and_returns_grader(self):
        from plugins import quiz
        class Player:
            value = None
            def show_quiz(self, topic, questions, grader):
                self.value = (topic, questions, grader)
        player = Player()
        result = quiz.run({"action": "start", "topic": "History", "questions": [
            {"type": "multiple_choice", "question": "Capital?", "options": ["A", "B"], "answer": "B"},
            {"type": "open", "question": "Explain?", "answer": "because"},
        ]}, player, None)
        self.assertIn("Started", result)
        topic, questions, grader = player.value
        self.assertEqual(topic, "History")
        self.assertTrue(callable(grader))
        self.assertTrue(grader(questions[0], "B"))
        self.assertFalse(grader(questions[0], "A"))
        self.assertIsNone(grader(questions[1], "some answer"))

    def test_pomodoro_stats_reports_only_completed_work_blocks_for_today(self):
        from plugins import pomodoro
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pomodoro.json"
            today = datetime.now(timezone.utc).date().isoformat()
            yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
            path.write_text(json.dumps({"completed": [
                {"phase": "work", "minutes": 25, "task": "REM", "at": today + "T10:00:00+00:00"},
                {"phase": "break", "minutes": 5, "task": "", "at": today + "T10:25:00+00:00"},
                {"phase": "work", "minutes": 50, "task": "old", "at": yesterday + "T10:00:00+00:00"},
            ]}), encoding="utf-8")
            with patch.object(pomodoro, "data_file", return_value=path):
                result = pomodoro.run({"action": "stats"})
        self.assertIn("1 completed focus blocks, 25 minutes", result)
        self.assertIn("REM (25 min)", result)

    def test_pomodoro_records_completed_focus_and_schedules_long_break_each_fourth(self):
        from plugins import pomodoro
        class Timer:
            daemon = False
            def __init__(self, _seconds, _fn): pass
            def start(self): pass
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pomodoro.json"
            state = {"status": "running", "phase": "work", "seconds_left": 1,
                     "work_seconds": 1500, "break_seconds": 300,
                     "task": "Acceptance", "work_blocks": 3}
            with patch.object(pomodoro, "_state", state), \
                 patch.object(pomodoro, "_timer", None), \
                 patch.object(pomodoro, "data_file", return_value=path), \
                 patch.object(pomodoro.threading, "Timer", Timer), \
                 patch.object(pomodoro, "_notify"):
                pomodoro._tick()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["completed"][0]["phase"], "work")
            self.assertEqual(saved["completed"][0]["task"], "Acceptance")
            self.assertEqual(state["phase"], "break")
            self.assertEqual(state["seconds_left"], 900)

    def test_excel_writer_restores_safe_totals_and_chart_on_new_workbook(self):
        from openpyxl import load_workbook
        from plugins import excel_writer
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "sales.xlsx"
            result = excel_writer.run({
                "action": "create", "path": str(target), "sheet_name": "Sales",
                "headers": ["Month", "Revenue"],
                "rows": [["Jan", "12"], ["Feb", "15"]],
                "total_columns": [1], "chart_type": "bar",
                "chart_category_column": 0, "chart_value_column": 1,
            })
            self.assertIn("Created and verified", result)
            book = load_workbook(target, data_only=False)
            sheet = book["Sales"]
            self.assertEqual(sheet["B4"].value, "=SUM(B2:B3)")
            self.assertEqual(len(sheet._charts), 1)
            book.close()

    def test_document_review_restores_sanitized_structured_rendering(self):
        from plugins import document_review
        class Player:
            value = None
            def show_review(self, title, summary, findings, unclear):
                self.value = (title, summary, findings, unclear)
        player = Player()
        result = document_review.run({
            "title": "Lease", "summary": "Review summary",
            "findings": [
                {"heading": "Routine", "detail": "Context", "severity": "note"},
                {"heading": "Renewal", "detail": "Renews automatically", "severity": "serious",
                 "quote": "12 months", "suggestion": "Ask about the notice deadline."},
            ],
            "unclear": ["The notice deadline is not stated."],
        }, player, None)
        self.assertIn("not legal advice", result)
        self.assertEqual(player.value[0], "Lease")
        self.assertEqual(player.value[2][0]["severity"], "serious")
        self.assertEqual(player.value[3], ["The notice deadline is not stated."])

    def test_telegram_pairing_uses_constant_time_check_and_attempt_limits(self):
        from plugins import telegram_remote
        from memory import config_manager
        wrong = {"update_id": 501, "message": {"chat": {"id": 777},
                 "text": "/pair wrong-code"}}
        with patch.object(telegram_remote, "_cfg", return_value={
                "pairing_code": "test-pair-code", "allowed_chat_ids": ""}), \
             patch.object(telegram_remote, "_enqueue"), \
             patch.object(config_manager, "save_plugin_config") as save, \
             patch.object(telegram_remote, "_pair_attempts", {}), \
             patch.object(telegram_remote, "_pair_global_attempts", []):
            for _ in range(telegram_remote._PAIR_MAX_PER_CHAT + 1):
                telegram_remote._message(wrong)
            save.assert_not_called()
        valid = {"update_id": 502, "message": {"chat": {"id": 778},
                 "text": "/pair test-pair-code"}}
        with patch.object(telegram_remote, "_cfg", return_value={
                "pairing_code": "test-pair-code", "allowed_chat_ids": ""}), \
             patch.object(telegram_remote, "_enqueue"), \
             patch.object(config_manager, "save_plugin_config") as save, \
             patch.object(telegram_remote, "_pair_attempts", {}), \
             patch.object(telegram_remote, "_pair_global_attempts", []), \
             patch("plugins.telegram_remote.hmac.compare_digest", wraps=telegram_remote.hmac.compare_digest) as compare:
            telegram_remote._message(valid)
            compare.assert_called_once()
            save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
