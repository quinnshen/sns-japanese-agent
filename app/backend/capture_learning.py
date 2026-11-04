"""Durable sentence learning from saved ASR; no audio or Google calls.

Every paid request has a committed attempt before dispatch and a private raw
response before its notes are accepted. Unknown outcomes require explicit retry
consent. Original transcription files are always read-only.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from pathlib import Path

PLAN_VERSION = "learning-batches-v1-6sentences-1800chars-context2"
MAX_ATTEMPTS = 3
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VALIDATION_FEEDBACK_CODES = frozenset({
    "invalid_item_shape", "source_text_changed", "missing_reading", "invalid_translation",
    "invalid_key_points", "invalid_notes", "missing_explanation", "missing_note_reading",
    "invalid_items", "missing_item", "duplicate_item_id",
})
_scheduler_lock = threading.Lock()
_learning_slot = threading.Semaphore(1)
_active: set[str] = set()
_dirty: set[str] = set()


class LearningError(Exception):
    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and _CODE.fullmatch(code) else "local_learning_failed"
        super().__init__(self.code)


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def sentence_fingerprint(sentence: dict) -> str:
    # Google may refine mapping and timing at capture completion. Those fields
    # are display data, not model input, and must not invalidate paid notes.
    return fingerprint({"id": sentence["id"], "text": sentence["text"]})


def _safe_code(error, default="learning_request_outcome_unknown") -> str:
    value = getattr(error, "error_code", None) or getattr(error, "code", None)
    return value if isinstance(value, str) and _CODE.fullmatch(value) else default


def _read_json(path: Path, maximum=50_000_000):
    if path.is_symlink():
        raise LearningError("invalid_learning_path")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode) or os.fstat(source.fileno()).st_size > maximum:
                raise LearningError("saved_learning_data_invalid")
            return json.load(source)
    except (OSError, ValueError, TypeError, RecursionError):
        raise LearningError("saved_learning_data_invalid") from None


def atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise LearningError("invalid_learning_path")
    content = canonical(value)
    descriptor, name = tempfile.mkstemp(prefix=".learning-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
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
        raise LearningError("invalid_capture_directory")
    root = capture_dir / "learning"
    for directory in (root, root / "batches"):
        if directory.is_symlink():
            raise LearningError("invalid_learning_path")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return root


@contextlib.contextmanager
def job_lock(root: Path):
    descriptor = None
    try:
        descriptor = os.open(root / "worker.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise LearningError("worker_already_running") from None
    try:
        yield
    finally:
        os.close(descriptor)


def _source(capture_dir: Path) -> tuple[dict | None, bool]:
    directory = Path(capture_dir) / "transcription"
    path = directory / "result.json"
    if directory.is_symlink() or path.is_symlink():
        raise LearningError("invalid_transcription_directory")
    if not path.exists():
        return None, False
    result = _read_json(path)
    if not isinstance(result, dict) or not isinstance(result.get("sentences"), list):
        raise LearningError("transcript_input_invalid")
    seen = set()
    for sentence in result["sentences"]:
        if not isinstance(sentence, dict):
            raise LearningError("transcript_input_invalid")
        identifier, text = sentence.get("id"), sentence.get("text")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier)
                or identifier in seen or not isinstance(text, str) or not text.strip() or len(text) > 20_000):
            raise LearningError("transcript_input_invalid")
        seen.add(identifier)
        try:
            pairs = [("audio_start", "audio_end", False), ("start", "end", sentence.get("mapping_uncertain") is True)]
            if "video_start" in sentence or "video_end" in sentence:
                pairs.append(("video_start", "video_end", sentence.get("mapping_uncertain") is True))
            for left, right, allow_unmapped in pairs:
                start, end = sentence.get(left), sentence.get(right)
                for value in (start, end):
                    if value is None and allow_unmapped:
                        continue
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise ValueError
                if start is not None and end is not None and end < start:
                    raise ValueError
            canonical(sentence)
        except (ValueError, OverflowError, TypeError, RecursionError):
            raise LearningError("transcript_input_invalid") from None
    try:
        canonical(result)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise LearningError("transcript_input_invalid") from None
    status_path = directory / "status.json"
    status = _read_json(status_path) if status_path.exists() else {}
    complete = result.get("status") == "transcribed" and isinstance(status, dict) and status.get("status") == "complete"
    return result, complete


class LearningState:
    def __init__(self, root: Path):
        self.root = root
        path = root / "state.sqlite3"
        for candidate in (path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if candidate.is_symlink():
                raise LearningError("invalid_learning_path")
        artifacts = any((root / "batches").glob("*.json")) or (root / "result.json").exists()
        if (not path.exists() or path.stat().st_size == 0) and artifacts:
            raise LearningError("learning_state_missing")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sentences (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, notes TEXT, error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS batches (
                batch_index INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, plan TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, retry_cost_accepted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS attempts (
                batch_index INTEGER NOT NULL, attempt INTEGER NOT NULL,
                request_fingerprint TEXT NOT NULL, request_json TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, outcome TEXT NOT NULL,
                error_code TEXT, response_hash TEXT, duplicate_cost_accepted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(batch_index, attempt)
            );
        """)
        self.db.commit()
        if artifacts and self.meta("plan_version") is None:
            self.db.close()
            raise LearningError("learning_state_missing")
        if self.meta("plan_version") not in (None, PLAN_VERSION):
            self.db.close()
            raise LearningError("learning_plan_changed")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('plan_version',?)", (PLAN_VERSION,))

    def close(self):
        self.db.close()

    def meta(self, key: str):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def freeze_config(self, config: dict, provider_fingerprint: str):
        frozen = canonical({"config": config, "provider_fingerprint": provider_fingerprint}).decode()
        previous = self.meta("provider")
        if previous is not None and previous != frozen:
            raise LearningError("learning_configuration_changed")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('provider',?)", (frozen,))

    def request_budget(self, requested: int, *, raise_budget=False) -> int:
        previous = self.meta("max_total_requests")
        current = int(previous) if previous is not None else requested
        chosen = requested if previous is None or requested < current or raise_budget else current
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('max_total_requests',?)", (str(chosen),))
        return chosen

    def rows(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM batches ORDER BY batch_index")]

    def notes(self) -> dict:
        return {row["id"]: json.loads(row["notes"]) for row in self.db.execute("SELECT id,notes FROM sentences WHERE notes IS NOT NULL")}

    def check_source(self, sentences: list[dict]):
        current = {item["id"]: sentence_fingerprint(item) for item in sentences}
        for row in self.db.execute("SELECT id,fingerprint FROM sentences"):
            if current.get(row["id"]) != row["fingerprint"]:
                raise LearningError("learning_input_changed")

    def add(self, batch: dict, source: dict, config: dict, provider_fingerprint: str):
        index = batch["index"]
        digest = fingerprint({"batch": batch, "config": config, "provider_fingerprint": provider_fingerprint, "plan_version": PLAN_VERSION})
        previous = self.db.execute("SELECT fingerprint FROM batches WHERE batch_index=?", (index,)).fetchone()
        if previous and previous[0] != digest:
            raise LearningError("learning_batch_input_changed")
        by_id = {item["id"]: item for item in source["sentences"]}
        with self.db:
            for item in batch["context"] + batch["targets"]:
                sentence_hash = sentence_fingerprint(by_id[item["id"]])
                row = self.db.execute("SELECT fingerprint FROM sentences WHERE id=?", (item["id"],)).fetchone()
                if row and row[0] != sentence_hash:
                    raise LearningError("learning_input_changed")
                self.db.execute("INSERT OR IGNORE INTO sentences(id,fingerprint) VALUES (?,?)", (item["id"], sentence_hash))
            self.db.execute("INSERT OR IGNORE INTO batches(batch_index,fingerprint,plan) VALUES (?,?,?)", (index, digest, canonical(batch).decode()))

    def begin(self, row: dict, request_batch: dict, *, accept_duplicate_cost=False) -> dict:
        if row["status"] != "queued" or row["attempts"] >= MAX_ATTEMPTS:
            raise LearningError("learning_batch_not_dispatchable")
        attempt = row["attempts"] + 1
        request_hash = fingerprint({"batch_fingerprint": row["fingerprint"], "request": request_batch})
        accepted = bool(accept_duplicate_cost or row["retry_cost_accepted"])
        with self.db:
            self.db.execute("UPDATE batches SET status='in_flight',attempts=?,error_code=NULL,retry_cost_accepted=0 WHERE batch_index=?", (attempt, row["batch_index"]))
            self.db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,NULL,'in_flight',NULL,NULL,?)", (row["batch_index"], attempt, request_hash, canonical(request_batch).decode(), time.time(), int(accepted)))
        return {"batch_index": row["batch_index"], "attempt": attempt, "request_fingerprint": request_hash,
                "request_json": canonical(request_batch).decode(), "outcome": "in_flight", "response_hash": None}

    def response_path(self, attempt: dict) -> Path:
        return self.root / "batches" / f"{attempt['batch_index']:06d}.attempt-{attempt['attempt']:02d}.json"

    def read_response(self, attempt: dict) -> dict | None:
        path = self.response_path(attempt)
        if not path.exists() and not path.is_symlink():
            return None
        value = _read_json(path, 20_000_000)
        if (not isinstance(value, dict) or value.get("request_fingerprint") != attempt["request_fingerprint"]
                or value.get("batch_index") != attempt["batch_index"] or value.get("attempt") != attempt["attempt"]
                or value.get("request") != json.loads(attempt["request_json"]) or not isinstance(value.get("envelope"), dict)):
            raise LearningError("saved_learning_response_invalid")
        if attempt["response_hash"] and fingerprint(value) != attempt["response_hash"]:
            raise LearningError("saved_learning_response_invalid")
        return value

    def apply_response(self, attempt: dict, response: dict, *, recovered=False, revalidated=False):
        from learning_text import validate_items
        request = response["request"]
        envelope = response["envelope"]
        result = validate_items(envelope.get("items", []), request["targets"])
        valid, invalid = result["valid"], result["invalid"]
        envelope_status = envelope.get("status")
        default_error = {"incomplete": "learning_response_incomplete", "refused": "learning_response_refused",
                         "invalid": "learning_response_invalid"}.get(envelope_status, "learning_annotation_invalid")
        current = self.db.execute("SELECT * FROM batches WHERE batch_index=?", (attempt["batch_index"],)).fetchone()
        preserved_gate = None
        if current["attempts"] > attempt["attempt"]:
            latest = self.db.execute("SELECT * FROM attempts WHERE batch_index=? AND attempt=?",
                                     (attempt["batch_index"], current["attempts"])).fetchone()
            if latest and latest["outcome"] in {"unknown", "in_flight"} and self.read_response(dict(latest)) is None:
                # Reassessing an earlier known response cannot resolve the
                # financial outcome of a later request with no response.
                if current["status"] == "queued" and current["retry_cost_accepted"]:
                    preserved_gate = ("queued", current["error_code"])
                else:
                    preserved_gate = (latest["outcome"], latest["error_code"])
        with self.db:
            for identifier, notes in valid.items():
                self.db.execute("UPDATE sentences SET notes=COALESCE(notes,?),error_code=NULL WHERE id=?", (canonical(notes).decode(), identifier))
            for item in request["targets"]:
                if item["id"] not in valid:
                    code = invalid.get(item["id"], default_error)
                    if not isinstance(code, str) or not _CODE.fullmatch(code):
                        code = default_error
                    self.db.execute("UPDATE sentences SET error_code=? WHERE id=? AND notes IS NULL", (code, item["id"]))
            original = json.loads(self.db.execute("SELECT plan FROM batches WHERE batch_index=?", (attempt["batch_index"],)).fetchone()[0])
            notes = self.notes()
            done = sum(identifier in notes for identifier in original["target_ids"])
            status = "succeeded" if done == len(original["target_ids"]) else "partial" if done else "failed"
            error = None if status == "succeeded" else default_error
            batch_status, batch_error = preserved_gate if preserved_gate is not None else (status, error)
            self.db.execute("UPDATE batches SET status=?,error_code=? WHERE batch_index=?", (batch_status, batch_error, attempt["batch_index"]))
            outcome = ("revalidated_" + status) if revalidated else "recovered_success" if recovered and status == "succeeded" else status
            self.db.execute("UPDATE attempts SET outcome=?,finished_at=COALESCE(finished_at,?),error_code=?,response_hash=? WHERE batch_index=? AND attempt=?", (outcome, time.time(), error, fingerprint(response), attempt["batch_index"], attempt["attempt"]))

    def recover(self, *, only_saved=False):
        attempts = [dict(row) for row in self.db.execute("SELECT * FROM attempts ORDER BY batch_index,attempt")]
        for attempt in attempts:
            response = self.read_response(attempt)
            if attempt["response_hash"] and response is None:
                raise LearningError("saved_learning_response_missing")
            if attempt["outcome"] in {"in_flight", "unknown"}:
                if response is not None:
                    self.apply_response(attempt, response, recovered=True)
                elif attempt["outcome"] == "in_flight" and not only_saved:
                    self.failure(attempt, "unknown", "interrupted_learning_outcome_unknown")

    def revalidate_saved(self):
        """Reassess known responses locally without changing dispatch history."""
        attempts = [dict(row) for row in self.db.execute("SELECT * FROM attempts ORDER BY batch_index,attempt")]
        for attempt in attempts:
            response = self.read_response(attempt)
            if response is not None:
                self.apply_response(attempt, response, revalidated=True)

    def failure(self, attempt: dict, status: str, code: str):
        with self.db:
            self.db.execute("UPDATE batches SET status=?,error_code=? WHERE batch_index=?", (status, code, attempt["batch_index"]))
            self.db.execute("UPDATE attempts SET outcome=?,finished_at=?,error_code=? WHERE batch_index=? AND attempt=?", (status, time.time(), code, attempt["batch_index"], attempt["attempt"]))

    def retry(self, index: int, accept_duplicate_cost: bool):
        row = self.db.execute("SELECT * FROM batches WHERE batch_index=?", (index,)).fetchone()
        if not row or row["status"] not in {"failed", "partial", "unknown"}:
            raise LearningError("learning_batch_not_retryable")
        if row["status"] == "unknown" and not accept_duplicate_cost:
            raise LearningError("duplicate_cost_confirmation_required")
        if row["attempts"] >= MAX_ATTEMPTS:
            raise LearningError("learning_attempt_limit_reached")
        with self.db:
            self.db.execute("UPDATE batches SET status='queued',error_code=NULL,retry_cost_accepted=? WHERE batch_index=?", (int(accept_duplicate_cost), index))


