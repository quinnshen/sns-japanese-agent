"""Temporary capture/library integration tests; no real files, network or credentials."""

import hashlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from unittest.mock import Mock, patch

import capture_learning
import capture_transcription
import learning_library as library_module
import server
from capture_store import CaptureStore
from learning_library import LearningLibrary, LibraryError, RETENTION_SECONDS

VIDEO_ID = "AbCdEf012_-"
SOURCE_URL = f"https://youtube.com/watch?v={VIDEO_ID}&t=30&feature=share#unrelated"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
NOW = 1_800_000_000


def identifier(index):
    return f"00000000-0000-4000-8000-{index:012d}"


class SyntheticProvider:
    config = {"model": "offline-fixture", "style_version": 2}
    provider_fingerprint = capture_learning.fingerprint(config)

    def prepare(self):
        pass

    def generate(self, batch):
        return {"status": "completed", "items": [
            {"id": item["id"], "reading_text": "食(た)べる。", "translation_zh": "吃。",
             "key_points": ["「食(た)べる」：表示吃。"], "context_note": "", "asr_doubt": ""}
            for item in batch["targets"]], "usage": {"input_tokens": 0, "output_tokens": 0}}


class LearningLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CaptureStore(self.root / "captures")
        self.library = LearningLibrary(self.root, store_factory=lambda _root: self.store, clock=lambda: NOW)

    def create_capture(self, index, *, start=30, end=40, status="complete", notes=False):
        capture_id = identifier(index)
        self.store.create({"id": capture_id, "source_url": SOURCE_URL, "video_duration_sec": 600,
                           "range_start_sec": start, "range_end_sec": end,
                           "batch_interval_ms": 5000, "mime_type": "audio/webm"})
        if status == "complete":
            self.store.set_timing(capture_id, {
                "recording_started_at_ms": 100_000, "playback_started_at_ms": 100_100,
                "playback_start_video_sec": start,
            })
            body = b"synthetic audio fragment"
            self.store.put_batch(capture_id, 0, body, {
                "sha256": hashlib.sha256(body).hexdigest(), "capture_start_ms": 0, "capture_end_ms": 1000,
            })
            self.store.complete(capture_id, {"expected_batches": 1,
                                              "recording_stopped_at_ms": 100_200 + (end - start) * 1000,
                                              "playback_end_video_sec": end})
        elif status == "aborted":
            self.store.abort(capture_id)
        directory = self.root / "captures" / capture_id
        if notes:
            self.persist_notes(directory, start)
        return directory

    def persist_notes(self, directory, start):
        transcription = directory / "transcription"
        transcription.mkdir(mode=0o700)
        source = {"status": "transcribed", "sentences": [
            {"id": "s0001", "text": "食べる。", "start": start, "end": start + 1,
             "audio_start": 0, "audio_end": 1, "words": ["synthetic private words"]},
        ], "raw_transcripts": ["synthetic private response"]}
        (transcription / "result.json").write_text(json.dumps(source), encoding="utf-8")
        (transcription / "status.json").write_text('{"status":"complete"}', encoding="utf-8")
        status = capture_learning.process_capture_learning(directory, run_openai=True, provider=SyntheticProvider())
        self.assertEqual(status["sentences_done"], 1)

    def age_capture(self, directory, *, age=RETENTION_SECONDS + 1):
        modified = NOW - age
        os.utime(directory / "manifest.json", (modified, modified))

    def favorite(self, index, saved=True):
        return {"capture_id": identifier(index), "sentence_id": "s0001", "saved": saved}

    def test_history_lists_each_completed_selection_with_accepted_notes(self):
        self.create_capture(1, start=30, end=40, notes=True)
        self.create_capture(2, start=70, end=90, notes=True)
        self.create_capture(3)
        self.create_capture(4, status="recording", notes=True)
        self.create_capture(5, status="aborted", notes=True)
        response = self.library.list()
        clips = {item["capture_id"]: item for item in response["clips"]}
        self.assertEqual(set(clips), {identifier(1), identifier(2)})
        self.assertEqual((clips[identifier(1)]["range_start_sec"], clips[identifier(2)]["range_start_sec"]), (30, 70))
        self.assertEqual(clips[identifier(2)]["source_url"], CANONICAL_URL)
        self.assertIsInstance(clips[identifier(2)]["updated_at"], float)
        self.assertEqual(clips[identifier(2)]["sentences_done"], 1)
        self.assertEqual(response["favorites"], [])
        self.assertFalse(self.library.favorite_root.exists(), "Read-only history should not initialize favorites")

    def test_favorite_snapshot_survives_expiry_and_duplicate_add_remove(self):
        capture = self.create_capture(1, notes=True)
        self.age_capture(capture)
        self.assertEqual(self.library.set_favorite(self.favorite(1)), {"saved": True})
        first = self.library.list()["favorites"][0]
        self.assertEqual(first["saved_at"], NOW)
        self.assertEqual(first["source_url"], CANONICAL_URL)
        self.assertEqual(first["sentence"]["text"], "食べる。")
        self.assertEqual(first["sentence"]["study"]["reading_text"], "食(た)べる。")
        self.assertNotIn("words", first["sentence"])
        self.assertEqual(stat.S_IMODE(self.library.favorite_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.library.favorite_root / "favorites.sqlite3").stat().st_mode), 0o600)
        self.assertEqual(self.library.cleanup_expired(), 1)
        self.assertFalse(capture.exists())
        reopened = LearningLibrary(self.root, clock=lambda: NOW + 5)
        self.assertEqual(reopened.set_favorite(self.favorite(1)), {"saved": True})
        self.assertEqual(reopened.list(), {"clips": [], "favorites": [first]})
        self.assertEqual(reopened.set_favorite(self.favorite(1, False)), {"saved": False})
        self.assertEqual(reopened.set_favorite(self.favorite(1, False)), {"saved": False})
        self.assertEqual(reopened.list(), {"clips": [], "favorites": []})

    def test_invalid_favorite_cannot_inject_client_notes_or_save_unaccepted_asr(self):
        self.create_capture(1)
        for payload in ({**self.favorite(1), "sentence": "spoofed"}, self.favorite(1, 1),
                        {**self.favorite(1), "capture_id": "../other"}):
            with self.subTest(payload=payload), self.assertRaises(LibraryError) as raised:
                self.library.set_favorite(payload)
            self.assertEqual(raised.exception.status, 400)
        with self.assertRaises(LibraryError) as raised:
            self.library.set_favorite(self.favorite(1))
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(self.library.list()["favorites"], [])

    def test_retention_keeps_recording_and_fresh_terminal_captures(self):
        old_complete = self.create_capture(1)
        old_abort = self.create_capture(2, status="aborted")
        active = self.create_capture(3, status="recording")
        fresh = self.create_capture(4)
        for directory in (old_complete, old_abort, active):
            self.age_capture(directory)
        self.age_capture(fresh, age=RETENTION_SECONDS - 1)
        legacy = self.root / "legacy.json"
        legacy.write_text("{}", encoding="utf-8")
        outside = self.root / "unrelated"
        outside.mkdir()
        (outside / "marker").write_text("protected", encoding="utf-8")
        (self.root / "captures" / identifier(5)).symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.library.cleanup_expired(), 2)
        self.assertFalse(old_complete.exists())
        self.assertFalse(old_abort.exists())
        self.assertTrue(active.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(legacy.exists())
        self.assertTrue((outside / "marker").exists())

    def test_retention_skips_active_scheduler_and_both_worker_locks(self):
        google = self.create_capture(1)
        learning = self.create_capture(2)
        queued = self.create_capture(3)
        idle = self.create_capture(4)
        for directory in (google, learning, queued, idle):
            self.age_capture(directory)
        with capture_transcription.job_lock(capture_transcription.private_root(google)), \
                capture_learning.job_lock(capture_learning.private_root(learning)), \
                patch.object(capture_learning, "_active", {str(queued.resolve())}):
            self.assertEqual(self.library.cleanup_expired(), 1)
            self.assertTrue(google.exists())
            self.assertTrue(learning.exists())
            self.assertTrue(queued.exists())
            self.assertFalse(idle.exists())
        self.assertEqual(self.library.cleanup_expired(), 3)

    def test_retention_waits_for_in_progress_favorite_snapshot(self):
        capture = self.create_capture(1, notes=True)
        self.age_capture(capture)
        entered, release = threading.Event(), threading.Event()
        original = library_module.read_learning_result
        def held_result(directory):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test did not release favorite reader")
            return original(directory)
        with patch.object(library_module, "read_learning_result", side_effect=held_result), ThreadPoolExecutor(max_workers=2) as pool:
            favorite = pool.submit(self.library.set_favorite, self.favorite(1))
            try:
                self.assertTrue(entered.wait(5))
                cleanup = pool.submit(self.library.cleanup_expired)
                with self.assertRaises(TimeoutError):
                    cleanup.result(timeout=0.05)
                self.assertTrue(capture.exists())
            finally:
                release.set()
            self.assertEqual(favorite.result(timeout=5), {"saved": True})
            self.assertEqual(cleanup.result(timeout=5), 1)
        self.assertEqual(len(self.library.list()["favorites"]), 1)

    def test_http_routes_return_private_library_and_clip_without_scheduling_cloud(self):
        self.create_capture(1, notes=True)
        self.create_capture(2, status="recording")
        def handler(path, payload=None, origin="chrome-extension://" + "a" * 32):
            value = object.__new__(server.Handler)
            value.path = path
            body = json.dumps(payload or {}).encode()
            value.headers = {"Host": "127.0.0.1:8766", "Origin": origin,
                             "Content-Type": "application/json", "Content-Length": str(len(body))}
            value.rfile = io.BytesIO(body)
            value.send_json = Mock()
            return value
        with patch.object(server, "DATA_DIR", self.root), patch.object(server, "learning_library", return_value=self.library), \
                patch.object(server, "capture_store", return_value=self.store), \
                patch.object(server, "wake_learning") as learning, patch.object(server, "wake_transcription") as google:
            request = handler(f"/captures/{identifier(2)}/learning")
            request.do_GET()
            code, result = request.send_json.call_args.args
            self.assertEqual(code, 200)
            self.assertEqual(result["status"], "not_started")
            self.assertIsNone(result["result"])
            self.assertEqual(result["clip"]["source_url"], CANONICAL_URL)
            self.assertEqual(result["clip"]["sentences_done"], 0)
            request = handler("/learning/favorites", self.favorite(1))
            request.do_POST()
            request.send_json.assert_called_once_with(200, {"saved": True})
            request = handler("/learning/library")
            request.do_GET()
            self.assertEqual(request.send_json.call_args.args[0], 200)
            self.assertEqual(len(request.send_json.call_args.args[1]["favorites"]), 1)
            for method, path in (("do_GET", "/learning/library"), ("do_POST", "/learning/favorites")):
                request = handler(path, origin="https://example.invalid")
                getattr(request, method)()
                request.send_json.assert_called_once_with(403, {"error": "Local extension requests only"})
            learning.assert_not_called()
            google.assert_not_called()


if __name__ == "__main__":
    unittest.main()
