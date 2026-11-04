"""Local learning routes: fake providers and temporary storage, no cloud or sockets."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import server


CAPTURE_ID = "00000000-0000-4000-8000-000000000001"
SECOND_ID = "00000000-0000-4000-8000-000000000002"
VIDEO_URL = "https://www.youtube.com/watch?v=AbCdEf012_-"


class LearningHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (("DATA_DIR", self.root), ("setting", lambda _name, default=None: default)):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(server.capture_store.cache_clear)

    def handler(self, path, payload=None, origin=None):
        handler = object.__new__(server.Handler)
        handler.path = path
        body = json.dumps({} if payload is None else payload).encode()
        handler.headers = {
            "Host": "127.0.0.1:8766", "Origin": origin or "chrome-extension://" + "a" * 32,
            "Content-Type": "application/json", "Content-Length": str(len(body)),
        }
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_json = Mock()
        return handler

    def create(self, capture_id=CAPTURE_ID, source_url=VIDEO_URL):
        handler = self.handler("/captures", {
            "id": capture_id, "source_url": source_url, "video_duration_sec": 600,
            "range_start_sec": 60, "range_end_sec": 90,
            "batch_interval_ms": 5000, "mime_type": "audio/webm;codecs=opus",
        })
        handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], 200)
        return self.root / "captures" / capture_id

    def test_paid_start_requires_separate_opt_in_and_does_not_retranscribe(self):
        self.create()
        path = f"/captures/{CAPTURE_ID}/learning"
        with patch("capture_learning.request_learning") as request, patch.object(server, "wake_transcription") as google:
            handler = self.handler(path)
            handler.do_POST()
            self.assertEqual(handler.send_json.call_args.args[0], 409)
            request.assert_not_called()
            with patch.object(server, "learning_enabled", return_value=True):
                handler = self.handler(path)
                handler.do_POST()
            self.assertEqual(handler.send_json.call_args.args[0], 202)
            request.assert_called_once()
            google.assert_not_called()

    def test_health_advertises_learning_only_when_explicitly_enabled(self):
        with patch.object(server, "learning_enabled", return_value=True):
            handler = self.handler("/health")
            handler.do_GET()
        capabilities = handler.send_json.call_args.args[1]["capabilities"]
        self.assertTrue(capabilities["capture_enrichment"])
        self.assertTrue(capabilities["cloud_processing"])
        self.assertFalse(capabilities["capture_transcription"])

    def test_learning_start_does_not_accept_browser_retry_parameters(self):
        self.create()
        with patch.object(server, "learning_enabled", return_value=True), patch("capture_learning.request_learning") as request:
            handler = self.handler(f"/captures/{CAPTURE_ID}/learning", {"retry_batch": 0})
            handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], 400)
        request.assert_not_called()

    def test_capture_cancellation_disables_new_learning_dispatches(self):
        directory = self.create()
        with patch.object(server, "learning_enabled", return_value=True), patch("capture_learning.request_learning") as request:
            server.wake_learning(directory)
            enabled = request.call_args.kwargs["enabled"]
            self.assertTrue(enabled())
            server.capture_store(self.root / "captures").abort(CAPTURE_ID, {})
            self.assertFalse(enabled(), "Cancelling recording must prevent the next paid learning request")

    def test_interrupted_capture_requires_separate_inspection(self):
        self.create()
        server.capture_store(self.root / "captures").abort(CAPTURE_ID, {})
        with patch.object(server, "learning_enabled", return_value=True), patch("capture_learning.request_learning") as request:
            handler = self.handler(f"/captures/{CAPTURE_ID}/learning")
            handler.do_POST()
        self.assertEqual(handler.send_json.call_args.args[0], 409)
        request.assert_not_called()

    def test_status_and_latest_reads_never_schedule_cloud_work(self):
        self.create()
        with patch("capture_learning.request_learning") as request:
            handler = self.handler(f"/captures/{CAPTURE_ID}/learning")
            handler.do_GET()
            self.assertEqual(handler.send_json.call_args.args[0], 200)
            self.assertEqual(handler.send_json.call_args.args[1]["status"], "not_started")
            handler = self.handler("/learning/latest?url=" + VIDEO_URL)
            handler.do_GET()
            self.assertEqual(handler.send_json.call_args.args[0], 200)
            request.assert_not_called()

    def test_untrusted_origin_is_rejected_before_reading_or_scheduling(self):
        for method, path in (("do_GET", f"/captures/{CAPTURE_ID}/learning"),
                             ("do_POST", f"/captures/{CAPTURE_ID}/learning"),
                             ("do_GET", "/learning/latest?url=" + VIDEO_URL)):
            with self.subTest(method=method, path=path), patch.object(server, "capture_store") as store, \
                    patch.object(server, "latest_learning_for_url") as latest, patch.object(server, "wake_learning") as wake:
                handler = self.handler(path, origin="https://example.invalid")
                getattr(handler, method)()
                handler.send_json.assert_called_once_with(403, {"error": "Local extension requests only"})
                store.assert_not_called()
                latest.assert_not_called()
                wake.assert_not_called()

    def test_invalid_latest_lookup_does_not_scan_local_data(self):
        for query in ("", "url=https://example.invalid/private", "url=" + VIDEO_URL + "&extra=1"):
            with self.subTest(query=query), patch.object(server, "latest_learning_for_url") as latest:
                handler = self.handler("/learning/latest?" + query)
                handler.do_GET()
                self.assertEqual(handler.send_json.call_args.args[0], 400)
                latest.assert_not_called()

    def test_cached_notes_are_preferred_over_a_newer_unprocessed_capture(self):
        older = self.create()
        self.create(SECOND_ID)
        with patch("capture_learning.read_learning_status", return_value={"status": "complete"}), \
                patch("capture_learning.read_learning_result", side_effect=lambda directory: {
                    "sentences": [{"id": "s0001", "study": {"reading_text": "はい。"}}]
                } if directory == older else None), patch("capture_learning.request_learning") as request:
            saved = server.latest_learning_for_url(VIDEO_URL + "&t=80")
        self.assertEqual(saved["capture_id"], CAPTURE_ID)
        request.assert_not_called()

    def test_learning_error_messages_do_not_relay_private_provider_details(self):
        self.create()
        from capture_learning import LearningError
        with patch("capture_learning.read_learning_status", side_effect=LearningError("synthetic_private_error")):
            handler = self.handler(f"/captures/{CAPTURE_ID}/learning")
            handler.do_GET()
        self.assertEqual(handler.send_json.call_args.args, (500, {"error": "Local learning storage operation failed"}))


if __name__ == "__main__":
    unittest.main()
