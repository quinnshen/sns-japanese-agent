"""Google Speech provider with explicit operator credentials and no RPC retries.

Constructing a recognizer does not load ADC or contact Google. Call prepare()
and validate_audio() before marking an attempt in flight. The worker owns
durable state, recovery and any user-authorized retry policy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import threading

from config import setting

MAX_AUDIO_SECONDS = 45
MAX_AUDIO_BYTES = 10_000_000
SAMPLE_RATE = 16_000


def public_config() -> dict:
    return {
        "provider": "google_speech_v2", "version": 1,
        "location": "global", "model": "long", "language_codes": ["ja-JP"],
        "enable_word_time_offsets": True, "enable_automatic_punctuation": True,
        "audio_format": "wav_pcm_s16le", "sample_rate_hertz": SAMPLE_RATE,
        "audio_channel_count": 1, "max_audio_seconds": MAX_AUDIO_SECONDS,
    }


class ProviderError(RuntimeError):
    """A fixed, safe code and outcome class; no SDK message or account values."""

    def __init__(self, error_code: str, retry_class: str):
        self.error_code = error_code
        self.retry_class = retry_class
        super().__init__(error_code)


def validate_audio(audio: bytes) -> dict:
    """Accept finite, complete mono 16 kHz PCM16 WAV bytes, at most 45 seconds.

    Streaming WAV headers with unknown lengths are intentionally rejected. Use
    a WAV writer with the final PCM byte count before creating a paid attempt.
    Ancillary RIFF chunks are accepted; duplicate fmt/data chunks are not.
    """
    if not isinstance(audio, bytes):
        raise ProviderError("google_audio_invalid_wav", "not_sent")
    if len(audio) > MAX_AUDIO_BYTES:
        raise ProviderError("google_audio_too_large", "not_sent")
    if (len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE"
            or struct.unpack_from("<I", audio, 4)[0] != len(audio) - 8):
        raise ProviderError("google_audio_invalid_wav", "not_sent")
    position = 12
    format_seen = False
    data_bytes = None
    while position < len(audio):
        if len(audio) - position < 8:
            raise ProviderError("google_audio_invalid_wav", "not_sent")
        chunk = audio[position:position + 4]
        size = struct.unpack_from("<I", audio, position + 4)[0]
        start, end = position + 8, position + 8 + size
        padded_end = end + size % 2
        if end > len(audio) or padded_end > len(audio):
            raise ProviderError("google_audio_invalid_wav", "not_sent")
        if chunk == b"fmt ":
            if format_seen or data_bytes is not None or size not in (16, 18):
                raise ProviderError("google_audio_invalid_wav", "not_sent")
            values = struct.unpack_from("<HHIIHH", audio, start)
            if values != (1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16):
                raise ProviderError("google_audio_unsupported_format", "not_sent")
            if size == 18 and struct.unpack_from("<H", audio, start + 16)[0] != 0:
                raise ProviderError("google_audio_invalid_wav", "not_sent")
            format_seen = True
        elif chunk == b"data":
            if not format_seen or data_bytes is not None or size % 2:
                raise ProviderError("google_audio_invalid_wav", "not_sent")
            data_bytes = size
        position = padded_end
    if not format_seen or data_bytes is None or data_bytes == 0:
        raise ProviderError("google_audio_invalid_wav", "not_sent")
    frames = data_bytes // 2
    if frames > SAMPLE_RATE * MAX_AUDIO_SECONDS:
        raise ProviderError("google_audio_too_long", "not_sent")
    return {"frames": frames, "duration_sec": frames / SAMPLE_RATE, "size_bytes": len(audio)}


class GoogleRecognizer:
    def __init__(self):
        try:
            project = (setting("GOOGLE_CLOUD_PROJECT", "") or "").strip()
        except Exception:
            raise ProviderError("google_configuration_unavailable", "not_sent") from None
        if (not project or re.fullmatch(r"(?:YOUR_[A-Z0-9_]+|REPLACE_ME|CHANGE_ME|<[^>]*>)", project, re.IGNORECASE)):
            raise ProviderError("google_project_not_configured", "not_sent")
        self._project_id = project
        self._client = None
        self._prepare_lock = threading.Lock()

    @property
    def config(self) -> dict:
        """Public recognition settings only; intentionally excludes account data."""
        return public_config()

    @property
    def provider_fingerprint(self) -> str:
        canonical = json.dumps(self.config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def validate_audio(audio: bytes) -> dict:
        return validate_audio(audio)

    def prepare(self) -> GoogleRecognizer:
        """Load this operator's ADC and client before durable in-flight state."""
        with self._prepare_lock:
            if self._client is not None:
                return self
            try:
                from google.auth import default as default_credentials
                from google.cloud.speech_v2 import SpeechClient

                credentials, _ = default_credentials(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                    quota_project_id=self._project_id,
                )
                self._client = SpeechClient(credentials=credentials)
            except Exception:
                raise ProviderError("google_initialization_failed", "not_sent") from None
        return self

    @staticmethod
    def _rpc_error(error: Exception) -> ProviderError:
        from google.api_core import exceptions

        rejected = (
            (exceptions.InvalidArgument, "google_invalid_argument"),
            (exceptions.PermissionDenied, "google_permission_denied"),
            (exceptions.Unauthenticated, "google_unauthenticated"),
            (exceptions.ResourceExhausted, "google_resource_exhausted"),
        )
        for exception_type, code in rejected:
            if isinstance(error, exception_type):
                return ProviderError(code, "rejected")
        if isinstance(error, (exceptions.DeadlineExceeded, TimeoutError)):
            return ProviderError("google_deadline_unknown", "unknown")
        if isinstance(error, exceptions.ServiceUnavailable):
            return ProviderError("google_unavailable_unknown", "unknown")
        return ProviderError("google_request_unknown", "unknown")

    def recognize(self, audio: bytes) -> list[dict]:
        validate_audio(audio)
        self.prepare()
        try:
            from google.cloud.speech_v2.types import cloud_speech

            request = cloud_speech.RecognizeRequest(
                recognizer=f"projects/{self._project_id}/locations/global/recognizers/_",
                config=cloud_speech.RecognitionConfig(
                    auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
                    language_codes=["ja-JP"], model="long",
                    features=cloud_speech.RecognitionFeatures(
                        enable_word_time_offsets=True, enable_automatic_punctuation=True,
                    ),
                ),
                content=audio,
            )
        except Exception:
            raise ProviderError("google_request_preparation_failed", "not_sent") from None
        try:
            response = self._client.recognize(request=request, retry=None, timeout=120)
        except Exception as error:
            raise self._rpc_error(error) from None
        try:
            results = []
            for result in response.results:
                if not result.alternatives:
                    continue
                alternative = result.alternatives[0]
                words = []
                for word in alternative.words:
                    start = word.start_offset.total_seconds()
                    end = word.end_offset.total_seconds()
                    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end):
                        raise ValueError
                    words.append({"text": word.word, "start": start, "end": end})
                # A transcript without word offsets is still a successful paid
                # response. Preserve it for a downstream coverage warning.
                results.append({"text": alternative.transcript, "words": words})
            return results
        except Exception:
            raise ProviderError("google_response_invalid", "unknown") from None

    def __call__(self, audio: bytes) -> list[dict]:
        return self.recognize(audio)
