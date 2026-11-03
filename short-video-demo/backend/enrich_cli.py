"""Add translation and learning notes to a saved transcript without retranscription."""

import argparse
import json
from pathlib import Path

from pipeline import save_result
from enrich import enrich_result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add Chinese translation and learning notes to a saved transcript")
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    path = args.result.expanduser().resolve()
    result = json.loads(path.read_text(encoding="utf-8"))

    def save_progress(done, total, current):
        save_result(current, path.parent)
        print(f"已解析 {done}/{total} 批", flush=True)

    enriched = enrich_result(result, progress=save_progress)
    output = save_result(enriched, path.parent)
    print(f"完成：{output}")
