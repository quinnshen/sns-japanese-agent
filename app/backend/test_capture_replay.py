import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import capture_replay


class CaptureReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "batches").mkdir()
        self.parts = [b"synthetic container header", b"synthetic packet A", b"synthetic packet B"]
        self.audio = b"".join(self.parts)
        self.manifest = {
            "schema_version": 1, "id": "00000000-0000-4000-8000-000000000001",
            "status": "complete", "source_url": "https://example.invalid/private-watch-history",
            "expected_batches": 3, "received_batches": 3, "missing_sequences": [],
            "total_bytes": len(self.audio), "audio_size_bytes": len(self.audio),
            "audio_sha256": hashlib.sha256(self.audio).hexdigest(),
            "timing": {
                "recording_started_at_ms": 1000, "playback_started_at_ms": 1120,
                "playback_start_video_sec": 30.02, "recording_stopped_at_ms": 16000,
                "playback_end_video_sec": 45,
                "playback_events": [{"type": "waiting", "at_ms": 8000, "video_sec": 36.5}],
            },
            "batches": [],
        }
        for sequence, data in enumerate(self.parts):
            filename = f"batches/{sequence:06d}.webm"
            (self.directory / filename).write_bytes(data)
            self.manifest["batches"].append({
                "sequence": sequence, "filename": filename,
                "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                "capture_start_ms": sequence * 5000, "capture_end_ms": (sequence + 1) * 5000,
            })
        self.write_manifest()
        (self.directory / "recording.webm").write_bytes(self.audio)

    def write_manifest(self):
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_read_only_verification_preserves_original_timing_and_hides_private_metadata(self):
        before = (self.directory / "manifest.json").read_bytes()
        report = capture_replay.verify_capture(self.directory)
        self.assertEqual(report["batch_count"], 3)
        self.assertEqual(report["total_bytes"], len(self.audio))
        self.assertEqual(report["observed_timing"], self.manifest["timing"])
        self.assertEqual((self.directory / "manifest.json").read_bytes(), before)
        self.assertNotIn("source_url", report)
        self.assertNotIn(str(self.directory), json.dumps(report))

    def test_unconfirmed_capture_is_not_reported_complete(self):
        self.manifest["status"] = "recording"
        self.write_manifest()
        with self.assertRaisesRegex(capture_replay.CaptureReplayError, "not been confirmed complete"):
            capture_replay.verify_capture(self.directory)

    def test_missing_batch_record_is_rejected(self):
        self.manifest["batches"].pop(1)
        self.write_manifest()
        with self.assertRaisesRegex(capture_replay.CaptureReplayError, "missing or extra"):
            capture_replay.verify_capture(self.directory)

    def test_duplicate_sequence_is_rejected(self):
        self.manifest["batches"][1]["sequence"] = 0
        self.write_manifest()
        with self.assertRaisesRegex(capture_replay.CaptureReplayError, "sequence"):
            capture_replay.verify_capture(self.directory)

    def test_missing_or_corrupt_bytes_never_replace_existing_output(self):
        output = self.directory / "previous-good.webm"
        output.write_bytes(b"previous good output")
        (self.directory / "batches/000001.webm").write_bytes(b"corrupt")
        with self.assertRaisesRegex(capture_replay.CaptureReplayError, "hash or size"):
            capture_replay.verify_capture(self.directory, output=output)
        self.assertEqual(output.read_bytes(), b"previous good output")
        (self.directory / "batches/000001.webm").unlink()
        with self.assertRaisesRegex(capture_replay.CaptureReplayError, "cannot be read"):
            capture_replay.verify_capture(self.directory, output=output)
        self.assertEqual(output.read_bytes(), b"previous good output")

    def test_invalid_declared_combined_hash_is_rejected(self):
        self.manifest["audio_sha256"] = "0" * 64
        self.write_manifest()
        with self.assertRaisesRegex(capture_replay.CaptureReplayError, "combined audio"):
            capture_replay.verify_capture(self.directory)

    def test_default_checks_existing_audio_and_explicit_output_rebuilds_in_order(self):
        recording = self.directory / "recording.webm"
        recording.write_bytes(b"broken combined file")
        with self.assertRaisesRegex(capture_replay.CaptureReplayError, "combined recording"):
            capture_replay.verify_capture(self.directory)
        capture_replay.verify_capture(self.directory, output=recording)
        self.assertEqual(recording.read_bytes(), self.audio)
        self.assertEqual(list(self.directory.glob(".capture-replay-*")), [])

    def test_path_traversal_manifest_is_rejected(self):
        self.manifest["batches"][0]["filename"] = "../private.webm"
        self.write_manifest()
        with self.assertRaisesRegex(capture_replay.CaptureReplayError, "filename"):
            capture_replay.verify_capture(self.directory)

    def test_rebuild_cannot_replace_manifest_or_batches(self):
        for output in [self.directory / "manifest.json", self.directory / "batches/000001.webm"]:
            with self.subTest(output=output.name):
                with self.assertRaisesRegex(capture_replay.CaptureReplayError, "must not replace"):
                    capture_replay.verify_capture(self.directory, output=output)

    def test_invalid_manifest_cli_error_is_safe(self):
        (self.directory / "manifest.json").write_text("not JSON", encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = capture_replay.main([str(self.directory)])
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertFalse(json.loads(errors.getvalue())["ok"])
        self.assertNotIn(str(self.directory), errors.getvalue())
        self.assertNotIn("private-watch-history", errors.getvalue())

    def test_deeply_nested_manifest_returns_safe_cli_error(self):
        nested = '{"nested":' * 1100 + '0' + '}' * 1100
        (self.directory / "manifest.json").write_text(nested, encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = capture_replay.main([str(self.directory)])
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(errors.getvalue()), {
            "ok": False, "error": "The capture manifest cannot be read as JSON.",
        })
        self.assertNotIn(str(self.directory), errors.getvalue())

    def test_unhashable_playback_event_types_are_rejected_safely(self):
        for kind in ([], {}):
            with self.subTest(kind=kind):
                self.manifest["timing"]["playback_events"][0]["type"] = kind
                self.write_manifest()
                with self.assertRaisesRegex(capture_replay.CaptureReplayError, "playback timing observations"):
                    capture_replay.verify_capture(self.directory)

    def test_boolean_numbers_are_not_accepted_as_manifest_counts_or_timing(self):
        original = json.dumps(self.manifest)
        fields = [
            ("schema_version",), ("expected_batches",), ("received_batches",),
            ("total_bytes",), ("audio_size_bytes",),
            ("batches", 0, "sequence"), ("batches", 0, "size_bytes"),
            ("batches", 0, "capture_start_ms"), ("batches", 0, "capture_end_ms"),
            ("timing", "playback_start_video_sec"),
            ("timing", "playback_events", 0, "at_ms"),
        ]
        for keys in fields:
            with self.subTest(field=keys):
                self.manifest = json.loads(original)
                parent = self.manifest
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = True
                self.write_manifest()
                with self.assertRaises(capture_replay.CaptureReplayError):
                    capture_replay.verify_capture(self.directory)

    def test_default_verification_does_not_launch_decoder(self):
        with patch.object(capture_replay.subprocess, "run") as run:
            capture_replay.verify_capture(self.directory)
        run.assert_not_called()

    def test_decode_success_reports_measured_output_duration(self):
        process = Mock(returncode=0, stdout="out_time_us=1500000\nprogress=continue\nout_time_us=14999000\nprogress=end\n")
        with patch.object(capture_replay.subprocess, "run", return_value=process) as run:
            report = capture_replay.verify_capture(self.directory, decode=True, ffmpeg="local-ffmpeg")
        self.assertEqual(report["decode_duration_sec"], 14.999)
        command = run.call_args.args[0]
        self.assertIn("-xerror", command)
        self.assertIn(str(self.directory / "recording.webm"), command)
        self.assertEqual(command[-3:], ["-f", "null", "-"])

    def test_decode_failure_does_not_expose_decoder_stderr(self):
        process = Mock(returncode=1, stdout="", stderr="private absolute path and metadata")
        with patch.object(capture_replay.subprocess, "run", return_value=process):
            with self.assertRaisesRegex(capture_replay.CaptureReplayError, "decoding failed") as caught:
                capture_replay.verify_capture(self.directory, decode=True, ffmpeg="local-ffmpeg")
        self.assertNotIn("private absolute path", str(caught.exception))

    def test_failed_decode_does_not_replace_explicit_output(self):
        output = self.directory / "previous-good.webm"
        output.write_bytes(b"previous good output")
        with patch.object(capture_replay, "decode_duration", side_effect=capture_replay.CaptureReplayError("Decode failed.")):
            with self.assertRaisesRegex(capture_replay.CaptureReplayError, "Decode failed"):
                capture_replay.verify_capture(self.directory, output=output, decode=True)
        self.assertEqual(output.read_bytes(), b"previous good output")
        self.assertEqual(list(self.directory.glob(".capture-replay-*")), [])

    def test_decode_timeout_and_empty_audio_are_rejected(self):
        with patch.object(capture_replay.subprocess, "run", side_effect=subprocess.TimeoutExpired("local", 60)):
            with self.assertRaisesRegex(capture_replay.CaptureReplayError, "could not finish"):
                capture_replay.decode_duration(self.directory / "recording.webm", ffmpeg="local-ffmpeg")
        with patch.object(capture_replay.subprocess, "run", return_value=Mock(returncode=0, stdout="progress=end\n")):
            with self.assertRaisesRegex(capture_replay.CaptureReplayError, "no measurable audio"):
                capture_replay.decode_duration(self.directory / "recording.webm", ffmpeg="local-ffmpeg")


if __name__ == "__main__":
    unittest.main()
