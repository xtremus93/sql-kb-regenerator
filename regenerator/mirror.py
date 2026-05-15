"""Step 7: Bidirectional dependency mirroring pass.

After all primary files are written, this pass:
- Scans every written object file's outbound dependencies.
- For each dependency D, opens D's file and ensures D's inbound list includes
  the referencing object.
- Updates read_by / written_by on table entries in tables.md.
- Updates called_by_* on function files.
- Populates invoked_by_jobs on procedure files by scanning job step slugs.

Only touches a file if changes are needed (SHA-256 idempotency check applies).
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


# ── Internal helpers ──────────────────────────────────────────────────────────


def _dump(data: Any) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _load_file(path: Path) -> tuple[dict, str] | None:
    """Load a file, returning (yaml_data, body_text) or None on failure."""
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    lines = content.split("\n")
    dash_indices = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(dash_indices) < 2:
        return None

    yaml_block = "\n".join(lines[dash_indices[0] + 1 : dash_indices[1]])
    body       = "\n".join(lines[dash_indices[1] + 1 :])
    try:
        data = _yaml.load(yaml_block)
    except Exception:
        return None
    return data or {}, body


def _rewrite(path: Path, data: dict, body: str, logger: Logger, server: str, database: str) -> None:
    new_content = "---\n" + _dump(data) + "---\n" + body
    if content_unchanged(new_content, path):
        logger.increment_skipped(server, database)
        return
    backup: str | None = None
    if path.exists():
        try:
            backup = path.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        path.write_text(new_content, encoding="utf-8")
        logger.increment_written(server, database)
    except OSError as exc:
        logger.error(f"Mirror write failure on {path}: {exc}")
        if backup is not None:
            try:
                path.write_text(backup, encoding="utf-8")
            except OSError:
                pass


def _slug_in_list(slug: str, dep_list: list) -> bool:
    return any(d.get("slug") == slug for d in dep_list)


def _make_dep(slug: str, fqn: str, is_cross: bool, via: str | None, method: str) -> dict:
    return {
        "slug": DQ(slug) if slug else None,
        "fqn":  DQ(fqn),
        "is_cross_server": is_cross,
        "accessed_via_linked_server": via,
        "access_method": method,
    }


def _path_for_slug(slug: str, db_dir: Path, object_type: str) -> Path | None:
    """Return the expected file path for a slug given its object_type."""
    if object_type == "table":
        return db_dir / "tables.md"
    elif object_type == "view":
        return db_dir / "views" / f"{slug}.md"
    elif object_type == "procedure":
        return db_dir / "procedures" / f"{slug}.md"
    elif object_type == "function":
        return db_dir / "functions" / f"{slug}.md"
    elif object_type == "trigger":
        return db_dir / "triggers" / f"{slug}.md"
    elif object_type == "job":
        return db_dir / "jobs" / f"{slug}.md"
    return None


# ── Public API ────────────────────────────────────────────────────────────────


def run_mirror_pass(
    db_dir: Path,
    slug_type_map: dict[str, str],  # slug -> object_type for all live objects
    logger: Logger,
    server: str,
    database: str,
) -> None:
    """Execute the bidirectional dependency mirroring pass for one database."""

    # Build a working index: slug -> (path, yaml_data, body)
    file_cache: dict[str, tuple[Path, dict, str]] = {}

    def _ensure_cached(slug: str, obj_type: str) -> bool:
        if slug in file_cache:
            return True
        path = _path_for_slug(slug, db_dir, obj_type)
        if path is None or not path.exists():
            return False
        result = _load_file(path)
        if result is None:
            return False
        data, body = result
        file_cache[slug] = (path, data, body)
        return True

    # ── Collect all outbound edges ────────────────────────────────────────────

    edges: list[tuple[str, str, dict]] = []  # (referencing_slug, referencing_type, dep_obj)

    for slug, obj_type in slug_type_map.items():
        if obj_type == "table":
            # tables.md holds multiple objects; process separately below
            continue
        if not _ensure_cached(slug, obj_type):
            continue
        _, data, _ = file_cache[slug]
        out_deps = (data.get("dependencies") or {}).get("outbound") or []
        for dep in out_deps:
            edges.append((slug, obj_type, dep))

    # tables.md: gather per-table outbound deps
    tables_path = db_dir / "tables.md"
    tables_data_body: tuple[dict, str] | None = None
    if tables_path.exists():
        result = _load_file(tables_path)
        if result:
            tables_data_body = result
            tables_data, tables_body = result
            for tbl in (tables_data.get("tables") or []):
                tbl_slug = tbl.get("id", "")
                out_deps = (tbl.get("dependencies") or {}).get("outbound") or []
                for dep in out_deps:
                    edges.append((tbl_slug, "table", dep))

    # ── Apply reverse edges ───────────────────────────────────────────────────

    dirty_slugs: set[str] = set()

    for ref_slug, ref_type, dep_obj in edges:
        dep_slug = dep_obj.get("slug")
        if not dep_slug:
            continue
        dep_type = slug_type_map.get(dep_slug)
        if dep_type is None:
            continue  # cross-server or unknown; skip

        if dep_type == "table":
            # Update tables.md inbound for that table entry
            if tables_data_body is None:
                continue
            tdata, tbody = tables_data_body
            for tbl in (tdata.get("tables") or []):
                if tbl.get("id") == dep_slug:
                    inbound = (tbl.get("dependencies") or {}).get("inbound") or []
                    if not _slug_in_list(ref_slug, inbound):
                        inbound.append(_make_dep(
                            ref_slug,
                            _fqn_for_slug(ref_slug, file_cache),
                            dep_obj.get("is_cross_server", False),
                            dep_obj.get("accessed_via_linked_server"),
                            dep_obj.get("access_method", "direct"),
                        ))
                        if tbl.get("dependencies") is None:
                            tbl["dependencies"] = {}
                        tbl["dependencies"]["inbound"] = inbound
                        dirty_slugs.add("__tables__")
                    # Also update read_by
                    read_by = tbl.get("read_by") or []
                    if not _slug_in_list(ref_slug, read_by):
                        read_by.append(_make_dep(
                            ref_slug,
                            _fqn_for_slug(ref_slug, file_cache),
                            dep_obj.get("is_cross_server", False),
                            dep_obj.get("accessed_via_linked_server"),
                            dep_obj.get("access_method", "direct"),
                        ))
                        tbl["read_by"] = read_by
                        dirty_slugs.add("__tables__")
        else:
            if not _ensure_cached(dep_slug, dep_type):
                continue
            dep_path, dep_data, dep_body = file_cache[dep_slug]
            deps = dep_data.get("dependencies") or {}
            inbound = deps.get("inbound") or []
            if not _slug_in_list(ref_slug, inbound):
                inbound.append(_make_dep(
                    ref_slug,
                    _fqn_for_slug(ref_slug, file_cache),
                    dep_obj.get("is_cross_server", False),
                    dep_obj.get("accessed_via_linked_server"),
                    dep_obj.get("access_method", "direct"),
                ))
                if dep_data.get("dependencies") is None:
                    dep_data["dependencies"] = {}
                dep_data["dependencies"]["inbound"] = inbound
                dirty_slugs.add(dep_slug)

            # Update called_by_* on functions
            if dep_type == "function" and ref_type == "procedure":
                lst = dep_data.get("called_by_procedures") or []
                if not _slug_in_list(ref_slug, lst):
                    lst.append(_make_dep(ref_slug, _fqn_for_slug(ref_slug, file_cache), False, None, "direct"))
                    dep_data["called_by_procedures"] = lst
                    dirty_slugs.add(dep_slug)
            elif dep_type == "function" and ref_type == "view":
                lst = dep_data.get("called_by_views") or []
                if not _slug_in_list(ref_slug, lst):
                    lst.append(_make_dep(ref_slug, _fqn_for_slug(ref_slug, file_cache), False, None, "direct"))
                    dep_data["called_by_views"] = lst
                    dirty_slugs.add(dep_slug)
            elif dep_type == "function" and ref_type == "trigger":
                lst = dep_data.get("called_by_triggers") or []
                if not _slug_in_list(ref_slug, lst):
                    lst.append(_make_dep(ref_slug, _fqn_for_slug(ref_slug, file_cache), False, None, "direct"))
                    dep_data["called_by_triggers"] = lst
                    dirty_slugs.add(dep_slug)

            file_cache[dep_slug] = (dep_path, dep_data, dep_body)

    # ── invoked_by_jobs: scan job steps ──────────────────────────────────────

    jobs_dir = db_dir / "jobs"
    if jobs_dir.exists():
        for job_file in jobs_dir.glob("*.md"):
            result = _load_file(job_file)
            if not result:
                continue
            jdata, jbody = result
            job_slug = jdata.get("id", "")
            job_fqn  = jdata.get("fqn", "")
            for step in (jdata.get("steps") or []):
                ref_proc = step.get("references_object_slug")
                if not ref_proc:
                    continue
                proc_type = slug_type_map.get(ref_proc)
                if proc_type != "procedure":
                    continue
                if not _ensure_cached(ref_proc, "procedure"):
                    continue
                ppath, pdata, pbody = file_cache[ref_proc]
                inv_list = pdata.get("invoked_by_jobs") or []
                if not any(e.get("job_slug") == job_slug for e in inv_list):
                    inv_list.append({
                        "job_slug":   DQ(job_slug),
                        "step_order": step.get("order", 0),
                    })
                    pdata["invoked_by_jobs"] = inv_list
                    dirty_slugs.add(ref_proc)
                file_cache[ref_proc] = (ppath, pdata, pbody)

    # ── Flush dirty files ─────────────────────────────────────────────────────

    for slug in dirty_slugs:
        if slug == "__tables__" and tables_data_body is not None:
            tdata, tbody = tables_data_body
            _rewrite(tables_path, tdata, tbody, logger, server, database)
        elif slug in file_cache:
            path, data, body = file_cache[slug]
            _rewrite(path, data, body, logger, server, database)

    logger.info(f"{server}/{database} — Step 7 complete: bidirectional dependency mirroring")


def _fqn_for_slug(slug: str, cache: dict) -> str:
    """Look up fqn from cache, return slug as fallback."""
    if slug in cache:
        data = cache[slug][1]
        if isinstance(data, dict):
            return data.get("fqn") or slug
    return slug
