"""Private temporary ASR + fake provider tests; no credentials or cloud calls."""
import contextlib
import copy
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import capture_learning as workflow
import capture_learn_cli


class SimulatedPowerLoss(BaseException):
    pass


class FakeProviderError(Exception):
    def __init__(self, error_code, retry_class):
        self.error_code = error_code
        self.retry_class = retry_class


class FakeProvider:
    def __init__(self, *, invalid_ids=(), error=None, prepare_error=None, model="test-model"):
        self.config = {"provider": "fake", "model": model, "style_version": 2, "prompt_sha256": "0" * 64,
                       "schema_sha256": "1" * 64, "reasoning_effort": "low", "max_output_tokens": 6400, "store": False}
        self.provider_fingerprint = workflow.fingerprint(self.config)
        self.invalid_ids = set(invalid_ids)
        self.error, self.prepare_error = error, prepare_error
        self.calls = []
        self.prepares = 0

    def validate_batch(self, batch):
        if not batch["targets"]:
            raise AssertionError("Empty provider request")

    def prepare(self):
        self.prepares += 1
        if self.prepare_error:
            raise self.prepare_error

    def generate(self, batch):
        self.calls.append(copy.deepcopy(batch))
        if self.error:
            raise self.error
        items = [{"id": item["id"], "reading_text": "いいえ。" if item["id"] in self.invalid_ids else item["text"],
                  "translation_zh": "是的。", "key_points": ["「はい」：表示肯定回答。"],
                  "context_note": "", "asr_doubt": ""} for item in batch["targets"]]
        return {"status": "completed", "items": items, "usage": {"input_tokens": 40, "output_tokens": 80},
                "raw_output": "synthetic private response", "response_id": "synthetic-response"}


class LearningWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.capture = Path(self.temporary.name) / "capture"
        self.capture.mkdir()
        self.transcription = self.capture / "transcription"
        self.transcription.mkdir()
        self.write_source(6)

    def write_source(self, count, *, complete=True, changes=None):
        sentences = [{"id": f"s{index+1:04d}", "text": "はい。", "start": 30 + index,
                      "end": 30.5 + index, "video_start": 30 + index, "video_end": 30.5 + index,
                      "audio_start": index, "audio_end": index + 0.5, "mapping_uncertain": False}
                     for index in range(count)]
        if changes:
            for index, value in changes.items():
                sentences[index].update(value)
        source = {"id": "synthetic-capture", "status": "transcribed" if complete else "partially_transcribed",
                  "sentences": sentences, "pending_sentence": None, "raw_transcripts": ["synthetic private ASR"]}
        (self.transcription / "result.json").write_text(json.dumps(source), encoding="utf-8")
        (self.transcription / "status.json").write_text(json.dumps({"status": "complete" if complete else "waiting_for_audio"}), encoding="utf-8")
        return source

    def run_learning(self, provider=None, **options):
        return workflow.process_capture_learning(self.capture, provider=provider or FakeProvider(), **options)

    def rows(self, table="batches"):
        with sqlite3.connect(self.capture / "learning/state.sqlite3") as connection:
            connection.row_factory = sqlite3.Row
            order = "batch_index,attempt" if table == "attempts" else "batch_index"
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]

    def test_absent_status_is_safe_and_does_not_create_storage(self):
        status = workflow.read_learning_status(self.capture)
        self.assertEqual(status["status"], "not_started")
        self.assertEqual(status["sentences_done"], 0)
        self.assertFalse((self.capture / "learning").exists())
        self.assertIsNone(workflow.read_learning_result(self.capture))

    def test_offline_plans_batches_without_preparing_or_calling_provider(self):
        self.write_source(13)
        provider = FakeProvider()
        before = (self.transcription / "result.json").read_bytes()
        status = self.run_learning(provider)
        self.assertEqual(status["status"], "queued")
        self.assertEqual(status["batches_total"], 3)
        self.assertEqual(status["sentences_done"], 0)
        self.assertEqual(provider.prepares, 0)
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.rows("attempts"), [])
        self.assertEqual((self.transcription / "result.json").read_bytes(), before)

    def test_success_attaches_notes_without_modifying_asr_and_reuses_cache(self):
        before = (self.transcription / "result.json").read_bytes()
        provider = FakeProvider()
        status = self.run_learning(provider, run_openai=True)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["sentences_done"], 6)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.rows()[0]["attempts"], 1)
        result = workflow.read_learning_result(self.capture)
        self.assertEqual(result["sentences"][0]["study"]["reading_text"], "はい。")
        self.assertEqual(result["sentences"][0]["study"]["style_version"], 2)
        self.assertEqual(result["sentences"][0]["text"], "はい。")
        self.assertEqual((self.transcription / "result.json").read_bytes(), before)
        cached = FakeProvider()
        later = self.run_learning(cached, run_openai=True)
        self.assertEqual(later["new_requests"], 0)
        self.assertEqual(cached.calls, [])
        self.assertEqual(later["revision"], status["revision"])

    def test_partial_invalid_note_preserves_neighbors_and_continues_later_batches(self):
        self.write_source(13)
        provider = FakeProvider(invalid_ids={"s0002", "s0004"})
        status = self.run_learning(provider, run_openai=True)
        self.assertEqual(status["status"], "needs_attention")
        self.assertEqual(status["sentences_done"], 11)
        self.assertEqual(status["invalid_sentences"], 2)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual([row["status"] for row in self.rows()], ["partial", "succeeded", "succeeded"])
        result = workflow.read_learning_result(self.capture)
        self.assertNotIn("study", result["sentences"][1])
        self.assertIn("study", result["sentences"][12])
        again = FakeProvider()
        self.assertEqual(self.run_learning(again, run_openai=True)["new_requests"], 0)
        self.assertEqual(again.calls, [])

    def test_explicit_partial_retry_only_requests_invalid_targets_and_freezes_context(self):
        provider = FakeProvider(invalid_ids={"s0002", "s0004"})
        self.run_learning(provider, run_openai=True)
        before = workflow.read_learning_result(self.capture)["sentences"][0]["study"]
        retry = FakeProvider()
        result = self.run_learning(retry, run_openai=True, retry_batch=0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(retry.calls[0]["target_ids"], ["s0002", "s0004"])
        self.assertEqual(retry.calls[0]["context"], provider.calls[0]["context"])
        self.assertNotIn("validation_errors", provider.calls[0])
        self.assertEqual(retry.calls[0]["validation_errors"], {
            "s0002": "source_text_changed", "s0004": "source_text_changed",
        })
        attempts = self.rows("attempts")
        self.assertNotEqual(attempts[0]["request_fingerprint"], attempts[1]["request_fingerprint"])
        self.assertEqual(json.loads(attempts[1]["request_json"])["validation_errors"], retry.calls[0]["validation_errors"])
        self.assertEqual(workflow.read_learning_result(self.capture)["sentences"][0]["study"], before)

    def test_retry_feedback_is_allowlisted_and_survives_preflight_interruption(self):
        self.run_learning(FakeProvider(invalid_ids={"s0002", "s0004"}), run_openai=True)
        unavailable = FakeProvider(prepare_error=FakeProviderError("openai_not_configured", "not_sent"))
        with self.assertRaises(FakeProviderError):
            self.run_learning(unavailable, run_openai=True, retry_batch=0)
        # Simulate an unrelated, non-validation diagnostic in the ledger. It
        # must not be copied into a future model request as free-form feedback.
        with sqlite3.connect(self.capture / "learning/state.sqlite3") as connection:
            connection.execute("UPDATE sentences SET error_code='unapproved_diagnostic' WHERE id='s0004'")
        retry = FakeProvider()
        self.assertEqual(self.run_learning(retry, run_openai=True)["status"], "complete")
        self.assertEqual(retry.calls[0]["target_ids"], ["s0002", "s0004"])
        self.assertEqual(retry.calls[0]["validation_errors"], {"s0002": "source_text_changed"})

    def test_unknown_outcome_requires_explicit_duplicate_cost_consent(self):
        failure = FakeProvider(error=FakeProviderError("openai_timeout", "unknown"))
        self.assertEqual(self.run_learning(failure, run_openai=True)["status"], "needs_attention")
        later = FakeProvider()
        self.run_learning(later, run_openai=True)
        self.assertEqual(later.calls, [])
        with self.assertRaises(workflow.LearningError) as caught:
            self.run_learning(later, run_openai=True, retry_batch=0)
        self.assertEqual(caught.exception.code, "duplicate_cost_confirmation_required")
        self.assertEqual(self.run_learning(later, run_openai=True, retry_batch=0, accept_duplicate_cost=True)["status"], "complete")
        self.assertEqual([row["duplicate_cost_accepted"] for row in self.rows("attempts")], [0, 1])

    def test_unknown_retries_are_bounded_and_keep_attempt_history(self):
        provider = FakeProvider(error=FakeProviderError("openai_timeout", "unknown"))
        self.run_learning(provider, run_openai=True)
        for _ in range(1, workflow.MAX_ATTEMPTS):
            self.run_learning(provider, run_openai=True, retry_batch=0, accept_duplicate_cost=True)
        with self.assertRaises(workflow.LearningError) as caught:
            self.run_learning(provider, run_openai=True, retry_batch=0, accept_duplicate_cost=True)
        self.assertEqual(caught.exception.code, "learning_attempt_limit_reached")
        self.assertEqual(len(provider.calls), workflow.MAX_ATTEMPTS)
        self.assertEqual(len(self.rows("attempts")), workflow.MAX_ATTEMPTS)

    def test_raw_response_survives_power_loss_before_success_without_reissuing(self):
        provider = FakeProvider()
        with patch.object(workflow.LearningState, "apply_response", side_effect=SimulatedPowerLoss), self.assertRaises(SimulatedPowerLoss):
            self.run_learning(provider, run_openai=True)
        self.assertEqual(self.rows()[0]["status"], "in_flight")
        self.assertEqual(len(list((self.capture / "learning/batches").glob("*.json"))), 1)
        recovered = FakeProvider()
        self.assertEqual(self.run_learning(recovered, run_openai=True)["status"], "complete")
        self.assertEqual(recovered.calls, [])
        self.assertEqual(self.rows("attempts")[0]["outcome"], "recovered_success")

    def test_saved_revalidation_accepts_fixed_validator_without_new_calls_or_history_changes(self):
        import learning_text
        validate = learning_text.validate_items
        def old_false_positive(items, targets):
            result = validate(items, targets)
            result["valid"].pop("s0002", None)
            result["invalid"]["s0002"] = "missing_note_reading"
            return result
        provider = FakeProvider()
        with patch.object(learning_text, "validate_items", side_effect=old_false_positive):
            before_status = self.run_learning(provider, run_openai=True, max_total_requests=2)
        self.assertEqual(before_status["sentences_done"], 5)
        before_attempts = self.rows("attempts")
        before_neighbor = workflow.read_learning_result(self.capture)["sentences"][0]["study"]
        raw = {path.name: path.read_bytes() for path in (self.capture / "learning/batches").glob("*.json")}
        source_bytes = (self.transcription / "result.json").read_bytes()
        with patch("learning_openai.OpenAIStudyProvider", side_effect=AssertionError("No provider for offline revalidation")):
            status = workflow.process_capture_learning(self.capture, revalidate_saved=True)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["new_requests"], 0)
        self.assertEqual(status["total_requests"], 1)
        self.assertEqual(status["max_total_requests"], 2)
        self.assertEqual(status["sentences_done"], 6)
        self.assertEqual(len(provider.calls), 1)
        after_attempts = self.rows("attempts")
        self.assertEqual(len(after_attempts), len(before_attempts))
        for key in ("attempt", "request_fingerprint", "request_json", "started_at", "finished_at", "response_hash"):
            self.assertEqual(after_attempts[0][key], before_attempts[0][key])
        self.assertEqual({path.name: path.read_bytes() for path in (self.capture / "learning/batches").glob("*.json")}, raw)
        self.assertEqual((self.transcription / "result.json").read_bytes(), source_bytes)
        self.assertEqual(workflow.read_learning_result(self.capture)["sentences"][0]["study"], before_neighbor)

    def test_saved_revalidation_does_not_touch_unknown_attempts_without_responses(self):
        self.run_learning(FakeProvider(error=FakeProviderError("openai_timeout", "unknown")), run_openai=True)
        attempts = self.rows("attempts")
        with patch("learning_openai.OpenAIStudyProvider", side_effect=AssertionError("No provider for offline revalidation")):
            status = workflow.process_capture_learning(self.capture, revalidate_saved=True)
        self.assertEqual(status["status"], "needs_attention")
        self.assertEqual(status["new_requests"], 0)
        self.assertEqual(self.rows("attempts"), attempts)

    def test_revalidating_earlier_partial_response_preserves_later_unknown_cost_gate(self):
        self.run_learning(FakeProvider(invalid_ids={"s0002"}), run_openai=True)
        unknown = FakeProvider(error=FakeProviderError("openai_timeout", "unknown"))
        status = self.run_learning(unknown, run_openai=True, retry_batch=0)
        self.assertEqual(status["blocked_batches"][0]["status"], "unknown")
        attempts = self.rows("attempts")
        raw = {path.name: path.read_bytes() for path in (self.capture / "learning/batches").glob("*.json")}
        self.assertEqual(len(raw), 1)
        with patch("learning_openai.OpenAIStudyProvider", side_effect=AssertionError("No provider for offline revalidation")):
            status = workflow.process_capture_learning(self.capture, revalidate_saved=True)
        self.assertEqual(status["status"], "needs_attention")
        self.assertEqual(status["blocked_batches"][0]["status"], "unknown")
        self.assertEqual(status["blocked_batches"][0]["error_code"], "openai_timeout")
        self.assertEqual(status["new_requests"], 0)
        after = self.rows("attempts")
        self.assertEqual(len(after), 2)
        self.assertEqual(after[1], attempts[1])
        self.assertEqual(after[0]["finished_at"], attempts[0]["finished_at"])
        self.assertEqual({path.name: path.read_bytes() for path in (self.capture / "learning/batches").glob("*.json")}, raw)
        with self.assertRaises(workflow.LearningError) as raised:
            self.run_learning(FakeProvider(), run_openai=True, retry_batch=0)
        self.assertEqual(raised.exception.code, "duplicate_cost_confirmation_required")
        self.assertEqual(len(self.rows("attempts")), 2)

    def test_inflight_without_raw_recovers_unknown_without_new_request(self):
        with self.assertRaises(SimulatedPowerLoss):
            self.run_learning(FakeProvider(error=SimulatedPowerLoss()), run_openai=True)
        other = FakeProvider()
        status = self.run_learning(other, run_openai=True)
        self.assertEqual(status["status"], "needs_attention")
        self.assertEqual(other.calls, [])
        self.assertEqual(self.rows()[0]["status"], "unknown")

    def test_missing_or_corrupt_saved_response_fails_closed(self):
        self.run_learning(FakeProvider(), run_openai=True)
        response = next((self.capture / "learning/batches").glob("*.json"))
        response.unlink()
        provider = FakeProvider()
        with self.assertRaises(workflow.LearningError) as caught:
            self.run_learning(provider, run_openai=True)
        self.assertEqual(caught.exception.code, "saved_learning_response_missing")
        self.assertEqual(provider.calls, [])

    def test_missing_database_with_saved_results_fails_closed(self):
        self.run_learning(FakeProvider(), run_openai=True)
        (self.capture / "learning/state.sqlite3").unlink()
        provider = FakeProvider()
        with self.assertRaises(workflow.LearningError) as caught:
            self.run_learning(provider, run_openai=True)
        self.assertEqual(caught.exception.code, "learning_state_missing")
        self.assertEqual(provider.calls, [])

    def test_source_or_model_change_after_success_cannot_silently_rebill(self):
        self.run_learning(FakeProvider(), run_openai=True)
        provider = FakeProvider(model="other-model")
        with self.assertRaises(workflow.LearningError) as caught:
            self.run_learning(provider, run_openai=True)
        self.assertEqual(caught.exception.code, "learning_configuration_changed")
        self.assertEqual(provider.calls, [])
        self.write_source(6, changes={0: {"text": "いいえ。"}})
        with self.assertRaises(workflow.LearningError) as caught:
            self.run_learning(FakeProvider(), run_openai=True)
        self.assertEqual(caught.exception.code, "learning_input_changed")
        with self.assertRaises(workflow.LearningError):
            workflow.read_learning_result(self.capture)

    def test_incremental_batches_hold_tail_and_only_process_new_sentences(self):
        self.write_source(8, complete=False)
        first = FakeProvider()
        status = self.run_learning(first, run_openai=True)
        self.assertEqual(status["status"], "waiting_for_transcript")
        self.assertEqual(status["sentences_done"], 6)
        self.assertEqual(len(first.calls), 1)
        self.write_source(10, complete=True)
        later = FakeProvider()
        status = self.run_learning(later, run_openai=True)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(later.calls[0]["target_ids"], ["s0007", "s0008", "s0009", "s0010"])
        self.assertEqual([row["attempts"] for row in self.rows()], [1, 1])

    def test_dispatch_guard_stops_between_batches_and_during_preflight(self):
        self.write_source(13)
        allowed = [True]
        provider = FakeProvider()
        generate = provider.generate
        def once(batch):
            response = generate(batch)
            allowed[0] = False
            return response
        provider.generate = once
        status = self.run_learning(provider, run_openai=True, dispatch_allowed=lambda: allowed[0])
        self.assertEqual(status["status"], "paused")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual([row["status"] for row in self.rows()], ["succeeded", "queued", "queued"])
        before = len(self.rows("attempts"))
        later = FakeProvider()
        later.prepare = lambda: allowed.__setitem__(0, False)
        allowed[0] = True
        self.run_learning(later, run_openai=True, dispatch_allowed=lambda: allowed[0])
        self.assertEqual(len(self.rows("attempts")), before)
        self.assertEqual(later.calls, [])

    def test_request_budget_resumes_without_repeating_success(self):
        self.write_source(13)
        provider = FakeProvider()
        status = self.run_learning(provider, run_openai=True, max_new_requests=1)
        self.assertEqual(status["status"], "request_budget_reached")
        self.assertEqual(len(provider.calls), 1)
        remaining = FakeProvider()
        self.assertEqual(self.run_learning(remaining, run_openai=True)["status"], "complete")
        self.assertEqual(len(remaining.calls), 2)

    def test_total_request_cap_persists_across_wakes_and_requires_explicit_raise(self):
        self.write_source(13)
        first = FakeProvider()
        status = self.run_learning(first, run_openai=True, max_total_requests=2)
        self.assertEqual(status["status"], "request_budget_reached")
        self.assertEqual(len(first.calls), 2)
        self.assertEqual(status["total_requests"], 2)
        later = FakeProvider()
        status = self.run_learning(later, run_openai=True)
        self.assertEqual(status["status"], "request_budget_reached")
        self.assertEqual(status["max_total_requests"], 2)
        self.assertEqual(later.calls, [])
        self.assertEqual(workflow.read_learning_status(self.capture)["total_requests"], 2)
        raised = self.run_learning(later, run_openai=True, max_total_requests=3, raise_request_budget=True)
        self.assertEqual(raised["status"], "complete")
        self.assertEqual(raised["total_requests"], 3)
        self.assertEqual(len(later.calls), 1)

    def test_timing_only_refinement_preserves_notes_and_uses_current_asr_base(self):
        self.write_source(8, complete=False, changes={0: {"start": None, "video_start": None, "mapping_uncertain": True}})
        first = FakeProvider()
        self.run_learning(first, run_openai=True)
        before = workflow.read_learning_result(self.capture)
        self.write_source(8, complete=True, changes={0: {"start": 30.01, "video_start": 30.01, "mapping_uncertain": False}})
        # Existing notes may be served immediately with the refined source
        # timing, before the new final tail batch has been processed.
        refined = workflow.read_learning_result(self.capture)
        self.assertEqual(refined["sentences"][0]["start"], 30.01)
        self.assertEqual(refined["sentences"][0]["study"], before["sentences"][0]["study"])
        later = FakeProvider()
        status = self.run_learning(later, run_openai=True)
        self.assertEqual(status["status"], "complete")
        self.assertEqual(later.calls[0]["target_ids"], ["s0007", "s0008"])
        self.assertEqual(len(later.calls), 1)
        self.assertEqual(self.rows()[0]["attempts"], 1)
        cached = FakeProvider()
        self.run_learning(cached, run_openai=True)
        self.assertEqual(cached.calls, [])

    def test_invalid_envelope_records_response_and_continues_other_batches(self):
        self.write_source(12)
        provider = FakeProvider()
        normal = provider.generate
        def first_refused(batch):
            if batch["index"] == 0:
                provider.calls.append(copy.deepcopy(batch))
                return {"status": "refused", "items": [], "usage": {"input_tokens": 5, "output_tokens": 0}}
            return normal(batch)
        provider.generate = first_refused
        status = self.run_learning(provider, run_openai=True)
        self.assertEqual(status["status"], "needs_attention")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(status["sentences_done"], 6)
        self.assertEqual(self.rows()[0]["status"], "failed")
        self.assertIsNotNone(self.rows("attempts")[0]["response_hash"])
        self.assertEqual(len(list((self.capture / "learning/batches").glob("*.json"))), 2)

    def test_retry_cost_consent_survives_not_sent_preflight_error(self):
        self.run_learning(FakeProvider(error=FakeProviderError("openai_timeout", "unknown")), run_openai=True)
        unavailable = FakeProvider(prepare_error=FakeProviderError("openai_not_configured", "not_sent"))
        with self.assertRaises(FakeProviderError):
            self.run_learning(unavailable, run_openai=True, retry_batch=0, accept_duplicate_cost=True)
        self.assertEqual(len(self.rows("attempts")), 1)
        self.assertEqual(self.run_learning(FakeProvider(), run_openai=True)["status"], "complete")
        self.assertEqual(self.rows("attempts")[1]["duplicate_cost_accepted"], 1)

    def test_no_source_and_empty_complete_source_need_no_provider(self):
        (self.transcription / "result.json").unlink()
        provider = FakeProvider()
        self.assertEqual(self.run_learning(provider, run_openai=True)["status"], "waiting_for_transcript")
        self.assertEqual(provider.calls, [])
        self.write_source(0)
        self.assertEqual(self.run_learning(provider, run_openai=True)["status"], "complete")
        self.assertEqual(provider.calls, [])

    def test_none_video_endpoint_is_preserved_but_audio_clock_remains_valid(self):
        self.write_source(6, changes={0: {"start": None, "video_start": None, "mapping_uncertain": True}})
        provider = FakeProvider()
        self.assertEqual(self.run_learning(provider, run_openai=True)["status"], "complete")
        first = workflow.read_learning_result(self.capture)["sentences"][0]
        self.assertIsNone(first["start"])
        self.assertIsNone(first["video_start"])
        self.assertEqual(first["audio_start"], 0)
        self.assertIn("study", first)

    def test_bad_audio_and_known_video_endpoints_are_rejected_before_dispatch(self):
        for changes in ({"start": None}, {"start": float("nan")}, {"start": 40, "end": 30},
                        {"audio_start": 2, "audio_end": 1}, {"audio_start": float("inf")}):
            with self.subTest(changes=changes):
                self.write_source(6, changes={0: changes})
                provider = FakeProvider()
                with self.assertRaises(workflow.LearningError) as caught:
                    self.run_learning(provider, run_openai=True)
                self.assertEqual(caught.exception.code, "transcript_input_invalid")
                self.assertEqual(provider.calls, [])

    def test_private_modes_and_task_lock(self):
        self.run_learning(FakeProvider(), run_openai=True)
        root = self.capture / "learning"
        for directory in (root, root / "batches"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in (root / "state.sqlite3", root / "status.json", root / "result.json", root / "worker.lock", *list((root / "batches").glob("*.json"))):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with workflow.job_lock(root):
            with self.assertRaises(workflow.LearningError) as caught:
                self.run_learning(FakeProvider(), run_openai=True)
            self.assertEqual(caught.exception.code, "worker_already_running")

    def test_preflight_failure_and_offline_retry_never_dispatch(self):
        provider = FakeProvider(prepare_error=FakeProviderError("openai_not_configured", "not_sent"))
        with self.assertRaises(FakeProviderError):
            self.run_learning(provider, run_openai=True)
        self.assertEqual(self.rows("attempts"), [])
        self.assertEqual(provider.calls, [])
        with self.assertRaises(workflow.LearningError) as caught:
            self.run_learning(FakeProvider(), retry_batch=0)
        self.assertEqual(caught.exception.code, "paid_retry_requires_run_flag")

    def test_cli_defaults_offline_and_only_prints_safe_status(self):
        output = io.StringIO()
        with patch.object(capture_learn_cli, "process_capture_learning", return_value={"status": "queued", "sentences_done": 0}) as process, contextlib.redirect_stdout(output):
            self.assertEqual(capture_learn_cli.main([str(self.capture)]), 0)
        self.assertFalse(process.call_args.kwargs["run_openai"])
        self.assertNotIn("はい", output.getvalue())
        self.assertNotIn(str(self.capture), output.getvalue())
        with patch.object(capture_learn_cli, "process_capture_learning", side_effect=RuntimeError("synthetic private error")), contextlib.redirect_stdout(output):
            self.assertEqual(capture_learn_cli.main([str(self.capture), "--run-openai"]), 1)
        self.assertNotIn("synthetic private error", output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            capture_learn_cli.main([str(self.capture), "--raise-request-budget"])
        self.assertEqual(raised.exception.code, 2)

    def test_saved_revalidation_cli_and_workflow_reject_paid_retry_combinations(self):
        with patch.object(capture_learn_cli, "process_capture_learning", return_value={"status": "complete"}) as process, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(capture_learn_cli.main([str(self.capture), "--revalidate-saved"]), 0)
        self.assertTrue(process.call_args.kwargs["revalidate_saved"])
        self.assertFalse(process.call_args.kwargs["run_openai"])
        for flags in (["--run-openai"], ["--retry-batch", "0"], ["--accept-duplicate-cost"], ["--max-total-requests", "1"]):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                capture_learn_cli.main([str(self.capture), "--revalidate-saved", *flags])
        for options in ({"run_openai": True}, {"retry_batch": 0}, {"accept_duplicate_cost": True}, {"raise_request_budget": True}):
            with self.subTest(options=options), self.assertRaises(workflow.LearningError) as raised:
                workflow.process_capture_learning(self.capture, revalidate_saved=True, **options)
            self.assertEqual(raised.exception.code, "saved_revalidation_requires_offline_mode")


if __name__ == "__main__":
    unittest.main()
