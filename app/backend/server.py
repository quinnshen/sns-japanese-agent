"""Loopback API for the production extension. Audio stays local after transcription."""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from config import setting
from pipeline import MAX_DURATION_SECONDS, save_result, transcribe_media
from enrich import enrich_result, local_api_key

HOST = "127.0.0.1"
PORT = 8766
MAX_UPLOAD_BYTES = 200_000_000
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


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
        if not trusted_request_source(self.headers.get("Host", ""), self.headers.get("Origin", "")):
            self.send_json(403, {"error": "Local extension requests only"})
            return True
        return False

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        if self.reject_untrusted_request():
            return
        origin = self.headers.get("Origin", "")
        if not origin.startswith("chrome-extension://"):
            self.send_json(403, {"error": "Extension origin required"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Japanese-Agent, X-Video-Duration-Sec, X-Capture-Offset-Sec, X-Source-Url",
        )
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:
        if self.reject_untrusted_request():
            return
        if self.path == "/health":
            self.send_json(200, {
                "ok": True,
                "project_configured": bool(setting("GOOGLE_CLOUD_PROJECT")),
                "enrichment_enabled": setting("ENABLE_ENRICHMENT") == "1",
                "enrichment_key_configured": bool(local_api_key()),
            })
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
        if self.path != "/jobs":
            self.send_json(404, {"error": "Not found"})
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
    configured = bool(setting("GOOGLE_CLOUD_PROJECT", "").strip())
    print(f"Listening on http://{HOST}:{PORT}; Google project configured: {configured}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
