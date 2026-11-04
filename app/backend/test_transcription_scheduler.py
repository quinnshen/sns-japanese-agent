"""Deterministic scheduler tests: no real threads, sleep, credentials or cloud."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import capture_transcription as workflow


class ControlledThread:
    """Queue work on start; finish it synchronously on join while mocks remain."""

    def __init__(self, *, target, daemon, name):
        self.target = target
        self.daemon = daemon
        self.name = name
        self.started = False
        self.finished = False

    def start(self):
        self.started = True

    def join(self, timeout=None):
        if self.started and not self.finished:
            try:
                self.target()
            finally:
                self.finished = True


class TranscriptionSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.capture = Path(self.temporary.name) / "capture"
        self.capture.mkdir()
        self.key = str(self.capture.resolve())

    @contextmanager
    def scheduler(self, process):
        threads = []
        sleep = Mock()

        def fake_thread(**arguments):
            thread = ControlledThread(**arguments)
            threads.append(thread)
            return thread

        def bounded_sleep(seconds):
            if sleep.call_count > 150:
                raise AssertionError("Busy retry loop exceeded its bounded wait budget")

        sleep.side_effect = bounded_sleep
        with patch.object(workflow, "threading", SimpleNamespace(Thread=fake_thread)), \
                patch.object(workflow, "time", SimpleNamespace(sleep=sleep)), \
                patch.object(workflow, "_active", set()), \
                patch.object(workflow, "_dirty", set()), \
                patch.object(workflow, "process_capture", process), \
                patch.object(workflow, "read_status", return_value={"status": "prepared"}) as read_status, \
                patch.object(workflow, "private_root", return_value=self.capture / "transcription"), \
                patch.object(workflow, "atomic_json") as write_status:
            try:
                yield SimpleNamespace(threads=threads, sleep=sleep, read_status=read_status, write_status=write_status)
            finally:
                # Even a failing assertion cannot leave queued work to execute
                # after the provider/storage mocks have been removed.
                for thread in threads:
                    thread.join()

    def test_busy_retry_preserves_new_completion_wake_during_active_work(self):
        enabled = Mock(return_value=True)
        calls = 0

        def process(capture_dir, *, run_google, dispatch_allowed):
            nonlocal calls
            calls += 1
            self.assertEqual(capture_dir, self.capture)
            self.assertTrue(run_google)
            self.assertIs(dispatch_allowed, enabled)
            if calls == 1:
                raise workflow.TranscriptionError("worker_already_running")
            if calls == 2:
                # Simulate complete arriving while the previous ACK-triggered
                # pass is running. It must cause another pass, not disappear.
                workflow.request_processing(self.capture, enabled=enabled)
                self.assertIn(self.key, workflow._dirty)
                return {"status": "waiting_for_audio"}
            self.assertEqual(calls, 3)
            return {"status": "complete"}

        worker = Mock(side_effect=process)
        with self.scheduler(worker) as scheduler:
            workflow.request_processing(self.capture, enabled=enabled)
            self.assertEqual(len(scheduler.threads), 1)
            worker.assert_not_called()
            self.assertIn(self.key, workflow._active)
            scheduler.threads[0].join()
            self.assertEqual(worker.call_count, 3)
            self.assertEqual(len(scheduler.threads), 1)
            scheduler.sleep.assert_called_once_with(1)
            self.assertEqual(workflow._active, set())
            self.assertEqual(workflow._dirty, set())
            scheduler.write_status.assert_not_called()
            self.assertTrue(all(thread.finished for thread in scheduler.threads))

    def test_initially_disabled_scheduler_does_not_invoke_worker(self):
        worker = Mock()
        with self.scheduler(worker) as scheduler:
            workflow.request_processing(self.capture, enabled=lambda: False)
            self.assertEqual(len(scheduler.threads), 1)
            scheduler.threads[0].join()
            worker.assert_not_called()
            scheduler.sleep.assert_not_called()
            scheduler.write_status.assert_not_called()
            self.assertEqual(workflow._active, set())
            self.assertEqual(workflow._dirty, set())
            self.assertTrue(scheduler.threads[0].finished)

    def test_exhausted_busy_budget_releases_capture_for_a_new_wake(self):
        worker = Mock(side_effect=workflow.TranscriptionError("worker_already_running"))
        with self.scheduler(worker) as scheduler:
            workflow.request_processing(self.capture)
            scheduler.threads[0].join()
            self.assertEqual(worker.call_count, 151)
            self.assertEqual(scheduler.sleep.call_count, 150)
            scheduler.read_status.assert_called_once_with(self.capture)
            scheduler.write_status.assert_called_once()
            status = scheduler.write_status.call_args.args[1]
            self.assertEqual(status["status"], "error")
            self.assertEqual(status["error_code"], "worker_already_running")
            self.assertEqual(workflow._active, set())
            self.assertEqual(workflow._dirty, set())

            worker.side_effect = None
            worker.return_value = {"status": "complete"}
            workflow.request_processing(self.capture)
            self.assertEqual(len(scheduler.threads), 2)
            scheduler.threads[1].join()
            self.assertEqual(worker.call_count, 152)
            self.assertEqual(scheduler.sleep.call_count, 150)
            self.assertEqual(workflow._active, set())
            self.assertEqual(workflow._dirty, set())
            self.assertTrue(all(thread.finished for thread in scheduler.threads))


if __name__ == "__main__":
    unittest.main()
