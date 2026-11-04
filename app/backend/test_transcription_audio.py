"""Offline regressions for continuous audio decoding and observed timing."""

import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from capture_store import CaptureStore
import transcription_audio as audio


class CaptureAudioFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = CaptureStore(self.root)
        self.identifier = "00000000-0000-4000-8000-000000000001"
        self.directory = self.root / self.identifier
        self.store.create({
            "id": self.identifier, "source_url": "https://www.youtube.com/watch?v=AbCdEf012_-",
            "video_duration_sec": 600, "range_start_sec": 30, "range_end_sec": 150,
            "batch_interval_ms": 5000, "mime_type": "audio/webm;codecs=opus",
        })
        self.store.set_timing(self.identifier, {
            "recording_started_at_ms": 1000, "playback_started_at_ms": 1120,
            "playback_start_video_sec": 30,
        })
        self.pcm = b"\x01\x00" * 32000
        self.inputs = []

    def batch(self, sequence, content=None):
        content = content or f"synthetic batch {sequence}".encode()
        self.store.put_batch(self.identifier, sequence, content, {
            "sha256": hashlib.sha256(content).hexdigest(),
            "capture_start_ms": sequence * 5000, "capture_end_ms": (sequence + 1) * 5000,
        })
        return content

    def complete(self, count):
        self.store.complete(self.identifier, {
            "expected_batches": count, "recording_stopped_at_ms": 121200,
            "playback_end_video_sec": 150,
            "playback_events": [{"type": "playing", "at_ms": 1120, "video_sec": 30}],
        })

    def decoder(self, command, **_kwargs):
        self.inputs.append(Path(command[command.index("-i") + 1]).read_bytes())
        Path(command[-1]).write_bytes(self.pcm)
        return Mock(returncode=0, stderr=b"")

    def prepare(self):
        return audio.prepare_capture_audio(self.directory, ffmpeg="synthetic-local-ffmpeg")


