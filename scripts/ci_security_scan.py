"""Fail CI when tracked files contain likely credentials or local runtime data."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath


FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),
    "OpenAI key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[str] = []
    for name in tracked_files():
        path = PurePosixPath(name)
        lowered = name.lower()
        if (
            (path.name.startswith(".env") and path.name != ".env.example")
            or path.name == "config.json"
            or "data" in path.parts
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
        ):
            findings.append(f"forbidden tracked path: {name}")
            continue
        data = Path(name).read_bytes()
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                findings.append(f"possible {label}: {name}")
    if findings:
        raise SystemExit("\n".join(findings))
    print("Tracked-path and credential-pattern scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
