"""Run the same transcription pipeline on a local video file."""

import argparse
import re
import subprocess
from pathlib import Path

from pipeline import ffmpeg_executable, save_result, transcribe_media

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def media_duration(path: Path) -> float:
    output = subprocess.run(
        [ffmpeg_executable(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
    ).stderr
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        raise RuntimeError("无法读取视频时长。")
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transcribe a local Japanese video")
    parser.add_argument("video", type=Path)
    args = parser.parse_args()
    path = args.video.expanduser().resolve()
    if not path.is_file():
        parser.error(f"文件不存在：{path}")
    duration = media_duration(path)
    result = transcribe_media(
        path,
        duration,
        source={"type": "local_file", "filename": path.name},
        progress=lambda done, total: print(f"已转写 {done}/{total} 段", flush=True),
    )
    output = save_result(result, DATA_DIR)
    print(f"完成：{output}")
