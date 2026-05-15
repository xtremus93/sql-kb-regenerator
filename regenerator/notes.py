"""Human Notes preservation: extraction, storage, injection.

Handles:
- Untagged delimiters  : <!-- HUMAN_NOTES_START --> / <!-- HUMAN_NOTES_END -->
- Slug-tagged delimiters: <!-- HUMAN_NOTES_START:{slug} --> / <!-- HUMAN_NOTES_END:{slug} -->
- Malformed delimiter detection (Section 3.7)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class NotesResult:
    content: str          # preserved verbatim content between delimiters
    malformed: bool       # True when delimiters were detected but are malformed


# ── Regex patterns ────────────────────────────────────────────────────────────

_START_UNTAGGED = re.compile(r"^<!-- HUMAN_NOTES_START -->$", re.MULTILINE)
_END_UNTAGGED   = re.compile(r"^<!-- HUMAN_NOTES_END -->$",   re.MULTILINE)

_START_TAGGED   = re.compile(r"^<!-- HUMAN_NOTES_START:(.+?) -->$", re.MULTILINE)
_END_TAGGED     = re.compile(r"^<!-- HUMAN_NOTES_END:(.+?) -->$",   re.MULTILINE)


# ── Public API ────────────────────────────────────────────────────────────────


def extract_untagged(file_content: str) -> NotesResult:
    """Extract content from the untagged <!-- HUMAN_NOTES_START/END --> block.

    Returns NotesResult with malformed=True if the delimiters are detected
    but in an invalid configuration (START without END, or reversed order).
    """
    start_match = _START_UNTAGGED.search(file_content)
    end_match   = _END_UNTAGGED.search(file_content)

    if start_match is None and end_match is None:
        # No delimiters at all — return empty, not malformed
        return NotesResult(content="", malformed=False)

    if start_match is None or end_match is None:
        # One delimiter present but not the other
        return NotesResult(content="", malformed=True)

    start_pos = start_match.end()
    end_pos   = end_match.start()

    if start_pos > end_pos:
        # Delimiters in wrong order
        return NotesResult(content="", malformed=True)

    content = file_content[start_pos:end_pos]
    # Strip leading/trailing newline only (preserve inner whitespace verbatim)
    if content.startswith("\n"):
        content = content[1:]
    if content.endswith("\n"):
        content = content[:-1]

    return NotesResult(content=content, malformed=False)


def extract_tagged(file_content: str, slug: str) -> NotesResult:
    """Extract content from slug-tagged HUMAN_NOTES delimiters in tables.md.

    Looks for:
      <!-- HUMAN_NOTES_START:{slug} -->
      ...
      <!-- HUMAN_NOTES_END:{slug} -->
    """
    start_pattern = re.compile(
        r"^<!-- HUMAN_NOTES_START:" + re.escape(slug) + r" -->$", re.MULTILINE
    )
    end_pattern = re.compile(
        r"^<!-- HUMAN_NOTES_END:" + re.escape(slug) + r" -->$", re.MULTILINE
    )

    start_match = start_pattern.search(file_content)
    end_match   = end_pattern.search(file_content)

    if start_match is None and end_match is None:
        return NotesResult(content="", malformed=False)

    if start_match is None or end_match is None:
        return NotesResult(content="", malformed=True)

    start_pos = start_match.end()
    end_pos   = end_match.start()

    if start_pos > end_pos:
        return NotesResult(content="", malformed=True)

    content = file_content[start_pos:end_pos]
    if content.startswith("\n"):
        content = content[1:]
    if content.endswith("\n"):
        content = content[:-1]

    return NotesResult(content=content, malformed=False)


def inject_untagged(notes_content: str) -> str:
    """Render the untagged HUMAN_NOTES block with content injected."""
    return (
        "<!-- HUMAN_NOTES_START -->\n"
        + (notes_content + "\n" if notes_content else "")
        + "<!-- HUMAN_NOTES_END -->"
    )


def inject_tagged(notes_content: str, slug: str) -> str:
    """Render the slug-tagged HUMAN_NOTES block for tables.md per-table sections."""
    return (
        f"<!-- HUMAN_NOTES_START:{slug} -->\n"
        + (notes_content + "\n" if notes_content else "")
        + f"<!-- HUMAN_NOTES_END:{slug} -->"
    )


def has_content(notes_content: str) -> bool:
    """True if notes_content contains at least one non-whitespace character."""
    return bool(notes_content.strip())


def extract_body_after_yaml(file_content: str) -> str:
    """Extract everything after the closing '---' of the YAML front matter."""
    # Find the second occurrence of '---' on its own line
    lines = file_content.split("\n")
    dash_count = 0
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip() == "---":
            dash_count += 1
            if dash_count == 2:
                body_start = i + 1
                break
    return "\n".join(lines[body_start:])
