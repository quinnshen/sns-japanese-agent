"""Private clip history, independent favorites, and bounded capture retention."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from capture_learning import LearningError, read_learning_result, read_learning_status
from capture_store import CaptureError, CaptureStore
from learning_text import STYLE_VERSION, validate_items

RETENTION_SECONDS = 7 * 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 60 * 60
_UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
_SENTENCE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


class LibraryError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def canonical_source_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise LibraryError(409, "Stored video source is invalid")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname not in {"youtube.com", "www.youtube.com"}
                or parsed.username is not None or parsed.password is not None or parsed.port is not None):
            raise ValueError
        if parsed.path == "/watch":
            ids = parse_qs(parsed.query).get("v", [])
            if len(ids) == 1 and _VIDEO_ID.fullmatch(ids[0]):
                return f"https://www.youtube.com/watch?v={ids[0]}"
        match = re.fullmatch(r"/shorts/([A-Za-z0-9_-]{11})/?", parsed.path)
        if match:
            return f"https://www.youtube.com/shorts/{match[1]}"
    except ValueError:
        pass
    raise LibraryError(409, "Stored video source is invalid")


def _mtime(path: Path) -> float:
    if path.parent.is_symlink():
        raise LibraryError(409, "Local library data is unavailable")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise LibraryError(409, "Local library data is unavailable")
    return info.st_mtime


def clip_metadata(directory: Path, manifest: dict, sentences_done: int) -> dict:
    updated = _mtime(directory / "manifest.json")
    try:
        updated = max(updated, _mtime(directory / "learning" / "result.json"))
    except FileNotFoundError:
        pass
    return {
        "capture_id": manifest["id"], "source_url": canonical_source_url(manifest["source_url"]),
        "range_start_sec": manifest["range_start_sec"], "range_end_sec": manifest["range_end_sec"],
        "updated_at": updated,
        "sentences_done": sentences_done if type(sentences_done) is int and sentences_done >= 0 else 0,
    }


def _accepted_sentence(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    identifier, source, study = item.get("id"), item.get("text"), item.get("study")
    if (not isinstance(identifier, str) or not _SENTENCE_ID.fullmatch(identifier)
            or not isinstance(source, str) or not source.strip() or len(source) > 20_000
            or not isinstance(study, dict) or study.get("style_version") != STYLE_VERSION):
        return None
    candidate = {
        "id": identifier, "reading_text": study.get("reading_text"), "translation_zh": item.get("translation_zh"),
        "key_points": study.get("key_points"), "context_note": study.get("context_note"), "asr_doubt": study.get("asr_doubt"),
    }
    validated = validate_items([candidate], [{"id": identifier, "text": source}])["valid"].get(identifier)
    if validated is None:
        return None
    times = {}
    for key in ("start", "end", "audio_start", "audio_end"):
        value = item.get(key)
        if value is not None:
            try:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    return None
            except OverflowError:
                return None
        times[key] = value
    for left, right in (("start", "end"), ("audio_start", "audio_end")):
        if times[left] is not None and times[right] is not None and times[right] < times[left]:
            return None
    return {"id": identifier, "text": source, **times, **validated,
            "mapping_uncertain": item.get("mapping_uncertain") is True or times["start"] is None or times["end"] is None}


@contextlib.contextmanager
def _idle_processing(directory: Path):
    """Exclude queued server work and already-running server or CLI workers."""
    import capture_learning
    import capture_transcription

    key = str(directory.resolve())
    with contextlib.ExitStack() as stack:
        for stage in (capture_transcription, capture_learning):
            stack.enter_context(stage._scheduler_lock)
            if key in stage._active:
                yield False
                return
        for name in ("transcription", "learning"):
            stage_dir = directory / name
            if stage_dir.is_symlink():
                yield False
                return
            # A first CLI invocation must see the same lock even when the
            # capture has never run this stage before.
            stage_dir.mkdir(mode=0o700, exist_ok=True)
            descriptor = os.open(stage_dir / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
            stack.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                yield False
                return
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True


class LearningLibrary:
    def __init__(self, data_dir: Path, *, store_factory=CaptureStore, clock=None):
        self.data_dir = Path(data_dir)
        self.capture_root = self.data_dir / "captures"
        self.favorite_root = self.data_dir / "learning_library"
        self._store_factory = store_factory
        self._clock = clock or time.time
        self._lock = threading.RLock()

    def _capture_store(self):
        if self.data_dir.is_symlink() or self.capture_root.is_symlink():
            raise LibraryError(409, "Local capture data is unavailable")
        return self._store_factory(self.capture_root) if self.capture_root.is_dir() else None

    @contextlib.contextmanager
    def _favorites(self, *, write=False):
        path = self.favorite_root / "favorites.sqlite3"
        if self.data_dir.is_symlink() or self.favorite_root.is_symlink() or path.is_symlink():
            raise LibraryError(409, "Local favorites data is unavailable")
        if not write and not path.exists():
            yield None
            return
        for suffix in ("-journal", "-wal", "-shm"):
            if path.with_name(path.name + suffix).is_symlink():
                raise LibraryError(409, "Local favorites data is unavailable")
        if write:
            self.favorite_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.favorite_root.chmod(0o700)
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise LibraryError(409, "Local favorites data is unavailable")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            connection = sqlite3.connect(path, timeout=5)
        else:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise LibraryError(409, "Local favorites data is unavailable")
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            if write:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("""CREATE TABLE IF NOT EXISTS favorites (
                    capture_id TEXT NOT NULL, sentence_id TEXT NOT NULL, source_url TEXT NOT NULL,
                    saved_at REAL NOT NULL, sentence_json TEXT NOT NULL,
                    PRIMARY KEY(capture_id, sentence_id)
                )""")
            with connection:
                if write:
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def _list_favorites(self) -> list:
        with self._favorites() as connection:
            if connection is None:
                return []
            favorites = []
            for row in connection.execute("SELECT * FROM favorites ORDER BY saved_at DESC, capture_id, sentence_id"):
                try:
                    snapshot = _accepted_sentence(json.loads(row["sentence_json"]))
                    if (snapshot is None or snapshot["id"] != row["sentence_id"]
                            or not _UUID.fullmatch(row["capture_id"])
                            or not isinstance(row["saved_at"], (int, float))
                            or not math.isfinite(row["saved_at"]) or row["saved_at"] < 0):
                        continue
                    favorites.append({"capture_id": row["capture_id"], "sentence_id": row["sentence_id"],
                                      "source_url": canonical_source_url(row["source_url"]), "saved_at": row["saved_at"],
                                      "sentence": snapshot})
                except (LibraryError, ValueError, TypeError, RecursionError):
                    # A damaged entry is never rendered as an accepted note.
                    continue
            return favorites

    def list(self) -> dict:
        with self._lock:
            clips = []
            store = self._capture_store()
            if store is not None:
                for directory in self.capture_root.iterdir():
                    if not _UUID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                        continue
                    try:
                        manifest = store.get(directory.name)
                        if manifest["status"] != "complete":
                            continue
                        count = read_learning_status(directory).get("sentences_done", 0)
                        if type(count) is int and count > 0:
                            clips.append(clip_metadata(directory, manifest, count))
                    except (CaptureError, LearningError, LibraryError, OSError, ValueError, TypeError, KeyError, sqlite3.DatabaseError):
                        continue
            clips.sort(key=lambda clip: (clip["updated_at"], clip["capture_id"]), reverse=True)
            return {"clips": clips, "favorites": self._list_favorites()}

    def set_favorite(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) != {"capture_id", "sentence_id", "saved"}:
            raise LibraryError(400, "Expected capture ID, sentence ID and saved state")
        capture_id, sentence_id, saved = payload["capture_id"], payload["sentence_id"], payload["saved"]
        if (not isinstance(capture_id, str) or not _UUID.fullmatch(capture_id)
                or not isinstance(sentence_id, str) or not _SENTENCE_ID.fullmatch(sentence_id) or type(saved) is not bool):
            raise LibraryError(400, "Invalid favorite selection")
        key = (capture_id, sentence_id)
        with self._lock:
            if not saved and not (self.favorite_root / "favorites.sqlite3").exists():
                return {"saved": False}
            with self._favorites(write=True) as connection:
                if not saved:
                    connection.execute("DELETE FROM favorites WHERE capture_id=? AND sentence_id=?", key)
                    return {"saved": False}
                if connection.execute("SELECT 1 FROM favorites WHERE capture_id=? AND sentence_id=?", key).fetchone():
                    return {"saved": True}
                store = self._capture_store()
                if store is None:
                    raise LibraryError(404, "Saved learning sentence not found")
                manifest = store.get(capture_id)
                result = read_learning_result(self.capture_root / capture_id)
                for sentence in (result or {}).get("sentences", []):
                    if sentence.get("id") != sentence_id:
                        continue
                    snapshot = _accepted_sentence(sentence)
                    if snapshot is not None:
                        connection.execute("INSERT INTO favorites VALUES (?,?,?,?,?)", (
                            *key, canonical_source_url(manifest["source_url"]), self._clock(),
                            json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
                        ))
                        return {"saved": True}
                    break
                raise LibraryError(404, "Saved learning sentence not found")

    def cleanup_expired(self, *, now=None) -> int:
        """Delete old terminal captures only; independent favorites never expire here."""
        cutoff = (self._clock() if now is None else now) - RETENTION_SECONDS
        store = self._capture_store()
        if store is None:
            return 0
        deleted = 0
        for directory in list(self.capture_root.iterdir()):
            if not _UUID.fullmatch(directory.name):
                continue
            try:
                with self._lock, store._lock:
                    if directory.is_symlink() or not directory.is_dir():
                        continue
                    manifest = store.get(directory.name)
                    if manifest["status"] not in {"complete", "aborted"} or _mtime(directory / "manifest.json") > cutoff:
                        continue
                    with _idle_processing(directory) as idle:
                        if not idle:
                            continue
                        # Publish removal atomically before removing files. No
                        # library reader or worker can see a half-deleted job.
                        retired = self.capture_root / f".expired-{directory.name}-{uuid.uuid4().hex}"
                        directory.rename(retired)
                shutil.rmtree(retired)
                deleted += 1
            except (CaptureError, LibraryError, OSError, ValueError, TypeError, KeyError):
                # Uncertain state, lock contention, or a filesystem failure
                # must not trigger fallback deletion outside the capture root.
                continue
        return deleted


def run_retention_sweep(library: LearningLibrary, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            library.cleanup_expired()
        except (LibraryError, OSError, ValueError):
            # The next sweep can retry; never emit private paths or source URLs.
            pass
        if stop.wait(CLEANUP_INTERVAL_SECONDS):
            break
