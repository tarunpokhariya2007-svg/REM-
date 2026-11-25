# REM avatar upgrade — report

## Files
- NEW core/avatar_anim.py — Qt-free animator: blink, mouth smoothing, states, expressions, idle motion
- CHANGED core/rem_avatar.py — RemLayeredAvatar + factory RemParticleAvatar(); old class kept as LegacyParticleAvatar (fallback)
- CHANGED gui/hud.py — passes state/muted to avatar; set_expression()
- CHANGED gui/window.py, ui.py — thread-safe expression signal/wrapper
- CHANGED main.py — keyword expression cue (REM_AVATAR_EXPRESSIONS=0 disables)
- NEW tools/build_rem_layers.py — builds layers from the two source PNGs
- NEW gui/assets/rem/*.png + manifest.json (+ source/ originals)
- NEW tests/test_avatar_animation.py

## Assets (all RGBA PNG, canvas 1277x1232, already in gui/assets/rem/)
rem-base, rem-eyes-lid035/070/100, rem-mouth-small/medium/open/round/wide/smile, manifest.json.
Source art: gui/assets/rem/source/rem-source-open.png and rem-source-closed.png.

## Verification
Automated: 18 tests pass (animation, assets); 3 Qt renderer tests skipped in sandbox (no PyQt6) — run them on Windows.
Visual: PIL reference composite of real layers with renderer blend logic (not real Qt output).
Not verified: Qt painting, resize, long sessions, on-screen audio sync.

## Still needs artwork
Separate bangs/ribbon/pupil/head layers for real head movement; hand-drawn A/E/O/MBP mouths; drawn half-lid and happy/concerned eyes.

## Run
python -m unittest tests.test_avatar_animation -v
$env:REM_AVATAR_DEBUG="1"; python main.py
