"""Offline storage tests; fake bytes test integrity, not WebM decodability."""

import hashlib
import json
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import capture_store
from capture_store import CaptureError, CaptureStore


CAPTURE_ID = "00000000-0000-4000-8000-000000000001"
EPOCH = 1_700_000_000_000


class CaptureStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "captures"
        self.store = CaptureStore(self.root)
        self.creation = {
            "id": CAPTURE_ID,
            "source_url": "https://www.youtube.com/watch?v=AbCdEf012_-",
            "video_duration_sec": 7_200,
            "range_start_sec": 30,
            "range_end_sec": 40,
            "batch_interval_ms": 5_000,
            "mime_type": "audio/webm;codecs=opus",
        }
        self.timing = {
            "recording_started_at_ms": EPOCH,
            "playback_started_at_ms": EPOCH + 120,
            "playback_start_video_sec": 30.01,
        }
        self.finish = {
            "expected_batches": 2,
            "recording_stopped_at_ms": EPOCH + 10_300,
            "playback_end_video_sec": 40.02,
        }

    def start(self):
        self.store.create(self.creation)
        self.store.set_timing(CAPTURE_ID, self.timing)

    def metadata(self, body, sequence=0, **changes):
        metadata = {
            "sha256": hashlib.sha256(body).hexdigest(),
            "capture_start_ms": sequence * 5_000 + (0.4 if sequence else 0),
            "capture_end_ms": (sequence + 1) * 5_000 + 0.4,
            "blob_timecode_ms": sequence * 5_001.3,
        }
        metadata.update(changes)
        return metadata

    def put(self, sequence, body=b"fragment", **changes):
        return self.store.put_batch(CAPTURE_ID, sequence, body, self.metadata(body, sequence, **changes))

    def assert_error(self, status, action):
        with self.assertRaises(CaptureError) as raised:
            action()
        self.assertEqual(raised.exception.status, status)
        return raised.exception

    def test_create_is_idempotent_and_source_can_exceed_selected_limit(self):
        first = self.store.create(self.creation)
        second = self.store.create(dict(self.creation))
        self.assertEqual(first, second)
        self.assertEqual(first["video_duration_sec"], 7_200)
        self.assertIsNone(first["expected_batches"])
        self.assertEqual(first["batches"], [])
        self.assert_error(409, lambda: self.store.create({**self.creation, "range_end_sec": 50}))

    def test_out_of_order_batches_are_concatenated_by_sequence(self):
        self.start()
        self.put(1, b"second")
        first_ack = self.put(0, b"container-header")
        self.assertEqual(first_ack["received_batches"], 2)
        self.assertFalse(first_ack["duplicate"])
        manifest = self.store.complete(CAPTURE_ID, self.finish)
        expected = b"container-headersecond"
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["audio_sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(manifest["audio_size_bytes"], len(expected))
        self.assertEqual(manifest["total_bytes"], len(expected))
        self.assertEqual(manifest["audio_url"], f"/captures/{CAPTURE_ID}/audio")
        self.assertEqual([batch["sequence"] for batch in manifest["batches"]], [0, 1])
        self.assertEqual(manifest["batches"][1]["capture_start_ms"], 5_000.4)
        self.assertEqual(manifest["batches"][1]["blob_timecode_ms"], 5_001.3)
        self.assertEqual(self.store.audio_path(CAPTURE_ID).read_bytes(), expected)
        self.assertEqual(CaptureStore(self.root).get(CAPTURE_ID), manifest)

    def test_duplicate_batch_preserves_counters_and_conflicting_reuse_is_rejected(self):
        self.start()
        first = self.put(0, b"first")
        duplicate = self.put(0, b"first")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["total_bytes"], first["total_bytes"])
        self.assertEqual(duplicate["received_batches"], 1)
        self.assert_error(409, lambda: self.put(0, b"other"))
        self.assert_error(409, lambda: self.put(0, b"first", capture_end_ms=4_999))
        self.assertEqual(self.store.get(CAPTURE_ID)["total_bytes"], 5)

    def test_missing_declaration_persists_and_new_instance_accepts_missing_batch(self):
        self.start()
        self.put(1, b"second")
        error = self.assert_error(409, lambda: self.store.complete(CAPTURE_ID, self.finish))
        self.assertEqual(error.details, {"missing_sequences": [0]})
        self.store = CaptureStore(self.root)
        manifest = self.store.get(CAPTURE_ID)
        self.assertEqual(manifest["expected_batches"], 2)
        self.assertEqual(manifest["missing_sequences"], [0])
        self.assert_error(409, lambda: self.put(2))
        self.assert_error(409, lambda: self.store.complete(CAPTURE_ID, {**self.finish, "expected_batches": 3}))
        self.put(0, b"first")
        self.assertEqual(self.store.get(CAPTURE_ID)["missing_sequences"], [])
        self.assertEqual(self.store.complete(CAPTURE_ID, self.finish)["status"], "complete")

    def test_completion_is_idempotent_but_closed_capture_rejects_new_work(self):
        self.start()
        self.put(0)
        self.put(1)
        complete = self.store.complete(CAPTURE_ID, self.finish)
        self.assertEqual(self.store.complete(CAPTURE_ID, dict(self.finish)), complete)
        self.assertTrue(self.put(0)["duplicate"])
        self.assert_error(409, lambda: self.put(2))
        self.assert_error(409, lambda: self.store.complete(CAPTURE_ID, {**self.finish, "playback_end_video_sec": 40.1}))
        self.assert_error(409, lambda: self.store.abort(CAPTURE_ID))

    def test_extra_sequences_prevent_completion(self):
        self.start()
        self.put(0)
        self.put(2)
        error = self.assert_error(409, lambda: self.store.complete(CAPTURE_ID, self.finish))
        self.assertEqual(error.details["unexpected_sequences"], [2])
        self.assertNotEqual(self.store.get(CAPTURE_ID)["status"], "complete")

    def test_abort_is_persistent_idempotent_and_preserves_acknowledged_data(self):
        self.start()
        self.put(0, b"saved")
        manifest = self.store.abort(CAPTURE_ID)
        self.assertEqual(manifest["status"], "aborted")
        self.assertEqual(self.store.abort(CAPTURE_ID), manifest)
        self.assertEqual(CaptureStore(self.root).get(CAPTURE_ID), manifest)
        self.assertEqual((self.root / CAPTURE_ID / "batches/000000.webm").read_bytes(), b"saved")
        self.assertTrue(self.put(0, b"saved")["duplicate"])
        self.assert_error(409, lambda: self.put(1))
        self.assert_error(409, lambda: self.store.complete(CAPTURE_ID, self.finish))
        self.assert_error(409, lambda: self.store.audio_path(CAPTURE_ID))

    def test_abort_persists_safe_failure_and_observations_without_completing_audio(self):
        self.start()
        self.put(0, b"saved")
        failure = {
            "code": "advertisement_started", "message": "检测到广告，采集已中断。",
            "at_ms": EPOCH + 5_200, "video_sec": 0,
            "identity_changed": False, "video_replaced": False, "ad_showing": True,
            "video_connected": True, "player_connected": True,
        }
        events = [
            {"type": "playing", "at_ms": EPOCH + 120, "video_sec": 30.01},
            {"type": "playing", "at_ms": EPOCH + 5_100, "video_sec": 0},
        ]
        payload = {
            "failure": failure, "recording_stopped_at_ms": EPOCH + 5_300,
            "playback_end_video_sec": 0, "playback_events": events,
        }
        with patch.object(self.store, "_assemble", side_effect=AssertionError("Abort must not assemble audio")):
            manifest = self.store.abort(CAPTURE_ID, payload)
        self.assertEqual(manifest["status"], "aborted")
        self.assertEqual(manifest["failure"], failure)
        self.assertEqual(manifest["timing"]["playback_events"], events)
        self.assertEqual(manifest["timing"]["playback_end_video_sec"], 0)
        self.assertEqual(manifest["received_batches"], 1)
        self.assertNotIn("audio_url", manifest)
        self.assertFalse((self.root / CAPTURE_ID / "recording.webm").exists())
        self.assertEqual(CaptureStore(self.root).get(CAPTURE_ID), manifest)
        self.assertEqual(self.store.abort(CAPTURE_ID, payload), manifest)
        self.assertEqual(self.store.abort(CAPTURE_ID, {}), manifest)
        later = {
            "failure": {"code": "user_cancelled", "message": "用户中断采集。", "at_ms": EPOCH + 6_000},
            "recording_stopped_at_ms": EPOCH + 6_100, "playback_end_video_sec": 31,
        }
        self.assertEqual(self.store.abort(CAPTURE_ID, later), manifest)

    def test_abort_before_start_timing_can_save_and_later_fill_diagnostics(self):
        self.store.create(self.creation)
        first = self.store.abort(CAPTURE_ID, {"recording_stopped_at_ms": EPOCH + 10})
        self.assertEqual(CaptureStore(self.root).get(CAPTURE_ID), first)
        later = self.store.abort(CAPTURE_ID, {
            "failure": {"code": "audio_capture_failed", "message": "浏览器音频采集中断。", "at_ms": EPOCH},
            "playback_end_video_sec": 30, "playback_events": [],
        })
        self.assertEqual(later["timing"]["recording_stopped_at_ms"], EPOCH + 10)
        self.assertEqual(later["failure"]["code"], "audio_capture_failed")
        self.assertEqual(later["timing"]["playback_events"], [])
        self.assertEqual(CaptureStore(self.root).get(CAPTURE_ID), later)

    def test_failure_detected_after_recording_stops_is_persisted(self):
        self.start()
        payload = {
            "failure": {
                "code": "audio_capture_failed", "message": "录音保存确认失败。",
                "at_ms": EPOCH + 20_000,
            },
            "recording_stopped_at_ms": EPOCH + 10_300,
            "playback_end_video_sec": 40.02,
            "playback_events": [{"type": "playing", "at_ms": EPOCH + 120, "video_sec": 30.01}],
        }
        manifest = self.store.abort(CAPTURE_ID, payload)
        self.assertEqual(manifest["status"], "aborted")
        self.assertEqual(manifest["failure"]["at_ms"], EPOCH + 20_000)
        self.assertEqual(manifest["timing"]["recording_stopped_at_ms"], EPOCH + 10_300)
        self.assertEqual(CaptureStore(self.root).get(CAPTURE_ID), manifest)

    def test_failure_observation_cannot_precede_recording_start(self):
        self.start()
        self.assert_error(400, lambda: self.store.abort(CAPTURE_ID, {
            "failure": {"code": "audio_capture_failed", "message": "采集失败。", "at_ms": EPOCH - 1},
            "recording_stopped_at_ms": EPOCH + 10_300,
        }))
        self.assertEqual(self.store.get(CAPTURE_ID)["status"], "recording")

    def test_abort_rejects_unapproved_diagnostics_without_mutating_state(self):
        self.start()
        good = {"code": "video_replaced", "message": "播放器已替换，采集已中断。"}
        invalid = [
            {"source_url": "https://example.invalid"}, {"failure": {**good, "source_url": "hidden"}},
            {"failure": {**good, "code": []}}, {"failure": {**good, "code": "VIDEO_CHANGED"}},
            {"failure": {**good, "code": "x" * 65}}, {"failure": {**good, "message": []}},
            {"failure": {**good, "message": "x" * 241}},
            {"failure": {**good, "message": "https://example.invalid"}},
            {"failure": {**good, "message": "example.invalid"}},
            {"failure": {**good, "message": "/tmp/private-data"}},
            {"failure": {**good, "message": "C:\\private-data"}},
            {"failure": {**good, "message": "operator@example.invalid"}},
            {"failure": {**good, "message": "private\ntext"}},
            {"failure": {**good, "video_connected": "true"}},
            {"failure": {**good, "player_connected": 1}},
            {"failure": {**good, "at_ms": True}},
            {"failure": {**good, "video_sec": float("nan")}},
            {"failure": {**good, "video_sec": 7_202}},
            {"recording_stopped_at_ms": EPOCH},
            {"playback_end_video_sec": -1}, {"playback_end_video_sec": float("inf")},
            {"playback_events": [{"type": "waiting", "at_ms": EPOCH + 500, "video_sec": 30}]},
            {"playback_events": [{"type": "playing", "at_ms": EPOCH - 1, "video_sec": 30}]},
            {"playback_events": [{"type": "playing", "at_ms": EPOCH + 500, "video_sec": 30}],
             "recording_stopped_at_ms": EPOCH + 300},
            {"playback_events": [{}] * (capture_store.MAX_PLAYBACK_EVENTS + 1)},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assert_error(400, lambda: self.store.abort(CAPTURE_ID, payload))
                self.assertEqual(self.store.get(CAPTURE_ID)["status"], "recording")
                self.assertNotIn("failure", self.store.get(CAPTURE_ID))
        self.assert_error(400, lambda: self.store.abort(CAPTURE_ID, []))

    def test_invalid_creation_ranges_and_numbers_are_rejected(self):
        invalid = [
            {"range_start_sec": True}, {"video_duration_sec": float("inf")},
            {"range_end_sec": float("nan")}, {"range_start_sec": -1},
            {"range_end_sec": 30.5}, {"range_end_sec": 1_831},
            {"range_end_sec": 8_000}, {"batch_interval_ms": 999},
            {"batch_interval_ms": 30_001}, {"batch_interval_ms": True},
            {"mime_type": "audio/ogg"}, {"mime_type": []}, {"mime_type": {}},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assert_error(400, lambda: self.store.create({**self.creation, **changes}))

    def test_invalid_source_urls_are_rejected_without_returning_them_in_errors(self):
        invalid = [
            "https://example.invalid/watch?v=AbCdEf012_-",
            "http://www.youtube.com/watch?v=AbCdEf012_-",
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/watch?v=AbCdEf012_-&v=AbCdEf012_-",
            "https://www.youtube.com/shorts/AbCdEf012_-/extra",
            "https://operator:secret" + "@" + "www.youtube.com/watch?v=AbCdEf012_-",
            "https://www.youtube.com:123/watch?v=AbCdEf012_-",
        ]
        for url in invalid:
            with self.subTest(url=url):
                error = self.assert_error(400, lambda: self.store.create({**self.creation, "source_url": url}))
                self.assertNotIn(url, error.error)
        good = {**self.creation, "source_url": "https://www.youtube.com/shorts/AbCdEf012_-?feature=share"}
        self.assertEqual(self.store.create(good)["source_url"], good["source_url"])
        trailing = {**good, "id": "00000000-0000-4000-8000-000000000002",
                    "source_url": "https://www.youtube.com/shorts/AbCdEf012_-/"}
        self.assertEqual(self.store.create(trailing)["source_url"], trailing["source_url"])

    def test_path_traversal_and_alias_identifiers_are_rejected(self):
        for identifier in ("../elsewhere", "/tmp/elsewhere", "", "not-a-uuid", True, CAPTURE_ID.replace("-", "")):
            with self.subTest(identifier=identifier):
                self.assert_error(400, lambda: self.store.get(identifier))
                self.assert_error(400, lambda: self.store.create({**self.creation, "id": identifier}))
        self.assert_error(404, lambda: self.store.get(CAPTURE_ID))

    def test_start_timing_is_observed_idempotent_and_cannot_change(self):
        self.store.create(self.creation)
        manifest = self.store.set_timing(CAPTURE_ID, self.timing)
        self.assertEqual(manifest["timing"], self.timing)
        self.assertEqual(self.store.set_timing(CAPTURE_ID, dict(self.timing)), manifest)
        self.assert_error(409, lambda: self.store.set_timing(CAPTURE_ID, {**self.timing, "playback_start_video_sec": 30.02}))

    def test_missing_and_invalid_start_timing_prevent_completion(self):
        self.store.create(self.creation)
        self.assert_error(409, lambda: self.store.complete(CAPTURE_ID, self.finish))
        for changes in (
            {"recording_started_at_ms": True}, {"playback_started_at_ms": float("nan")},
            {"playback_started_at_ms": EPOCH - 1}, {"playback_start_video_sec": 20},
        ):
            with self.subTest(changes=changes):
                self.assert_error(400, lambda: self.store.set_timing(CAPTURE_ID, {**self.timing, **changes}))

    def test_batch_checksums_timing_and_counts_are_validated(self):
        self.start()
        for changes in (
            {"sha256": "0" * 64}, {"sha256": "not-a-hash"},
            {"capture_start_ms": True}, {"capture_end_ms": float("inf")},
            {"capture_start_ms": 6_000, "capture_end_ms": 5_000},
            {"blob_timecode_ms": float("nan")},
        ):
            with self.subTest(changes=changes):
                self.assert_error(400, lambda: self.put(0, b"bytes", **changes))
        for sequence in (True, -1, 1.0, capture_store.MAX_BATCHES):
            self.assert_error(400, lambda: self.put(sequence))
        self.assert_error(400, lambda: self.put(0, b""))
        self.put(1)
        self.assert_error(409, lambda: self.put(0, capture_end_ms=6_000))

    def test_single_and_total_size_limits_do_not_write_rejected_batches(self):
        self.start()
        with patch.object(capture_store, "MAX_BATCH_BYTES", 5):
            self.assert_error(413, lambda: self.put(0, b"123456"))
        with patch.object(capture_store, "MAX_TOTAL_BYTES", 5):
            self.put(0, b"1234")
            self.assert_error(413, lambda: self.put(1, b"56"))
        manifest = self.store.get(CAPTURE_ID)
        self.assertEqual(manifest["received_batches"], 1)
        self.assertEqual(manifest["total_bytes"], 4)
        self.assertFalse((self.root / CAPTURE_ID / "batches/000001.webm").exists())

    def test_invalid_completion_fields_and_batch_duration_are_rejected(self):
        self.start()
        self.put(0)
        for changes in (
            {"expected_batches": True}, {"expected_batches": 0},
            {"recording_stopped_at_ms": EPOCH - 1}, {"playback_end_video_sec": float("nan")},
            {"playback_end_video_sec": 35}, {"playback_end_video_sec": 42},
            {"recording_stopped_at_ms": EPOCH + 150},
        ):
            with self.subTest(changes=changes):
                self.assert_error(400, lambda: self.store.complete(CAPTURE_ID, {**self.finish, **changes}))

    def test_playback_events_are_preserved_and_part_of_completion_identity(self):
        self.start()
        self.put(0)
        self.put(1)
        events = [
            {"type": "playing", "at_ms": EPOCH + 120, "video_sec": 30.01},
            {"type": "waiting", "at_ms": EPOCH + 3_000, "video_sec": 32.8},
            {"type": "playing", "at_ms": EPOCH + 3_400, "video_sec": 32.8},
        ]
        finish = {**self.finish, "playback_events": events}
        manifest = self.store.complete(CAPTURE_ID, finish)
        self.assertEqual(manifest["timing"]["playback_events"], events)
        self.assertEqual(self.store.complete(CAPTURE_ID, finish), manifest)
        self.assert_error(409, lambda: self.store.complete(CAPTURE_ID, self.finish))

    def test_invalid_playback_events_are_rejected(self):
        self.start()
        invalid = [
            [{"type": "waiting", "at_ms": EPOCH + 1, "video_sec": 30}],
            [{"type": "playing", "at_ms": EPOCH - 1, "video_sec": 30}],
            [{"type": "playing", "at_ms": EPOCH + 20_000, "video_sec": 30}],
            [{"type": "playing", "at_ms": EPOCH + 120, "video_sec": 99}],
            [{"type": [], "at_ms": EPOCH + 120, "video_sec": 30}],
            [{"type": {}, "at_ms": EPOCH + 120, "video_sec": 30}],
            [{"type": "playing", "at_ms": True, "video_sec": 30}],
            [{"type": "playing", "at_ms": EPOCH + 120, "video_sec": 30},
             {"type": "waiting", "at_ms": EPOCH + 119, "video_sec": 30}],
        ]
        for events in invalid:
            with self.subTest(events=events):
                self.assert_error(400, lambda: self.store.complete(CAPTURE_ID, {**self.finish, "playback_events": events}))

    def test_corrupted_batch_is_not_acknowledged_or_marked_complete(self):
        self.start()
        self.put(0, b"first")
        self.put(1, b"second")
        (self.root / CAPTURE_ID / "batches/000000.webm").write_bytes(b"other")
        self.assert_error(409, lambda: self.put(0, b"first"))
        self.assert_error(409, lambda: self.store.complete(CAPTURE_ID, self.finish))
        self.assertEqual(self.store.get(CAPTURE_ID)["status"], "recording")
        self.assertFalse((self.root / CAPTURE_ID / "recording.webm").exists())
        self.assertEqual(list((self.root / CAPTURE_ID).glob(".recording.webm.*")), [])

    def test_bytes_are_saved_before_manifest_ack_and_failed_save_can_be_retried(self):
        self.start()
        persist = self.store._persist
        def observed_persist(manifest):
            self.assertEqual((self.root / CAPTURE_ID / "batches/000000.webm").read_bytes(), b"first")
            persist(manifest)
        with patch.object(self.store, "_persist", side_effect=OSError("simulated write failure")):
            with self.assertRaises(OSError):
                self.put(0, b"first")
        self.assertEqual(self.store.get(CAPTURE_ID)["received_batches"], 0)
        with patch.object(self.store, "_persist", side_effect=observed_persist):
            self.assertTrue(self.put(0, b"first")["stored"])
        self.assertEqual(CaptureStore(self.root).get(CAPTURE_ID)["received_batches"], 1)

    def test_private_directory_and_file_permissions(self):
        self.start()
        self.put(0)
        self.put(1)
        self.store.complete(CAPTURE_ID, self.finish)
        directory = self.root / CAPTURE_ID
        for path in (self.root, directory, directory / "batches"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (directory / "manifest.json", directory / "recording.webm", directory / "batches/000000.webm"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_capture_and_batch_symlinks_are_rejected(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (self.root / CAPTURE_ID).symlink_to(outside, target_is_directory=True)
        self.assert_error(409, lambda: self.store.create(self.creation))
        (self.root / CAPTURE_ID).unlink()
        self.start()
        (self.root / CAPTURE_ID / "batches/000000.webm").symlink_to(outside / "secret")
        self.assert_error(409, lambda: self.put(0))
        self.assertFalse((outside / "secret").exists())

    def test_parallel_duplicate_uploads_are_counted_once(self):
        self.start()
        with ThreadPoolExecutor(max_workers=4) as pool:
            acknowledgements = list(pool.map(lambda _: self.put(0, b"one"), range(8)))
        self.assertEqual(sum(not ack["duplicate"] for ack in acknowledgements), 1)
        self.assertEqual(self.store.get(CAPTURE_ID)["total_bytes"], 3)

    def test_truncated_manifest_raises_a_safe_error(self):
        self.start()
        path = self.root / CAPTURE_ID / "manifest.json"
        path.write_text("{", encoding="utf-8")
        error = self.assert_error(409, lambda: self.store.get(CAPTURE_ID))
        self.assertNotIn(str(path), error.error)

    def test_damaged_manifest_fields_are_rejected_before_use(self):
        self.start()
        path = self.root / CAPTURE_ID / "manifest.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        for changes in (
            {"expected_batches": 10**12}, {"status": []},
            {"batches": [{}]}, {"total_bytes": 1}, {"mime_type": {}},
            {"timing": {"recording_started_at_ms": EPOCH}},
        ):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**original, **changes}), encoding="utf-8")
                self.assert_error(409, lambda: self.store.get(CAPTURE_ID))

    def test_batch_directory_symlink_cannot_redirect_duplicate_reads(self):
        self.start()
        self.put(0, b"saved")
        batches = self.root / CAPTURE_ID / "batches"
        moved = self.root / CAPTURE_ID / "moved"
        batches.rename(moved)
        batches.symlink_to(moved, target_is_directory=True)
        self.assert_error(409, lambda: self.put(0, b"saved"))


if __name__ == "__main__":
    unittest.main()
