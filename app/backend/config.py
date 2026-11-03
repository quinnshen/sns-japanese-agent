"""Read local development settings without exporting secrets into process state."""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
from pathlib import Path

from dotenv import dotenv_values

LOCAL_ENV_FILE = Path(__file__).with_name(".env.local")


def read_local_config(path: Path | None = None) -> dict[str, str]:
    path = path if path is not None else LOCAL_ENV_FILE
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    except OSError:
        raise RuntimeError("无法安全读取本机 .env.local 配置。") from None
    with os.fdopen(descriptor, encoding="utf-8") as source:
        text = source.read()
    # Accept the original one-line key file until it is rewritten by the setup tool.
    legacy = text.strip()
    if legacy.startswith("sk-") and "=" not in legacy and not any(c.isspace() for c in legacy):
        return {"OPENAI_API_KEY": legacy}
    return {key: value for key, value in dotenv_values(stream=io.StringIO(text), interpolate=False).items()
            if value is not None}


def setting(name: str, default: str | None = None, *, path: Path | None = None) -> str | None:
    """Explicit process environment takes precedence, including an explicit empty value."""
    if name in os.environ:
        return os.environ[name]
    return read_local_config(path).get(name, default)


def save_local_config(values: dict[str, str], path: Path | None = None) -> None:
    """Atomically replace a local config with an owner-only file; never echo values."""
    path = path if path is not None else LOCAL_ENV_FILE
    if path.is_symlink():
        raise RuntimeError("拒绝覆盖符号链接形式的 .env.local。")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in values):
        raise ValueError("配置变量名无效。")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".env.local.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write("# Private local settings. Never commit this file.\n")
            for key, value in sorted(values.items()):
                output.write(f"{key}={json.dumps(value, ensure_ascii=False)}\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def update_local_config(updates: dict[str, str], path: Path | None = None) -> None:
    values = read_local_config(path)
    values.update(updates)
    save_local_config(values, path)