class AudioPreparationTests(CaptureAudioFixture, unittest.TestCase):
    def test_no_acknowledged_prefix_is_not_ready_and_does_not_decode(self):
        with patch.object(audio.subprocess, "run") as run:
            report = self.prepare()
        run.assert_not_called()
        self.assertFalse(report["ready"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["prefix_batches"], 0)

    def test_recording_uses_only_contiguous_prefix_and_one_continuous_decoder_input(self):
        first = self.batch(0)
        self.batch(2)
        with patch.object(audio.subprocess, "run", side_effect=self.decoder) as run:
            report = self.prepare()
        self.assertEqual(report["prefix_batches"], 1)
        self.assertEqual(report["manifest"]["received_batches"], 2)
        self.assertEqual(self.inputs, [first])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report["duration_sec"], 2)
        self.assertEqual(report["sample_count"], 32000)
        self.assertFalse(report["complete"])
        self.assertNotIn("source_url", report["manifest"])

    def test_unchanged_prefix_is_verified_and_reused_then_new_prefix_is_decoded(self):
        first = self.batch(0)
        with patch.object(audio.subprocess, "run", side_effect=self.decoder) as run:
            initial = self.prepare()
            repeated = self.prepare()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(initial["source_fingerprint"], repeated["source_fingerprint"])
            second = self.batch(1)
            updated = self.prepare()
            self.assertEqual(run.call_count, 2)
        self.assertEqual(self.inputs, [first, first + second])
        self.assertNotEqual(initial["source_fingerprint"], updated["source_fingerprint"])
        metadata = json.loads((self.directory / "transcription/audio.json").read_text())
        self.assertEqual(metadata["pcm_sha256"], hashlib.sha256(self.pcm).hexdigest())
        self.assertNotIn("source_url", metadata)
        for name in ("audio.pcm", "audio.json"):
            self.assertEqual(stat.S_IMODE((self.directory / "transcription" / name).stat().st_mode), 0o600)

    def test_final_storage_metadata_does_not_redecode_unchanged_valid_audio(self):
        self.batch(0)
        with patch.object(audio.subprocess, "run", side_effect=self.decoder) as run:
            initial = self.prepare()
            self.complete(1)
            final = self.prepare()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(final["source_fingerprint"], initial["source_fingerprint"])
        self.assertTrue(final["complete"])
        self.assertEqual(final["duration_sec"], 2, "Measured audio samples, not wall-clock stop, define duration")

    def test_modified_batch_and_modified_pcm_are_rejected_before_reuse(self):
        self.batch(0)
        with patch.object(audio.subprocess, "run", side_effect=self.decoder):
            self.prepare()
        cached = self.directory / "transcription/audio.pcm"
        original = cached.read_bytes()
        cached.write_bytes(b"wrong")
        with patch.object(audio.subprocess, "run") as run:
            with self.assertRaisesRegex(audio.TranscriptionAudioError, "cached PCM"):
                self.prepare()
        run.assert_not_called()
        cached.write_bytes(original)
        (self.directory / "batches/000000.webm").write_bytes(b"modified")
        with self.assertRaisesRegex(audio.TranscriptionAudioError, "checksum or size"):
            self.prepare()

    def test_unknown_decoder_error_never_replaces_previous_good_pcm(self):
        self.batch(0)
        with patch.object(audio.subprocess, "run", side_effect=self.decoder):
            self.prepare()
        before = (self.directory / "transcription/audio.pcm").read_bytes()
        self.batch(1)
        def corrupt(command, **kwargs):
            self.decoder(command, **kwargs)
            return Mock(returncode=0, stderr=b"unknown codec corruption /private/example-path")
        with patch.object(audio.subprocess, "run", side_effect=corrupt):
            with self.assertRaises(audio.TranscriptionAudioError) as caught:
                self.prepare()
        self.assertNotIn("/private", str(caught.exception))
        self.assertEqual((self.directory / "transcription/audio.pcm").read_bytes(), before)
        self.assertEqual(list((self.directory / "transcription").glob(".prefix-*")), [])
        self.assertEqual(list((self.directory / "transcription").glob(".pcm-*")), [])

    def test_only_unfinished_recording_allows_precise_premature_end_diagnostic(self):
        self.batch(0)
        def truncated(command, **kwargs):
            self.decoder(command, **kwargs)
            return Mock(returncode=1, stderr=b"[matroska,webm @ 0x123abc] File ended prematurely\n")
        with patch.object(audio.subprocess, "run", side_effect=truncated) as run:
            report = self.prepare()
            self.assertTrue(report["decoder_incomplete"])
            self.assertTrue(report["ready"])
            self.assertFalse(report["complete"])
            self.complete(1)
            with self.assertRaisesRegex(audio.TranscriptionAudioError, "unfinished audio container"):
                self.prepare()
        self.assertEqual(run.call_count, 1)

    def test_completed_capture_does_not_accept_a_truncated_or_empty_decode(self):
        self.batch(0)
        self.complete(1)
        def truncated(command, **kwargs):
            self.decoder(command, **kwargs)
            return Mock(returncode=1, stderr=b"[matroska,webm @ abc123] File ended prematurely\n")
        with patch.object(audio.subprocess, "run", side_effect=truncated):
            with self.assertRaises(audio.TranscriptionAudioError):
                self.prepare()
        self.pcm = b""
        with patch.object(audio.subprocess, "run", side_effect=self.decoder):
            with self.assertRaisesRegex(audio.TranscriptionAudioError, "no decoded audio"):
                self.prepare()

    def test_unsafe_filename_symlink_and_aborted_capture_are_rejected(self):
        self.batch(0)
        batch = self.directory / "batches/000000.webm"
        outside = self.root / "unrelated.webm"
        outside.write_bytes(batch.read_bytes())
        batch.unlink()
        batch.symlink_to(outside)
        with self.assertRaises(audio.TranscriptionAudioError):
            self.prepare()
        batch.unlink()
        batch.write_bytes(outside.read_bytes())
        manifest_path = self.directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["batches"][0]["filename"] = "../unrelated.webm"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(audio.TranscriptionAudioError, "filename"):
            self.prepare()
        manifest["batches"][0]["filename"] = "batches/000000.webm"
        manifest["status"] = "aborted"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(audio.TranscriptionAudioError, "Only recording or completed"):
            self.prepare()

    def test_pcm_window_is_deterministic_standard_wav_with_sample_boundaries(self):
        self.batch(0)
        self.pcm = b"".join((sample % 30000).to_bytes(2, "little") for sample in range(32000))
        with patch.object(audio.subprocess, "run", side_effect=self.decoder):
            report = self.prepare()
        window = audio.window_plan(2, True, core_seconds=1, context_seconds=0.25)[1]
        first = audio.wav_for_window(report["pcm_path"], window)
        self.assertEqual(first, audio.wav_for_window(report["pcm_path"], window))
        with wave.open(io.BytesIO(first), "rb") as decoded:
            self.assertEqual((decoded.getframerate(), decoded.getnchannels(), decoded.getsampwidth()), (16000, 1, 2))
            self.assertEqual(decoded.getnframes(), 20000)
            self.assertEqual(decoded.readframes(20000), self.pcm[24000:64000])
        invalid = {**window, "audio_end_sample": 32001}
        with self.assertRaisesRegex(audio.TranscriptionAudioError, "not available"):
            audio.wav_for_window(report["pcm_path"], invalid)


