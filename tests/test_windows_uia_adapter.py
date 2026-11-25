"""Regression coverage for the isolated CUIAutomation8 adapter."""
from __future__ import annotations

import unittest
from unittest.mock import patch


class _Rect:
    left, top, right, bottom = 10, 20, 210, 70


class _Node:
    def __init__(self, hwnd, pid, kind, name, rid):
        self.CurrentProcessId = pid
        self.CurrentNativeWindowHandle = hwnd
        self.CurrentControlType = kind
        self.CurrentName = name
        self.CurrentAutomationId = name.replace(" ", "")
        self.CurrentIsPassword = False
        self.CurrentBoundingRectangle = _Rect()
        self.CurrentIsOffscreen = False
        self.CurrentIsEnabled = True
        self.CurrentHasKeyboardFocus = False
        self._rid = rid

    def __bool__(self):
        return True

    def GetRuntimeId(self):
        return self._rid

    def GetCurrentPattern(self, _pattern_id):
        return None


class _Walker:
    def __init__(self, children):
        self.children = children

    def GetFirstChildElement(self, parent):
        children = self.children.get(parent, [])
        return children[0] if children else None

    def GetNextSiblingElement(self, node):
        for children in self.children.values():
            if node in children:
                index = children.index(node) + 1
                return children[index] if index < len(children) else None
        return None


class _Automation:
    def __init__(self, root, walker):
        self.root, self.ControlViewWalker = root, walker

    def ElementFromHandle(self, _hwnd):
        return self.root

    def GetRootElement(self):
        return self.root


class WindowsUiaAdapterTests(unittest.TestCase):
    def test_scan_returns_controls_with_uia_identity_and_capabilities(self):
        from core import windows_uia

        root = _Node(1234, 55, 50032, "Fixture", (1, 2))
        edit = _Node(1234, 55, 50004, "Search field", (1, 3))
        button = _Node(1234, 55, 50000, "Run test search", (1, 4))
        desktop = _Node(0, 0, 50025, "Desktop", (1, 1))
        walker = _Walker({desktop: [root], root: [edit, button], edit: [], button: []})
        automation = _Automation(root, walker)
        automation.GetRootElement = lambda: desktop
        with patch.object(windows_uia, "_on_mta", side_effect=lambda work, timeout: work(automation, object())), \
             patch.object(windows_uia, "_siblings", side_effect=lambda w, parent, deadline: walker.children.get(parent, [])):
            elements = windows_uia.scan(1234, 55, "Fixture", 1.0, float("inf"))

        self.assertEqual([e.name for e in elements], ["Fixture", "Search field", "Run test search"])
        field, button_element = elements[1:]
        self.assertEqual((field.control_type, field.automation_id, field.runtime_id),
                         ("Edit", "Searchfield", (1, 3)))
        self.assertEqual(button_element.control_type, "Button")
        self.assertEqual(field.window_pid, 55)

    def test_window_identity_mismatch_is_refused(self):
        from core import windows_uia

        root = _Node(4321, 55, 50032, "Wrong window", (1, 2))
        with self.assertRaises(PermissionError):
            windows_uia._process_roots(_Automation(root, _Walker({})), object(),
                                       1234, 55, float("inf"))

    def test_pattern_dispatch_is_semantic_and_obeys_stop(self):
        from core import windows_uia

        with patch.object(windows_uia, "_on_mta") as on_mta:
            stopped = windows_uia.perform(1, 2, "title", (1,), "id", "name",
                                          "Button", False, "invoke", stop=lambda: True)
        self.assertFalse(stopped[0])
        on_mta.assert_not_called()

    def test_screenview_uses_cuia_even_without_pywinauto_and_without_screenshot(self):
        from core import screenview
        shell = screenview.Element(0, "window", "Fixture", "", 0, 0, 500, 400,
                                   control_type="Window", runtime_id=(1, 1),
                                   window_pid=55, window_title="Fixture")
        field = screenview.Element(1, "edit", "Search field", "", 10, 30, 200, 25,
                                  control_type="Edit", runtime_id=(1, 2),
                                  window_pid=55, window_title="Fixture",
                                  patterns=("value",))
        button = screenview.Element(2, "button", "Run", "", 220, 30, 80, 25,
                                   control_type="Button", runtime_id=(1, 3),
                                   window_pid=55, window_title="Fixture",
                                   patterns=("invoke",))
        with patch.object(screenview, "_OS", "windows"), \
             patch.object(screenview, "_HAS_PYWINAUTO", False), \
             patch.object(screenview, "_physical_to_logical", return_value=1.0), \
             patch.object(screenview, "_logical_screen", return_value=(0, 0, 1920, 1080)), \
             patch.object(screenview, "_foreground", return_value=("fixture.exe", "Fixture", 55, 1234)), \
             patch.object(screenview, "observation_exclusion", return_value=""), \
             patch.object(screenview, "assert_observation_allowed", return_value=("fixture.exe", "Fixture", 55)), \
             patch.object(screenview, "all_windows", return_value=[]), \
             patch.object(screenview, "_foreground_rect", return_value=(0, 0, 500, 400)), \
             patch.object(screenview, "_is_own_window", return_value=False), \
             patch.object(screenview, "_is_slow", return_value=False), \
             patch.object(screenview, "_scan_windows", return_value=[shell, field, button]) as scan, \
             patch.object(screenview, "_thinness_reason", return_value=""), \
             patch.object(screenview, "_digest_elements", return_value="digest"), \
             patch.object(screenview, "_digest_stable", return_value="stable"), \
             patch.object(screenview, "_uia_diagnostic"), \
             patch.object(screenview, "_scan_ocr", side_effect=AssertionError("OCR/screenshot fallback")):
            view = screenview.look(allow_ocr=True)
        self.assertEqual(view.source, "uia")
        self.assertEqual([e.name for e in view.elements],
                         ["Fixture", "Search field", "Run"])
        scan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
