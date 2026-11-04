"""Offline provider tests: every config read, ADC loader and API client is mocked."""

import io
import json
import struct
import unittest
import wave
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch, sentinel

from google.api_core import exceptions

import transcription_google as provider


def wav_audio(seconds=1, *, channels=1, rate=16_000, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(b"\0" * (int(seconds * rate) * channels * width))
    return output.getvalue()


class AudioValidationTests(unittest.TestCase):
    def test_exact_limit_and_one_sample_over(self):
        self.assertEqual(provider.validate_audio(wav_audio(45))["frames"], 45 * 16_000)
        with self.assertRaises(provider.ProviderError) as caught:
            provider.validate_audio(wav_audio(45 + 1 / 16_000))
        self.assertEqual(caught.exception.error_code, "google_audio_too_long")
        self.assertEqual(caught.exception.retry_class, "not_sent")

    def test_unsupported_and_incomplete_formats(self):
        valid = wav_audio()
        streaming = bytearray(valid)
        struct.pack_into("<I", streaming, 4, 0xFFFFFFFF)
        cases = [
            b"not audio", bytearray(valid), wav_audio(0), valid[:-1], valid + b"tail",
            bytes(streaming), wav_audio(channels=2), wav_audio(rate=44_100), wav_audio(width=1),
        ]
        for audio in cases:
            with self.subTest(size=len(audio)), self.assertRaises(provider.ProviderError) as caught:
                provider.validate_audio(audio)
            self.assertEqual(caught.exception.retry_class, "not_sent")

    def test_oversized_body_is_rejected_before_parsing(self):
        with self.assertRaises(provider.ProviderError) as caught:
            provider.validate_audio(b"\0" * (provider.MAX_AUDIO_BYTES + 1))
        self.assertEqual(caught.exception.error_code, "google_audio_too_large")

    def test_ancillary_riff_chunk_is_valid_but_duplicate_data_is_not(self):
        valid = wav_audio()
        ancillary = b"JUNK" + struct.pack("<I", 2) + b"\0\0"
        extended = valid[:12] + ancillary + valid[12:]
        extended = extended[:4] + struct.pack("<I", len(extended) - 8) + extended[8:]
        self.assertEqual(provider.validate_audio(extended)["duration_sec"], 1)
        duplicate = valid + b"data" + struct.pack("<I", 2) + b"\0\0"
        duplicate = duplicate[:4] + struct.pack("<I", len(duplicate) - 8) + duplicate[8:]
        with self.assertRaises(provider.ProviderError):
            provider.validate_audio(duplicate)


class GoogleRecognizerTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.object(provider, "setting", return_value="operator-project")
        self.setting = self.settings.start()
        self.default_patch = patch("google.auth.default", return_value=(sentinel.operator_credentials, "unused-project"))
        self.default_credentials = self.default_patch.start()
        self.client_patch = patch("google.cloud.speech_v2.SpeechClient")
        self.client_type = self.client_patch.start()
        self.client = self.client_type.return_value
        self.client.recognize.return_value = SimpleNamespace(results=[])
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.default_patch.stop)
        self.addCleanup(self.settings.stop)

    def test_constructor_is_lazy_and_rejects_missing_project(self):
        provider.GoogleRecognizer()
        self.default_credentials.assert_not_called()
        self.client_type.assert_not_called()
        for project in (None, "", "   ", "YOUR_PROJECT_ID", "your_project_id", "REPLACE_ME", "<your-project>"):
            self.setting.return_value = project
            with self.subTest(project=project), self.assertRaises(provider.ProviderError) as caught:
                provider.GoogleRecognizer()
            self.assertEqual(caught.exception.retry_class, "not_sent")
        self.default_credentials.assert_not_called()

    def test_prepare_uses_operator_credentials_and_explicit_quota(self):
        recognizer = provider.GoogleRecognizer()
        self.assertIs(recognizer.prepare(), recognizer)
        self.assertIs(recognizer.prepare(), recognizer)
        self.default_credentials.assert_called_once_with(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            quota_project_id="operator-project",
        )
        self.client_type.assert_called_once_with(credentials=sentinel.operator_credentials)
        self.client.recognize.assert_not_called()

    def test_initialization_failure_is_safe_and_not_sent(self):
        secret = "sk-" + "x" * 32
        self.default_credentials.side_effect = RuntimeError(secret)
        with self.assertRaises(provider.ProviderError) as caught:
            provider.GoogleRecognizer().prepare()
        self.assertEqual(caught.exception.error_code, "google_initialization_failed")
        self.assertEqual(caught.exception.retry_class, "not_sent")
        self.assertNotIn(secret, str(caught.exception))
        self.client_type.assert_not_called()

    def test_public_config_and_fingerprint_exclude_account(self):
        first = provider.GoogleRecognizer()
        self.setting.return_value = "another-operator-project"
        second = provider.GoogleRecognizer()
        self.assertEqual(first.config["language_codes"], ["ja-JP"])
        self.assertEqual(first.config["model"], "long")
        self.assertEqual(first.config["location"], "global")
        self.assertTrue(first.config["enable_word_time_offsets"])
        self.assertTrue(first.config["enable_automatic_punctuation"])
        self.assertEqual(first.provider_fingerprint, second.provider_fingerprint)
        self.assertRegex(first.provider_fingerprint, r"^[0-9a-f]{64}$")
        self.assertNotIn("operator-project", json.dumps(first.config))
        first.config["language_codes"].append("other")
        self.assertEqual(first.config["language_codes"], ["ja-JP"])

    def test_invalid_audio_never_loads_adc_or_starts_rpc(self):
        with self.assertRaises(provider.ProviderError):
            provider.GoogleRecognizer().recognize(b"invalid")
        self.default_credentials.assert_not_called()
        self.client.recognize.assert_not_called()

    def test_request_has_no_gapic_retry_and_exact_audio(self):
        audio = wav_audio()
        recognizer = provider.GoogleRecognizer()
        self.assertEqual(recognizer(audio), [])
        self.client.recognize.assert_called_once()
        arguments = self.client.recognize.call_args.kwargs
        self.assertIsNone(arguments["retry"])
        self.assertEqual(arguments["timeout"], 120)
        request = arguments["request"]
        self.assertEqual(request.recognizer, "projects/operator-project/locations/global/recognizers/_")
        self.assertEqual(request.content, audio)
        self.assertEqual(list(request.config.language_codes), ["ja-JP"])
        self.assertEqual(request.config.model, "long")
        self.assertTrue(request.config.features.enable_word_time_offsets)
        self.assertTrue(request.config.features.enable_automatic_punctuation)

    def test_words_and_text_without_words_are_retained(self):
        word = SimpleNamespace(word="日語", start_offset=timedelta(seconds=0.1), end_offset=timedelta(seconds=0.6))
        alternatives = [
            SimpleNamespace(transcript="日語。", words=[word]),
            SimpleNamespace(transcript="言葉のみ。", words=[]),
        ]
        self.client.recognize.return_value = SimpleNamespace(results=[
            SimpleNamespace(alternatives=[item]) for item in alternatives
        ])
        result = provider.GoogleRecognizer().recognize(wav_audio())
        self.assertEqual(result, [
            {"text": "日語。", "words": [{"text": "日語", "start": 0.1, "end": 0.6}]},
            {"text": "言葉のみ。", "words": []},
        ])

    def test_rejected_errors_are_not_retried_or_echoed(self):
        secret = "sk-" + "q" * 32
        for error_type in (
            exceptions.InvalidArgument, exceptions.PermissionDenied,
            exceptions.Unauthenticated, exceptions.ResourceExhausted,
        ):
            with self.subTest(error=error_type.__name__):
                self.client.recognize.reset_mock()
                self.client.recognize.side_effect = error_type(secret)
                with self.assertRaises(provider.ProviderError) as caught:
                    provider.GoogleRecognizer().recognize(wav_audio())
                self.assertEqual(caught.exception.retry_class, "rejected")
                self.assertNotIn(secret, str(caught.exception))
                self.client.recognize.assert_called_once()

    def test_timeout_network_and_other_rpc_errors_are_unknown(self):
        secret = "sk-" + "z" * 32
        for error_type in (
            exceptions.DeadlineExceeded, exceptions.ServiceUnavailable, exceptions.InternalServerError,
            exceptions.Cancelled, TimeoutError, ConnectionError, RuntimeError,
        ):
            with self.subTest(error=error_type.__name__):
                self.client.recognize.reset_mock()
                self.client.recognize.side_effect = error_type(secret)
                with self.assertRaises(provider.ProviderError) as caught:
                    provider.GoogleRecognizer().recognize(wav_audio())
                self.assertEqual(caught.exception.retry_class, "unknown")
                self.assertNotIn(secret, str(caught.exception))
                self.client.recognize.assert_called_once()

    def test_malformed_success_is_unknown_not_an_automatic_rebill(self):
        self.client.recognize.return_value = Mock(results=None)
        with self.assertRaises(provider.ProviderError) as caught:
            provider.GoogleRecognizer().recognize(wav_audio())
        self.assertEqual(caught.exception.error_code, "google_response_invalid")
        self.assertEqual(caught.exception.retry_class, "unknown")
        self.client.recognize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
