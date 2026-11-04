"""Loopback API for the production extension. Audio stays local after transcription."""

from __future__ import annotations

import json
import re
import threading
import uuid
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import setting
from capture_store import CaptureError, CaptureStore
from capture_transcription import TranscriptionError
from pipeline import MAX_DURATION_SECONDS, save_result, transcribe_media
from enrich import enrich_result, local_api_key

HOST = "127.0.0.1"
PORT = 8766
MAX_UPLOAD_BYTES = 200_000_000
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


@lru_cache(maxsize=8)
def capture_store(root: Path) -> CaptureStore:
    return CaptureStore(root)


def transcription_enabled() -> bool:
    return setting("ENABLE_CAPTURE_TRANSCRIPTION", "0") == "1"


def learning_enabled() -> bool:
    return setting("ENABLE_CAPTURE_ENRICHMENT", "0") == "1"


def capture_processing_allowed(capture_dir: Path, stage_enabled) -> bool:
    """Recheck explicit cancellation before each new cloud request."""
    try:
        if not stage_enabled():
            return False
        manifest = capture_store(DATA_DIR / "captures").get(capture_dir.name)
        return manifest["status"] in {"recording", "complete"}
    except (CaptureError, OSError, ValueError, TypeError, KeyError):
        return False


def wake_learning(capture_dir: Path) -> None:
    if learning_enabled():
        from capture_learning import request_learning
        request_learning(capture_dir, enabled=lambda: capture_processing_allowed(capture_dir, learning_enabled))


def wake_transcription(manifest: dict) -> None:
    if transcription_enabled() and manifest["status"] in {"recording", "complete"}:
        from capture_transcription import request_processing
        directory = DATA_DIR / "captures" / manifest["id"]
        request_processing(directory, enabled=lambda: capture_processing_allowed(directory, transcription_enabled),
                           on_result=wake_learning)


def trusted_request_source(host: str, origin: str) -> bool:
    """Reject remote web origins and DNS-rebinding hosts before reading local data or starting paid work."""
    return host.lower() in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"} and (
        not origin or re.fullmatch(r"chrome-extension://[a-p]{32}", origin) is not None
    )


def valid_youtube_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"youtube.com", "www.youtube.com"}
        and (parsed.path == "/watch" or parsed.path.startswith("/shorts/"))
    )


def video_key(url: str) -> str | None:
    if not valid_youtube_url(url):
        return None
    parsed = urlparse(url)
    if parsed.path == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        video_id = parsed.path.split("/")[2]
    return video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else None


