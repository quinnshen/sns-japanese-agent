"""Verify or rebuild a saved browser capture, entirely offline.

The recording is one continuous MediaRecorder container. Individual upload
batches are storage fragments and are never decoded independently here.
"""

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


class CaptureReplayError(ValueError):
    """A safe error that does not contain source URLs or private paths."""


def _integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _digest(path, combined=None):
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
                if combined is not None:
                    combined.update(block)
                size += len(block)
    except OSError:
        raise CaptureReplayError("A required audio file cannot be read.") from None
    return digest.hexdigest(), size


def _manifest(capture_dir):
    try:
        manifest = json.loads((capture_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise CaptureReplayError("The capture manifest cannot be read as JSON.") from None
    if (not isinstance(manifest, dict) or not _integer(manifest.get("schema_version"), 1)
            or manifest["schema_version"] != 1):
        raise CaptureReplayError("The capture manifest schema is unsupported.")
    try:
        identifier = str(uuid.UUID(manifest.get("id", "")))
    except (ValueError, AttributeError, TypeError):
        raise CaptureReplayError("The capture ID is invalid.") from None
    if manifest.get("id") != identifier:
        raise CaptureReplayError("The capture ID is invalid.")
    if manifest.get("status") != "complete":
        raise CaptureReplayError("The capture has not been confirmed complete.")
    expected = manifest.get("expected_batches")
    if not _integer(expected, 1):
        raise CaptureReplayError("The expected batch count is invalid.")
    batches = manifest.get("batches")
    if not isinstance(batches, list) or len(batches) != expected:
        raise CaptureReplayError("The capture has missing or extra batches.")
    if (not _integer(manifest.get("received_batches"), 1) or manifest["received_batches"] != expected
            or manifest.get("missing_sequences", []) != []):
        raise CaptureReplayError("The capture has missing or inconsistent batch records.")
    timing = manifest.get("timing")
    timing_fields = (
        "recording_started_at_ms", "playback_started_at_ms", "playback_start_video_sec",
        "recording_stopped_at_ms", "playback_end_video_sec",
    )
    if not isinstance(timing, dict) or any(not _number(timing.get(key)) for key in timing_fields):
        raise CaptureReplayError("The observed capture timing is incomplete or invalid.")
    if not (timing["recording_started_at_ms"] <= timing["playback_started_at_ms"]
            <= timing["recording_stopped_at_ms"]):
        raise CaptureReplayError("The observed capture timing is inconsistent.")
    entries = []
    for sequence, batch in enumerate(batches):
        if not isinstance(batch, dict) or not _integer(batch.get("sequence")) or batch["sequence"] != sequence:
            raise CaptureReplayError("The batch sequence is not contiguous and ordered.")
        filename = f"batches/{sequence:06d}.webm"
        if batch.get("filename") != filename:
            raise CaptureReplayError("A batch filename is invalid.")
        if not isinstance(batch.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", batch["sha256"]):
            raise CaptureReplayError("A batch hash is invalid.")
        if not _integer(batch.get("size_bytes"), 1):
            raise CaptureReplayError("A batch size is invalid.")
        start, end = batch.get("capture_start_ms"), batch.get("capture_end_ms")
        if not _number(start) or not _number(end) or start < 0 or end < start:
            raise CaptureReplayError("A batch timing observation is invalid.")
        path = capture_dir / filename
        if path.is_symlink() or path.parent.is_symlink():
            raise CaptureReplayError("A batch file must not be a symbolic link.")
        entries.append((batch, path))
    observed = {key: timing[key] for key in timing_fields}
    if "playback_events" in timing:
        events = timing["playback_events"]
        if not isinstance(events, list) or any(
            not isinstance(event, dict) or not isinstance(event.get("type"), str)
            or event["type"] not in {"playing", "waiting"}
            or not _number(event.get("at_ms")) or not _number(event.get("video_sec"))
            for event in events
        ):
            raise CaptureReplayError("The playback timing observations are invalid.")
        observed["playback_events"] = [
            {key: event[key] for key in ("type", "at_ms", "video_sec")} for event in events
        ]
    return manifest, entries, observed


def _rebuild(entries, output, expected_hash, expected_size, *, decode=False, ffmpeg=None):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".capture-replay-", dir=output.parent, delete=False) as target:
            temporary = Path(target.name)
            digest = hashlib.sha256()
            size = 0
            for _, path in entries:
                with path.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        target.write(block)
                        digest.update(block)
                        size += len(block)
            target.flush()
            os.fsync(target.fileno())
        if digest.hexdigest() != expected_hash or size != expected_size:
            raise CaptureReplayError("Audio batches changed during rebuilding; the output was not replaced.")
        duration = decode_duration(temporary, ffmpeg) if decode else None
        os.replace(temporary, output)
        return duration
    except OSError:
        raise CaptureReplayError("The rebuilt audio could not be saved.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def decode_duration(path, ffmpeg=None):
    """Decode every audio packet to a null sink and return decoded output time."""
    if ffmpeg is None:
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError, OSError):
            raise CaptureReplayError("A local FFmpeg binary is required for --decode.") from None
    try:
        process = subprocess.run(
            [str(ffmpeg), "-nostdin", "-hide_banner", "-v", "error", "-xerror",
             "-err_detect", "explode", "-i", str(path), "-map", "0:a:0",
             "-progress", "pipe:1", "-nostats", "-f", "null", "-"],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CaptureReplayError("Local audio decoding could not finish.") from None
    if process.returncode != 0:
        raise CaptureReplayError("Local audio decoding failed; check the saved recording.")
    durations = []
    for match in re.finditer(r"^out_time_us=(\d+)$", process.stdout, re.MULTILINE):
        durations.append(int(match.group(1)) / 1_000_000)
    if not durations or max(durations) <= 0:
        raise CaptureReplayError("Local decoding produced no measurable audio.")
    return max(durations)


def verify_capture(capture_dir, *, output=None, decode=False, ffmpeg=None):
    """Validate saved batches and their combined file; optionally rebuild it.

    Validation is read-only by default. Explicit output is atomically replaced
    only after all declared batches and the combined content pass validation.
    No manifest or observed timing record is modified.
    """
    capture_dir = Path(capture_dir)
    manifest, entries, timing = _manifest(capture_dir)
    combined = hashlib.sha256()
    total = 0
    for batch, path in entries:
        digest, size = _digest(path, combined)
        if digest != batch["sha256"] or size != batch["size_bytes"]:
            raise CaptureReplayError("A saved batch failed its hash or size check.")
        total += size
    digest = combined.hexdigest()
    if not _integer(manifest.get("total_bytes"), 1) or manifest["total_bytes"] != total:
        raise CaptureReplayError("The total batch byte count is inconsistent.")
    if (manifest.get("audio_sha256") != digest or not _integer(manifest.get("audio_size_bytes"), 1)
            or manifest["audio_size_bytes"] != total):
        raise CaptureReplayError("The combined audio hash or size is inconsistent.")
    recording = capture_dir / "recording.webm"
    duration = None
    if output is not None:
        output = Path(output)
        resolved = output.resolve()
        if resolved == (capture_dir / "manifest.json").resolve() or (capture_dir / "batches").resolve() in resolved.parents:
            raise CaptureReplayError("The output must not replace the manifest or saved batches.")
        duration = _rebuild(entries, output, digest, total, decode=decode, ffmpeg=ffmpeg)
        recording = output
    else:
        if recording.is_symlink():
            raise CaptureReplayError("The combined recording must not be a symbolic link.")
        recorded_hash, recorded_size = _digest(recording)
        if recorded_hash != digest or recorded_size != total:
            raise CaptureReplayError("The combined recording failed its hash or size check.")
    report = {
        "id": manifest["id"], "status": "complete", "batch_count": len(entries),
        "total_bytes": total, "audio_sha256": digest, "observed_timing": timing,
    }
    if decode:
        report["decode_duration_sec"] = duration if duration is not None else decode_duration(recording, ffmpeg)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify a saved browser capture locally; no cloud calls.")
    parser.add_argument("capture_dir", type=Path, help="Directory containing manifest.json and batches/.")
    parser.add_argument("--output", type=Path, help="Explicitly rebuild combined audio to this file atomically.")
    parser.add_argument("--decode", action="store_true", help="Decode locally with FFmpeg and report audio duration.")
    parser.add_argument("--ffmpeg", type=Path, help="Local FFmpeg executable; used only with --decode.")
    args = parser.parse_args(argv)
    try:
        report = verify_capture(args.capture_dir, output=args.output, decode=args.decode, ffmpeg=args.ffmpeg)
    except CaptureReplayError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
