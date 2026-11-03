"""Offline checks for credential isolation and the loopback request boundary.

All credentials, API clients, media operations, and HTTP handlers are mocked.
These tests neither read the developer's local key file nor call cloud services.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch, sentinel

import config
import enrich
import pipeline
import server


class CredentialIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(config, "LOCAL_ENV_FILE", Path(directory.name) / "absent-config")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_or_placeholder_google_project_never_loads_credentials(self):
        for project in (None, "", "   ", "YOUR_PROJECT_ID"):
            environment = {} if project is None else {"GOOGLE_CLOUD_PROJECT": project}
            with self.subTest(project=project), patch.dict(os.environ, environment, clear=True), \
                    patch("google.auth.default") as default_credentials, \
                    patch("google.cloud.speech_v2.SpeechClient") as speech_client, \
                    patch.object(pipeline, "wav_slice") as wav_slice:
                with self.assertRaisesRegex(RuntimeError, "GOOGLE_CLOUD_PROJECT"):
                    pipeline.transcribe_media(Path("unused.webm"), 5.0)
                default_credentials.assert_not_called()
                speech_client.assert_not_called()
                wav_slice.assert_not_called()

    def test_google_credentials_and_quota_project_belong_to_the_operator(self):
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "operator-project"}, clear=True), \
                patch("google.auth.default", return_value=(sentinel.operator_credentials, "adc-project")) as default_credentials, \
                patch("google.cloud.speech_v2.SpeechClient", return_value=sentinel.speech_client) as speech_client, \
                patch.object(pipeline, "wav_slice", return_value=b"mock-wav") as wav_slice, \
                patch.object(pipeline, "recognize_wav", return_value=[]) as recognize:
            result = pipeline.transcribe_media(Path("unused.webm"), 5.0)
        default_credentials.assert_called_once_with(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            quota_project_id="operator-project",
        )
        speech_client.assert_called_once_with(credentials=sentinel.operator_credentials)
        wav_slice.assert_called_once_with(Path("unused.webm"), 0.0, 5.0)
        recognize.assert_called_once_with(b"mock-wav", sentinel.speech_client, "operator-project")
        self.assertEqual(result["sentences"], [])

    def test_missing_openai_key_never_constructs_a_client(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(config, "LOCAL_ENV_FILE", Path(directory) / "missing-local-key"), \
                patch("openai.OpenAI") as openai_client:
            self.assertIsNone(enrich.local_api_key())
            with self.assertRaisesRegex(RuntimeError, "OpenAI API Key"):
                enrich.openai_enricher("test-model")
            openai_client.assert_not_called()

    def test_openai_client_receives_only_the_operator_key(self):
        response = SimpleNamespace(
            status="completed",
            output_text=json.dumps({"items": []}),
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"OPENAI_API_KEY": "operator-key-for-offline-test"}, clear=True), \
                patch.object(config, "LOCAL_ENV_FILE", Path(directory) / "missing-local-key"), \
                patch("openai.OpenAI") as openai_client:
            openai_client.return_value.responses.create.return_value = response
            items, usage = enrich.openai_enricher("test-model")([], [])
        openai_client.assert_called_once_with(api_key="operator-key-for-offline-test", max_retries=0)
        request = openai_client.return_value.responses.create.call_args.kwargs
        self.assertEqual(request["model"], "test-model")
        self.assertFalse(request["store"])
        self.assertNotIn("operator-key-for-offline-test", request["input"])
        self.assertEqual(items, [])
        self.assertEqual(usage, {"input_tokens": 3, "output_tokens": 2})


class LocalRequestBoundaryTests(unittest.TestCase):
    EXTENSION_ORIGIN = "chrome-extension://" + "a" * 32

    def test_only_explicit_loopback_hosts_and_extension_origins_are_trusted(self):
        for host in ("127.0.0.1:8766", "localhost:8766", "LOCALHOST:8766"):
            for origin in ("", self.EXTENSION_ORIGIN, "chrome-extension://" + "p" * 32):
                with self.subTest(host=host, origin=origin):
                    self.assertTrue(server.trusted_request_source(host, origin))
        bad_hosts = (
            "", "127.0.0.1", "localhost", "127.0.0.1:8765", "localhost:8765",
            "127.0.0.1:8766.attacker.invalid", "localhost.attacker.invalid:8766",
            "attacker.invalid:8766", "[::1]:8766", "0.0.0.0:8766",
        )
        for host in bad_hosts:
            with self.subTest(host=host):
                self.assertFalse(server.trusted_request_source(host, self.EXTENSION_ORIGIN))
        bad_origins = (
            "null", "https://attacker.invalid", "http://localhost:8766",
            "chrome-extension://", "chrome-extension://" + "a" * 31,
            "chrome-extension://" + "a" * 33, "chrome-extension://" + "q" * 32,
            self.EXTENSION_ORIGIN + "/", self.EXTENSION_ORIGIN + ".attacker.invalid",
        )
        for origin in bad_origins:
            with self.subTest(origin=origin):
                self.assertFalse(server.trusted_request_source("127.0.0.1:8766", origin))

    @staticmethod
    def make_handler(host, origin, path):
        # Avoid BaseHTTPRequestHandler.__init__, sockets, and a real server.
        handler = object.__new__(server.Handler)
        handler.headers = {"Host": host, "Origin": origin}
        handler.path = path
        handler.send_json = Mock()
        handler.rfile = Mock()
        return handler

    def test_invalid_sources_are_rejected_before_data_access_or_paid_work(self):
        requests = (
            ("do_GET", "/health"),
            ("do_GET", "/results?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dabcdefghijk"),
            ("do_GET", "/jobs/00000000-0000-0000-0000-000000000000"),
            ("do_POST", "/jobs"),
            ("do_OPTIONS", "/jobs"),
        )
        sources = (
            ("attacker.invalid:8766", self.EXTENSION_ORIGIN),
            ("127.0.0.1:8766", "https://attacker.invalid"),
            ("127.0.0.1:8766", "null"),
            ("127.0.0.1:8766", "chrome-extension://invalid"),
        )
        for method, path in requests:
            for host, origin in sources:
                with self.subTest(method=method, path=path, host=host, origin=origin), \
                        patch.object(server, "DATA_DIR") as data_dir, \
                        patch.object(server, "latest_result_for_url") as latest_result, \
                        patch.object(server, "local_api_key") as local_key, \
                        patch.object(server, "transcribe_media") as transcribe, \
                        patch.object(server, "enrich_result") as enrich_result, \
                        patch.object(server.threading, "Thread") as thread:
                    handler = self.make_handler(host, origin, path)
                    getattr(handler, method)()
                    handler.send_json.assert_called_once_with(403, {"error": "Local extension requests only"})
                    self.assertEqual(data_dir.mock_calls, [])
                    latest_result.assert_not_called()
                    local_key.assert_not_called()
                    transcribe.assert_not_called()
                    enrich_result.assert_not_called()
                    thread.assert_not_called()
                    handler.rfile.read.assert_not_called()

    def test_valid_source_does_not_trigger_rejection(self):
        handler = self.make_handler("127.0.0.1:8766", self.EXTENSION_ORIGIN, "/jobs")
        self.assertFalse(handler.reject_untrusted_request())
        handler.send_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
