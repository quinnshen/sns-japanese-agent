"""Durable, local-only storage for one continuous browser recording.

Upload batches are container fragments, not standalone audio files. This module
stores observed timing and concatenates bytes only; it does not decode audio,
read credentials, or schedule cloud work.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SCHEMA_VERSION = 1
MAX_BATCH_BYTES = 10_000_000
MAX_TOTAL_BYTES = 200_000_000
MAX_RANGE_SECONDS = 30 * 60
# A defensive metadata bound; normal 30-minute captures at 1s need <= 1,801.
MAX_BATCHES = 10_000
MAX_PLAYBACK_EVENTS = 1_000
VIDEO_TOLERANCE_SECONDS = 1.0
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_CREATE_KEYS = {
    "id", "source_url", "video_duration_sec", "range_start_sec",
    "range_end_sec", "batch_interval_ms", "mime_type",
}
_START_KEYS = {
    "recording_started_at_ms", "playback_started_at_ms", "playback_start_video_sec",
}
_FINISH_KEYS = {"expected_batches", "recording_stopped_at_ms", "playback_end_video_sec"}
_ABORT_TIMING_KEYS = {"recording_stopped_at_ms", "playback_end_video_sec", "playback_events"}
_FAILURE_FLAGS = {
    "identity_changed", "video_replaced", "ad_showing", "video_connected", "player_connected",
}
_FAILURE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class CaptureError(Exception):
    """An API-safe failure: messages never include paths, URLs, or raw data."""

    def __init__(self, status: int, error: str, **details):
        super().__init__(error)
        self.status = status
        self.error = error
        self.details = details


def _object(payload: dict, required: set[str], optional: set[str] | None = None) -> None:
    if (
        not isinstance(payload, dict)
        or not required <= payload.keys()
        or not payload.keys() <= required | (optional or set())
    ):
        raise CaptureError(400, "Invalid capture metadata")


def _number(value, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptureError(400, "Capture times must be finite numbers")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise CaptureError(400, "Capture times must be finite numbers") from None
    if not math.isfinite(result) or result < minimum:
        raise CaptureError(400, "Capture times must be finite numbers")
    return result


def _integer(value, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CaptureError(400, "Invalid capture count or interval")
    return value


def _capture_id(value: str) -> str:
    if not isinstance(value, str):
        raise CaptureError(400, "Invalid capture identifier")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise CaptureError(400, "Invalid capture identifier") from None
    if str(parsed) != value:
        raise CaptureError(400, "Invalid capture identifier")
    return value


def _youtube_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        raise CaptureError(400, "Invalid YouTube source URL")
    try:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"youtube.com", "www.youtube.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or any(character.isspace() for character in value)
        ):
            raise ValueError
        if parsed.path == "/watch":
            candidates = parse_qs(parsed.query).get("v", [])
            video_id = candidates[0] if len(candidates) == 1 else ""
        elif parsed.path.startswith("/shorts/"):
            video_id = parsed.path[len("/shorts/"):].removesuffix("/")
        else:
            video_id = ""
        if _VIDEO_ID_RE.fullmatch(video_id) is None:
            raise ValueError
    except ValueError:
        raise CaptureError(400, "Invalid YouTube source URL") from None
    return value


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(directory: Path) -> None:
    if directory.is_symlink():
        raise CaptureError(409, "Capture storage path is invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir():
        raise CaptureError(409, "Capture storage path is invalid")
    directory.chmod(0o700)


def _regular_reader(path: Path):
    if path.is_symlink():
        raise CaptureError(409, "Stored capture data is unavailable")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise CaptureError(409, "Stored capture data is unavailable") from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CaptureError(409, "Stored capture data is unavailable")
    return os.fdopen(descriptor, "rb")


def _atomic_bytes(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise CaptureError(409, "Capture storage path is invalid")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class CaptureStore:
    """Store capture manifests and raw fragments, acknowledging durable writes."""

    def __init__(self, root: Path):
        root = Path(root)
        _private_directory(root)
        self.root = root.resolve()
        self._lock = threading.RLock()

    def _directory(self, capture_id: str) -> Path:
        directory = self.root / _capture_id(capture_id)
        if directory.is_symlink():
            raise CaptureError(409, "Capture storage path is invalid")
        return directory

    def _load(self, capture_id: str) -> dict:
        directory = self._directory(capture_id)
        if not directory.exists():
            raise CaptureError(404, "Capture not found")
        path = directory / "manifest.json"
        if not path.exists() and not path.is_symlink():
            raise CaptureError(409, "Capture manifest is unavailable")
        try:
            with _regular_reader(path) as source:
                manifest = json.load(source)
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("id") != capture_id
                or manifest.get("status") not in {"recording", "complete", "aborted"}
                or not isinstance(manifest.get("batches"), list)
                or not isinstance(manifest.get("timing"), dict)
            ):
                raise ValueError
            # Stored metadata is validated too: a damaged JSON document must
            # produce a safe error, never an unbounded missing list or path.
            if not _CREATE_KEYS <= manifest.keys():
                raise ValueError
            _youtube_url(manifest["source_url"])
            duration = _number(manifest["video_duration_sec"], minimum=1)
            start = _number(manifest["range_start_sec"])
            end = _number(manifest["range_end_sec"])
            if end > duration or not 1 <= end - start <= MAX_RANGE_SECONDS:
                raise ValueError
            _integer(manifest["batch_interval_ms"], 1_000, 30_000)
            if not isinstance(manifest["mime_type"], str) or manifest["mime_type"] not in {
                "audio/webm", "audio/webm;codecs=opus",
            }:
                raise ValueError
            expected = manifest["expected_batches"]
            if expected is not None:
                _integer(expected, 1, MAX_BATCHES)
            if len(manifest["batches"]) > MAX_BATCHES:
                raise ValueError
            sequences = []
            byte_count = 0
            for batch in manifest["batches"]:
                if not isinstance(batch, dict):
                    raise ValueError
                sequence = _integer(batch["sequence"], 0, MAX_BATCHES - 1)
                sequences.append(sequence)
                if batch["filename"] != f"batches/{sequence:06d}.webm":
                    raise ValueError
                if not isinstance(batch["sha256"], str) or _HASH_RE.fullmatch(batch["sha256"]) is None:
                    raise ValueError
                byte_count += _integer(batch["size_bytes"], 1, MAX_BATCH_BYTES)
                observed_start = _number(batch["capture_start_ms"])
                observed_end = _number(batch["capture_end_ms"])
                if observed_end < observed_start:
                    raise ValueError
                if "blob_timecode_ms" in batch:
                    _number(batch["blob_timecode_ms"])
            if sequences != sorted(set(sequences)):
                raise ValueError
            if manifest["received_batches"] != len(sequences) or manifest["total_bytes"] != byte_count:
                raise ValueError
            _integer(manifest["total_bytes"], 0, MAX_TOTAL_BYTES)
            if manifest["timing"]:
                present_start = _START_KEYS & manifest["timing"].keys()
                if present_start and present_start != _START_KEYS:
                    raise ValueError
                if not present_start and manifest["status"] != "aborted":
                    raise ValueError
                numeric_timing_keys = present_start | (
                    (_ABORT_TIMING_KEYS - {"playback_events"}) & manifest["timing"].keys()
                )
                for key in numeric_timing_keys:
                    _number(manifest["timing"][key])
        except (ValueError, TypeError, KeyError, UnicodeError, CaptureError):
            raise CaptureError(409, "Capture manifest is corrupt") from None
        return manifest

    def _batch_path(self, capture_id: str, filename: str) -> Path:
        directory = self._directory(capture_id) / "batches"
        if directory.is_symlink() or not directory.is_dir():
            raise CaptureError(409, "Capture storage path is invalid")
        return self._directory(capture_id) / filename

    def _persist(self, manifest: dict) -> None:
        data = json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        _atomic_bytes(self._directory(manifest["id"]) / "manifest.json", data)

    @staticmethod
    def _missing(manifest: dict) -> None:
        expected = manifest["expected_batches"]
        if expected is not None:
            received = {batch["sequence"] for batch in manifest["batches"]}
            manifest["missing_sequences"] = [index for index in range(expected) if index not in received]

    @staticmethod
    def _ack(manifest: dict, batch: dict, duplicate: bool) -> dict:
        return {
            "id": manifest["id"], "sequence": batch["sequence"], "stored": True,
            "duplicate": duplicate, "sha256": batch["sha256"], "size_bytes": batch["size_bytes"],
            "received_batches": manifest["received_batches"], "total_bytes": manifest["total_bytes"],
        }

    def create(self, payload: dict) -> dict:
        _object(payload, _CREATE_KEYS)
        capture_id = _capture_id(payload["id"])
        duration = _number(payload["video_duration_sec"], minimum=1)
        start = _number(payload["range_start_sec"])
        end = _number(payload["range_end_sec"])
        if end > duration or not 1 <= end - start <= MAX_RANGE_SECONDS:
            raise CaptureError(400, "Invalid selected video range")
        interval = _integer(payload["batch_interval_ms"], 1_000, 30_000)
        mime = payload["mime_type"]
        if not isinstance(mime, str) or mime not in {"audio/webm", "audio/webm;codecs=opus"}:
            raise CaptureError(400, "Unsupported capture audio type")
        metadata = {
            "id": capture_id, "source_url": _youtube_url(payload["source_url"]),
            "video_duration_sec": duration, "range_start_sec": start, "range_end_sec": end,
            "batch_interval_ms": interval, "mime_type": mime,
        }
        with self._lock:
            directory = self._directory(capture_id)
            if directory.exists():
                existing = self._load(capture_id)
                if any(existing.get(key) != value for key, value in metadata.items()):
                    raise CaptureError(409, "Capture identifier has different metadata")
                return existing
            _private_directory(directory)
            _private_directory(directory / "batches")
            _sync_directory(self.root)
            _sync_directory(directory)
            manifest = {
                "schema_version": SCHEMA_VERSION, **metadata, "status": "recording", "timing": {},
                "batches": [], "received_batches": 0, "total_bytes": 0, "expected_batches": None,
            }
            self._persist(manifest)
            return manifest

    def get(self, capture_id: str) -> dict:
        with self._lock:
            manifest = self._load(capture_id)
            self._missing(manifest)
            return manifest

    def set_timing(self, capture_id: str, payload: dict) -> dict:
        _object(payload, _START_KEYS)
        timing = {key: _number(payload[key]) for key in _START_KEYS}
        if timing["playback_started_at_ms"] < timing["recording_started_at_ms"]:
            raise CaptureError(400, "Playback cannot start before recording")
        with self._lock:
            manifest = self._load(capture_id)
            position = timing["playback_start_video_sec"]
            if (
                position < max(0, manifest["range_start_sec"] - VIDEO_TOLERANCE_SECONDS)
                or position >= manifest["range_end_sec"]
                or position > manifest["video_duration_sec"]
            ):
                raise CaptureError(400, "Playback start is outside the selected range")
            if manifest["timing"]:
                if any(manifest["timing"].get(key) != value for key, value in timing.items()):
                    raise CaptureError(409, "Capture timing is already set")
                return manifest
            if manifest["status"] != "recording":
                raise CaptureError(409, "Capture is no longer recording")
            manifest["timing"] = timing
            self._persist(manifest)
            return manifest

    def put_batch(self, capture_id: str, sequence: int, body: bytes, metadata: dict) -> dict:
        sequence = _integer(sequence, 0, MAX_BATCHES - 1)
        _object(metadata, {"sha256", "capture_start_ms", "capture_end_ms"}, {"blob_timecode_ms"})
        if not isinstance(body, bytes) or not body:
            raise CaptureError(400, "Audio batch must contain bytes")
        if len(body) > MAX_BATCH_BYTES:
            raise CaptureError(413, "Audio batch is too large")
        digest = metadata["sha256"]
        if not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None:
            raise CaptureError(400, "Invalid batch checksum")
        if hashlib.sha256(body).hexdigest() != digest:
            raise CaptureError(400, "Batch checksum does not match audio")
        start = _number(metadata["capture_start_ms"])
        end = _number(metadata["capture_end_ms"])
        if end < start:
            raise CaptureError(400, "Invalid batch capture interval")
        batch = {
            "sequence": sequence, "filename": f"batches/{sequence:06d}.webm", "sha256": digest,
            "size_bytes": len(body), "capture_start_ms": start, "capture_end_ms": end,
        }
        if "blob_timecode_ms" in metadata:
            batch["blob_timecode_ms"] = _number(metadata["blob_timecode_ms"])
        with self._lock:
            manifest = self._load(capture_id)
            for previous in manifest["batches"]:
                if previous["sequence"] == sequence:
                    if previous != batch:
                        raise CaptureError(409, "Batch sequence has different content or timing")
                    path = self._batch_path(capture_id, batch["filename"])
                    with _regular_reader(path) as source:
                        content = source.read(MAX_BATCH_BYTES + 1)
                    if content != body:
                        raise CaptureError(409, "Stored batch is corrupt")
                    return self._ack(manifest, previous, True)
            if manifest["status"] != "recording":
                raise CaptureError(409, "Capture is no longer recording")
            expected = manifest["expected_batches"]
            if expected is not None and sequence >= expected:
                raise CaptureError(409, "Unexpected batch sequence", unexpected_sequences=[sequence])
            if manifest["total_bytes"] + len(body) > MAX_TOTAL_BYTES:
                raise CaptureError(413, "Capture audio is too large")
            for previous in manifest["batches"]:
                if (
                    previous["sequence"] < sequence and previous["capture_end_ms"] > start + 1
                ) or (
                    previous["sequence"] > sequence and end > previous["capture_start_ms"] + 1
                ):
                    raise CaptureError(409, "Batch timing conflicts with sequence order")
            directory = self._directory(capture_id)
            _private_directory(directory / "batches")
            _atomic_bytes(directory / batch["filename"], body)
            manifest["batches"].append(batch)
            manifest["batches"].sort(key=lambda item: item["sequence"])
            manifest["received_batches"] = len(manifest["batches"])
            manifest["total_bytes"] += len(body)
            self._missing(manifest)
            self._persist(manifest)
            return self._ack(manifest, batch, False)

    @staticmethod
    def _playback_events(manifest: dict, events: list, stopped: float | None, *, interrupted: bool = False) -> list:
        timing = manifest["timing"]
        if not isinstance(events, list) or len(events) > MAX_PLAYBACK_EVENTS:
            raise CaptureError(400, "Invalid playback events")
        normalized = []
        previous_at = timing.get("recording_started_at_ms", 0)
        minimum_position = 0 if interrupted else max(0, manifest["range_start_sec"] - VIDEO_TOLERANCE_SECONDS)
        maximum_position = manifest["video_duration_sec"] if interrupted else manifest["range_end_sec"]
        for event in events:
            _object(event, {"type", "at_ms", "video_sec"})
            kind = event["type"]
            if not isinstance(kind, str) or kind not in {"playing", "waiting"}:
                raise CaptureError(400, "Invalid playback event type")
            at = _number(event["at_ms"])
            position = _number(event["video_sec"])
            if (
                at < previous_at or (stopped is not None and at > stopped)
                or position < minimum_position
                or position > maximum_position + VIDEO_TOLERANCE_SECONDS
                or (not normalized and kind != "playing")
            ):
                raise CaptureError(400, "Invalid playback event timing")
            normalized.append({"type": kind, "at_ms": at, "video_sec": position})
            previous_at = at
        return normalized

    @staticmethod
    def _finish_metadata(manifest: dict, payload: dict) -> dict:
        _object(payload, _FINISH_KEYS, {"playback_events"})
        expected = _integer(payload["expected_batches"], 1, MAX_BATCHES)
        stopped = _number(payload["recording_stopped_at_ms"])
        end = _number(payload["playback_end_video_sec"])
        timing = manifest["timing"]
        if not _START_KEYS <= timing.keys():
            raise CaptureError(409, "Capture start timing is missing")
        if (
            stopped < timing["playback_started_at_ms"]
            or end < timing["playback_start_video_sec"]
            or end < manifest["range_end_sec"] - VIDEO_TOLERANCE_SECONDS
            or end > min(manifest["video_duration_sec"], manifest["range_end_sec"]) + VIDEO_TOLERANCE_SECONDS
        ):
            raise CaptureError(400, "Invalid recording completion timing")
        normalized = CaptureStore._playback_events(manifest, payload.get("playback_events", []), stopped)
        return {
            "expected_batches": expected, "recording_stopped_at_ms": stopped,
            "playback_end_video_sec": end, "playback_events": normalized,
        }

    def _assemble(self, manifest: dict) -> tuple[str, int]:
        directory = self._directory(manifest["id"])
        target = directory / "recording.webm"
        if target.is_symlink():
            raise CaptureError(409, "Capture storage path is invalid")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".recording.webm.", dir=directory)
        temporary = Path(temporary_name)
        audio_hash = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                for batch in manifest["batches"]:
                    expected_path = f"batches/{batch['sequence']:06d}.webm"
                    if batch["filename"] != expected_path:
                        raise CaptureError(409, "Stored batch metadata is corrupt")
                    digest = hashlib.sha256()
                    batch_size = 0
                    with _regular_reader(self._batch_path(manifest["id"], expected_path)) as source:
                        while content := source.read(64 * 1_024):
                            batch_size += len(content)
                            if batch_size > MAX_BATCH_BYTES:
                                raise CaptureError(409, "Stored batch is corrupt")
                            digest.update(content)
                            audio_hash.update(content)
                            output.write(content)
                    if digest.hexdigest() != batch["sha256"] or batch_size != batch["size_bytes"]:
                        raise CaptureError(409, "Stored batch is corrupt")
                    size += batch_size
                if size != manifest["total_bytes"]:
                    raise CaptureError(409, "Stored capture byte count is corrupt")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            _sync_directory(directory)
            return audio_hash.hexdigest(), size
        finally:
            temporary.unlink(missing_ok=True)

    def complete(self, capture_id: str, payload: dict) -> dict:
        with self._lock:
            manifest = self._load(capture_id)
            finish = self._finish_metadata(manifest, payload)
            expected = finish["expected_batches"]
            if manifest["expected_batches"] is not None:
                previous = {"expected_batches": manifest["expected_batches"]}
                previous.update({key: manifest["timing"].get(key) for key in finish if key != "expected_batches"})
                if previous != finish:
                    raise CaptureError(409, "Capture completion has different metadata")
            if manifest["status"] == "complete":
                return manifest
            if manifest["status"] != "recording":
                raise CaptureError(409, "Capture is no longer recording")
            extra = [batch["sequence"] for batch in manifest["batches"] if batch["sequence"] >= expected]
            if extra:
                raise CaptureError(409, "Capture has unexpected batches", unexpected_sequences=extra)
            recording_elapsed = finish["recording_stopped_at_ms"] - manifest["timing"]["recording_started_at_ms"]
            if any(batch["capture_end_ms"] > recording_elapsed + 1_000 for batch in manifest["batches"]):
                raise CaptureError(400, "Batch timing exceeds recording completion")
            manifest["expected_batches"] = expected
            manifest["timing"].update({key: value for key, value in finish.items() if key != "expected_batches"})
            self._missing(manifest)
            self._persist(manifest)
            if manifest["missing_sequences"]:
                raise CaptureError(409, "Capture is missing batches", missing_sequences=manifest["missing_sequences"])
            digest, size = self._assemble(manifest)
            manifest.update(
                status="complete", audio_sha256=digest, audio_size_bytes=size,
                audio_url=f"/captures/{capture_id}/audio",
            )
            self._persist(manifest)
            return manifest

    @staticmethod
    def _failure_metadata(manifest: dict, payload: dict) -> dict:
        _object(payload, {"code", "message"}, _FAILURE_FLAGS | {"at_ms", "video_sec"})
        code, message = payload["code"], payload["message"]
        if not isinstance(code, str) or _FAILURE_CODE_RE.fullmatch(code) is None:
            raise CaptureError(400, "Invalid capture failure code")
        # These are fixed diagnostic messages, never raw browser/fetch errors.
        # Reject paths, URLs, email addresses and control characters before any
        # local persistence; do not echo the rejected text in an API error.
        if (
            not isinstance(message, str) or not message.strip() or len(message) > 240
            or any(character in message for character in "/\\@")
            or any(ord(character) < 32 or ord(character) == 127 for character in message)
            or re.search(r"(?:[a-z][a-z0-9+.-]*:|www\.|\b[a-z0-9-]+\.[a-z]{2,}\b)", message, re.IGNORECASE)
        ):
            raise CaptureError(400, "Invalid capture failure message")
        failure = {"code": code, "message": message}
        for key in _FAILURE_FLAGS:
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise CaptureError(400, "Invalid capture failure flag")
                failure[key] = payload[key]
        if "video_sec" in payload:
            position = _number(payload["video_sec"])
            if position > manifest["video_duration_sec"] + VIDEO_TOLERANCE_SECONDS:
                raise CaptureError(400, "Invalid capture failure video position")
            failure["video_sec"] = position
        if "at_ms" in payload:
            observed = _number(payload["at_ms"])
            # Upload or completion failures can be detected after recording
            # has stopped; this is the error observation time, not audio PTS.
            if observed < manifest["timing"].get("recording_started_at_ms", 0):
                raise CaptureError(400, "Invalid capture failure observation time")
            failure["at_ms"] = observed
        return failure

    def abort(self, capture_id: str, payload: dict | None = None) -> dict:
        payload = {} if payload is None else payload
        _object(payload, set(), _ABORT_TIMING_KEYS | {"failure"})
        with self._lock:
            manifest = self._load(capture_id)
            if manifest["status"] == "complete":
                raise CaptureError(409, "Completed capture cannot be aborted")
            observed = {}
            stopped = None
            if "recording_stopped_at_ms" in payload:
                stopped = _number(payload["recording_stopped_at_ms"])
                if stopped < max(
                    manifest["timing"].get("recording_started_at_ms", 0),
                    manifest["timing"].get("playback_started_at_ms", 0),
                ):
                    raise CaptureError(400, "Invalid interrupted recording timing")
                observed["recording_stopped_at_ms"] = stopped
            if "playback_end_video_sec" in payload:
                end = _number(payload["playback_end_video_sec"])
                if end > manifest["video_duration_sec"] + VIDEO_TOLERANCE_SECONDS:
                    raise CaptureError(400, "Invalid interrupted playback position")
                observed["playback_end_video_sec"] = end
            if "playback_events" in payload:
                observed["playback_events"] = self._playback_events(
                    manifest, payload["playback_events"], stopped, interrupted=True,
                )
            failure = self._failure_metadata(manifest, payload["failure"]) if "failure" in payload else None
            changed = False
            # Preserve the original failure and stop observations. A subsequent
            # user cancellation or an unknown request outcome must not erase it.
            if failure is not None and "failure" not in manifest:
                manifest["failure"] = failure
                changed = True
            for key, value in observed.items():
                if key not in manifest["timing"]:
                    manifest["timing"][key] = value
                    changed = True
            if manifest["status"] != "aborted":
                manifest["status"] = "aborted"
                changed = True
            if changed:
                self._persist(manifest)
            return manifest

    def audio_path(self, capture_id: str) -> Path:
        with self._lock:
            manifest = self._load(capture_id)
            if manifest["status"] != "complete":
                raise CaptureError(409, "Capture audio is not complete")
            path = self._directory(capture_id) / "recording.webm"
            with _regular_reader(path) as source:
                if os.fstat(source.fileno()).st_size != manifest["audio_size_bytes"]:
                    raise CaptureError(409, "Stored capture audio is corrupt")
            return path
