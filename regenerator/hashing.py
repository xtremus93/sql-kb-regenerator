"""SHA-256 hash utilities for idempotency checking (Section 3.4)."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_of_str(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def sha256_of_file(path: Path) -> str | None:
    """Return SHA-256 hex digest of file, or None if the file does not exist."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_unchanged(new_content: str, path: Path) -> bool:
    """True if the file exists and its content matches new_content byte-for-byte."""
    existing = sha256_of_file(path)
    if existing is None:
        return False
    return existing == sha256_of_str(new_content)
