#!/usr/bin/env python3
"""Keep public version literals aligned with the backend source of truth."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
VERSION_SOURCE = ROOT / "source/backend/zhixing/__init__.py"
VERSION_ASSIGNMENT = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)
PUBLIC_VERSION = re.compile(r"\b\d+\.\d{6}\.\d{2}\b")
SKIP_DIRS = {".git", ".vite", "__pycache__", "dist", "node_modules"}
REQUIRED_SURFACES = (ROOT / "README.md", ROOT / ".env.example", ROOT / "deploy/compose.yaml")


def text_files() -> list[Path]:
    result: list[Path] = []
    for current, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
        base = Path(current)
        result.extend(base / name for name in sorted(names))
    return result


def main() -> int:
    source = VERSION_SOURCE.read_text(encoding="utf-8")
    match = VERSION_ASSIGNMENT.search(source)
    if match is None:
        print("无法从后端版本源读取 __version__。", file=sys.stderr)
        return 1
    canonical = match.group(1)
    failures: list[str] = []

    for path in text_files():
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for found in PUBLIC_VERSION.findall(line):
                if found != canonical:
                    relative = path.relative_to(ROOT).as_posix()
                    failures.append(f"{relative}:{line_number}: {found}")

    for path in REQUIRED_SURFACES:
        if canonical not in path.read_text(encoding="utf-8"):
            failures.append(f"{path.relative_to(ROOT).as_posix()}: 缺少当前构建号")

    if failures:
        print(f"版本一致性检查失败，后端构建号为 {canonical}：", file=sys.stderr)
        for failure in sorted(set(failures)):
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"版本一致性检查通过：{canonical}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
