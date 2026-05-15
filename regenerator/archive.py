"""Step 6: archive logic — deprecate and move REMOVED objects.

For each REMOVED object:
1. Read existing MD file from disk.
2. Parse YAML front matter; set deprecated: true and deprecated_at.
3. Rewrite with updated YAML.
4. Move to _archive/{object_type}/{slug}.md.
5. For REMOVED tables in tables.md: extract from tables.md and write to
   _archive/table/{slug}.md as a standalone file.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ

from regenerator.logger import Logger
from regenerator.hashing import content_unchanged

_yaml = YAML()
_yaml.default_flow_style = False
_yaml.width = 4096
_yaml.best_width = 4096
_yaml.indent(mapping=2, sequence=2, offset=2)
_yaml.preserve_quotes = True


def _dump(data: Any) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _split_front_matter(content: str) -> tuple[str, str]:
    """Split file content into (yaml_block, body).

    yaml_block is the raw YAML text between the two '---' lines.
    body is everything after the closing '---'.
    """
    lines = content.split("\n")
    dash_indices = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(dash_indices) < 2:
        return ("", content)
    start = dash_indices[0] + 1
    end   = dash_indices[1]
    yaml_block = "\n".join(lines[start:end])
    body       = "\n".join(lines[end + 1:])
    return yaml_block, body


def _set_deprecated(yaml_block: str, deprecated_at: str) -> str:
    """Parse YAML front matter, set deprecated=true and deprecated_at, re-dump."""
    data = _yaml.load(yaml_block)
    if data is None:
        data = {}
    data["deprecated"]    = True
    data["deprecated_at"] = DQ(deprecated_at)
    return _dump(data)


def archive_object(
    source_path: Path,
    object_type: str,
    slug: str,
    db_dir: Path,
    deprecated_at: str,
    logger: Logger,
    server: str,
    database: str,
) -> bool:
    """Archive a single object file.  Returns True if the archive write succeeded."""
    if not source_path.exists():
        logger.warn(
            f"Archive target not found on disk: {source_path}. "
            f"Skipping archival of {slug}."
        )
        return False

    try:
        original_content = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error(f"Cannot read {source_path} for archival: {exc}")
        return False

    yaml_block, body = _split_front_matter(original_content)
    try:
        updated_yaml = _set_deprecated(yaml_block, deprecated_at)
    except Exception as exc:
        logger.error(f"Cannot parse YAML front matter in {source_path}: {exc}")
        return False

    updated_content = "---\n" + updated_yaml + "---\n" + body

    archive_dir = db_dir / "_archive" / object_type
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest_path = archive_dir / f"{slug}.md"

    if content_unchanged(updated_content, dest_path):
        logger.increment_skipped(server, database)
    else:
        _safe_write(dest_path, updated_content, source_path, logger, server, database)

    # Remove original from live directory
    try:
        source_path.unlink()
    except OSError as exc:
        logger.error(f"Cannot remove live file after archival {source_path}: {exc}")
        return False

    logger.increment_archived(server, database)
    return True


def _safe_write(
    dest: Path,
    content: str,
    backup_source: Path | None,
    logger: Logger,
    server: str,
    database: str,
) -> bool:
    """Write content to dest, restoring backup on failure (Section 3.7)."""
    backup: str | None = None
    if dest.exists():
        try:
            backup = dest.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        logger.increment_written(server, database)
        return True
    except OSError as exc:
        logger.error(f"Partial write failure on {dest}: {exc}")
        # Attempt to restore
        if backup is not None:
            try:
                dest.write_text(backup, encoding="utf-8")
            except OSError:
                pass
        return False
