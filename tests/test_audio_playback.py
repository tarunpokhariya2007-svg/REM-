import unittest
from unittest.mock import patch

from core.audio_playback import PlaybackBuffer
from core.voice_state import VoicePhase, VoiceState
from tests.manual_async import manual_loop, run_ready


class PlaybackBufferTests(unittest.TestCase):
    def test_buffer_capacity_is_bounded_and_preserves_order(self):
        buffer = PlaybackBuffer(maxsize=3)
        for i in range(3):
            self.assertTrue(run_ready(buffer.put(bytes([i]), 7)))
        self.assertEqual(buffer.qsize(), 3)
        self.assertEqual(buffer.maxsize, 3)
        received = [buffer.get_nowait().data for _ in range(3)]
        self.assertEqual(received, [b"\x00", b"\x01", b"\x02"])
        self.assertEqual(buffer.stats()["generated"], 3)

    def test_full_buffer_applies_backpressure_without_dropping_chunks(self):
        with manual_loop():
            buffer = PlaybackBuffer(maxsize=2)
            run_ready(buffer.put(b"a", 1))
            run_ready(buffer.put(b"b", 1))
            producer = buffer.put(b"c", 1)
            with patch("core.audio_playback.time.monotonic", side_effect=[10.0, 20.0, 22.0]):
                waiter = producer.send(None)
                self.assertFalse(waiter.done())
                self.assertEqual(buffer.get_nowait().data, b"a")
                with self.assertRaises(StopIteration) as completed:
                    producer.send(None)
                self.assertTrue(completed.exception.value)
            received = [buffer.get_nowait().data for _ in range(2)]
            self.assertEqual(received, [b"b", b"c"])
            self.assertEqual(buffer.producer_wait_seconds, 2.0)

    def test_sustained_fast_producer_stays_bounded_and_delivers_every_chunk(self):
        with manual_loop():
            buffer = PlaybackBuffer(maxsize=8)
            produced = list(range(500))
            played = []
            for i in produced:
                producer = buffer.put(str(i).encode(), 3)
                try:
                    producer.send(None)
                except StopIteration:
                    continue
                item = buffer.get_nowait()
                played.append(int(item.data))
                buffer.mark_played(queued_at=item.queued_at)
                with self.assertRaises(StopIteration):
                    producer.send(None)
            while not buffer.empty():
                item = buffer.get_nowait()
                played.append(int(item.data))
                buffer.mark_played(queued_at=item.queued_at)
            self.assertEqual(played, produced)
            self.assertLessEqual(buffer.high_water, 8)
            stats = buffer.stats()
            self.assertEqual(stats["generated"], 500)
            self.assertEqual(stats["played"], 500)
            self.assertEqual(stats["discarded"], {})

    def test_interruption_discards_only_cancelled_generation(self):
        buffer = PlaybackBuffer(maxsize=5)
        run_ready(buffer.put(b"old-1", 1))
        run_ready(buffer.put(b"new-1", 2))
        run_ready(buffer.put(b"old-2", 1))
        self.assertEqual(buffer.discard_generation(1, "interruption"), 2)
        self.assertEqual(buffer.get_nowait().data, b"new-1")
        self.assertEqual(buffer.stats()["discarded"], {"interruption": 2})

    def test_close_discards_buffer_and_rejects_future_audio(self):
        buffer = PlaybackBuffer(maxsize=2)
        run_ready(buffer.put(b"queued", 1))
        self.assertEqual(buffer.close(), 1)
        self.assertFalse(run_ready(buffer.put(b"late", 1)))
        self.assertTrue(buffer.empty())
        self.assertEqual(buffer.stats()["discarded"]["shutdown"], 2)

class VoiceStateTests(unittest.TestCase):
    def test_state_machine_tracks_interrupt_and_listen_transitions_safely(self):
        state = VoiceState()
        self.assertEqual(state.phase, VoicePhase.IDLE)
        state.set(VoicePhase.LISTENING)
        state.set(VoicePhase.THINKING)
        state.set(VoicePhase.GENERATING_AUDIO)
        state.set(VoicePhase.SPEAKING)
        state.set(VoicePhase.USER_INTERRUPTION)
        state.set(VoicePhase.CANCELLING)
        state.set(VoicePhase.LISTENING)
        self.assertEqual(state.phase, VoicePhase.LISTENING)
        self.assertEqual(state.transitions, 7)


class PlaybackConfigurationTests(unittest.TestCase):
    def test_playback_buffer_setting_is_bounded_and_preserves_unrelated_config(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from memory import config_manager

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api_keys.json"
            path.write_text(json.dumps({"keep": "value"}), encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", path):
                self.assertEqual(config_manager.get_playback_buffer_seconds(), 30)
                self.assertEqual(config_manager.save_playback_buffer_seconds(999), 120)
                self.assertEqual(config_manager.get_playback_buffer_seconds(), 120)
                config_manager.save_playback_buffer_seconds(1)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["playback_buffer_seconds"], 5)
                self.assertEqual(saved["keep"], "value")


class EchoGuardTests(unittest.TestCase):
    def test_playback_echo_is_rejected_and_distinct_speech_is_detected(self):
        import numpy as np
        from core.echo import EchoGuard
        from main import _pcm_level

        guard = EchoGuard()
        n = np.arange(1024, dtype=np.float32)
        output = (12000 * np.sin(2 * np.pi * 330 * n / 16000)).astype(np.int16)
        echo = (output.astype(np.float32) * 0.35).astype(np.int16)
        speech = (6000 * np.sin(2 * np.pi * 1700 * n / 16000)).astype(np.int16)
        now = 100.0
        for _ in range(25):
            guard.note_output(output, 16000, _pcm_level(output), when=now)
            self.assertFalse(guard.is_user_speech(echo, 16000, _pcm_level(echo), when=now + .001))
            now += .064
        positive = [guard.is_user_speech(speech, 16000, _pcm_level(speech), when=now + .001 + i*.064)
                    for i in range(guard.required_blocks + 1)]
        self.assertTrue(any(positive))

    def test_low_level_background_noise_is_not_an_interruption(self):
        import numpy as np
        from core.echo import EchoGuard
        from main import _pcm_level

        guard = EchoGuard()
        noise = np.full(1024, 5, dtype=np.int16)
        self.assertFalse(guard.is_user_speech(noise, 16000, _pcm_level(noise), when=1.0))


if __name__ == "__main__":
    unittest.main()
