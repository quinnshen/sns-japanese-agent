"""Offline recovery/cost tests with real PCM windows and fake recognition only.

No test loads local configuration or ADC, decodes personal recordings, or calls
a provider. Temporary PCM, databases and responses are deleted after each test.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import capture_transcription as workflow
import transcription_audio
from transcription_google import ProviderError, validate_audio


BACKEND = Path(__file__).resolve().parent
CAPTURE_ID = "00000000-0000-4000-8000-000000000001"


class SimulatedPowerLoss(BaseException):
    """Bypass normal Exception handling to leave durable in-flight state."""


class FakeRecognizer:
    def __init__(self, *, results=None, error=None, prepare_error=None, validation_error=None, counter_file=None):
        self.results = [] if results is None else results
        self.error = error
        self.prepare_error = prepare_error
        self.validation_error = validation_error
        self.counter_file = counter_file
        self.calls = []
        self.prepare_calls = 0

    def validate_audio(self, audio):
        if self.validation_error is not None:
            raise self.validation_error
        return validate_audio(audio)

    def prepare(self):
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        return self

    def recognize(self, audio):
        self.calls.append(validate_audio(audio))
        if self.counter_file is not None:
            with Path(self.counter_file).open("a", encoding="utf-8") as target:
                target.write("request\n")
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.results)


class CaptureTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.capture = Path(self.temporary.name) / "capture"
        self.capture.mkdir()
        self.pcm = self.capture / "fixture.pcm"
        self.duration = 2.0
        self.complete = True
        self.write_pcm(self.duration)
        # A regression that accidentally constructs the real provider must fail
        # before configuration/credentials can be read.
        self.provider_guard = patch("transcription_google.GoogleRecognizer", side_effect=AssertionError("Real provider forbidden in offline tests"))
        self.real_provider = self.provider_guard.start()
        self.addCleanup(self.provider_guard.stop)

    def write_pcm(self, seconds, *, byte_pair=b"\0\0"):
        self.duration = float(seconds)
        self.pcm.write_bytes(byte_pair * int(seconds * 16_000))

    def prepared(self, _capture=None):
        duration = self.duration
        timing = {
            "recording_started_at_ms": 1_700_000_000_000,
            "playback_started_at_ms": 1_700_000_000_000,
            "playback_start_video_sec": 30.0,
        }
        if self.complete:
            timing.update(recording_stopped_at_ms=1_700_000_000_000 + duration * 1000,
                          playback_end_video_sec=30.0 + duration)
        return {
            "ready": True, "duration_sec": duration, "complete": self.complete,
            "pcm_path": self.pcm,
            "manifest": {
                "id": CAPTURE_ID, "schema_version": 1,
                "status": "complete" if self.complete else "recording",
                "range_start_sec": 30.0, "range_end_sec": 30.0 + duration,
                "video_duration_sec": 600.0, "timing": timing,
            },
        }

    def run_capture(self, recognizer=None, **options):
        if recognizer is not None:
            options["recognizer"] = recognizer
        return workflow.process_capture(self.capture, prepare_audio=self.prepared, **options)

    def sql(self, query):
        connection = sqlite3.connect(self.capture / "transcription" / "state.sqlite3")
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(query)]
        finally:
            connection.close()

    def windows(self):
        return self.sql("SELECT * FROM windows ORDER BY window_index")

    def attempts(self):
        return self.sql("SELECT * FROM attempts ORDER BY window_index,attempt")

    def test_default_offline_preparation_never_constructs_or_calls_provider(self):
        fake = FakeRecognizer()
        status = self.run_capture(fake)
        self.assertEqual(status["status"], "prepared")
        self.assertEqual(status["new_requests"], 0)
        self.assertEqual(status["windows_total"], 1)
        self.assertEqual(fake.calls, [])
        self.assertEqual(fake.prepare_calls, 0)
        self.real_provider.assert_not_called()
        self.assertEqual(self.windows()[0]["status"], "queued")
        self.assertEqual(self.attempts(), [])

    def test_successful_windows_are_reused_in_a_fresh_process(self):
        self.write_pcm(90)
        prepared = self.prepared()
        prepared.pop("pcm_path")
        (self.capture / "fixture.json").write_text(json.dumps(prepared), encoding="utf-8")
        code = """