def latest_result_for_url(url: str) -> dict | None:
    key = video_key(url)
    if not key:
        return None
    for path in sorted(DATA_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        result = json.loads(path.read_text(encoding="utf-8"))
        if video_key(result.get("source", {}).get("url", "")) == key:
            return result
    return None


def latest_learning_for_url(url: str) -> dict | None:
    """Read cached notes for this video; never schedule a provider request."""
    key = video_key(url)
    if not key:
        return None
    from capture_learning import LearningError, read_learning_result, read_learning_status
    root = DATA_DIR / "captures"
    if not root.is_dir() or root.is_symlink():
        return None
    candidates = []
    for path in root.glob("*/manifest.json"):
        try:
            if path.is_symlink() or path.parent.is_symlink():
                continue
            manifest = capture_store(root).get(path.parent.name)
            if video_key(manifest.get("source_url", "")) != key:
                continue
            result = read_learning_result(path.parent)
            status = read_learning_status(path.parent)
            ready = sum(bool(sentence.get("study")) for sentence in (result or {}).get("sentences", []))
            saved = path.parent / "learning" / "result.json"
            modified = saved.stat().st_mtime if saved.exists() else path.stat().st_mtime
            candidates.append((ready > 0, modified, {"capture_id": manifest["id"], **status, "result": result}))
        except (CaptureError, LearningError, OSError, ValueError, TypeError, KeyError):
            # A damaged or incomplete old recording must not hide other saved notes.
            continue
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def run_job(job_id: str, path: Path, duration: float, offset: float, url: str) -> None:
    try:
        def progress(done: int, total: int) -> None:
            with jobs_lock:
                jobs[job_id].update(status="transcribing", chunks_done=done, chunks_total=total)

        result = transcribe_media(
            path,
            duration,
            capture_offset=offset,
            source={"type": "youtube", "url": url},
            progress=progress,
        )
        result["id"] = job_id
        save_result(result, DATA_DIR)
        if setting("ENABLE_ENRICHMENT") == "1":
            with jobs_lock:
                jobs[job_id].update(status="enriching", enrichment_batches_done=0, enrichment_batches_total=0)
            try:
                def enrichment_progress(done: int, total: int, current: dict) -> None:
                    save_result(current, DATA_DIR)
                    with jobs_lock:
                        jobs[job_id].update(enrichment_batches_done=done, enrichment_batches_total=total)

                result = enrich_result(result, progress=enrichment_progress)
                save_result(result, DATA_DIR)
            except Exception as exc:
                # An enrichment failure must not discard a completed transcript or saved batches.
                result = json.loads((DATA_DIR / f"{job_id}.json").read_text(encoding="utf-8"))
                result["enrichment_error"] = str(exc)
                result["status"] = "partially_enriched" if any(s.get("study") for s in result["sentences"]) else "transcribed"
                save_result(result, DATA_DIR)
        with jobs_lock:
            jobs[job_id].update(status="complete", result=result)
    except Exception as exc:
        with jobs_lock:
            jobs[job_id].update(status="error", error=str(exc))
    finally:
        path.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_request(self, code="-", size="-") -> None:
        # The default HTTP access log includes source video URLs in query strings.
        self.log_message("%s %s %s", self.command, urlparse(self.path).path, str(code))

    def reject_untrusted_request(self) -> bool:
        origin = self.headers.get("Origin", "")
        cross_site_without_origin = not origin and self.headers.get("Sec-Fetch-Site") == "cross-site"
        if cross_site_without_origin or not trusted_request_source(self.headers.get("Host", ""), origin):
            self.send_json(403, {"error": "Local extension requests only"})
            return True
        return False

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def capture_json(self) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise CaptureError(415, "Expected JSON request")
        length = self.body_length(256_000)
        try:
            payload = json.loads(self.read_body(length))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise CaptureError(400, "Invalid JSON request") from None
        if not isinstance(payload, dict):
            raise CaptureError(400, "Expected JSON object")
        return payload

    def body_length(self, maximum: int) -> int:
        if self.headers.get("Transfer-Encoding"):
            raise CaptureError(400, "Transfer-Encoding is not supported")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise CaptureError(411, "Content-Length required") from None
        if not 0 < length <= maximum:
            raise CaptureError(413, "Request body exceeds limit or is empty")
        return length

    def read_body(self, length: int) -> bytes:
        body = self.rfile.read(length)
        if len(body) != length:
            raise CaptureError(400, "Incomplete request body")
        return body

    def capture_error(self, error: CaptureError) -> None:
        self.send_json(error.status, {"error": error.error, **error.details})

    def acknowledge_capture(self, payload: dict, manifest: dict | None = None) -> None:
        try:
            self.send_json(200, payload)
        finally:
            if manifest is not None:
                try:
                    wake_transcription(manifest)
                except Exception:
                    # A scheduler failure must never rewrite an already saved batch ACK.
                    self.log_message("Local transcription scheduling failed")

    def capture_post(self) -> None:
        try:
            payload = self.capture_json()
            store = capture_store(DATA_DIR / "captures")
            if self.path == "/captures":
                self.send_json(200, store.create(payload))
                return
            match = re.fullmatch(r"/captures/([0-9a-f-]{36})/(timing|complete|abort)", self.path)
            if not match:
                raise CaptureError(404, "Capture route not found")
            capture_id, action = match.groups()
            operation = {"timing": store.set_timing, "complete": store.complete}
            result = store.abort(capture_id, payload) if action == "abort" else operation[action](capture_id, payload)
            self.acknowledge_capture(result, result if action != "abort" else None)
        except (BrokenPipeError, ConnectionResetError):
            return
        except CaptureError as error:
            self.capture_error(error)
        except (OSError, ValueError, TypeError, KeyError):
            self.send_json(500, {"error": "Local capture storage operation failed"})

    def capture_audio(self, capture_id: str) -> None:
        store = capture_store(DATA_DIR / "captures")
        path = store.audio_path(capture_id)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", requested)
            if not match or not any(match.groups()):
                self.send_json(416, {"error": "Unsupported byte range"})
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), end) if last else end
            else:
                start = max(0, size - int(last))
            if not 0 <= start <= end < size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        with path.open("rb") as source:
            source.seek(start)
            self.send_response(status)
            self.send_header("Content-Type", "audio/webm")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            origin = self.headers.get("Origin", "")
            if re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            remaining = end - start + 1
            while remaining:
                block = source.read(min(64 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def do_PUT(self) -> None:
        if self.reject_untrusted_request():
            return
        try:
            match = re.fullmatch(r"/captures/([0-9a-f-]{36})/batches/(\d{1,5})", self.path)
            if not match:
                raise CaptureError(404, "Capture batch route not found")
            mime = self.headers.get("Content-Type", "").replace(" ", "").lower()
            if mime not in {"audio/webm", "audio/webm;codecs=opus"}:
                raise CaptureError(415, "Expected WebM audio")
            length = self.body_length(10_000_000)
            try:
                metadata = {
                    "sha256": self.headers.get("X-Batch-SHA256", ""),
                    "capture_start_ms": float(self.headers.get("X-Capture-Start-Ms", "")),
                    "capture_end_ms": float(self.headers.get("X-Capture-End-Ms", "")),
                }
                if self.headers.get("X-Blob-Timecode-Ms") is not None:
                    metadata["blob_timecode_ms"] = float(self.headers["X-Blob-Timecode-Ms"])
            except ValueError:
                raise CaptureError(400, "Invalid batch timing headers") from None
            store = capture_store(DATA_DIR / "captures")
            result = store.put_batch(match[1], int(match[2]), self.read_body(length), metadata)
            self.acknowledge_capture(result, store.get(match[1]) if transcription_enabled() else None)
        except (BrokenPipeError, ConnectionResetError):
            return
        except CaptureError as error:
            self.capture_error(error)
        except (OSError, ValueError, TypeError, KeyError):
            self.send_json(500, {"error": "Local capture storage operation failed"})

    def do_OPTIONS(self) -> None:
        if self.reject_untrusted_request():
            return
        origin = self.headers.get("Origin", "")
        if not origin.startswith("chrome-extension://"):
            self.send_json(403, {"error": "Extension origin required"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Japanese-Agent, X-Video-Duration-Sec, X-Capture-Offset-Sec, X-Source-Url, "
            "X-Batch-SHA256, X-Capture-Start-Ms, X-Capture-End-Ms, X-Blob-Timecode-Ms",
        )
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:
        if self.reject_untrusted_request():
            return
        if self.path == "/health":
            self.send_json(200, {
                "ok": True,
                "capabilities": {
                    "capture_batches": True,
                    "cloud_processing": setting("ENABLE_LEGACY_PIPELINE", "0") == "1" or transcription_enabled() or learning_enabled(),
                    "capture_transcription": transcription_enabled(),
                    "capture_enrichment": learning_enabled(),
                },
            })
            return
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/learning/latest":
            try:
                parameters = parse_qs(parsed_path.query)
                values = parameters.get("url", [])
                if len(self.path) > 8192 or set(parameters) != {"url"} or len(values) != 1 or not video_key(values[0]):
                    self.send_json(400, {"error": "Expected one YouTube video URL"})
                    return
                payload = latest_learning_for_url(values[0])
                self.send_json(200, payload or {"capture_id": None, "status": "not_found", "result": None})
            except (OSError, ValueError, TypeError, KeyError):
                self.send_json(500, {"error": "Local learning storage operation failed"})
            return
        if self.path.startswith("/captures/"):
            try:
                learning_match = re.fullmatch(r"/captures/([0-9a-f-]{36})/learning", self.path)
                if learning_match:
                    from capture_learning import LearningError, read_learning_status, read_learning_result
                    capture_store(DATA_DIR / "captures").get(learning_match[1])
                    directory = DATA_DIR / "captures" / learning_match[1]
                    try:
                        self.send_json(200, {**read_learning_status(directory), "result": read_learning_result(directory)})
                    except LearningError:
                        self.send_json(500, {"error": "Local learning storage operation failed"})
                    return
                transcription_match = re.fullmatch(r"/captures/([0-9a-f-]{36})/transcription", self.path)
                if transcription_match:
                    from capture_transcription import read_status, read_result
                    capture_store(DATA_DIR / "captures").get(transcription_match[1])
                    directory = DATA_DIR / "captures" / transcription_match[1]
                    self.send_json(200, {**read_status(directory), "result": read_result(directory)})
                    return
                match = re.fullmatch(r"/captures/([0-9a-f-]{36})(/audio)?", self.path)
                if not match:
                    raise CaptureError(404, "Capture route not found")
                if match[2]:
                    self.capture_audio(match[1])
                else:
                    self.send_json(200, capture_store(DATA_DIR / "captures").get(match[1]))
            except (BrokenPipeError, ConnectionResetError):
                return
            except CaptureError as error:
                self.capture_error(error)
            except (OSError, ValueError, TypeError, KeyError, TranscriptionError):
                self.send_json(500, {"error": "Local capture storage operation failed"})
            return
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/results":
            result = latest_result_for_url(parse_qs(parsed_path.query).get("url", [""])[0])
            self.send_json(200 if result else 404, {"result": result} if result else {"error": "No saved result"})
            return
        match = re.fullmatch(r"/jobs/([0-9a-f-]{36})", self.path)
        if not match:
            self.send_json(404, {"error": "Not found"})
            return
        job_id = match.group(1)
        with jobs_lock:
            job = jobs.get(job_id)
            payload = dict(job) if job else None
        saved = DATA_DIR / f"{job_id}.json"
        if saved.exists() and (payload is None or payload.get("status") == "complete"):
            payload = {"id": job_id, "status": "complete", "result": json.loads(saved.read_text(encoding="utf-8"))}
        self.send_json(200 if payload else 404, payload or {"error": "Job not found"})

    def do_POST(self) -> None:
        if self.reject_untrusted_request():
            return
        learning_match = re.fullmatch(r"/captures/([0-9a-f-]{36})/learning", self.path)
        if learning_match:
            try:
                if not learning_enabled():
                    self.send_json(409, {"error": "Capture learning is disabled"})
                    return
                if self.capture_json() != {}:
                    raise CaptureError(400, "Expected an empty learning start request")
                manifest = capture_store(DATA_DIR / "captures").get(learning_match[1])
                if manifest["status"] == "aborted":
                    raise CaptureError(409, "Interrupted captures require separate inspection")
                wake_learning(DATA_DIR / "captures" / manifest["id"])
                self.send_json(202, {"id": manifest["id"], "status": "scheduled"})
            except CaptureError as error:
                self.capture_error(error)
            except Exception:
                self.send_json(500, {"error": "Local learning scheduling failed"})
            return
        transcription_match = re.fullmatch(r"/captures/([0-9a-f-]{36})/transcription", self.path)
        if transcription_match:
            try:
                if not transcription_enabled():
                    self.send_json(409, {"error": "Capture transcription is disabled"})
                    return
                if self.capture_json() != {}:
                    raise CaptureError(400, "Expected an empty transcription start request")
                manifest = capture_store(DATA_DIR / "captures").get(transcription_match[1])
                if manifest["status"] == "aborted":
                    raise CaptureError(409, "Interrupted captures require separate inspection")
                wake_transcription(manifest)
                self.send_json(202, {"id": manifest["id"], "status": "scheduled"})
            except CaptureError as error:
                self.capture_error(error)
            return
        if self.path == "/captures" or self.path.startswith("/captures/"):
            self.capture_post()
            return
        if self.path != "/jobs":
            self.send_json(404, {"error": "Not found"})
            return
        if setting("ENABLE_LEGACY_PIPELINE", "0") != "1":
            self.send_json(409, {"error": "Cloud processing is disabled during capture acceptance"})
            return
        if self.headers.get("X-Japanese-Agent") != "full-video-v1":
            self.send_json(403, {"error": "Extension request required"})
            return
        if not self.headers.get("Content-Type", "").startswith("audio/webm"):
            self.send_json(415, {"error": "Expected WebM audio"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            duration = float(self.headers.get("X-Video-Duration-Sec", "0"))
            offset = float(self.headers.get("X-Capture-Offset-Sec", "-1"))
        except ValueError:
            self.send_json(400, {"error": "Invalid recording metadata"})
            return
        url = self.headers.get("X-Source-Url", "")
        if not 0 < length <= MAX_UPLOAD_BYTES:
            self.send_json(413, {"error": "Audio upload exceeds 200 MB limit"})
            return
        if not 0 < duration <= MAX_DURATION_SECONDS or not 0 <= offset <= 30:
            self.send_json(400, {"error": "Invalid video duration or capture offset"})
            return
        if not valid_youtube_url(url):
            self.send_json(400, {"error": "Expected a YouTube watch or Shorts URL"})
            return
        with jobs_lock:
            if any(job["status"] in {"queued", "transcribing", "enriching"} for job in jobs.values()):
                self.send_json(409, {"error": "A transcription is already running"})
                return
            job_id = str(uuid.uuid4())
            jobs[job_id] = {"id": job_id, "status": "queued", "chunks_done": 0, "chunks_total": 0}

        uploads = DATA_DIR / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / f"{job_id}.webm"
        try:
            remaining = length
            with path.open("wb") as output:
                while remaining:
                    block = self.rfile.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ConnectionError("Incomplete audio upload")
                    output.write(block)
                    remaining -= len(block)
        except Exception as exc:
            path.unlink(missing_ok=True)
            with jobs_lock:
                jobs.pop(job_id, None)
            self.send_json(400, {"error": str(exc)})
            return
        threading.Thread(target=run_job, args=(job_id, path, duration, offset, url), daemon=True).start()
        self.send_json(202, {"id": job_id, "status": "queued"})


if __name__ == "__main__":
    print(f"Listening on http://{HOST}:{PORT}; local batch capture ready")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