class AudioWindowAndTimingTests(unittest.TestCase):
    def test_open_capture_only_emits_full_right_context_and_final_capture_flushes_tail(self):
        self.assertEqual(audio.window_plan(43.999, False), [])
        self.assertEqual(len(audio.window_plan(44, False)), 1)
        windows = audio.window_plan(87, False)
        self.assertEqual(len(windows), 2)
        self.assertEqual((windows[1]["audio_start_sec"], windows[1]["audio_end_sec"]), (42, 87))
        final = audio.window_plan(90.25, True)
        self.assertEqual(len(final), 3)
        self.assertEqual((final[2]["core_start_sec"], final[2]["core_end_sec"]), (86, 90.25))
        self.assertEqual(final[2]["audio_end_sample"], 90.25 * 16000)
        self.assertEqual(final[0], audio.window_plan(44, False)[0], "Earlier window identity must stay stable")
        for window in final:
            self.assertLessEqual(window["audio_end_sample"] - window["audio_start_sample"], 45 * 16000)
        with self.assertRaises(audio.TranscriptionAudioError):
            audio.window_plan(90, False, core_seconds=44, context_seconds=1)

    def manifest(self):
        return {"status": "complete", "timing": {
            "recording_started_at_ms": 1000, "playback_started_at_ms": 1120,
            "playback_start_video_sec": 30, "recording_stopped_at_ms": 23120,
        }}

    def test_initial_offset_and_stop_clock_mismatch_never_scale_measured_audio(self):
        manifest = self.manifest()
        mapping = audio.build_time_map(manifest, 20.12)
        self.assertIsNone(audio.map_audio_time(mapping, 0.1)["video_sec"])
        self.assertEqual(audio.map_audio_time(mapping, 0.12)["video_sec"], 30)
        self.assertAlmostEqual(audio.map_audio_time(mapping, 1.12)["video_sec"], 31)
        self.assertAlmostEqual(audio.map_audio_time(mapping, 20.12)["video_sec"], 50)
        self.assertIn("wall_clock_duration_differs_from_decoded_sample_duration", mapping["warnings"])
        self.assertTrue(audio.map_audio_time(mapping, 1)["uncertain"])
        self.assertIsNone(audio.map_audio_time(mapping, 20.13)["video_sec"])

    def test_waiting_and_playing_use_piecewise_observations_and_end_boundary(self):
        manifest = self.manifest()
        manifest["timing"]["playback_events"] = [
            {"type": "playing", "at_ms": 1120, "video_sec": 30},
            {"type": "waiting", "at_ms": 11120, "video_sec": 40},
            {"type": "playing", "at_ms": 13120, "video_sec": 40},
        ]
        mapping = audio.build_time_map(manifest, 20.12)
        self.assertEqual(audio.map_audio_time(mapping, 11.12)["video_sec"], 40)
        self.assertEqual(audio.map_audio_time(mapping, 12.12)["video_sec"], 40)
        self.assertAlmostEqual(audio.map_audio_time(mapping, 13.12)["video_sec"], 41)
        self.assertAlmostEqual(audio.map_audio_time(mapping, 20.12)["video_sec"], 48)
        self.assertEqual(mapping["quality"], "uncertain")
        self.assertIn("buffering_observed_alignment_is_approximate", mapping["warnings"])

    def test_missing_or_nonmonotonic_observations_are_explicitly_uncertain(self):
        mapping = audio.build_time_map({"status": "recording"}, 10)
        self.assertIsNone(audio.map_audio_time(mapping, 2)["video_sec"])
        self.assertEqual(mapping["quality"], "uncertain")
        manifest = self.manifest()
        manifest["timing"]["playback_events"] = [
            {"type": "waiting", "at_ms": 10000, "video_sec": 39},
            {"type": "playing", "at_ms": 8000, "video_sec": 37},
        ]
        mapping = audio.build_time_map(manifest, 20)
        self.assertIn("invalid_playback_observations_ignored", mapping["warnings"])
        self.assertEqual(mapping["quality"], "uncertain")
        for invalid in (True, -1, float("inf"), 1e300):
            with self.subTest(invalid=invalid), self.assertRaises(audio.TranscriptionAudioError):
                audio.window_plan(invalid, False)


class RealLocalDecoderTests(CaptureAudioFixture, unittest.TestCase):
    def test_real_webm_prefix_grows_and_preserves_already_ready_window_bytes(self):
        try:
            executable = audio._ffmpeg_binary(None)
        except audio.TranscriptionAudioError:
            self.skipTest("Optional local FFmpeg binary is unavailable")
        source = self.root / "generated.webm"
        generated = subprocess.run([
            executable, "-nostdin", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=3", "-c:a", "libopus", "-b:a", "64k", "-y", str(source),
        ], capture_output=True, timeout=15)
        self.assertEqual(generated.returncode, 0, "Synthetic local audio generation must succeed")
        container = source.read_bytes()
        cut = len(container) * 2 // 3
        self.batch(0, container[:cut])
        initial = audio.prepare_capture_audio(self.directory, ffmpeg=executable)
        self.assertTrue(initial["ready"])
        self.assertFalse(initial["complete"])
        window = audio.window_plan(initial["duration_sec"], False, core_seconds=0.5, context_seconds=0.1)[0]
        wav_before = audio.wav_for_window(initial["pcm_path"], window)
        self.batch(1, container[cut:])
        self.complete(2)
        final = audio.prepare_capture_audio(self.directory, ffmpeg=executable)
        self.assertTrue(final["complete"])
        self.assertAlmostEqual(final["duration_sec"], 3, delta=0.01)
        self.assertEqual(wav_before, audio.wav_for_window(final["pcm_path"], window))


if __name__ == "__main__":
    unittest.main()
