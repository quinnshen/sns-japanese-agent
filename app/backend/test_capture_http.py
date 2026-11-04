"""Exercise capture routes against real temporary storage without cloud calls or sockets."""

import hashlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import server


class CaptureHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.capture_id = str(uuid.uuid4())
        for target, value in (
            ("DATA_DIR", self.root),
            ("setting", lambda _name, default=None: default),
            ("transcribe_media", Mock(side_effect=AssertionError("Unexpected cloud processing"))),
            ("enrich_result", Mock(side_effect=AssertionError("Unexpected cloud processing"))),
            ("local_api_key", Mock(side_effect=AssertionError("Unexpected credential access"))),
        ):
            patcher = patch.object(server, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(server.capture_store.cache_clear)

    def handler(self, path, payload=None, *, body=None, headers=None):
        handler = object.__new__(server.Handler)
        handler.path = path
        body = body if body is not None else json.dumps(payload or {}).encode()
        handler.headers = {
            "Host": "127.0.0.1:8766", "Origin": "chrome-extension://" + "a" * 32,
            "Content-Type": "application/json", "Content-Length": str(len(body)),
            **(headers or {}),
        }
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_json = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        return handler

    def post(self, path, payload):
        handler = self.handler(path, payload)
        handler.do_POST()
        return handler.send_json.call_args.args

    def create(self):
        status, result = self.post("/captures", {
            "id": self.capture_id,
            "source_url": "https://www.youtube.com/watch?v=AbCdEf012_-",
            "video_duration_sec": 3600,
            "range_start_sec": 60, "range_end_sec": 65,
            "batch_interval_ms": 1000, "mime_type": "audio/webm;codecs=opus",
        })
        self.assertEqual(status, 200)
        return result

    def put(self, sequence, body):
        handler = self.handler(f"/captures/{self.capture_id}/batches/{sequence}", body=body, headers={
            "Content-Type": "audio/webm;codecs=opus",
            "X-Batch-SHA256": hashlib.sha256(body).hexdigest(),
            "X-Capture-Start-Ms": str(sequence * 1000),
            "X-Capture-End-Ms": str((sequence + 1) * 1000),
        })
        handler.do_PUT()
        return handler.send_json.call_args.args

    def test_health_and_legacy_route_are_cloud_free_by_default(self):
        handler = self.handler("/health")
        handler.do_GET()
        handler.send_json.assert_called_once_with(200, {
            "ok": True, "capabilities": {"capture_batches": True, "cloud_processing": False, "capture_transcription": False, "capture_enrichment": False},
        })
        status, _ = self.post("/jobs", {})
        self.assertEqual(status, 409)
        self.assertFalse(self.root.joinpath("captures").exists())

    def test_transcription_start_requires_explicit_enable_and_trusted_origin(self):
        self.create()
        path = f"/captures/{self.capture_id}/transcription"
        with patch("capture_transcription.request_processing") as schedule:
            self.assertEqual(self.post(path, {})[0], 409)
            schedule.assert_not_called()
            with patch.object(server, "setting", lambda name, default=None: "1" if name == "ENABLE_CAPTURE_TRANSCRIPTION" else default):
                self.assertEqual(self.post(path, {"unexpected": True})[0], 400)
                self.assertEqual(self.post(path, {})[0], 202)
                schedule.assert_called_once()
                untrusted = self.handler(path, headers={"Origin": "https://example.invalid"})
                untrusted.do_POST()
                self.assertEqual(untrusted.send_json.call_args.args[0], 403)
                self.assertEqual(schedule.call_count, 1)
        handler = self.handler(path)
        handler.do_GET()
        self.assertEqual(handler.send_json.call_args.args[1]["status"], "not_started")

    def test_enabled_batch_ack_precedes_asynchronous_transcription_scheduling(self):
        self.create()
        events = []
        handler = self.handler(f"/captures/{self.capture_id}/batches/0", body=b"audio", headers={
            "Content-Type": "audio/webm", "X-Batch-SHA256": hashlib.sha256(b"audio").hexdigest(),
            "X-Capture-Start-Ms": "0", "X-Capture-End-Ms": "5000",
        })
        handler.send_json.side_effect = lambda *_: events.append("ack")
        with patch.object(server, "setting", lambda name, default=None: "1" if name == "ENABLE_CAPTURE_TRANSCRIPTION" else default), \
             patch("capture_transcription.request_processing", side_effect=lambda *_args, **_kwargs: events.append("scheduled")):
            handler.do_PUT()
        self.assertEqual(events, ["ack", "scheduled"])
        self.assertEqual(server.capture_store(self.root / "captures").get(self.capture_id)["received_batches"], 1)

    def test_lost_ack_still_schedules_and_scheduler_failure_does_not_rewrite_ack(self):
        manifest = {"id": self.capture_id, "status": "complete"}
        handler = self.handler("/captures")
        handler.send_json.side_effect = BrokenPipeError()
        with patch.object(server, "wake_transcription") as schedule:
            with self.assertRaises(BrokenPipeError):
                handler.acknowledge_capture(manifest, manifest)
            schedule.assert_called_once_with(manifest)
        handler = self.handler("/captures")
        handler.log_message = Mock()
        with patch.object(server, "wake_transcription", side_effect=RuntimeError("synthetic scheduler failure")):
            handler.acknowledge_capture(manifest, manifest)
        handler.send_json.assert_called_once_with(200, manifest)
        handler.log_message.assert_called_once()

    def test_create_duplicate_missing_completion_and_byte_range_playback(self):
        original = self.create()
        self.assertEqual(original["received_batches"], 0)
        status, ack = self.put(0, b"hello")
        self.assertEqual(status, 200)
        self.assertTrue(ack["stored"])
        self.assertFalse(ack["duplicate"])
        status, duplicate = self.put(0, b"hello")
        self.assertEqual(status, 200)
        self.assertTrue(duplicate["duplicate"])
        status, _ = self.post(f"/captures/{self.capture_id}/timing", {
            "recording_started_at_ms": 100000, "playback_started_at_ms": 100100,
            "playback_start_video_sec": 60.01,
        })
        self.assertEqual(status, 200)
        finish = {"expected_batches": 2, "recording_stopped_at_ms": 105200, "playback_end_video_sec": 65.01}
        status, error = self.post(f"/captures/{self.capture_id}/complete", finish)
        self.assertEqual(status, 409)
        self.assertEqual(error["missing_sequences"], [1])
        self.assertEqual(self.put(1, b"world")[0], 200)
        status, completed = self.post(f"/captures/{self.capture_id}/complete", finish)
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["audio_size_bytes"], 10)
        self.assertEqual(self.post(f"/captures/{self.capture_id}/complete", finish)[0], 200)
        handler = self.handler(f"/captures/{self.capture_id}/audio", headers={"Range": "bytes=1-3"})
        handler.do_GET()
        handler.send_response.assert_called_once_with(206)
        handler.send_header.assert_any_call("Content-Range", "bytes 1-3/10")
        self.assertEqual(handler.wfile.getvalue(), b"ell")
        cancelled = self.handler(f"/captures/{self.capture_id}/audio")
        cancelled.wfile = Mock()
        cancelled.wfile.write.side_effect = BrokenPipeError()
        cancelled.do_GET()
        cancelled.send_response.assert_called_once_with(200)
        cancelled.send_json.assert_not_called()
        invalid_range = self.handler(f"/captures/{self.capture_id}/audio", headers={"Range": "bytes=" + "1" * 5000 + "-"})
        invalid_range.do_GET()
        self.assertEqual(invalid_range.send_json.call_args.args[0], 416)

    def test_untrusted_web_origin_is_rejected_before_any_capture_storage(self):
        for method, path in (("do_POST", "/captures"), ("do_GET", f"/captures/{self.capture_id}"),
                             ("do_PUT", f"/captures/{self.capture_id}/batches/0")):
            with self.subTest(method=method):
                handler = self.handler(path, headers={"Origin": "https://example.invalid"})
                with patch.object(server, "capture_store") as store:
                    getattr(handler, method)()
                    handler.send_json.assert_called_once_with(403, {"error": "Local extension requests only"})
                    store.assert_not_called()
                    self.assertEqual(handler.rfile.tell(), 0)

    def test_abort_route_persists_safe_diagnostics_and_empty_retry_keeps_them(self):
        self.create()
        self.assertEqual(self.put(0, b"saved")[0], 200)
        self.assertEqual(self.post(f"/captures/{self.capture_id}/timing", {
            "recording_started_at_ms": 100000, "playback_started_at_ms": 100100,
            "playback_start_video_sec": 60.01,
        })[0], 200)
        failure = {
            "code": "identity_changed", "message": "视频页面已切换，采集已中断。",
            "at_ms": 103900, "video_sec": 0,
            "identity_changed": True, "video_replaced": False, "ad_showing": False,
            "video_connected": True, "player_connected": True,
        }
        status, result = self.post(f"/captures/{self.capture_id}/abort", {
            "failure": failure, "recording_stopped_at_ms": 104000, "playback_end_video_sec": 0,
            "playback_events": [{"type": "playing", "at_ms": 100100, "video_sec": 60.01}],
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["failure"], failure)
        self.assertEqual(result["timing"]["playback_end_video_sec"], 0)
        self.assertEqual(result["received_batches"], 1)
        self.assertNotIn("audio_url", result)
        self.assertEqual(self.post(f"/captures/{self.capture_id}/abort", {}), (200, result))
        self.assertFalse((self.root / "captures" / self.capture_id / "recording.webm").exists())

    def test_abort_route_rejects_private_or_unapproved_diagnostic_fields(self):
        self.create()
        for failure in (
            {"code": "audio_capture_failed", "message": "https://example.invalid"},
            {"code": "audio_capture_failed", "message": "采集失败。", "source_url": "hidden"},
        ):
            with self.subTest(failure=failure):
                status, result = self.post(f"/captures/{self.capture_id}/abort", {"failure": failure})
                self.assertEqual(status, 400)
                self.assertNotIn("source_url", result)
                self.assertNotIn("https://", result["error"])

    def test_invalid_batch_headers_and_oversized_requests_are_rejected(self):
        self.create()
        handler = self.handler(f"/captures/{self.capture_id}/batches/0", body=b"audio", headers={
            "Content-Type": "audio/webm", "X-Capture-Start-Ms": "bad", "X-Capture-End-Ms": "1000",
        })
        handler.do_PUT()
        self.assertEqual(handler.send_json.call_args.args[0], 400)
        handler = self.handler("/captures", headers={"Content-Length": "999999999"})
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], 413)
        self.assertEqual(handler.rfile.tell(), 0)

    def test_deeply_nested_json_and_cross_site_media_requests_are_rejected(self):
        handler = self.handler("/captures", body=b'{"nested":' * 1100 + b"0" + b"}" * 1100)
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], 400)
        handler = self.handler(f"/captures/{self.capture_id}/audio", headers={
            "Origin": "", "Sec-Fetch-Site": "cross-site",
        })
        with patch.object(server, "capture_store") as store:
            handler.do_GET()
            self.assertEqual(handler.send_json.call_args.args[0], 403)
            store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
