#!/usr/bin/env python3
"""Inspect Git objects without reading ignored local configuration or printing values.

The default checks every file in the index, not just changed files. --all checks
the HEAD snapshot and historical commit metadata. --pre-push reads Git's hook protocol
and checks the complete history reachable from each non-deleted push tip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import PurePosixPath


class CheckError(Exception):
    pass


TOKEN_RULES = (
    ("api-token", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{30,}")),
    ("github-token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("aws-access-key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("google-oauth-token", re.compile(r"(?:1//[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]{15,})")),
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("google-oauth-client", re.compile(r"[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com")),
    ("google-credential-json", re.compile(r'''["']type["']\s*:\s*["'](?:authorized_user|service_account)["']''')),
)
PERSONAL_PATH = re.compile(
    r"(?:" + "/" + r"Users/[^\s/\"'<>]+|" + "/" + r"home/[^\s/\"'<>]+|"
    + "/" + r"(?:private/)?var/folders/[^\s\"'<>]+|[A-Za-z]:\\Users\\[^\s\\\"'<>]+)"
)
EMAIL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-\[\]]*@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
SENSITIVE_NAMES = (
    "OPENAI_API_KEY", "GOOGLE_API_KEY", "API_KEY", "CLIENT_SECRET", "REFRESH_TOKEN",
    "ACCESS_TOKEN", "PRIVATE_KEY", "PASSWORD", "PASSWD", "AUTHORIZATION",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_QUOTA_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS",
)
SENSITIVE_ASSIGNMENT = re.compile(
    r'''(?i)["']?(?P<name>''' + "|".join(SENSITIVE_NAMES)
    + r''')["']?\s*(?:=|:)\s*(?P<quote>["'])(?P<value>[^\r\n]*?)(?P=quote)'''
)
DOTENV_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(.*?)[ \t]*$", re.MULTILINE)
PLACEHOLDER = re.compile(r"(?:YOUR_[A-Z0-9_]+|REPLACE_ME|CHANGE_ME|\$\{[A-Z0-9_]+\}|\$[A-Z0-9_]+|<your[-_ A-Za-z0-9]*>)", re.IGNORECASE)
OFFLINE_FIXTURES = frozenset({
    "operator-key-for-offline-test", "test-key", "test-openai-key", "fake-key",
    "test-password", "test-project", "operator-project", "adc-project",
})
BLOCKED_DIRECTORIES = frozenset({
    ".config", ".credentials", ".aws", "gcloud", "google-cloud-sdk", ".tools",
    ".venv", "venv", "node_modules", "data", "uploads", "recordings", "captures",
})
BLOCKED_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".keystore",
    ".webm", ".wav", ".mp3", ".mp4", ".mov", ".m4v", ".mkv", ".aac",
    ".ogg", ".opus", ".flac", ".aiff", ".sqlite", ".sqlite3", ".db", ".log",
})
MAX_BLOB_BYTES = 5 * 1024 * 1024


def git(*arguments: str, input_bytes: bytes | None = None, optional: bool = False) -> bytes:
    result = subprocess.run(
        ["git", *arguments], input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode and not optional:
        # Git diagnostics can contain personal paths or commit values. Do not relay them.
        raise CheckError("git-operation-failed")
    return result.stdout if result.returncode == 0 else b""


def public_email(value: str) -> bool:
    value = value.strip().lower()
    return value == "noreply@github.com" or value.endswith("@users.noreply.github.com")


def example_email(value: str) -> bool:
    domain = value.rsplit("@", 1)[-1].lower()
    return domain in {"example.com", "example.org", "example.net"} or domain.endswith((".example", ".invalid"))


def placeholder(value: str) -> bool:
    return not value or bool(PLACEHOLDER.fullmatch(value)) or value in OFFLINE_FIXTURES


def path_rules(path: str) -> set[str]:
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    rules = set()
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        rules.add("local-environment-file")
    if any(part.lower() in BLOCKED_DIRECTORIES for part in parts[:-1]):
        rules.add("local-data-or-dependencies")
    if PurePosixPath(name).suffix in BLOCKED_SUFFIXES:
        rules.add("credential-or-runtime-file")
    if (
        name in {"application_default_credentials.json", "id_rsa", "id_ed25519"}
        or re.fullmatch(r"(?:credentials|client_secret|token).*\.json", name)
        or re.fullmatch(r".*service[-_]account.*\.json", name)
    ):
        rules.add("credential-file")
    return rules


def text_rules(text: str) -> set[str]:
    """Rules shared by file contents and commit messages; never return values."""
    rules = {name for name, expression in TOKEN_RULES if expression.search(text)}
    if PERSONAL_PATH.search(text):
        rules.add("personal-absolute-path")
    if any(not public_email(match[0]) and not example_email(match[0]) for match in EMAIL.finditer(text)):
        rules.add("private-email")
    return rules


def content_rules(path: str, content: bytes) -> set[str]:
    text = content.decode("utf-8", errors="replace")
    rules = text_rules(text)
    for match in SENSITIVE_ASSIGNMENT.finditer(text):
        if not placeholder(match["value"]):
            rules.add("non-placeholder-sensitive-value")
    if PurePosixPath(path).name == ".env.example":
        for name, value in DOTENV_ASSIGNMENT.findall(text):
            if name in SENSITIVE_NAMES and not placeholder(value.strip("\"'")):
                rules.add("non-placeholder-template-value")
    return rules


def display_path(path: str) -> str:
    # A malicious filename can itself contain a credential. Do not echo it either.
    if PERSONAL_PATH.search(path) or any(expression.search(path) for _, expression in TOKEN_RULES) or EMAIL.search(path):
        path = "<redacted-path:" + hashlib.sha256(path.encode("utf-8", errors="surrogateescape")).hexdigest()[:12] + ">"
    return json.dumps(path, ensure_ascii=True)


class Scanner:
    def __init__(self) -> None:
        self.findings: set[tuple[str, str]] = set()
        self.scanned: set[tuple[str, str]] = set()
        self.content_cache: dict[str, bytes] = {}

    def add(self, path: str, rule: str) -> None:
        self.findings.add((path, rule))

    def file(self, path: str, object_id: str, mode: str) -> None:
        identity = (path, object_id)
        if identity in self.scanned:
            return
        self.scanned.add(identity)
        for rule in path_rules(path):
            self.add(path, rule)
        if mode == "160000":
            self.add(path, "unscanned-git-submodule")
            return
        if object_id not in self.content_cache:
            size = int(git("cat-file", "-s", object_id).strip())
            if size > MAX_BLOB_BYTES:
                self.add(path, "oversized-unreviewed-file")
                return
            self.content_cache[object_id] = git("cat-file", "blob", object_id)
        for rule in content_rules(path, self.content_cache[object_id]):
            self.add(path, rule)

    def index(self) -> None:
        for record in git("ls-files", "--stage", "-z").split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if stage != "0":
                self.add(path, "unresolved-index-entry")
            else:
                self.file(path, object_id, mode)

    def tree(self, commit: str) -> None:
        for record in git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, _, object_id = metadata.decode("ascii").split()
            self.file(raw_path.decode("utf-8", errors="surrogateescape"), object_id, mode)

    def local_identity(self) -> None:
        for role in ("AUTHOR", "COMMITTER"):
            identity = git("var", "GIT_" + role + "_IDENT", optional=True).decode("utf-8", errors="replace")
            match = re.search(r"<([^<>]+)>\s+[0-9]+\s+[+-][0-9]+\s*$", identity)
            if not match or not public_email(match[1]):
                self.add("<git-identity>", role.lower() + "-email-must-use-github-noreply")

    def metadata(self, commits: list[str]) -> None:
        for commit in commits:
            values = git("show", "-s", "--format=%ae%x00%ce%x00%B", commit).decode("utf-8", errors="replace").split("\0", 2)
            if len(values) != 3:
                raise CheckError("invalid-commit-metadata")
            label = "<commit:" + commit[:12] + ">"
            for role, email in zip(("author", "committer"), values[:2]):
                if not public_email(email):
                    self.add(label, role + "-email-must-use-github-noreply")
            for rule in text_rules(values[2]):
                self.add(label, "commit-message-" + rule)

    def report(self) -> int:
        for path, rule in sorted(self.findings):
            print(display_path(path) + ": " + rule)
        return 1 if self.findings else 0


def revisions(tips: list[str]) -> list[str]:
    if not tips:
        return []
    return git("rev-list", "--stdin", input_bytes=("\n".join(tips) + "\n").encode("ascii")).decode("ascii").split()


def push_tips(stream) -> list[str]:
    tips = []
    for line in stream:
        fields = line.split()
        if len(fields) != 4 or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[1]):
            raise CheckError("invalid-pre-push-input")
        if set(fields[1]) != {"0"}:
            tips.append(fields[1])
    return tips


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="check HEAD files and historical commit emails/messages")
    group.add_argument("--pre-push", action="store_true", help="check all files and metadata in histories supplied by Git's pre-push protocol")
    arguments = parser.parse_args(argv)
    try:
        git("rev-parse", "--show-toplevel")
        scanner = Scanner()
        if arguments.pre_push:
            commits = revisions(push_tips(sys.stdin))
            scanner.metadata(commits)
            for commit in commits:
                scanner.tree(commit)
        elif arguments.all:
            scanner.tree("HEAD")
            scanner.metadata(revisions(["HEAD"]))
        else:
            scanner.local_identity()
            scanner.index()
        return scanner.report()
    except (CheckError, OSError, ValueError):
        print('"<scanner>": unable-to-complete-check')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
