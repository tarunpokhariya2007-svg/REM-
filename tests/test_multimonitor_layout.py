"""Virtual display-layout coverage; these do not claim physical second-monitor acceptance."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import screenview


class MultiMonitorLayoutTests(unittest.TestCase):
    def test_foreground_window_on_negative_origin_monitor_selects_that_monitor(self):
        primary = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        left = {"left": -1280, "top": 0, "width": 1280, "height": 1024}
        all_monitors = {"left": -1280, "top": 0, "width": 3200, "height": 1080}
        view = SimpleNamespace(window_rect=(-1200, 100, 700, 500))
        with patch.object(screenview, "monitors",
                          return_value=[all_monitors, primary, left]):
            self.assertEqual(screenview.monitor_rect(view), left)

    def test_foreground_window_on_monitor_with_negative_top_selects_that_monitor(self):
        primary = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        upper = {"left": 1920, "top": -900, "width": 2560, "height": 1440}
        all_monitors = {"left": 0, "top": -900, "width": 4480, "height": 1980}
        view = SimpleNamespace(window_rect=(2300, -700, 900, 700))
        with patch.object(screenview, "monitors",
                          return_value=[all_monitors, primary, upper]):
            self.assertEqual(screenview.monitor_rect(view), upper)

    def test_unmatched_or_missing_window_uses_primary_monitor(self):
        primary = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        secondary = {"left": 1920, "top": 0, "width": 1920, "height": 1080}
        all_monitors = {"left": 0, "top": 0, "width": 3840, "height": 1080}
        with patch.object(screenview, "monitors",
                          return_value=[all_monitors, primary, secondary]):
            self.assertEqual(screenview.monitor_rect(None), primary)
            offscreen = SimpleNamespace(window_rect=(-50, 0, 20, 20))
            self.assertEqual(screenview.monitor_rect(offscreen), primary)


if __name__ == "__main__":
    unittest.main()