def _counts(rows: list, source: dict | None, notes: dict) -> dict:
    blocked = [{"batch_index": row["batch_index"], "status": row["status"], "error_code": row["error_code"], "attempts": row["attempts"]}
               for row in rows if row["status"] in {"failed", "partial", "unknown"}]
    return {"batches_done": sum(row["status"] == "succeeded" for row in rows), "batches_total": len(rows),
            "sentences_done": len(notes), "sentences_total": len(source["sentences"]) if source else 0,
            "blocked_batches": blocked, "result_available": bool(notes)}


def _augmented(source: dict, notes: dict, revision: int, config: dict, status: str) -> dict:
    result = copy.deepcopy(source)
    for sentence in result["sentences"]:
        if sentence["id"] in notes:
            sentence.update(copy.deepcopy(notes[sentence["id"]]))
    result.update(learning_revision=revision, learning_status=status, learning_model=config.get("model"),
                  learning_style_version=config.get("style_version"))
    return result


def _publish(root: Path, state: LearningState, source: dict | None, complete: bool, *, issued=0,
             run_openai=False, paused=False, budget=50, total_budget=50) -> dict:
    rows, notes = state.rows(), state.notes()
    counts = _counts(rows, source, notes)
    pending = any(row["status"] == "queued" for row in rows)
    total_requests = state.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    if any(row["status"] == "in_flight" for row in rows):
        status = "running"
    elif counts["blocked_batches"]:
        status = "needs_attention"
    elif complete and source is not None and len(notes) == len(source["sentences"]):
        status = "complete"
    elif paused:
        status = "paused"
    elif pending and (total_requests >= total_budget or (run_openai and issued >= budget)):
        status = "request_budget_reached"
    elif pending:
        status = "queued"
    else:
        status = "waiting_for_transcript"
    config = json.loads(state.meta("provider"))["config"] if state.meta("provider") else {}
    revision = int(state.meta("revision") or 0)
    if source is not None:
        candidate = _augmented(source, notes, 0, config, status)
        result_hash = fingerprint(candidate)
        if result_hash != state.meta("result_hash"):
            revision += 1
            with state.db:
                state.db.execute("INSERT OR REPLACE INTO metadata VALUES ('revision',?)", (str(revision),))
                state.db.execute("INSERT OR REPLACE INTO metadata VALUES ('result_hash',?)", (result_hash,))
        candidate["learning_revision"] = revision
        atomic_json(root / "result.json", candidate)
    invalid = state.db.execute("SELECT COUNT(*) FROM sentences WHERE error_code IS NOT NULL AND notes IS NULL").fetchone()[0]
    result = {"status": status, **counts, "invalid_sentences": invalid, "revision": revision,
              "new_requests": issued, "total_requests": total_requests, "max_total_requests": total_budget,
              "model": config.get("model"), "style_version": config.get("style_version")}
    atomic_json(root / "status.json", result)
    return result


