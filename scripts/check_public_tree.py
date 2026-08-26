#!/usr/bin/env python3
"""Reject common private-data artifacts before publishing the repository."""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
SKIP_DIRS = {".git", ".idea", ".vite", ".vscode", "__pycache__", "dist", "node_modules"}
FORBIDDEN_DIRS = {
    "archives",
    "backups",
    "broker_profile",
    "captcha-samples",
    "chrome_profile",
    "chromium_profile",
    "logs",
    "private",
    "runtime",
    "user-data-dir",
}
FORBIDDEN_NAMES = {
    ".env",
    "account.json",
    "broker.json",
    "broker.account",
    "broker.loginproof",
    "broker.password",
    "captcha.json",
    "captcha.chain",
    "captcha.secret",
    "catalog.json",
    "history_seed.json",
    "model.json",
    "model.secret",
    "runtime.json",
    "schedule.json",
    "unattended.json",
    "cookies",
    "cookies-journal",
    "history",
    "history-journal",
    "local state",
    "login data",
    "login data-journal",
    "web data",
    "web data-journal",
}
FORBIDDEN_SUFFIXES = {".har", ".key", ".p12", ".pem", ".pfx", ".pyc", ".sqlite"}
FORBIDDEN_ARCHIVE_ENDINGS = (".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".zip")
ALLOWED_FIXTURE_FILES = {
    "source/frontend/src/fixtures/settings/broker.json",
    "source/frontend/src/fixtures/settings/captcha.json",
    "source/frontend/src/fixtures/settings/model.json",
    "source/frontend/src/fixtures/settings/schedule.json",
}

CONTENT_RULES = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("windows-user-path", re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+")),
    ("sync-drive-path", re.compile(r"(?i)BaiduSyncdisk")),
    ("email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("cn-mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
)
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def iter_files(failures: list[str]) -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(ROOT):
        base = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            lower_name = name.lower()
            if lower_name in FORBIDDEN_DIRS:
                add(failures, "private-runtime-directory", base / name)
            elif lower_name not in SKIP_DIRS:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        files.extend(base / name for name in sorted(names))
    return files


def add(failures: list[str], rule: str, path: Path, line: int | None = None) -> None:
    relative = path.relative_to(ROOT).as_posix()
    location = f"{relative}:{line}" if line is not None else relative
    failures.append(f"{rule}: {location}")


def main() -> int:
    failures: list[str] = []

    for path in iter_files(failures):
        if path.is_symlink():
            try:
                path.resolve().relative_to(ROOT)
            except ValueError:
                add(failures, "external-symlink", path)
            continue

        lower_name = path.name.lower()
        relative = path.relative_to(ROOT).as_posix()
        if lower_name in FORBIDDEN_NAMES and relative not in ALLOWED_FIXTURE_FILES:
            add(failures, "private-runtime-file", path)
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            add(failures, "private-or-generated-file", path)
        if lower_name.endswith(FORBIDDEN_ARCHIVE_ENDINGS):
            add(failures, "compressed-backup-file", path)
        if path.resolve() == SELF:
            continue

        try:
            raw = path.read_bytes()
        except OSError:
            add(failures, "unreadable-file", path)
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule, pattern in CONTENT_RULES:
                for match in pattern.finditer(line):
                    value = match.group(0)
                    if rule == "api-key" and "DEMOFAKE" in value.upper():
                        continue
                    if rule == "email" and value.lower().endswith(".invalid"):
                        continue
                    add(failures, rule, path, line_number)

            for match in IPV4.finditer(line):
                try:
                    address = ipaddress.ip_address(match.group(0))
                except ValueError:
                    continue
                if address.is_global:
                    add(failures, "public-ip-address", path, line_number)

    if failures:
        print("公开内容扫描失败：", file=sys.stderr)
        for failure in sorted(set(failures)):
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("公开内容扫描通过：未发现常见凭据、个人路径、公网 IP 或运行数据文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
