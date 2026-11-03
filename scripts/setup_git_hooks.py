#!/usr/bin/env python3
"""Enable this repository's commit/push guards without replacing existing hooks."""

from pathlib import Path
import subprocess


def run(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *arguments], text=True, capture_output=True, check=False)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    check = run(root, "rev-parse", "--show-toplevel")
    if check.returncode or Path(check.stdout.strip()).resolve() != root:
        print("Cannot install guards: this directory must be a Git repository root.")
        return 1
    # Inspect the effective value as well, so a global hook policy is not silently
    # shadowed by this repository's local configuration.
    configuration = run(root, "config", "--get", "core.hooksPath")
    if configuration.returncode not in (0, 1):
        print("Cannot inspect the local Git hook configuration.")
        return 1
    existing = configuration.stdout.strip()
    if existing and existing != ".githooks":
        print("Existing Git hook configuration found; installation stopped without changing it.")
        return 1
    if not existing:
        hook_path = run(root, "rev-parse", "--git-path", "hooks")
        if hook_path.returncode:
            print("Cannot inspect existing Git hooks.")
            return 1
        hooks = Path(hook_path.stdout.strip())
        if not hooks.is_absolute():
            hooks = root / hooks
        if any((hooks / name).exists() for name in ("pre-commit", "pre-push")):
            print("Existing commit or push hooks found; installation stopped without changing them.")
            return 1
    for name in ("pre-commit", "pre-push"):
        hook = root / ".githooks" / name
        if not hook.is_file():
            print("Required repository hooks are missing; installation stopped.")
            return 1
        hook.chmod(hook.stat().st_mode | 0o111)
    if run(root, "config", "--local", "core.hooksPath", ".githooks").returncode:
        print("Cannot update this repository's local Git hook configuration.")
        return 1
    print("Commit and push guards enabled for this repository. No global Git setting was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
