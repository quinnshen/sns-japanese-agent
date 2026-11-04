"""Deterministic learning scheduler tests; no SDK, real threads or sleeps."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import capture_learning as workflow


class ControlledThread:
    def __init__(self, *, target, daemon, name):
        self.target, self.finished = target, False

    def start(self):
        pass

    def join(self):
        if not self.finished:
            try:
                self.target()
            finally:
                self.finished = True


class LearningSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.capture = Path(self.temporary.name) / "capture"
        self.capture.mkdir()
        self.key = str(self.capture.resolve())

    @contextmanager
    def scheduler(self, process):
        threads = []
        def thread(**arguments):
            value = ControlledThread(**arguments)
            threads.append(value)
            return value
        with patch.object(workflow, "threading", SimpleNamespace(Thread=thread)), \
                patch.object(workflow, "time", SimpleNamespace(sleep=Mock())), \
                patch.object(workflow, "_active", set()), patch.object(workflow, "_dirty", set()), \
                patch.object(workflow, "process_capture_learning", process), \
                patch.object(workflow, "read_learning_status", return_value={"status": "queued"}), \
                patch.object(workflow, "private_root", return_value=self.capture / "learning"), \
                patch.object(workflow, "atomic_json") as save:
            try:
                yield SimpleNamespace(threads=threads, save=save)
            finally:
                for value in threads:
                    value.join()

    def test_notifications_coalesce_and_wake_during_work_is_preserved(self):
        calls = []
        enabled = Mock(return_value=True)
        def process(capture_dir, *, run_openai, dispatch_allowed):
            calls.append(capture_dir)
            self.assertTrue(run_openai)
            self.assertIs(dispatch_allowed, enabled)
            if len(calls) == 1:
                workflow.request_learning(self.capture, enabled=enabled)
            return {"status": "complete"}
        with self.scheduler(Mock(side_effect=process)) as scheduler:
            workflow.request_learning(self.capture, enabled=enabled)
            workflow.request_learning(self.capture, enabled=enabled)
            self.assertEqual(len(scheduler.threads), 1)
            self.assertEqual(calls, [])
            scheduler.threads[0].join()
            self.assertEqual(len(calls), 2)
            self.assertEqual(workflow._active, set())
            self.assertEqual(workflow._dirty, set())
            scheduler.save.assert_not_called()

    def test_disabled_scheduler_does_not_dispatch(self):
        process = Mock()
        with self.scheduler(process) as scheduler:
            workflow.request_learning(self.capture, enabled=lambda: False)
            scheduler.threads[0].join()
            process.assert_not_called()
            self.assertEqual(workflow._active, set())
            self.assertEqual(workflow._dirty, set())

    def test_busy_task_is_retried_without_losing_dirty_notification(self):
        process = Mock(side_effect=[workflow.LearningError("worker_already_running"), {"status": "complete"}, {"status": "complete"}])
        with self.scheduler(process) as scheduler:
            workflow.request_learning(self.capture)
            workflow.request_learning(self.capture)
            scheduler.threads[0].join()
            self.assertEqual(process.call_count, 3)
            scheduler.save.assert_not_called()
            self.assertEqual(workflow._active, set())


if __name__ == "__main__":
    unittest.main()
