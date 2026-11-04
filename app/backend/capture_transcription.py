"""Durable, resumable local capture -> Google window workflow; never calls an LLM."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

PLAN_VERSION = "google-window-v1-core43-context1"
MAX_ATTEMPTS = 3
_scheduler_lock = threading.Lock()
_cloud_slot = threading.Semaphore(1)
_active: set[str] = set()
_dirty: set[str] = set()


class TranscriptionError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def atomic_json(path: Path, value: dict) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".transcription-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(canonical(value))
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def private_root(capture_dir: Path) -> Path:
    capture_dir = Path(capture_dir)
    if capture_dir.is_symlink() or not capture_dir.is_dir():
        raise TranscriptionError("invalid_capture_directory")
    root = capture_dir / "transcription"
    if root.is_symlink():
        raise TranscriptionError("invalid_transcription_directory")
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    windows = root / "windows"
    if windows.is_symlink():
        raise TranscriptionError("invalid_transcription_directory")
    windows.mkdir(mode=0o700, exist_ok=True)
    return root


@contextlib.contextmanager
def job_lock(root: Path):
    try:
        descriptor = os.open(root / "worker.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        if "descriptor" in locals():
            os.close(descriptor)
        raise TranscriptionError("worker_already_running") from None
    try:
        yield
    finally:
        os.close(descriptor)


class WindowState:
    def __init__(self, root: Path):
        self.root = root
        path = root / "state.sqlite3"
        if path.is_symlink():
            raise TranscriptionError("invalid_state_path")
        has_results = any((root / "windows").glob("*.json")) or (root / "result.json").exists()
        if (not path.exists() or path.stat().st_size == 0) and has_results:
            raise TranscriptionError("transcription_state_missing")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS windows (
                window_index INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL,
                plan TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, response_hash TEXT, retry_cost_accepted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS attempts (
                window_index INTEGER NOT NULL, attempt INTEGER NOT NULL, started_at REAL NOT NULL,
                finished_at REAL, outcome TEXT NOT NULL, error_code TEXT,
                duplicate_cost_accepted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(window_index, attempt)
            );
        """)
        self.db.commit()
        existing = self.db.execute("SELECT value FROM metadata WHERE key='plan_version'").fetchone()
        if existing is None and has_results:
            self.db.close()
            raise TranscriptionError("transcription_state_missing")
        if existing and existing[0] != PLAN_VERSION:
            self.db.close()
            raise TranscriptionError("transcription_plan_changed")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('plan_version', ?)", (PLAN_VERSION,))

    def close(self):
        self.db.close()

    def rows(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM windows ORDER BY window_index")]

    def add(self, window: dict, fingerprint: str):
        index = window["index"]
        row = self.db.execute("SELECT * FROM windows WHERE window_index=?", (index,)).fetchone()
        if row and row["fingerprint"] != fingerprint:
            raise TranscriptionError("recognized_window_input_changed")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO windows(window_index,fingerprint,plan) VALUES (?,?,?)",
                            (index, fingerprint, canonical(window).decode()))

    def response(self, row: dict) -> dict | None:
        path = self.root / "windows" / f"{row['window_index']:06d}.json"
        if not path.exists():
            return None
        if path.is_symlink() or path.stat().st_size > 10_000_000:
            raise TranscriptionError("saved_window_response_invalid")
        try:
            content = path.read_bytes()
            value = json.loads(content)
            if value["fingerprint"] != row["fingerprint"] or value["window"]["index"] != row["window_index"] or not isinstance(value["results"], list):
                raise ValueError
            if row["response_hash"] and hashlib.sha256(content).hexdigest() != row["response_hash"]:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise TranscriptionError("saved_window_response_invalid") from None
        return value

    def recover(self):
        for row in self.rows():
            saved = self.response(row)
            if saved is not None and row["status"] in {"in_flight", "unknown", "succeeded"}:
                self.success(row["window_index"], hashlib.sha256(canonical(saved)).hexdigest(), recovered=True)
            elif row["status"] == "in_flight":
                self.failure(row["window_index"], "unknown", "interrupted_request_outcome_unknown")
            elif row["status"] == "succeeded" and saved is None:
                raise TranscriptionError("saved_window_response_missing")

    def begin(self, index: int, *, duplicate_cost_accepted: bool = False):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM windows WHERE window_index=?", (index,)).fetchone()
            if not row or row["status"] != "queued" or row["attempts"] >= MAX_ATTEMPTS:
                raise TranscriptionError("window_not_dispatchable")
            attempt = row["attempts"] + 1
            self.db.execute("UPDATE windows SET status='in_flight',attempts=?,error_code=NULL WHERE window_index=?", (attempt, index))
            self.db.execute("INSERT INTO attempts VALUES (?,?,?,NULL,'in_flight',NULL,?)",
                            (index, attempt, time.time(), int(duplicate_cost_accepted or row["retry_cost_accepted"])))
            self.db.execute("UPDATE windows SET retry_cost_accepted=0 WHERE window_index=?", (index,))

    def success(self, index: int, response_hash: str, *, recovered=False):
        with self.db:
            self.db.execute("UPDATE windows SET status='succeeded',error_code=NULL,response_hash=? WHERE window_index=?", (response_hash, index))
            self.db.execute("UPDATE attempts SET outcome=?,finished_at=? WHERE window_index=? AND attempt=(SELECT attempts FROM windows WHERE window_index=?) AND outcome IN ('in_flight','unknown')",
                            ("recovered_success" if recovered else "succeeded", time.time(), index, index))

    def failure(self, index: int, status: str, code: str):
        with self.db:
            self.db.execute("UPDATE windows SET status=?,error_code=? WHERE window_index=?", (status, code, index))
            self.db.execute("UPDATE attempts SET outcome=?,error_code=?,finished_at=? WHERE window_index=? AND outcome='in_flight'",
                            (status, code, time.time(), index))

    def retry(self, index: int, accept_duplicate_cost: bool):
        row = self.db.execute("SELECT * FROM windows WHERE window_index=?", (index,)).fetchone()
        if not row or row["status"] not in {"unknown", "failed"}:
            raise TranscriptionError("window_not_retryable")
        if row["status"] == "unknown" and not accept_duplicate_cost:
            raise TranscriptionError("duplicate_cost_confirmation_required")
        if row["attempts"] >= MAX_ATTEMPTS:
            raise TranscriptionError("window_attempt_limit_reached")
        with self.db:
            self.db.execute("UPDATE windows SET status='queued',error_code=NULL,retry_cost_accepted=? WHERE window_index=?", (int(accept_duplicate_cost), index))


