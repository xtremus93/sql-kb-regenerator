"""Structured logger: emits [INFO]/[WARN]/[ERROR] prefixed lines to stdout.
Tracks write vs. skip counts for the final summary."""

from __future__ import annotations

import sys
from collections import defaultdict
from typing import DefaultDict


class Logger:
    def __init__(self) -> None:
        self._counts: DefaultDict[str, dict[str, int]] = defaultdict(
            lambda: {"written": 0, "skipped": 0, "archived": 0}
        )
        self._has_warn: bool = False
        self._has_error: bool = False

    # ── Logging primitives ────────────────────────────────────────────────────

    def info(self, msg: str) -> None:
        print(f"[INFO] {msg}", flush=True)

    def warn(self, msg: str) -> None:
        self._has_warn = True
        print(f"[WARN] {msg}", flush=True)

    def error(self, msg: str) -> None:
        self._has_error = True
        print(f"[ERROR] {msg}", file=sys.stderr, flush=True)

    # ── Counter helpers ───────────────────────────────────────────────────────

    def _key(self, server: str, database: str) -> str:
        return f"{server}/{database}"

    def increment_written(self, server: str, database: str) -> None:
        self._counts[self._key(server, database)]["written"] += 1

    def increment_skipped(self, server: str, database: str) -> None:
        self._counts[self._key(server, database)]["skipped"] += 1

    def increment_archived(self, server: str, database: str) -> None:
        self._counts[self._key(server, database)]["archived"] += 1

    def ensure_target(self, server: str, database: str) -> None:
        """Ensure the counter entry exists even if nothing was written."""
        _ = self._counts[self._key(server, database)]

    # ── Summary ───────────────────────────────────────────────────────────────

    def summary(self) -> None:
        for target, counts in self._counts.items():
            print(
                f"[SUMMARY] {target}: "
                f"{counts['written']} files written, "
                f"{counts['skipped']} skipped (hash match), "
                f"{counts['archived']} archived",
                flush=True,
            )

    # ── Exit-code helper ──────────────────────────────────────────────────────

    @property
    def has_issues(self) -> bool:
        """True if any [WARN] or [ERROR] was emitted."""
        return self._has_warn or self._has_error
