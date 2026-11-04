"""Offline preparation by default; --run-google explicitly starts paid recognition."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from capture_transcription import process_capture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("--run-google", action="store_true")
    parser.add_argument("--max-new-requests", type=int, default=50)
    parser.add_argument("--retry-window", type=int)
    parser.add_argument("--accept-duplicate-cost", action="store_true")
    args = parser.parse_args()
    try:
        result = process_capture(args.capture_dir, run_google=args.run_google, retry_window=args.retry_window,
                                 accept_duplicate_cost=args.accept_duplicate_cost, max_new_requests=args.max_new_requests)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["status"] == "needs_attention" else 0
    except Exception as error:
        # SDK exceptions and local paths must not be dumped into a terminal or public issue.
        code = getattr(error, "code", None) or getattr(error, "error_code", None) or "local_transcription_preparation_failed"
        if not isinstance(code, str) or not code.replace("_", "").isalnum():
            code = "local_transcription_preparation_failed"
        print(json.dumps({"status": "error", "error_code": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