import json
from pathlib import Path
import sys
from unittest.mock import patch
import capture_transcription as workflow
from test_capture_transcription import FakeRecognizer
capture = Path(sys.argv[1])
prepared = json.loads((capture / 'fixture.json').read_text())
prepared['pcm_path'] = capture / 'fixture.pcm'
fake = FakeRecognizer(counter_file=capture / 'request-count.txt')
with patch('transcription_google.GoogleRecognizer', side_effect=AssertionError('Real provider forbidden')):
    status = workflow.process_capture(capture, run_google=True, recognizer=fake, prepare_audio=lambda _: prepared)
assert status['status'] == 'complete'
"""
        for run in range(2):
            result = subprocess.run([sys.executable, "-c", code, str(self.capture)], cwd=BACKEND,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, "Fresh-process offline worker failed")
            self.assertEqual((self.capture / "request-count.txt").read_text().splitlines(), ["request"] * 3)
        self.assertEqual([row["attempts"] for row in self.windows()], [1, 1, 1])

    def test_timeout_becomes_unknown_and_normal_rerun_never_reissues_it(self):
        failed = FakeRecognizer(error=ProviderError("google_deadline_unknown", "unknown"))
        status = self.run_capture(failed, run_google=True)
        self.assertEqual(status["status"], "needs_attention")
        self.assertEqual(len(failed.calls), 1)
        self.assertEqual(self.windows()[0]["status"], "unknown")
        other = FakeRecognizer()
        status = self.run_capture(other, run_google=True)
        self.assertEqual(status["new_requests"], 0)
        self.assertEqual(other.calls, [])
        with self.assertRaises(workflow.TranscriptionError) as caught:
            self.run_capture(other, run_google=True, retry_window=0)
        self.assertEqual(caught.exception.code, "duplicate_cost_confirmation_required")
        self.assertEqual(other.calls, [])
        self.assertEqual(len(self.attempts()), 1)

    def test_disabling_dispatch_between_windows_preserves_success_and_stops_new_calls(self):
        self.write_pcm(90)
        allowed = [True]
        fake = FakeRecognizer()
        original = fake.recognize
        def recognize(audio):
            value = original(audio)
            allowed[0] = False
            return value
        fake.recognize = recognize
        status = self.run_capture(fake, run_google=True, dispatch_allowed=lambda: allowed[0])
        self.assertEqual(status["status"], "paused")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual([row["status"] for row in self.windows()], ["succeeded", "queued", "queued"])

    def test_disabling_dispatch_during_preflight_does_not_create_paid_attempt(self):
        allowed = [True]
        fake = FakeRecognizer()
        def prepare():
            allowed[0] = False
        fake.prepare = prepare
        status = self.run_capture(fake, run_google=True, dispatch_allowed=lambda: allowed[0])
        self.assertEqual(status["status"], "paused")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.attempts(), [])

    def test_explicit_retries_preserve_history_and_obey_attempt_limit(self):
        failed = FakeRecognizer(error=ProviderError("google_deadline_unknown", "unknown"))
        self.run_capture(failed, run_google=True)
        for _ in range(1, workflow.MAX_ATTEMPTS):
            self.run_capture(failed, run_google=True, retry_window=0, accept_duplicate_cost=True)
        history = self.attempts()
        self.assertEqual([row["attempt"] for row in history], list(range(1, workflow.MAX_ATTEMPTS + 1)))
        self.assertEqual([row["outcome"] for row in history], ["unknown"] * workflow.MAX_ATTEMPTS)
        self.assertEqual([row["duplicate_cost_accepted"] for row in history], [0] + [1] * (workflow.MAX_ATTEMPTS - 1))
        with self.assertRaises(workflow.TranscriptionError) as caught:
            self.run_capture(failed, run_google=True, retry_window=0, accept_duplicate_cost=True)
        self.assertEqual(caught.exception.code, "window_attempt_limit_reached")
        self.assertEqual(len(failed.calls), workflow.MAX_ATTEMPTS)

    def test_duplicate_cost_consent_survives_retry_preflight_failure(self):
        self.run_capture(FakeRecognizer(error=ProviderError("google_deadline_unknown", "unknown")), run_google=True)
        unavailable = FakeRecognizer(prepare_error=ProviderError("google_initialization_failed", "not_sent"))
        with self.assertRaises(ProviderError):
            self.run_capture(unavailable, run_google=True, retry_window=0, accept_duplicate_cost=True)
        self.assertEqual(unavailable.calls, [])
        self.assertEqual(len(self.attempts()), 1)
        success = FakeRecognizer()
        self.assertEqual(self.run_capture(success, run_google=True)["status"], "complete")
        history = self.attempts()
        self.assertEqual([row["duplicate_cost_accepted"] for row in history], [0, 1])
        self.assertEqual([row["outcome"] for row in history], ["unknown", "succeeded"])

    def test_raw_response_durable_before_database_success_is_recovered_without_rpc(self):
        paid = FakeRecognizer()
        with patch.object(workflow.WindowState, "success", side_effect=SimulatedPowerLoss), self.assertRaises(SimulatedPowerLoss):
            self.run_capture(paid, run_google=True)
        self.assertEqual(self.windows()[0]["status"], "in_flight")
        self.assertTrue((self.capture / "transcription" / "windows" / "000000.json").is_file())
        recovered = FakeRecognizer()
        self.assertEqual(self.run_capture(recovered, run_google=True)["status"], "complete")
        self.assertEqual(recovered.calls, [])
        self.assertEqual(len(paid.calls), 1)
        self.assertEqual(self.attempts()[0]["outcome"], "recovered_success")

    def test_in_flight_without_a_saved_response_recovers_to_unknown(self):
        failed = FakeRecognizer(error=SimulatedPowerLoss())
        with self.assertRaises(SimulatedPowerLoss):
            self.run_capture(failed, run_google=True)
        self.assertEqual(self.windows()[0]["status"], "in_flight")
        other = FakeRecognizer()
        self.assertEqual(self.run_capture(other, run_google=True)["status"], "needs_attention")
        self.assertEqual(self.windows()[0]["status"], "unknown")
        self.assertEqual(other.calls, [])

    def test_database_success_with_missing_response_refuses_to_reissue(self):
        self.run_capture(FakeRecognizer(), run_google=True)
        (self.capture / "transcription" / "windows" / "000000.json").unlink()
        fake = FakeRecognizer()
        with self.assertRaises(workflow.TranscriptionError) as caught:
            self.run_capture(fake, run_google=True)
        self.assertEqual(caught.exception.code, "saved_window_response_missing")
        self.assertEqual(fake.calls, [])

    def test_missing_database_with_saved_results_fails_closed(self):
        self.run_capture(FakeRecognizer(), run_google=True)
        (self.capture / "transcription" / "state.sqlite3").unlink()
        fake = FakeRecognizer()
        with self.assertRaises(workflow.TranscriptionError) as caught:
            self.run_capture(fake, run_google=True)
        self.assertEqual(caught.exception.code, "transcription_state_missing")
        self.assertEqual(fake.calls, [])

    def test_empty_database_with_saved_results_fails_closed(self):
        self.run_capture(FakeRecognizer(), run_google=True)
        (self.capture / "transcription" / "state.sqlite3").write_bytes(b"")
        fake = FakeRecognizer()
        with self.assertRaises(workflow.TranscriptionError) as caught:
            self.run_capture(fake, run_google=True)
        self.assertEqual(caught.exception.code, "transcription_state_missing")
        self.assertEqual(fake.calls, [])

    def test_changed_pcm_fingerprint_stops_instead_of_silently_rebilling(self):
        self.run_capture(FakeRecognizer(), run_google=True)
        self.write_pcm(self.duration, byte_pair=b"\x01\0")
        fake = FakeRecognizer()
        with self.assertRaises(workflow.TranscriptionError) as caught:
            self.run_capture(fake, run_google=True)
        self.assertEqual(caught.exception.code, "recognized_window_input_changed")
        self.assertEqual(fake.calls, [])

    def test_audio_changed_between_planning_and_dispatch_is_rejected_before_in_flight(self):
        original = transcription_audio.wav_for_window
        calls = 0

        def changing_pcm(path, window):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.write_pcm(self.duration, byte_pair=b"\x01\0")
            return original(path, window)

        fake = FakeRecognizer()
        with patch.object(transcription_audio, "wav_for_window", side_effect=changing_pcm), self.assertRaises(workflow.TranscriptionError) as caught:
            self.run_capture(fake, run_google=True)
        self.assertEqual(caught.exception.code, "recognized_window_input_changed")
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.attempts(), [])

    def test_a_second_process_cannot_dispatch_for_the_same_capture(self):
        root = workflow.private_root(self.capture)
        code = """