def read_status(capture_dir: Path) -> dict:
    root = Path(capture_dir) / "transcription"
    path = root / "status.json"
    if root.is_symlink() or path.is_symlink():
        raise TranscriptionError("invalid_transcription_directory")
    status = json.loads(path.read_text()) if path.exists() else {"status": "not_started", "windows_done": 0, "windows_total": 0}
    database = root / "state.sqlite3"
    if database.is_symlink():
        raise TranscriptionError("invalid_state_path")
    if database.exists():
        try:
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
                rows = connection.execute("SELECT window_index,status,error_code,attempts FROM windows ORDER BY window_index").fetchall()
            status.update(windows_done=sum(row[1] == "succeeded" for row in rows), windows_total=len(rows))
            if any(row[1] == "in_flight" for row in rows):
                status["status"] = "running"
            blocked = [{"window_index": row[0], "status": row[1], "error_code": row[2], "attempts": row[3]} for row in rows if row[1] in {"unknown", "failed"}]
            if blocked:
                status.update(status="needs_attention", blocked_windows=blocked)
        except sqlite3.DatabaseError:
            raise TranscriptionError("transcription_state_invalid") from None
    return status


def read_result(capture_dir: Path) -> dict | None:
    path = Path(capture_dir) / "transcription" / "result.json"
    if path.parent.is_symlink() or path.is_symlink():
        raise TranscriptionError("invalid_transcription_directory")
    return json.loads(path.read_text()) if path.exists() else None