def process_capture_learning(capture_dir: Path, *, run_openai=False, provider=None, max_new_requests=50,
                             retry_batch=None, accept_duplicate_cost=False, dispatch_allowed=None,
                             max_total_requests=50, raise_request_budget=False, revalidate_saved=False) -> dict:
    if revalidate_saved and (run_openai or retry_batch is not None or accept_duplicate_cost or raise_request_budget):
        raise LearningError("saved_revalidation_requires_offline_mode")
    if isinstance(max_new_requests, bool) or not isinstance(max_new_requests, int) or not 1 <= max_new_requests <= 50:
        raise LearningError("invalid_request_budget")
    if isinstance(max_total_requests, bool) or not isinstance(max_total_requests, int) or not 1 <= max_total_requests <= 500:
        raise LearningError("invalid_total_request_budget")
    if retry_batch is not None and (isinstance(retry_batch, bool) or not isinstance(retry_batch, int) or retry_batch < 0):
        raise LearningError("invalid_retry_batch")
    if retry_batch is not None and not run_openai:
        raise LearningError("paid_retry_requires_run_flag")
    from learning_text import plan_batches
    root = private_root(capture_dir)
    with job_lock(root):
        state = LearningState(root)
        try:
            if revalidate_saved:
                source, complete = _source(capture_dir)
                state.check_source(source["sentences"] if source else [])
                state.recover(only_saved=True)
                state.revalidate_saved()
                return _publish(root, state, source, complete, total_budget=int(state.meta("max_total_requests") or 50))
            max_total_requests = state.request_budget(max_total_requests, raise_budget=raise_request_budget)
            source, complete = _source(capture_dir)
            if source is None:
                return _publish(root, state, source, complete, total_budget=max_total_requests)
            if provider is None:
                from learning_openai import OpenAIStudyProvider
                provider = OpenAIStudyProvider()
            config = copy.deepcopy(provider.config)
            provider_hash = provider.provider_fingerprint
            if not isinstance(config, dict) or not isinstance(provider_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", provider_hash):
                raise LearningError("learning_provider_configuration_invalid")
            state.freeze_config(config, provider_hash)
            state.check_source(source["sentences"])
            for batch in plan_batches(source["sentences"], complete=complete):
                state.add(batch, source, config, provider_hash)
            state.recover()
            if retry_batch is not None:
                state.retry(retry_batch, accept_duplicate_cost)
            issued, paused = 0, False
            if run_openai:
                for row in state.rows():
                    if row["status"] != "queued":
                        continue
                    if issued >= max_new_requests or state.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] >= max_total_requests:
                        break
                    if dispatch_allowed is not None and not dispatch_allowed():
                        paused = True
                        break
                    request_batch = json.loads(row["plan"])
                    notes = state.notes()
                    request_batch["targets"] = [item for item in request_batch["targets"] if item["id"] not in notes]
                    request_batch["target_ids"] = [item["id"] for item in request_batch["targets"]]
                    if not request_batch["targets"]:
                        with state.db:
                            state.db.execute("UPDATE batches SET status='succeeded',error_code=NULL WHERE batch_index=?", (row["batch_index"],))
                        continue
                    # A queued batch with a previous attempt can only have been
                    # explicitly retried. Preserve that consent through a
                    # preflight interruption, and send fixed codes, never raw
                    # model/error text or already accepted neighboring notes.
                    if row["attempts"] > 0:
                        errors = {item["id"]: item["error_code"] for item in state.db.execute(
                            "SELECT id,error_code FROM sentences WHERE notes IS NULL AND error_code IS NOT NULL"
                        )}
                        feedback = {identifier: errors[identifier] for identifier in request_batch["target_ids"]
                                    if errors.get(identifier) in _VALIDATION_FEEDBACK_CODES}
                        if feedback:
                            request_batch["validation_errors"] = feedback
                    if hasattr(provider, "validate_batch"):
                        provider.validate_batch(request_batch)
                    provider.prepare()
                    if dispatch_allowed is not None and not dispatch_allowed():
                        paused = True
                        break
                    # Recheck the source after potentially slow preflight, before
                    # marking dispatch or permitting a paid provider invocation.
                    latest, _ = _source(capture_dir)
                    state.check_source(latest["sentences"] if latest else [])
                    attempt = state.begin(row, request_batch, accept_duplicate_cost=accept_duplicate_cost and row["batch_index"] == retry_batch)
                    dispatched = False
                    try:
                        _publish(root, state, source, complete, issued=issued, run_openai=True, total_budget=max_total_requests)
                        issued += 1
                        dispatched = True
                        envelope = provider.generate(request_batch)
                        if not isinstance(envelope, dict):
                            envelope = {"status": "invalid", "items": [], "usage": {}}
                        envelope = {key: value for key, value in envelope.items() if key in {"status", "items", "usage", "raw_output", "response_id"}}
                        response = {"batch_index": attempt["batch_index"], "attempt": attempt["attempt"],
                                    "request_fingerprint": attempt["request_fingerprint"], "request": request_batch,
                                    "provider_fingerprint": provider_hash, "envelope": envelope}
                        atomic_json(state.response_path(attempt), response)
                        state.apply_response(attempt, response)
                    except Exception as error:
                        classification = getattr(error, "retry_class", "unknown") if dispatched else "not_sent"
                        state.failure(attempt, "failed" if classification in {"not_sent", "rejected"} else "unknown", _safe_code(error))
            latest, latest_complete = _source(capture_dir)
            state.check_source(latest["sentences"] if latest else [])
            return _publish(root, state, latest, latest_complete, issued=issued, run_openai=run_openai,
                            paused=paused, budget=max_new_requests, total_budget=max_total_requests)
        finally:
            state.close()


