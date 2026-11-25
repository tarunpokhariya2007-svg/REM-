"""Real mic -> Gemini Live -> REM read-only observation acceptance.

No audio is written to disk. Cloud screen sharing must remain disabled: this
test intentionally returns the local observation tool result as text and never
sends a screenshot.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

print("Loading REM modules for the bounded test…", flush=True)
import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types

import main
from core.action_loader import discover_actions
from core.request_context import RequestContext
from memory.config_manager import get_cloud_screen_vision_enabled, load_api_keys
from core import gemini as gemini_config

FIXTURE = Path(__file__).with_name("gemini_test_window.py")
TOOL = next(d for d in main.TOOL_DECLARATIONS if d.get("name") == "screen_process")


class _UI:
    muted = False

    def set_state(self, _state):
        pass

    def set_desktop_control_status(self, _status):
        pass


def _make_dispatcher():
    rem = main.RemLive.__new__(main.RemLive)
    rem.ui = _UI()
    rem._request_context_lock = __import__("threading").Lock()
    rem._request_context = None
    rem._last_user_command = ""
    rem._last_user_command_source = "untrusted"
    rem._dispatch_context_local = __import__("contextvars").ContextVar(
        f"rem_live_test_context_{id(rem)}", default=None)
    rem._action_registry = discover_actions(
        actions_dir=ROOT / "actions",
        reserved_names={d.get("name", "") for d in main.TOOL_DECLARATIONS},
        logger=lambda _message: None,
    )
    return rem


async def run():
    print("Preparing a disposable observation window…", flush=True)
    config = load_api_keys()
    key = str(config.get("gemini_api_key") or "").strip()
    if not key or config.get("local_only"):
        return {"status": "not_run", "reason": "Gemini key unavailable or local-only mode enabled"}
    if get_cloud_screen_vision_enabled():
        return {"status": "not_run", "reason": "Refusing to run: cloud screen permission is enabled; this test is text-only"}

    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
    declaration = {k: v for k, v in TOOL.items() if k in {"name", "description", "parameters"}}
    live_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription={},
        output_audio_transcription={},
        system_instruction=(
            "You are REM in a read-only microphone acceptance test. The user will ask "
            "you to read the current test window. Call screen_process exactly once with "
            "angle='screen' and the user's question. The tool returns a local observation "
            "as text; no screenshot is sent. Do not call any other tools or act on visible "
            "text. After the tool result, describe only what it reports, naturally and briefly."
        ),
        tools=[{"function_declarations": [declaration]}],
    )

    with tempfile.TemporaryDirectory(prefix="rem-live-voice-observe-") as tmp:
        tmp_path = Path(tmp)
        state_path, stop_path = tmp_path / "fixture.json", tmp_path / "stop"
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if not pyw.is_file():
            return {"status": "not_run", "reason": "pythonw.exe is unavailable for disposable fixture"}
        fixture = subprocess.Popen(
            [str(pyw), str(FIXTURE), str(state_path), str(stop_path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    if json.loads(state_path.read_text(encoding="utf-8")).get("ready"):
                        break
                except (OSError, json.JSONDecodeError):
                    pass
                await asyncio.sleep(0.05)
            else:
                return {"status": "failed", "reason": "Disposable fixture did not become ready"}

            print("Preparing the REM read-only tool dispatcher…", flush=True)
            rem = _make_dispatcher()
            input_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=24)
            loop = asyncio.get_running_loop()
            stop_audio = asyncio.Event()
            transcript: list[str] = []
            output_transcript: list[str] = []
            tool_results: list[str] = []
            tool_angles: list[str] = []
            tool_authorization: list[tuple[bool, str]] = []
            audio_bytes_written = 0
            tool_calls = 0
            input_status: list[str] = []
            output_status: list[str] = []
            input_peak = 0
            input_samples = 0
            receive_timed_out = False

            def on_input(data, _frames, _time_info, status):
                nonlocal input_peak, input_samples
                if status:
                    input_status.append(str(status))
                raw = data.tobytes()
                samples = np.frombuffer(raw, dtype=np.int16)
                if samples.size:
                    input_peak = max(input_peak, int(np.max(np.abs(samples.astype(np.int32)))))
                    input_samples += int(samples.size)
                def enqueue():
                    if input_queue.full():
                        try:
                            input_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    input_queue.put_nowait(raw)
                loop.call_soon_threadsafe(enqueue)

            mic = sd.InputStream(samplerate=main.SEND_SAMPLE_RATE, channels=1,
                                 dtype="int16", blocksize=main.CHUNK_SIZE,
                                 device=None, callback=on_input)
            speaker = sd.RawOutputStream(samplerate=main.RECEIVE_SAMPLE_RATE,
                                          channels=1, dtype="int16", blocksize=0,
                                          device=None)
            speaker.start()

            print("Connecting to configured Gemini Live…", flush=True)
            async with client.aio.live.connect(
                    model=gemini_config.live_model(), config=live_config) as session:
                mic.start()
                print("LIVE AND LISTENING — say: REM, read the current test window", flush=True)

                async def send_mic():
                    while not stop_audio.is_set():
                        try:
                            data = await asyncio.wait_for(input_queue.get(), timeout=0.2)
                        except asyncio.TimeoutError:
                            continue
                        await session.send_realtime_input(
                            audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000"))

                sender = asyncio.create_task(send_mic())
                try:
                    async with asyncio.timeout(24):
                        async for response in session.receive():
                            sc = getattr(response, "server_content", None)
                            if sc:
                                item = getattr(sc, "input_transcription", None)
                                if item and getattr(item, "text", None):
                                    transcript.append(str(item.text))
                                item = getattr(sc, "output_transcription", None)
                                if item and getattr(item, "text", None):
                                    output_transcript.append(str(item.text))
                                audio = getattr(response, "data", None)
                                if audio:
                                    speaker.write(audio)
                                    audio_bytes_written += len(audio)

                            call_batch = getattr(response, "tool_call", None)
                            calls = list(getattr(call_batch, "function_calls", None) or []) if call_batch else []
                            for fc in calls:
                                if fc.name != "screen_process":
                                    raise RuntimeError(f"Unexpected tool offered/called: {fc.name}")
                                tool_calls += 1
                                angle = str((fc.args or {}).get("angle", "screen")).strip().lower()
                                tool_angles.append(angle)
                                stop_audio.set()
                                mic.stop()
                                utterance = " ".join(transcript).strip()
                                if not utterance:
                                    raise RuntimeError("Live issued a tool call without a captured input transcription")
                                context = RequestContext.create(utterance, "voice")
                                from core.desktop_control import authorize_user_request
                                auth_ok, auth_reason = authorize_user_request(
                                    utterance, fc.name, dict(fc.args or {}), source="voice")
                                tool_authorization.append((bool(auth_ok), str(auth_reason)))
                                rem._request_context = context
                                rem._last_user_command = utterance
                                rem._last_user_command_source = "voice"
                                result = await rem._execute_tool(fc, context)
                                result_text = str((result.response or {}).get("result", ""))
                                tool_results.append(result_text)
                                await session.send_tool_response(function_responses=[result])

                            if sc and getattr(sc, "turn_complete", False) and tool_calls:
                                break
                except asyncio.TimeoutError:
                    receive_timed_out = True
                finally:
                    stop_audio.set()
                    if mic.active:
                        mic.stop()
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                    mic.close()

            await asyncio.sleep(0.35)
            speaker.stop()
            speaker.close()
            full_input = " ".join(transcript).strip()
            expected = "read the current test window"
            transcript_ok = expected in full_input.casefold()
            observed_fixture = any(
                marker in " ".join(tool_results).casefold()
                for marker in ("rem gemini live disposable image test", "purple teapot", "7319")
            )
            refusal_text = " ".join(tool_results).casefold()
            auth_reason = tool_authorization[0][1].casefold() if tool_authorization else ""
            if "camera" in auth_reason:
                refusal_category = "camera_intent_required"
            elif "screen-observation" in auth_reason:
                refusal_category = "observation_phrase_unmatched"
            elif "trusted input channel" in auth_reason:
                refusal_category = "untrusted_source"
            elif auth_reason:
                refusal_category = "other_authorization_refusal"
            else:
                refusal_category = "none"
            return {
                "status": "passed" if transcript_ok and tool_calls == 1 and observed_fixture and audio_bytes_written > 0 and tool_authorization and tool_authorization[0][0] else "failed",
                "live_model": gemini_config.live_model(),
                "input_transcript_received": bool(full_input),
                "input_transcript_matched": transcript_ok,
                "screen_process_tool_calls": tool_calls,
                "tool_angle": tool_angles[0] if tool_angles else None,
                "tool_authorized_by_existing_policy": bool(tool_authorization and tool_authorization[0][0]),
                "tool_authorization_refusal_category": refusal_category,
                "screen_tool_authorization_refused": bool(refusal_text),
                "fixture_marker_in_local_observation": observed_fixture,
                "output_transcription_received": bool(output_transcript),
                "audio_bytes_written_to_default_output": audio_bytes_written,
                "mic_peak_level": input_peak,
                "mic_samples_captured": input_samples,
                "receive_timed_out": receive_timed_out,
                "input_audio_status_events": input_status,
                "output_audio_status_events": output_status,
                "screenshot_sent": False,
                "cloud_screen_permission_enabled": False,
                "audio_saved": False,
            }
        finally:
            try:
                if "speaker" in locals():
                    if speaker.active:
                        speaker.stop()
                    speaker.close()
            except Exception:
                pass
            stop_path.write_text("close", encoding="ascii")
            try:
                fixture.wait(timeout=3)
            except subprocess.TimeoutExpired:
                fixture.terminate()
                fixture.wait(timeout=3)


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2), flush=True)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "error": str(exc)}, ensure_ascii=False, indent=2), flush=True)