def request_processing(capture_dir: Path, *, enabled=lambda: True, on_result=None) -> None:
    """Coalesce upload notifications; never run decoding or cloud RPCs on the ACK thread."""
    key = str(Path(capture_dir).resolve())
    with _scheduler_lock:
        if key in _active:
            _dirty.add(key)
            return
        _active.add(key)

    def work():
        released = False
        busy_waits = 0
        try:
            while enabled():
                saved = False
                try:
                    with _cloud_slot:
                        if enabled():
                            status = process_capture(Path(capture_dir), run_google=True, dispatch_allowed=enabled)
                            saved = bool(status and status.get("result_available"))
                except TranscriptionError as error:
                    if error.code == "worker_already_running" and busy_waits < 150:
                        busy_waits += 1
                        time.sleep(1)
                        continue
                    else:
                        status = read_status(capture_dir)
                        atomic_json(private_root(capture_dir) / "status.json", {**status, "status": "error", "error_code": error.code})
                except Exception as error:
                    code = getattr(error, "error_code", "local_transcription_failed")
                    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                        code = "local_transcription_failed"
                    atomic_json(private_root(capture_dir) / "status.json", {"status": "error", "error_code": code})
                if saved and on_result is not None:
                    try:
                        on_result(Path(capture_dir))
                    except Exception:
                        # Learning has its own state; its failure cannot invalidate ASR results.
                        pass
                with _scheduler_lock:
                    if key not in _dirty:
                        _active.discard(key)
                        released = True
                        return
                    _dirty.discard(key)
        finally:
            if not released:
                with _scheduler_lock:
                    _active.discard(key)
                    _dirty.discard(key)

    threading.Thread(target=work, daemon=True, name="capture-transcription").start()


