"""Prepare learning batches offline; --run-openai explicitly permits paid work."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from capture_learning import process_capture_learning


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("--run-openai", action="store_true")
    parser.add_argument("--revalidate-saved", action="store_true")
    parser.add_argument("--max-new-requests", type=int, default=50)
    parser.add_argument("--max-total-requests", type=int)
    parser.add_argument("--raise-request-budget", action="store_true")
    parser.add_argument("--retry-batch", type=int)
    parser.add_argument("--accept-duplicate-cost", action="store_true")
    arguments = parser.parse_args(argv)
    if arguments.raise_request_budget and arguments.max_total_requests is None:
        parser.error("--raise-request-budget requires an explicit --max-total-requests value")
    if arguments.revalidate_saved and (arguments.run_openai or arguments.retry_batch is not None
                                      or arguments.accept_duplicate_cost or arguments.raise_request_budget
                                      or arguments.max_total_requests is not None):
        parser.error("--revalidate-saved is offline and cannot be combined with paid work or retry flags")
    try:
        result = process_capture_learning(arguments.capture_dir, run_openai=arguments.run_openai,
                                          max_new_requests=arguments.max_new_requests, retry_batch=arguments.retry_batch,
                                          accept_duplicate_cost=arguments.accept_duplicate_cost,
                                          max_total_requests=arguments.max_total_requests if arguments.max_total_requests is not None else 50,
                                          raise_request_budget=arguments.raise_request_budget,
                                          revalidate_saved=arguments.revalidate_saved)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result["status"] == "needs_attention" else 0
    except Exception as error:
        code = getattr(error, "code", None) or getattr(error, "error_code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "local_learning_failed"
        print(json.dumps({"status": "error", "error_code": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