from pathlib import Path
import sys
from capture_transcription import job_lock
with job_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, "-c", code, str(root)], cwd=BACKEND,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(ready, "Lock-holder process did not become ready")
            self.assertEqual(process.stdout.readline().strip(), "locked")
            fake = FakeRecognizer()
            with self.assertRaises(workflow.TranscriptionError) as caught:
                self.run_capture(fake, run_google=True)
            self.assertEqual(caught.exception.code, "worker_already_running")
            self.assertEqual(fake.calls, [])
        finally:
            process.communicate(input="release\n", timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_incremental_pending_tail_is_finalized_only_after_capture_completion(self):
        self.write_pcm(44)
        self.complete = False
        text = [{"text": "続き", "words": [{"text": "続き", "start": 0.1, "end": 0.3}]}]
        first = FakeRecognizer(results=text)
        status = self.run_capture(first, run_google=True)
        self.assertEqual(status["windows_done"], 1)
        self.assertEqual(len(first.calls), 1)
        result = workflow.read_result(self.capture)
        self.assertEqual(result["sentences"], [])
        self.assertEqual(result["pending_sentence"]["text"], "続き")
        self.complete = True
        tail = FakeRecognizer()
        self.assertEqual(self.run_capture(tail, run_google=True)["status"], "complete")
        self.assertEqual(len(tail.calls), 1)
        self.assertEqual([row["attempts"] for row in self.windows()], [1, 1])
        result = workflow.read_result(self.capture)
        self.assertIsNone(result["pending_sentence"])
        self.assertEqual(result["sentences"][0]["text"], "続き")
        self.assertEqual(result["sentences"][0]["boundary_reason"], "capture_end")

    def test_incremental_core_waits_for_right_context(self):
        self.write_pcm(43.5)
        self.complete = False
        fake = FakeRecognizer()
        status = self.run_capture(fake, run_google=True)
        self.assertEqual(status["windows_total"], 0)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.attempts(), [])

    def test_empty_google_response_is_successfully_cached(self):
        first = FakeRecognizer(results=[])
        status = self.run_capture(first, run_google=True)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["sentences"], 0)
        later = FakeRecognizer()
        self.assertEqual(self.run_capture(later, run_google=True)["new_requests"], 0)
        self.assertEqual(later.calls, [])
        self.assertEqual(self.windows()[0]["attempts"], 1)
        self.assertEqual(workflow.read_result(self.capture)["raw_transcripts"], [])

    def test_provider_preflight_not_sent_never_creates_an_attempt(self):
        for phase in ("validation", "prepare"):
            with self.subTest(phase=phase):
                options = {phase + "_error": ProviderError("google_initialization_failed", "not_sent")}
                fake = FakeRecognizer(**options)
                with self.assertRaises(ProviderError):
                    self.run_capture(fake, run_google=True)
                self.assertEqual(fake.calls, [])
                self.assertEqual(self.attempts(), [])
                self.assertEqual(self.windows()[0]["status"], "queued")

    def test_local_aggregation_failure_reuses_saved_paid_responses(self):
        paid = FakeRecognizer()
        with patch("transcription_text.merge_transcript", side_effect=ValueError("Local merge failed")), self.assertRaises(ValueError):
            self.run_capture(paid, run_google=True)
        self.assertEqual(self.windows()[0]["status"], "succeeded")
        later = FakeRecognizer()
        self.assertEqual(self.run_capture(later, run_google=True)["status"], "complete")
        self.assertEqual(later.calls, [])
        self.assertEqual(len(paid.calls), 1)

    def test_request_budget_pauses_without_reissuing_successful_prefix(self):
        self.write_pcm(90)
        first = FakeRecognizer()
        status = self.run_capture(first, run_google=True, max_new_requests=1)
        self.assertEqual(status["status"], "request_budget_reached")
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(status["windows_done"], 1)
        rest = FakeRecognizer()
        self.assertEqual(self.run_capture(rest, run_google=True)["status"], "complete")
        self.assertEqual(len(rest.calls), 2)
        self.assertEqual([row["attempts"] for row in self.windows()], [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
