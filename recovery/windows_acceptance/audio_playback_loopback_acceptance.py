"""Local-only Windows output acceptance. Never stores or prints PCM samples."""
from __future__ import annotations

import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np
import sounddevice as sd


def run():
    out_rate, in_rate = 24_000, 16_000
    input_device, output_device = sd.default.device
    input_name = "system default microphone"
    if len(sys.argv) > 1:
        from core.audio_devices import resolve
        input_name = sys.argv[1]
        input_device = resolve(input_name, "input")
    input_info = sd.query_devices(input_device, "input")
    output_info = sd.query_devices(output_device, "output")
    blocks = []
    statuses = []
    playing = [0.0, 0.0]

    def input_callback(data, frames, _time_info, status):
        if status:
            statuses.append(str(status))
        # Keep data only in this process and only until the measurement is done.
        blocks.append((time.monotonic(), bytes(data)))

    seconds = 0.7
    sample_count = int(out_rate * seconds)
    samples = np.arange(sample_count, dtype=np.float64) / out_rate
    tone = (np.sin(2 * np.pi * 660 * samples) * 2600).astype(np.int16)
    capture = sd.RawInputStream(
        samplerate=in_rate, channels=1, dtype="int16", blocksize=800,
        device=input_device, callback=input_callback,
    )
    capture.start()
    time.sleep(0.25)
    output = sd.RawOutputStream(
        samplerate=out_rate, channels=1, dtype="int16", blocksize=1024,
        device=output_device,
    )
    try:
        output.start()
        playing[0] = time.monotonic()
        output.write(tone.tobytes())
        playing[1] = time.monotonic()
        time.sleep(0.30)
    finally:
        output.stop()
        output.close()
        capture.stop()
        capture.close()

    # Isolate the acoustic/mixed-output window with 50 ms margins. Only numeric
    # summary metrics leave this function; buffers are discarded immediately.
    window_start = playing[0] - 0.05
    window_end = playing[1] + 0.05
    payload = b"".join(raw for stamp, raw in blocks
                      if window_start <= stamp <= window_end)
    pcm = np.frombuffer(payload, dtype=np.int16).astype(np.float64)
    if pcm.size:
        pcm -= pcm.mean()
        phase = 2 * np.pi * 660 * np.arange(pcm.size) / in_rate
        sine = float(np.dot(pcm, np.sin(phase)))
        cosine = float(np.dot(pcm, np.cos(phase)))
        tone_amplitude = (2.0 / pcm.size) * (sine * sine + cosine * cosine) ** 0.5
        rms = float(np.sqrt(np.mean(pcm * pcm)))
        peak = int(np.max(np.abs(pcm)))
    else:
        tone_amplitude, rms, peak = 0.0, 0.0, 0
    elapsed = playing[1] - playing[0]
    consumed_realtime = elapsed >= seconds * 0.5
    audible_loopback = tone_amplitude >= 80.0
    return {
        "status": "passed" if consumed_realtime and audible_loopback else "failed",
        "input_device": str(input_info["name"]),
        "input_selection": input_name,
        "output_device": str(output_info["name"]),
        "output_sample_rate": out_rate,
        "nonzero_samples_written": int(np.count_nonzero(tone)),
        "output_sample_count": sample_count,
        "requested_playback_seconds": seconds,
        "write_elapsed_seconds": round(elapsed, 3),
        "consumed_at_realtime_rate": consumed_realtime,
        "loopback_input_sample_count": int(pcm.size),
        "loopback_peak": peak,
        "loopback_rms": round(rms, 2),
        "loopback_660hz_amplitude": round(tone_amplitude, 2),
        "audible_tone_in_local_input": audible_loopback,
        "input_status_count": len(statuses),
        "raw_audio_saved": False,
        "loopback_scope": "selected local input endpoint; analysis local only",
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
