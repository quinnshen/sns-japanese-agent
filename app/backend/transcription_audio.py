"""Local preparation of continuous capture audio and observed video-time mapping.

Upload fragments are storage units, never independent media files. PCM sample
positions are measured; browser wall clocks and playback events are observations
and cannot establish sample-accurate alignment. No credentials or cloud APIs are
used by this module.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import threading
import uuid
import wave
from pathlib import Path

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
MAX_SOURCE_BYTES = 200_000_000
MAX_PCM_BYTES = (30 * 60 + 120) * SAMPLE_RATE * SAMPLE_WIDTH
MAX_BATCHES = 10_000
_PROFILE = "continuous-webm-pcm-s16le-mono-16000-v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_PREMATURE_END = re.compile(
    r"(?:\[matroska,webm @ (?:0x)?[0-9a-fA-F]+\]\s*)?"
    r"File ended prematurely(?: at pos\. \d+ \(0x[0-9a-fA-F]+\))?\.?"
)
_PREPARE_LOCK = threading.RLock()


class TranscriptionAudioError(ValueError):
    """A safe local failure, without paths, source URLs, or decoder stderr."""


def _number(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptionAudioError("An audio count or timing value is invalid.")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise TranscriptionAudioError("An audio count or timing value is invalid.") from None
    if not math.isfinite(value) or value < minimum:
        raise TranscriptionAudioError("An audio count or timing value is invalid.")
    return value


def _integer(value, minimum=0, maximum=MAX_SOURCE_BYTES):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TranscriptionAudioError("An audio count or timing value is invalid.")
    return value


def _reader(path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise OSError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError
        return os.fdopen(descriptor, "rb")
    except OSError:
        raise TranscriptionAudioError("Required local audio data is unavailable.") from None


def _directory(path, create=False):
    if path.is_symlink():
        raise TranscriptionAudioError("An audio storage directory is invalid.")
    try:
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)
        if not path.is_dir():
            raise OSError
    except OSError:
        raise TranscriptionAudioError("An audio storage directory is unavailable.") from None


def _json(path):
    try:
        with _reader(path) as source:
            if os.fstat(source.fileno()).st_size > 8_000_000:
                raise ValueError
            value = json.load(source)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise TranscriptionAudioError("Stored audio metadata is invalid.") from None


def _hash_file(path, aggregate=None, target=None):
    digest = hashlib.sha256()
    size = 0
    try:
        with _reader(path) as source:
            while block := source.read(64 * 1024):
                size += len(block)
                if size > MAX_SOURCE_BYTES:
                    raise TranscriptionAudioError("Stored audio exceeds the supported size.")
                digest.update(block)
                if aggregate is not None:
                    aggregate.update(block)
                if target is not None:
                    target.write(block)
    except OSError:
        raise TranscriptionAudioError("Required local audio data cannot be read or copied.") from None
    return digest.hexdigest(), size


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path, payload):
    if path.is_symlink():
        raise TranscriptionAudioError("An audio output path is invalid.")
    descriptor, name = tempfile.mkstemp(prefix=".audio-metadata-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(json.dumps(payload, allow_nan=False, sort_keys=True).encode("utf-8"))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _prefix(capture_dir):
    _directory(capture_dir)
    _directory(capture_dir / "batches")
    manifest = _json(capture_dir / "manifest.json")
    if manifest.get("schema_version") != 1 or isinstance(manifest.get("schema_version"), bool):
        raise TranscriptionAudioError("The capture schema is unsupported.")
    try:
        if str(uuid.UUID(manifest.get("id", ""))) != manifest.get("id"):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise TranscriptionAudioError("The capture identifier is invalid.") from None
    status = manifest.get("status")
    if status not in ("recording", "complete"):
        raise TranscriptionAudioError("Only recording or completed captures can be prepared.")
    batches = manifest.get("batches")
    if not isinstance(batches, list) or len(batches) > MAX_BATCHES:
        raise TranscriptionAudioError("The capture batch list is invalid.")
    by_sequence = {}
    declared_size = 0
    for batch in batches:
        if not isinstance(batch, dict):
            raise TranscriptionAudioError("A capture batch record is invalid.")
        sequence = _integer(batch.get("sequence"), maximum=MAX_BATCHES - 1)
        if sequence in by_sequence or batch.get("filename") != f"batches/{sequence:06d}.webm":
            raise TranscriptionAudioError("A batch sequence or filename is invalid.")
        if not isinstance(batch.get("sha256"), str) or _HASH.fullmatch(batch["sha256"]) is None:
            raise TranscriptionAudioError("A batch checksum is invalid.")
        declared_size += _integer(batch.get("size_bytes"), 1, 10_000_000)
        by_sequence[sequence] = batch
    if (_integer(manifest.get("received_batches"), maximum=MAX_BATCHES) != len(batches)
            or _integer(manifest.get("total_bytes")) != declared_size
            or declared_size > MAX_SOURCE_BYTES):
        raise TranscriptionAudioError("The capture byte or batch count is inconsistent.")
    entries = []
    while len(entries) in by_sequence:
        batch = by_sequence[len(entries)]
        entries.append((batch, capture_dir / batch["filename"]))
    complete = status == "complete"
    if complete:
        expected = _integer(manifest.get("expected_batches"), 1, MAX_BATCHES)
        if expected != len(entries) or len(entries) != len(batches) or manifest.get("missing_sequences", []) != []:
            raise TranscriptionAudioError("The completed capture has missing or extra batches.")
    digest = hashlib.sha256()
    total = 0
    for batch, path in entries:
        observed_hash, observed_size = _hash_file(path, digest)
        if observed_hash != batch["sha256"] or observed_size != batch["size_bytes"]:
            raise TranscriptionAudioError("A saved audio batch failed its checksum or size check.")
        total += observed_size
    prefix_hash = digest.hexdigest()
    if complete and (manifest.get("audio_sha256") != prefix_hash
                     or _integer(manifest.get("audio_size_bytes"), 1) != total):
        raise TranscriptionAudioError("The completed audio checksum or size is inconsistent.")
    # Keep source URLs and arbitrary manifest fields out of downstream diagnostics.
    safe_manifest = {key: manifest[key] for key in (
        "schema_version", "id", "status", "video_duration_sec", "range_start_sec", "range_end_sec",
        "expected_batches", "received_batches", "total_bytes", "timing",
    ) if key in manifest}
    timing = safe_manifest.get("timing", {})
    if not isinstance(timing, dict):
        raise TranscriptionAudioError("The capture timing record is invalid.")
    safe_timing = {key: timing[key] for key in (
        "recording_started_at_ms", "playback_started_at_ms", "playback_start_video_sec",
        "recording_stopped_at_ms", "playback_end_video_sec",
    ) if key in timing}
    if "playback_events" in timing:
        events = timing["playback_events"]
        if not isinstance(events, list) or len(events) > 1000:
            raise TranscriptionAudioError("The capture playback observations are invalid.")
        safe_timing["playback_events"] = []
        for event in events:
            if not isinstance(event, dict) or event.get("type") not in ("playing", "waiting"):
                raise TranscriptionAudioError("The capture playback observations are invalid.")
            safe_timing["playback_events"].append({"type": event["type"], "at_ms": _number(event.get("at_ms")),
                                                   "video_sec": _number(event.get("video_sec"))})
    for key, value in safe_timing.items():
        if key != "playback_events":
            _number(value)
    safe_manifest["timing"] = safe_timing
    return safe_manifest, entries, prefix_hash, total, complete


def _ffmpeg_binary(ffmpeg):
    if ffmpeg is not None:
        return str(ffmpeg)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        raise TranscriptionAudioError("A local FFmpeg binary is required for audio preparation.") from None


def _decode(source, output, *, complete, ffmpeg):
    try:
        process = subprocess.run(
            [_ffmpeg_binary(ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-xerror",
             "-err_detect", "explode", "-y", "-protocol_whitelist", "file,pipe", "-f", "matroska", "-i", str(source), "-map", "0:a:0", "-vn",
             "-ac", "1", "-ar", str(SAMPLE_RATE), "-acodec", "pcm_s16le", "-f", "s16le",
             "-fs", str(MAX_PCM_BYTES + SAMPLE_WIDTH), str(output)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise TranscriptionAudioError("Local audio decoding could not finish.") from None
    stderr = process.stderr.decode("utf-8", errors="replace") if isinstance(process.stderr, bytes) else process.stderr or ""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    premature = bool(lines) and all(_PREMATURE_END.fullmatch(line) for line in lines)
    if (lines and not (not complete and premature)) or (process.returncode != 0 and not (not complete and premature)):
        raise TranscriptionAudioError("The continuous capture audio could not be decoded safely.")
    size = output.stat().st_size
    if size > MAX_PCM_BYTES or size % SAMPLE_WIDTH:
        raise TranscriptionAudioError("Decoded audio has an invalid size or exceeds the supported duration.")
    if complete and size == 0:
        raise TranscriptionAudioError("The completed capture contains no decoded audio.")
    return premature


def prepare_capture_audio(capture_dir: Path, *, ffmpeg=None) -> dict:
    """Verify and decode the longest contiguous ACK prefix as one WebM stream.

    Repeated calls verify source/cache integrity but do not decode an unchanged
    prefix. A gap ends the usable prefix, and an unfinished container is never
    promoted to a complete capture. PCM duration comes from sample count only.
    """
    capture_dir = Path(capture_dir)
    with _PREPARE_LOCK:
        manifest, entries, prefix_hash, total, complete = _prefix(capture_dir)
        fingerprint = hashlib.sha256(f"{_PROFILE}:{prefix_hash}:{total}".encode("ascii")).hexdigest()
        directory = capture_dir / "transcription"
        _directory(directory, create=True)
        pcm_path = directory / "audio.pcm"
        metadata_path = directory / "audio.json"
        result = {
            "manifest": manifest, "timing": manifest.get("timing", {}), "pcm_path": pcm_path,
            "sample_rate": SAMPLE_RATE, "source_fingerprint": fingerprint,
            "prefix_batches": len(entries), "prefix_sha256": prefix_hash, "prefix_size_bytes": total,
            "complete": complete, "ready": False, "sample_count": 0, "duration_sec": 0.0,
            "decoder_incomplete": False,
        }
        if not entries:
            return result
        if pcm_path.is_symlink() or metadata_path.is_symlink():
            raise TranscriptionAudioError("An audio output path is invalid.")
        if metadata_path.exists() and pcm_path.exists():
            cached = _json(metadata_path)
            if cached.get("source_fingerprint") == fingerprint and cached.get("profile") == _PROFILE:
                pcm_hash, pcm_size = _hash_file(pcm_path)
                if (cached.get("pcm_sha256") != pcm_hash or cached.get("pcm_size_bytes") != pcm_size
                        or pcm_size % SAMPLE_WIDTH or pcm_size > MAX_PCM_BYTES):
                    raise TranscriptionAudioError("The cached PCM audio failed its integrity check.")
                if complete and cached.get("decoder_incomplete"):
                    raise TranscriptionAudioError("The completed capture still has an unfinished audio container.")
                if complete and pcm_size == 0:
                    raise TranscriptionAudioError("The completed capture contains no decoded audio.")
                result.update(sample_count=pcm_size // SAMPLE_WIDTH, duration_sec=pcm_size / (SAMPLE_RATE * SAMPLE_WIDTH),
                              ready=pcm_size > 0, decoder_incomplete=bool(cached.get("decoder_incomplete")))
                return result
        source_name = pcm_name = None
        try:
            descriptor, source_name = tempfile.mkstemp(prefix=".prefix-", suffix=".webm", dir=directory)
            with os.fdopen(descriptor, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                digest = hashlib.sha256()
                copied = 0
                for batch, path in entries:
                    batch_hash, batch_size = _hash_file(path, digest, target)
                    if batch_hash != batch["sha256"] or batch_size != batch["size_bytes"]:
                        raise TranscriptionAudioError("Audio batches changed during preparation.")
                    copied += batch_size
                target.flush()
                os.fsync(target.fileno())
            if copied != total or digest.hexdigest() != prefix_hash:
                raise TranscriptionAudioError("Audio batches changed during preparation.")
            descriptor, pcm_name = tempfile.mkstemp(prefix=".pcm-", dir=directory)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            temporary_pcm = Path(pcm_name)
            premature = _decode(Path(source_name), temporary_pcm, complete=complete, ffmpeg=ffmpeg)
            pcm_hash, pcm_size = _hash_file(temporary_pcm)
            metadata = {
                "schema_version": 1, "profile": _PROFILE, "source_fingerprint": fingerprint,
                "prefix_batches": len(entries), "prefix_sha256": prefix_hash, "prefix_size_bytes": total,
                "sample_rate": SAMPLE_RATE, "sample_width": SAMPLE_WIDTH, "channels": 1,
                "sample_count": pcm_size // SAMPLE_WIDTH, "pcm_size_bytes": pcm_size,
                "pcm_sha256": pcm_hash, "decoder_incomplete": premature,
            }
            with _reader(temporary_pcm) as source:
                os.fsync(source.fileno())
            os.replace(temporary_pcm, pcm_path)
            pcm_path.chmod(0o600)
            _sync(directory)
            _atomic_json(metadata_path, metadata)
            result.update(sample_count=metadata["sample_count"], duration_sec=pcm_size / (SAMPLE_RATE * SAMPLE_WIDTH),
                          ready=pcm_size > 0, decoder_incomplete=premature)
            return result
        except OSError:
            raise TranscriptionAudioError("Prepared audio could not be saved locally.") from None
        finally:
            for name in (source_name, pcm_name):
                if name:
                    Path(name).unlink(missing_ok=True)


def _samples(seconds):
    seconds = _number(seconds)
    if seconds > MAX_PCM_BYTES / (SAMPLE_RATE * SAMPLE_WIDTH):
        raise TranscriptionAudioError("An audio window exceeds the supported duration.")
    return int(math.floor(seconds * SAMPLE_RATE + 1e-7))


def window_plan(duration_sec, complete, core_seconds=43, context_seconds=1) -> list[dict]:
    """Plan stable sample-aligned cores with independent audio context overlap."""
    if not isinstance(complete, bool):
        raise TranscriptionAudioError("Window completion status must be a boolean.")
    count = _samples(duration_sec)
    core, context = _samples(core_seconds), _samples(context_seconds)
    if core < 1 or core + 2 * context > 45 * SAMPLE_RATE:
        raise TranscriptionAudioError("Recognition windows must fit within 45 seconds.")
    windows = []
    start = 0
    while start < count:
        core_end = min(start + core, count)
        if not complete and start + core + context > count:
            break
        audio_start, audio_end = max(0, start - context), min(count, core_end + context)
        windows.append({
            "index": len(windows), "sample_rate": SAMPLE_RATE,
            "start_sample": audio_start, "end_sample": audio_end,
            "audio_start_sample": audio_start, "audio_end_sample": audio_end,
            "core_start_sample": start, "core_end_sample": core_end,
            "audio_start_sec": audio_start / SAMPLE_RATE, "audio_end_sec": audio_end / SAMPLE_RATE,
            "start_sec": audio_start / SAMPLE_RATE, "end_sec": audio_end / SAMPLE_RATE,
            "core_start_sec": start / SAMPLE_RATE, "core_end_sec": core_end / SAMPLE_RATE,
        })
        start += core
    return windows


def wav_for_window(pcm_path, window) -> bytes:
    """Return a deterministic standard WAV for one integer-sample window."""
    start = _integer(window.get("audio_start_sample", window.get("start_sample")), maximum=MAX_PCM_BYTES // SAMPLE_WIDTH)
    end = _integer(window.get("audio_end_sample", window.get("end_sample")), start + 1, MAX_PCM_BYTES // SAMPLE_WIDTH)
    if end - start > 45 * SAMPLE_RATE:
        raise TranscriptionAudioError("Recognition windows must fit within 45 seconds.")
    if window.get("sample_rate", SAMPLE_RATE) != SAMPLE_RATE:
        raise TranscriptionAudioError("The PCM sample rate is unsupported.")
    with _PREPARE_LOCK, _reader(Path(pcm_path)) as source:
        size = os.fstat(source.fileno()).st_size
        if size % SAMPLE_WIDTH or end * SAMPLE_WIDTH > size:
            raise TranscriptionAudioError("The requested audio window is not available.")
        source.seek(start * SAMPLE_WIDTH)
        pcm = source.read((end - start) * SAMPLE_WIDTH)
    if len(pcm) != (end - start) * SAMPLE_WIDTH:
        raise TranscriptionAudioError("The requested audio window changed while being read.")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(SAMPLE_WIDTH)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(pcm)
    return output.getvalue()


def build_time_map(manifest, audio_duration_sec) -> dict:
    """Build observed playing/waiting segments, never fit wall-clock stop to PCM."""
    duration = _number(audio_duration_sec)
    timing = manifest.get("timing", {}) if isinstance(manifest, dict) else {}
    result = {
        "audio_duration_sec": duration, "segments": [], "quality": "observed",
        "warnings": ["audio_video_alignment_uses_browser_observations_not_precise_audio_pts"],
    }
    try:
        recording_start = _number(timing.get("recording_started_at_ms"))
        playing_start = _number(timing.get("playback_started_at_ms"))
        video_start = _number(timing.get("playback_start_video_sec"))
        if playing_start < recording_start:
            raise TranscriptionAudioError("Invalid playback start.")
    except (TranscriptionAudioError, AttributeError):
        result.update(quality="uncertain", warnings=result["warnings"] + ["initial_playback_timing_unavailable"])
        return result
    offset = (playing_start - recording_start) / 1000
    anchors = [{"audio_sec": offset, "video_sec": video_start, "rate": 1}]
    events = timing.get("playback_events", [])
    if not isinstance(events, list) or len(events) > 1000:
        events = []
        result["warnings"].append("invalid_playback_observations_ignored")
        result["quality"] = "uncertain"
    for event in events:
        try:
            if not isinstance(event, dict) or event.get("type") not in ("playing", "waiting"):
                raise TranscriptionAudioError("Invalid playback observation.")
            at = (_number(event.get("at_ms")) - recording_start) / 1000
            position = _number(event.get("video_sec"))
            if at < offset or at < anchors[-1]["audio_sec"]:
                raise TranscriptionAudioError("Unordered playback observation.")
            anchor = {"audio_sec": at, "video_sec": position, "rate": int(event["type"] == "playing")}
            previous = anchors[-1]
            expected = previous["video_sec"] + previous["rate"] * (at - previous["audio_sec"])
            if abs(expected - position) > 0.5:
                result["warnings"].append("playback_clock_anchor_discontinuity")
                result["quality"] = "uncertain"
            if at == previous["audio_sec"]:
                anchors[-1] = anchor
            else:
                anchors.append(anchor)
            if event["type"] == "waiting":
                result["warnings"].append("buffering_observed_alignment_is_approximate")
                result["quality"] = "uncertain"
        except TranscriptionAudioError:
            result["warnings"].append("invalid_playback_observations_ignored")
            result["quality"] = "uncertain"
    if manifest.get("status") != "complete":
        result["warnings"].append("capture_timing_is_not_final")
        result["quality"] = "uncertain"
    if "recording_stopped_at_ms" in timing:
        try:
            observed_duration = (_number(timing["recording_stopped_at_ms"]) - recording_start) / 1000
            if abs(observed_duration - duration) > 0.5:
                result["warnings"].append("wall_clock_duration_differs_from_decoded_sample_duration")
                result["quality"] = "uncertain"
        except TranscriptionAudioError:
            result["warnings"].append("invalid_recording_stop_observation")
            result["quality"] = "uncertain"
    if offset > 0:
        result["segments"].append({
            "audio_start_sec": 0.0, "audio_end_sec": min(offset, duration),
            "video_start_sec": None, "rate": 0, "uncertain": True,
        })
    for index, anchor in enumerate(anchors):
        start = anchor["audio_sec"]
        end = min(duration, anchors[index + 1]["audio_sec"] if index + 1 < len(anchors) else duration)
        if start >= duration:
            result["warnings"].append("playback_observation_exceeds_decoded_audio")
            result["quality"] = "uncertain"
            continue
        if end > start:
            result["segments"].append({
                "audio_start_sec": start, "audio_end_sec": end,
                "video_start_sec": anchor["video_sec"], "rate": anchor["rate"], "uncertain": True,
            })
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


def map_audio_time(time_map, audio_sec) -> dict:
    """Map measured audio time to an approximate observed video time, or None."""
    at = _number(audio_sec)
    reasons = list(time_map.get("warnings", []))
    answer = {"audio_sec": at, "video_sec": None, "uncertain": True,
              "quality": time_map.get("quality", "uncertain"), "reasons": reasons}
    duration = _number(time_map.get("audio_duration_sec", 0))
    if at > duration + 1e-7:
        answer["quality"] = "uncertain"
        answer["reasons"] = reasons + ["outside_decoded_audio"]
        return answer
    for index, segment in enumerate(time_map.get("segments", [])):
        start, end = segment["audio_start_sec"], segment["audio_end_sec"]
        if start <= at < end or (index == len(time_map["segments"]) - 1 and abs(at - end) < 1e-7):
            if segment["video_start_sec"] is None:
                answer["quality"] = "uncertain"
                answer["reasons"] = reasons + ["before_observed_playback_start"]
            else:
                answer["video_sec"] = segment["video_start_sec"] + segment["rate"] * (at - start)
            return answer
    answer["quality"] = "uncertain"
    answer["reasons"] = reasons + ["no_observed_playback_mapping"]
    return answer
