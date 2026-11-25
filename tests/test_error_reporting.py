import asyncio
import logging
import unittest
from unittest.mock import Mock

from core.error_reporting import classify_error, report_error


class SafeErrorReportingTests(unittest.TestCase):
    def test_expected_cancellation_is_not_classified_as_failure(self):
        self.assertEqual(classify_error(asyncio.CancelledError()),
                         "expected_cancellation")

    def test_timeout_and_critical_errors_are_classified(self):
        self.assertEqual(classify_error(TimeoutError()), "recoverable")
        self.assertEqual(classify_error(MemoryError()), "critical")

    def test_structured_record_omits_exception_message(self):
        logger = Mock(spec=logging.Logger)
        record = report_error(
            "browser", "navigate", RuntimeError("token=PRIVATE_VALUE"),
            task_id="task-123", logger=logger,
        )
        self.assertEqual(record["component"], "browser")
        self.assertEqual(record["operation"], "navigate")
        self.assertEqual(record["task_id"], "task-123")
        message = logger.log.call_args.args[1]
        payload = logger.log.call_args.args[2]
        self.assertIn("REM_RUNTIME_ERROR", message)
        self.assertNotIn("PRIVATE_VALUE", payload)
        self.assertEqual(record["final_status"], "failed")


if __name__ == "__main__":
    unittest.main()