def _read_state(capture_dir: Path):
    root = Path(capture_dir) / "learning"
    database = root / "state.sqlite3"
    if Path(capture_dir).is_symlink() or root.is_symlink() or database.is_symlink():
        raise LearningError("invalid_learning_path")
    if not database.exists():
        return root, None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    except sqlite3.DatabaseError:
        raise LearningError("learning_state_invalid") from None
    connection.row_factory = sqlite3.Row
    return root, connection


def read_learning_status(capture_dir: Path) -> dict:
    empty = {"status": "not_started", "batches_done": 0, "batches_total": 0, "sentences_done": 0,
             "sentences_total": 0, "invalid_sentences": 0, "blocked_batches": [], "result_available": False, "revision": 0,
             "total_requests": 0, "max_total_requests": 50}
    root, connection = _read_state(capture_dir)
    if connection is None:
        return empty
    try:
        source, source_complete = _source(capture_dir)
        rows = [dict(row) for row in connection.execute("SELECT * FROM batches ORDER BY batch_index")]
        notes = {row["id"]: True for row in connection.execute("SELECT id FROM sentences WHERE notes IS NOT NULL")}
        saved = _read_json(root / "status.json") if (root / "status.json").exists() else empty
        if not isinstance(saved, dict):
            raise LearningError("saved_learning_data_invalid")
        result = {key: saved.get(key, value) for key, value in empty.items()}
        result.update(_counts(rows, source, notes))
        result["invalid_sentences"] = connection.execute("SELECT COUNT(*) FROM sentences WHERE error_code IS NOT NULL AND notes IS NULL").fetchone()[0]
        if any(row["status"] == "in_flight" for row in rows):
            result["status"] = "running"
        elif result["blocked_batches"]:
            result["status"] = "needs_attention"
        elif source_complete and source is not None and len(notes) == len(source["sentences"]):
            result["status"] = "complete"
        elif result["status"] == "complete":
            result["status"] = "queued" if any(row["status"] == "queued" for row in rows) else "waiting_for_transcript"
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        result["revision"] = int(metadata.get("revision", 0))
        result["max_total_requests"] = int(metadata.get("max_total_requests", 50))
        result["total_requests"] = connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        if "provider" in metadata:
            config = json.loads(metadata["provider"])["config"]
            result.update(model=config.get("model"), style_version=config.get("style_version"))
        if "error_code" in saved and isinstance(saved["error_code"], str) and _CODE.fullmatch(saved["error_code"]):
            result["error_code"] = saved["error_code"]
        return result
    except (sqlite3.DatabaseError, ValueError, TypeError, KeyError):
        raise LearningError("learning_state_invalid") from None
    finally:
        connection.close()


