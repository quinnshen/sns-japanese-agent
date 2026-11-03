"""Local media -> timestamped Japanese sentences. No translation is done here."""

from __future__ import annotations

import json
import math
import os
import subprocess
import uuid
from pathlib import Path

CHUNK_SECONDS = 45.0
OVERLAP_SECONDS = 2.0
MAX_DURATION_SECONDS = 30 * 60


def ffmpeg_executable() -> str:
    override = os.getenv("FFMPEG_PATH")
    if override:
        return override
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def wav_slice(path: Path, start: float, duration: float) -> bytes:
    command = [
        ffmpeg_executable(), "-v", "error", "-ss", str(start), "-i", str(path),
        "-t", str(duration), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
    ]
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode or len(completed.stdout) < 44:
        raise RuntimeError("无法从录音提取音频；请确认视频包含可播放的音轨。")
    return completed.stdout


def recognize_wav(audio: bytes, client, project_id: str) -> list[dict]:
    from google.cloud.speech_v2.types import cloud_speech

    request = cloud_speech.RecognizeRequest(
        recognizer=f"projects/{project_id}/locations/global/recognizers/_",
        config=cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            language_codes=["ja-JP"],
            model="long",
            features=cloud_speech.RecognitionFeatures(
                enable_word_time_offsets=True,
                enable_automatic_punctuation=True,
            ),
        ),
        content=audio,
    )
    response = client.recognize(request=request, timeout=120)
    results = []
    for result in response.results:
        if not result.alternatives:
            continue
        alternative = result.alternatives[0]
        words = [
            {
                "text": word.word,
                "start": word.start_offset.total_seconds(),
                "end": word.end_offset.total_seconds(),
            }
            for word in alternative.words
        ]
        results.append({"text": alternative.transcript, "words": words})
    return results


def punctuate_result_words(result: dict) -> list[dict]:
    """Google puts punctuation in the transcript, but often not in WordInfo.word."""
    transcript = result.get("text", "")
    raw_words = result["words"]
    words = []
    cursor = 0
    for index, raw in enumerate(raw_words):
        word = dict(raw)
        position = transcript.find(word["text"], cursor)
        if position >= 0:
            after = position + len(word["text"])
            if index + 1 < len(raw_words):
                next_position = transcript.find(raw_words[index + 1]["text"], after)
                between = transcript[after:next_position] if next_position >= 0 else ""
            else:
                between = transcript[after:]
            word["text"] += "".join(char for char in between if char in "。、！？!?.,")
            cursor = after + len(between)
        words.append(word)
    return words


def collect_words(results: list[dict], chunk_start: float, core_end: float, is_last: bool) -> list[dict]:
    selected = []
    for result in results:
        punctuated = punctuate_result_words(result)
        result_words = []
        for word in punctuated:
            midpoint = chunk_start + (word["start"] + word["end"]) / 2
            if not is_last and midpoint >= core_end:
                continue
            result_words.append({
                "text": word["text"],
                "start": round(chunk_start + word["start"], 2),
                "end": round(chunk_start + word["end"], 2),
            })
        if result_words and len(result_words) == len(punctuated):
            result_words[-1]["result_end"] = True
        selected.extend(result_words)
    return selected


def sentences_from_words(words: list[dict]) -> list[dict]:
    sentences = []
    group = []

    def flush() -> None:
        if not group:
            return
        sentences.append({
            "id": f"s{len(sentences) + 1:04d}",
            "start": group[0]["start"],
            "end": group[-1]["end"],
            "text": "".join(word["text"] for word in group).strip(),
            "words": [{key: word[key] for key in ("text", "start", "end")} for word in group],
        })
        group.clear()

    for word in sorted(words, key=lambda item: (item["start"], item["end"])):
        if group and word["start"] - group[-1]["end"] > 1.2:
            flush()
        group.append(word)
        if word["text"].rstrip().endswith(("。", "！", "？", "!", "?", ".")) or word.get("result_end"):
            flush()
    flush()
    return [sentence for sentence in sentences if sentence["text"]]


def transcribe_media(
    path: Path,
    duration: float,
    *,
    capture_offset: float = 0.0,
    source: dict | None = None,
    recognize=None,
    progress=None,
) -> dict:
    """Duration is video timeline length; capture_offset trims audio before video playback."""
    if not 0 < duration <= MAX_DURATION_SECONDS:
        raise ValueError("视频时长需在 0 到 30 分钟之间。")
    if not 0 <= capture_offset <= 30:
        raise ValueError("录音起点与播放起点的偏移无效。")

    if recognize is None:
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project_id or project_id == "YOUR_PROJECT_ID":
            raise RuntimeError("请设置 GOOGLE_CLOUD_PROJECT。")
        from google.auth import default as default_credentials
        from google.cloud.speech_v2 import SpeechClient

        credentials, _ = default_credentials(
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
            quota_project_id=project_id,
        )
        client = SpeechClient(credentials=credentials)
        recognize = lambda audio: recognize_wav(audio, client, project_id)

    step = CHUNK_SECONDS - OVERLAP_SECONDS
    total_chunks = max(1, 1 + math.ceil((duration - CHUNK_SECONDS) / step))
    words = []
    chunk_count = 0
    start = 0.0
    while start < duration:
        length = min(CHUNK_SECONDS, duration - start)
        audio = wav_slice(path, capture_offset + start, length)
        if len(audio) > 10_000_000:
            raise RuntimeError("音频片段超过 Google 同步识别的大小限制。")
        results = recognize(audio)
        is_last = start + CHUNK_SECONDS >= duration
        words.extend(collect_words(results, start, start + step, is_last))
        chunk_count += 1
        if progress:
            progress(chunk_count, total_chunks)
        if is_last:
            break
        start += step

    sentences = sentences_from_words(words)
    return {
        "schema_version": 1,
        "id": str(uuid.uuid4()),
        "source": source or {"type": "local_file"},
        "language": "ja-JP",
        "duration": round(duration, 2),
        "status": "transcribed",
        "transcript": "".join(item["text"] for item in sentences),
        "sentences": sentences,
        "chunk_count": chunk_count,
    }


def save_result(result: dict, data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / f"{result['id']}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target
