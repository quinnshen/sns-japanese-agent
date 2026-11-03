"""Loopback-only bridge from the test extension to Google Speech-to-Text V2."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8765
MAX_BYTES = 10_000_000


def transcribe(audio: bytes) -> dict:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project_id or project_id == "YOUR_PROJECT_ID":
        raise RuntimeError("请先设置 GOOGLE_CLOUD_PROJECT 项目 ID。")

    try:
        from google.cloud.speech_v2 import SpeechClient
        from google.cloud.speech_v2.types import cloud_speech
        from google.auth import default as default_credentials
    except ImportError as exc:
        raise RuntimeError("缺少 google-cloud-speech；请安装 requirements.txt。") from exc

    config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=["ja-JP"],
        model="long",
        features=cloud_speech.RecognitionFeatures(
            enable_word_time_offsets=True,
            enable_automatic_punctuation=True,
        ),
    )
    request = cloud_speech.RecognizeRequest(
        recognizer=f"projects/{project_id}/locations/global/recognizers/_",
        config=config,
        content=audio,
    )
    # Resolve credentials only on the computer running this bridge. Explicitly
    # bind the quota project to the operator's chosen project, not an ADC default.
    credentials, _ = default_credentials(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        quota_project_id=project_id,
    )
    response = SpeechClient(credentials=credentials).recognize(request=request, timeout=90)

    results = []
    words = []
    for item in response.results:
        if not item.alternatives:
            continue
        alternative = item.alternatives[0]
        results.append(alternative.transcript)
        for word in alternative.words:
            words.append({
                "word": word.word,
                "start": round(word.start_offset.total_seconds(), 2),
                "end": round(word.end_offset.total_seconds(), 2),
            })
    return {"text": "".join(results), "words": words}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        origin = self.headers.get("Origin", "")
        if origin.startswith("chrome-extension://"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin", "")
        if not origin.startswith("chrome-extension://"):
            self.send_json(403, {"error": "Extension origin required"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Japanese-Agent")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_json(404, {"error": "Not found"})
            return
        self.send_json(200, {"ok": True, "project_configured": bool(os.getenv("GOOGLE_CLOUD_PROJECT"))})

    def do_POST(self) -> None:
        if self.path != "/transcribe":
            self.send_json(404, {"error": "Not found"})
            return
        if self.headers.get("X-Japanese-Agent") != "capture-test":
            self.send_json(403, {"error": "Extension request required"})
            return
        if not self.headers.get("Content-Type", "").startswith("audio/webm"):
            self.send_json(415, {"error": "Expected WebM Opus audio"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 1 or length > MAX_BYTES:
            self.send_json(413, {"error": "Audio must be between 1 byte and 10 MB"})
            return
        audio = self.rfile.read(length)
        try:
            self.send_json(200, transcribe(audio))
        except Exception as exc:
            self.send_json(503, {"error": str(exc)})


if __name__ == "__main__":
    configured = bool(os.getenv("GOOGLE_CLOUD_PROJECT", "").strip())
    print(f"Listening on http://{HOST}:{PORT}; Google project configured: {configured}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
