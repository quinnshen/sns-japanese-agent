"""Credential and billing boundaries; all cloud calls are mocked."""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google.auth.exceptions import DefaultCredentialsError

import transcribe_server


class CredentialBoundaryTests(unittest.TestCase):
    def test_missing_project_cannot_use_a_default_account_project(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("google.auth.default") as authenticate:
                with self.assertRaisesRegex(RuntimeError, "GOOGLE_CLOUD_PROJECT"):
                    transcribe_server.transcribe(b"test audio")
                authenticate.assert_not_called()

    def test_operator_project_is_used_for_both_resource_and_quota(self):
        credentials = object()
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "operator-project"}, clear=True):
            with patch("google.auth.default", return_value=(credentials, "different-default-project")) as authenticate:
                with patch("google.cloud.speech_v2.SpeechClient") as client:
                    client.return_value.recognize.return_value = SimpleNamespace(results=[])
                    self.assertEqual(transcribe_server.transcribe(b"test audio"), {"text": "", "words": []})
                    authenticate.assert_called_once_with(
                        scopes=["https://www.googleapis.com/auth/cloud-platform"],
                        quota_project_id="operator-project",
                    )
                    client.assert_called_once_with(credentials=credentials)
                    request = client.return_value.recognize.call_args.kwargs["request"]
                    self.assertEqual(request.recognizer, "projects/operator-project/locations/global/recognizers/_")

    def test_missing_local_credentials_never_falls_back_to_a_bundled_account(self):
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "operator-project"}, clear=True):
            with patch("google.auth.default", side_effect=DefaultCredentialsError("No operator credentials")):
                with patch("google.cloud.speech_v2.SpeechClient") as client:
                    with self.assertRaises(DefaultCredentialsError):
                        transcribe_server.transcribe(b"test audio")
                    client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
