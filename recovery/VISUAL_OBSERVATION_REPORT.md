# Visual Observation Report

Date: 2026-11-24

- Fresh screenshot capture is available through MSS and currently succeeds at 1920×1080.
- `screen_process` supplies the actual user question and local observation metadata with the screenshot to the current Gemini Live session using the installed SDK's `send_realtime_input(video=...)` path. Mocked tests cover frame/question ordering and capture/encoding/delivery failure handling; Live receipt remains unverified.
- Foreground process/window metadata is gathered by `core.screenview` independently from accessibility scanning.
- Windows UIA is unavailable in this interpreter because `win32ui.pyd` requires missing `mfc140u.dll`, which breaks pywinauto import. OCR is unavailable because neither supported backend is installed.
- No screenshot is stored during capture. Live external vision delivery is subject to the configured Gemini service and privacy settings; this pass did not contact Gemini.
- Native target automation has no current, locally validated visual-coordinate-to-semantic-target round trip. A vision model describing an object is not sufficient evidence to click it.

## Display handling

`core.screenview` tracks physical monitor rectangles and converts accessibility element bounds into logical input coordinates. It includes virtual desktop dimensions and window bounds. Mixed-DPI, negative-origin multi-monitor, scaling, window-resize and obscuring-HUD correctness have not been physically acceptance-tested in this run.