def process_capture(capture_dir: Path, *, run_google=False, recognizer=None, retry_window: int | None = None,
                    accept_duplicate_cost=False, max_new_requests=50, prepare_audio=None, dispatch_allowed=None) -> dict:
    """Prepare offline by default; a run flag is required before any paid request."""
    if isinstance(max_new_requests, bool) or not isinstance(max_new_requests, int) or not 1 <= max_new_requests <= 50:
        raise TranscriptionError("invalid_request_budget")
    from transcription_audio import prepare_capture_audio, window_plan, wav_for_window, build_time_map
    from transcription_text import merge_transcript
    root = private_root(capture_dir)
    with job_lock(root):
        state = WindowState(root)
        try:
            state.recover()
            audio = (prepare_audio or prepare_capture_audio)(Path(capture_dir))
            if not audio["ready"]:
                status = {"status": "waiting_for_audio", "windows_done": 0, "windows_total": 0}
                atomic_json(root / "status.json", status)
                return status
            windows = window_plan(audio["duration_sec"], audio["complete"])
            time_map = build_time_map(audio["manifest"], audio["duration_sec"])
            # Public settings only. No project IDs, ADC or API keys are stored in window metadata.
            from transcription_google import public_config
            config = {**public_config(), "plan_version": PLAN_VERSION}
            def fingerprint_for(wav, window):
                return hashlib.sha256(canonical({"audio_sha256": hashlib.sha256(wav).hexdigest(), "window": window, "config": config})).hexdigest()
            for window in windows:
                wav = wav_for_window(audio["pcm_path"], window)
                fingerprint = fingerprint_for(wav, window)
                state.add(window, fingerprint)
            if retry_window is not None:
                if not run_google:
                    raise TranscriptionError("paid_retry_requires_run_flag")
                state.retry(retry_window, accept_duplicate_cost)
            issued = 0
            paused = False
            if run_google:
                if recognizer is None:
                    from transcription_google import GoogleRecognizer
                    recognizer = GoogleRecognizer()
                for row in state.rows():
                    if row["status"] in {"failed", "unknown"}:
                        break
                    if row["status"] == "succeeded":
                        continue
                    if issued >= max_new_requests:
                        break
                    if dispatch_allowed is not None and not dispatch_allowed():
                        paused = True
                        break
                    window = json.loads(row["plan"])
                    wav = wav_for_window(audio["pcm_path"], window)
                    if fingerprint_for(wav, window) != row["fingerprint"]:
                        raise TranscriptionError("recognized_window_input_changed")
                    # Both audio validation and credentials initialization precede the durable dispatch marker.
                    if hasattr(recognizer, "validate_audio"):
                        recognizer.validate_audio(wav)
                    if hasattr(recognizer, "prepare"):
                        recognizer.prepare()
                    if dispatch_allowed is not None and not dispatch_allowed():
                        paused = True
                        break
                    state.begin(row["window_index"], duplicate_cost_accepted=accept_duplicate_cost and row["window_index"] == retry_window)
                    issued += 1
                    try:
                        results = recognizer.recognize(wav) if hasattr(recognizer, "recognize") else recognizer(wav)
                        response = {"fingerprint": row["fingerprint"], "window": window, "results": results}
                        # Crash after this durable file but before SQLite success is recoverable without another RPC.
                        atomic_json(root / "windows" / f"{row['window_index']:06d}.json", response)
                        state.success(row["window_index"], hashlib.sha256(canonical(response)).hexdigest())
                    except Exception as error:
                        classification = getattr(error, "retry_class", "unknown")
                        code = getattr(error, "error_code", "recognition_outcome_unknown")
                        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                            code = "recognition_outcome_unknown"
                        state.failure(row["window_index"], "failed" if classification in {"rejected", "not_sent"} else "unknown", code)
                        break
            rows = state.rows()
            succeeded = [row for row in rows if row["status"] == "succeeded"]
            ready = []
            for row in succeeded:
                response = state.response(row)
                ready.append({**response["window"], "results": response["results"]})
            fully_done = audio["complete"] and len(succeeded) == len(windows) and bool(windows)
            # Only an unbroken prefix is finalized into sentences, so a failed middle window cannot create false joins.
            prefix = []
            for window in ready:
                if window["index"] != len(prefix):
                    break
                prefix.append(window)
            result = merge_transcript(prefix, time_map, complete=fully_done)
            result.update(id=audio["manifest"]["id"], language="ja-JP", status="transcribed" if fully_done else "partially_transcribed",
                          capture_id=audio["manifest"]["id"], audio_duration_sec=audio["duration_sec"], time_map=time_map,
                          stage="google_transcription", plan_version=PLAN_VERSION)
            atomic_json(root / "result.json", result)
            blocked = [dict(window_index=row["window_index"], status=row["status"], error_code=row["error_code"], attempts=row["attempts"]) for row in rows if row["status"] in {"unknown", "failed"}]
            status = {"status": "complete" if fully_done else "needs_attention" if blocked else "paused" if paused else "prepared" if not run_google else "waiting_for_audio" if not audio["complete"] else "request_budget_reached",
                      "windows_done": len(succeeded), "windows_total": len(windows), "new_requests": issued,
                      "capture_complete": audio["complete"], "audio_duration_sec": audio["duration_sec"], "sentences": len(result["sentences"]),
                      "blocked_windows": blocked, "result_available": True, "plan_version": PLAN_VERSION,
                      "planned_audio_seconds": round(sum(window["audio_end_sec"] - window["audio_start_sec"] for window in windows), 3)}
            atomic_json(root / "status.json", status)
            return status
        finally:
            state.close()