def read_learning_result(capture_dir: Path) -> dict | None:
    root, connection = _read_state(capture_dir)
    if connection is None:
        return None
    try:
        source, _ = _source(capture_dir)
        if source is None:
            return None
        current = {item["id"]: sentence_fingerprint(item) for item in source["sentences"]}
        for row in connection.execute("SELECT id,fingerprint FROM sentences"):
            if current.get(row["id"]) != row["fingerprint"]:
                raise LearningError("learning_input_changed")
        notes = {row["id"]: json.loads(row["notes"]) for row in connection.execute("SELECT id,notes FROM sentences WHERE notes IS NOT NULL")}
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        config = json.loads(metadata.get("provider", '{"config":{}}'))["config"]
        status = read_learning_status(capture_dir)
        return _augmented(source, notes, int(metadata.get("revision", 0)), config, status["status"])
    except (sqlite3.DatabaseError, ValueError, TypeError, KeyError):
        raise LearningError("learning_state_invalid") from None
    finally:
        connection.close()


def request_learning(capture_dir: Path, *, enabled=lambda: True) -> None:
    """Coalesce ASR publications on a semaphore independent of Google work."""
    key = str(Path(capture_dir).resolve())
    with _scheduler_lock:
        if key in _active:
            _dirty.add(key)
            return
        _active.add(key)

    def work():
        released, busy_waits = False, 0
        try:
            while enabled():
                try:
                    with _learning_slot:
                        if enabled():
                            process_capture_learning(Path(capture_dir), run_openai=True, dispatch_allowed=enabled)
                except LearningError as error:
                    if error.code == "worker_already_running" and busy_waits < 150:
                        busy_waits += 1
                        time.sleep(1)
                        continue
                    status = read_learning_status(capture_dir)
                    atomic_json(private_root(capture_dir) / "status.json", {**status, "status": "error", "error_code": error.code})
                except Exception as error:
                    atomic_json(private_root(capture_dir) / "status.json", {"status": "error", "error_code": _safe_code(error, "local_learning_failed")})
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

    threading.Thread(target=work, daemon=True, name="capture-learning").start()
