"""8-step regenerator pipeline (Section 3.3).

Steps:
  1. Record wall-clock UTC start timestamp; establish connections.
  2. Build current_set from query group (a); load jobs from group (g).
  3. Load prior_set from existing _index.md YAML front matter.
  4. Classify objects: NEW / MODIFIED / UNCHANGED / REMOVED.
     In full mode: reclassify UNCHANGED as MODIFIED.
  5. For each NEW or MODIFIED object: extract, render, write.
  6. Archive REMOVED objects.
  7. Bidirectional dependency mirroring pass.
  8. Rebuild _index.md for each {server, database} pair.
"""

from __future__ import annotations

import io
import re
import datetime
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyodbc
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ

from regenerator import queries
from regenerator.logger import Logger
from regenerator.hashing import content_unchanged
from regenerator.notes import (
    extract_untagged,
    extract_tagged,
    has_content,
    extract_body_after_yaml,
)
from regenerator.renderer import (
    render_index,
    render_tables,
    render_view,
    render_procedure,
    render_function,
    render_trigger,
    render_job,
)
from regenerator.archive import archive_object
from regenerator.mirror import run_mirror_pass

_yaml = YAML()
_yaml.default_flow_style = False
_yaml.width = 4096
_yaml.best_width = 4096
_yaml.indent(mapping=2, sequence=2, offset=2)
_yaml.preserve_quotes = True

# ── Utility helpers ───────────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(data: Any) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _load_yaml_front_matter(content: str) -> dict | None:
    lines = content.split("\n")
    dash_indices = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(dash_indices) < 2:
        return None
    yaml_block = "\n".join(lines[dash_indices[0] + 1 : dash_indices[1]])
    try:
        return _yaml.load(yaml_block)
    except Exception:
        return None


def _safe_write(
    path: Path,
    content: str,
    logger: Logger,
    server: str,
    database: str,
    prior_content: str | None = None,
) -> bool:
    """Write content to path; restore prior_content on failure (Section 3.7)."""
    if content_unchanged(content, path):
        logger.increment_skipped(server, database)
        return True
    backup = prior_content
    if backup is None and path.exists():
        try:
            backup = path.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.increment_written(server, database)
        return True
    except OSError as exc:
        logger.error(f"Partial write failure on {path}: {exc}")
        if backup is not None:
            try:
                path.write_text(backup, encoding="utf-8")
            except OSError:
                pass
        return False


def _read_optional(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# ── Cross-server slug resolution ──────────────────────────────────────────────


def _build_linked_server_map(conn: pyodbc.Connection) -> dict[str, str]:
    """Return {alias_upper -> real_server_lower} from sys.servers."""
    rows = queries.get_linked_servers(conn)
    return {r["linked_server_alias"].upper(): r["real_server_address"].lower() for r in rows}


def _resolve_dep(
    row: dict,
    server_lower: str,
    database_lower: str,
    linked_map: dict[str, str],
    logger: Logger,
    referencing_slug: str,
) -> dict:
    """Convert a raw dependency row from queries.py into a dep object dict."""
    is_cross = bool(row.get("is_cross_server"))
    alias    = row.get("linked_server_alias") or row.get("ref_server_name")

    if is_cross:
        real_server = linked_map.get((alias or "").upper())
        if real_server is None:
            # Unresolvable linked server alias (Section 3.7)
            logger.warn(
                f"Linked server alias '{alias}' in {referencing_slug} is not "
                f"registered in sys.servers. Dependency recorded as unresolved."
            )
            return {
                "slug":                      None,
                "fqn":                       row.get("referenced_fqn") or row.get("ref_object_name", ""),
                "is_cross_server":           True,
                "accessed_via_linked_server": alias,
                "access_method":             row.get("access_method_hint") or "linked_server",
                "unresolved":                True,
            }
        ref_db     = (row.get("ref_database_name") or database_lower).lower()
        ref_schema = (row.get("ref_schema_name") or "dbo").lower()
        ref_name   = (row.get("ref_object_name") or "").lower()
        slug       = f"{real_server}.{ref_db}.{ref_schema}.{ref_name}"
        method     = row.get("access_method_hint") or "linked_server"
    else:
        slug   = row.get("referenced_slug") or ""
        method = "direct"
        real_server = None
        alias  = None

    fqn = row.get("referenced_fqn") or slug

    return {
        "slug":                      DQ(slug) if slug else None,
        "fqn":                       DQ(fqn),
        "is_cross_server":           is_cross,
        "accessed_via_linked_server": DQ(alias) if alias else None,
        "access_method":             method,
    }


def _dedup_deps(deps: list[dict]) -> list[dict]:
    """Deduplicate dependency list by slug (keeping first occurrence)."""
    seen: set[str] = set()
    result = []
    for d in deps:
        key = d.get("slug") or d.get("fqn") or ""
        if key not in seen:
            seen.add(key)
            result.append(d)
    return result


# ── Job helper: assign jobs to databases ──────────────────────────────────────

_EXEC_PATTERN = re.compile(
    r"\bEXEC(?:UTE)?\s+(?:\[?(\w+)\]?\.)?\[?(\w+)\]?\.\[?(\w+)\]?",
    re.IGNORECASE,
)


def _parse_proc_refs_from_step(command: str) -> list[str]:
    """Extract bare procedure names from a T-SQL step command."""
    names = []
    for m in _EXEC_PATTERN.finditer(command or ""):
        # group(3) is the proc name (rightmost)
        names.append(m.group(3).lower())
    return names


def _assign_jobs_to_databases(
    jobs_data: dict,
    db_proc_slugs: dict[str, set[str]],  # {db_lower -> set of proc slugs}
    server_lower: str,
) -> dict[str, list[str]]:
    """Return {database_lower -> [job_id]} assigning each job to one database."""
    assignment: dict[str, list[str]] = defaultdict(list)
    g1 = jobs_data["g1"]
    g2 = jobs_data["g2"]

    steps_by_job: dict[str, list[dict]] = defaultdict(list)
    for row in g2:
        steps_by_job[row["job_id"]].append(row)

    for job_row in g1:
        job_id = job_row["id"]
        steps  = steps_by_job.get(job_id, [])

        db_counts: dict[str, int] = defaultdict(int)
        for step in steps:
            if step.get("subsystem", "").upper() != "TSQL":
                continue
            cmd = step.get("command", "") or ""
            proc_names = _parse_proc_refs_from_step(cmd)
            for db_lower, proc_slugs in db_proc_slugs.items():
                for pname in proc_names:
                    # Match by name suffix
                    if any(slug.endswith(f".{pname}") for slug in proc_slugs):
                        db_counts[db_lower] += 1

        if not db_counts:
            continue  # job references no documented procedures; skip

        max_count = max(db_counts.values())
        candidates = sorted(
            [db for db, cnt in db_counts.items() if cnt == max_count]
        )
        assignment[candidates[0]].append(job_id)

    return assignment


# ── Step 5 helpers — build per-object context dicts ──────────────────────────


def _build_table_ctx(
    table_row: dict,
    b_data: dict,
    ext_props: dict,
    vol_data: dict,
    ts: str,
    server: str,
    database: str,
) -> dict:
    slug = table_row["id"]
    fqn  = table_row["fqn"]
    schema = table_row["schema"]
    name   = table_row["name"]
    sql_mod = table_row["sql_modify_date"]

    desc = ext_props.get(slug, {}).get("object", None)
    alias_ep = ext_props.get(slug, {}).get("alias", None)

    # Columns
    columns = [
        {
            "column_name":   r["column_name"],
            "sql_type":      r["sql_type"],
            "is_nullable":   bool(r["is_nullable"]),
            "column_default": r.get("column_default"),
            "column_notes":  r.get("column_notes"),
        }
        for r in b_data["b2"]
        if r["table_id"] == slug
    ]

    # Primary key
    pk_cols = [
        r["pk_column"]
        for r in sorted(
            [r for r in b_data["b3"] if r["table_id"] == slug],
            key=lambda x: x["key_ordinal"],
        )
    ]

    # Foreign keys — group by fk_name to get per-FK column+ref
    fk_map: dict[str, list] = defaultdict(list)
    for r in b_data["b4"]:
        if r["table_id"] == slug:
            fk_map[r["fk_name"]].append(r)
    foreign_keys = []
    for fk_rows in fk_map.values():
        for r in fk_rows:
            foreign_keys.append({
                "column":          r["fk_column"],
                "references_slug": DQ(r["references_slug"]),
                "references_column": r["references_column"],
            })

    # Unique constraints
    uc_map: dict[str, list] = defaultdict(list)
    for r in b_data["b5"]:
        if r["table_id"] == slug:
            uc_map[r["uc_name"]].append(r)
    unique_constraints = [
        {
            "name": uc_name,
            "columns": [r["uc_column"] for r in sorted(rows, key=lambda x: x["key_ordinal"])],
        }
        for uc_name, rows in uc_map.items()
    ]

    # Indexes
    idx_map: dict[str, dict] = {}
    for r in b_data["b6"]:
        if r["table_id"] != slug:
            continue
        iname = r["index_name"]
        if iname not in idx_map:
            idx_map[iname] = {
                "name":             iname,
                "is_unique":        bool(r["is_unique"]),
                "is_clustered":     bool(r["is_clustered"]),
                "columns":          [],
                "included_columns": [],
            }
        if r["is_included_column"]:
            idx_map[iname]["included_columns"].append(r["column_name"])
        else:
            idx_map[iname]["columns"].append((r["key_ordinal"], r["column_name"]))
    indexes = []
    for idx in idx_map.values():
        idx["columns"] = [c for _, c in sorted(idx["columns"])]
        indexes.append(idx)

    # Volumetry
    vol = vol_data.get(slug, {})
    row_count = vol.get("approximate_row_count", 0) or 0
    data_mb   = float(vol.get("data_size_mb", 0.0) or 0.0)

    # Outbound deps from foreign keys
    fk_out_slugs = {fk["references_slug"] for fk in foreign_keys}
    out_deps = [
        {"slug": DQ(s), "fqn": DQ(s), "is_cross_server": False,
         "accessed_via_linked_server": None, "access_method": "direct"}
        for s in fk_out_slugs
    ]

    return {
        "id":             slug,
        "fqn":            fqn,
        "server":         server,
        "database":       database,
        "schema":         schema,
        "name":           name,
        "last_extracted_at": ts,
        "sql_modify_date":  sql_mod,
        "description":    desc,
        "alias":          alias_ep,
        "primary_key":    pk_cols,
        "foreign_keys":   foreign_keys,
        "unique_constraints": unique_constraints,
        "indexes":        indexes,
        "approximate_row_count": int(row_count),
        "data_size_mb":   data_mb,
        "columns":        columns,
        "read_by":        [],
        "written_by":     [],
        "dependencies": {
            "outbound": out_deps,
            "inbound":  [],
        },
    }


def _type_code_to_object_type(type_code: str) -> str:
    mapping = {"U": "table", "V": "view", "P": "procedure",
               "FN": "function", "IF": "function", "TF": "function", "TR": "trigger"}
    return mapping.get(type_code, "unknown")


def _build_dep_list(dep_rows: list[dict], server_lower: str, db_lower: str,
                    linked_map: dict, logger: Logger, ref_slug: str,
                    filter_types: list[str] | None = None) -> list[dict]:
    deps = []
    for r in dep_rows:
        type_code = r.get("ref_type_code", "")
        obj_type  = _type_code_to_object_type(type_code)
        if filter_types is not None and obj_type not in filter_types:
            continue
        dep = _resolve_dep(r, server_lower, db_lower, linked_map, logger, ref_slug)
        deps.append(dep)
    return _dedup_deps(deps)


# ── Core pipeline ─────────────────────────────────────────────────────────────


def run(
    targets: list[tuple[str, str]],
    mode: str,
    output_root: Path,
    connections: dict[str, pyodbc.Connection],
    logger: Logger,
    wall_clock_start: str,
) -> bool:
    """Execute the full 8-step pipeline.  Returns True if any errors occurred."""
    any_error = False

    for server, database in targets:
        server_lower   = server.lower()
        database_lower = database.lower()
        db_dir         = output_root / server_lower / database_lower

        conn = connections.get(server_lower)
        if conn is None:
            # Already warned during connection phase; just skip
            any_error = True
            continue

        logger.ensure_target(server_lower, database_lower)

        # ── Step 2: build current_set ────────────────────────────────────────

        try:
            inventory_rows = queries.get_inventory(conn, database)
        except pyodbc.Error as exc:
            err_msg = str(exc)
            if "permission" in err_msg.lower() or "select" in err_msg.lower():
                logger.error(
                    f"Missing permission on sys.objects for login. "
                    f"Cannot extract metadata for {server}/{database}."
                )
            else:
                logger.error(f"Error querying inventory for {server}/{database}: {exc}")
            any_error = True
            continue

        current_set: dict[str, dict] = {r["id"]: r for r in inventory_rows}
        logger.info(f"{server}/{database} — Step 2 complete: {len(current_set)} objects in current_set")

        # Jobs: load all and assign to databases
        try:
            jobs_data = queries.get_jobs(conn)
        except pyodbc.Error as exc:
            logger.warn(f"Cannot query SQL Agent jobs for {server}: {exc}")
            jobs_data = {"g1": [], "g2": [], "g3": [], "g4": []}

        linked_map = {}
        try:
            linked_map = _build_linked_server_map(conn)
        except pyodbc.Error as exc:
            logger.warn(f"Cannot query linked servers for {server}: {exc}")

        # ── Step 3: load prior_set ────────────────────────────────────────────

        prior_set: dict[str, dict] = {}
        index_path = db_dir / "_index.md"
        if index_path.exists():
            index_content = _read_optional(index_path)
            if index_content:
                prior_yaml = _load_yaml_front_matter(index_content)
                if prior_yaml and isinstance(prior_yaml.get("inventory"), list):
                    for entry in prior_yaml["inventory"]:
                        eid = entry.get("id")
                        if eid:
                            prior_set[eid] = {
                                "sql_modify_date": entry.get("sql_modify_date", ""),
                                "deprecated": entry.get("deprecated", False),
                            }

        logger.info(f"{server}/{database} — Step 3 complete: {len(prior_set)} objects in prior_set")

        # ── Step 4: classify ─────────────────────────────────────────────────

        classification: dict[str, str] = {}  # id -> NEW|MODIFIED|UNCHANGED|REMOVED

        all_ids = set(current_set) | {k for k, v in prior_set.items() if not v.get("deprecated")}

        for obj_id in all_ids:
            in_current = obj_id in current_set
            in_prior   = obj_id in prior_set and not prior_set[obj_id].get("deprecated")
            if in_current and not in_prior:
                classification[obj_id] = "NEW"
            elif in_current and in_prior:
                cur_date  = current_set[obj_id].get("sql_modify_date", "")
                prior_date = prior_set[obj_id].get("sql_modify_date", "")
                if cur_date > prior_date:
                    classification[obj_id] = "MODIFIED"
                else:
                    classification[obj_id] = "UNCHANGED"
            elif not in_current and in_prior:
                classification[obj_id] = "REMOVED"

        if mode == "full":
            for obj_id, cls in classification.items():
                if cls == "UNCHANGED":
                    classification[obj_id] = "MODIFIED"

        n_new = sum(1 for c in classification.values() if c == "NEW")
        n_mod = sum(1 for c in classification.values() if c == "MODIFIED")
        n_unc = sum(1 for c in classification.values() if c == "UNCHANGED")
        n_rem = sum(1 for c in classification.values() if c == "REMOVED")
        logger.info(
            f"{server}/{database} — Step 4 complete: "
            f"{n_new} NEW, {n_mod} MODIFIED, {n_unc} UNCHANGED, {n_rem} REMOVED"
        )

        # ── Step 5: regenerate NEW and MODIFIED objects ───────────────────────

        to_regenerate = {k for k, v in classification.items() if v in ("NEW", "MODIFIED")}

        # Gather which object types need extraction
        need_tables    = any(current_set[i]["object_type"] == "table"     for i in to_regenerate if i in current_set)
        need_views     = any(current_set[i]["object_type"] == "view"      for i in to_regenerate if i in current_set)
        need_procs     = any(current_set[i]["object_type"] == "procedure" for i in to_regenerate if i in current_set)
        need_funcs     = any(current_set[i]["object_type"] == "function"  for i in to_regenerate if i in current_set)
        need_triggers  = any(current_set[i]["object_type"] == "trigger"   for i in to_regenerate if i in current_set)

        # Always reload tables.md as a unit (even for UNCHANGED tables)
        # Per spec: "all NEW, MODIFIED, and UNCHANGED tables written together"
        all_table_ids  = [i for i in current_set if current_set[i]["object_type"] == "table"]
        tables_need_rewrite = bool(
            [i for i in all_table_ids if classification.get(i) in ("NEW", "MODIFIED")]
            or mode == "full"
        )

        # Extended properties and volumetry (load once per database if needed)
        ext_props: dict[str, dict] = {}
        vol_data:  dict[str, dict] = {}

        if tables_need_rewrite or need_views or need_procs or need_funcs or need_triggers:
            try:
                ep_rows = queries.get_extended_properties(conn, database)
                for row in ep_rows:
                    slug = row["object_id_slug"]
                    lvl  = row["ep_level"]
                    if slug not in ext_props:
                        ext_props[slug] = {}
                    if lvl == "object":
                        ext_props[slug]["object"] = row.get("description")
                    elif lvl == "column":
                        if "columns" not in ext_props[slug]:
                            ext_props[slug]["columns"] = {}
                        ext_props[slug]["columns"][row["column_name"]] = row.get("description")
            except pyodbc.Error as exc:
                logger.warn(f"Cannot load extended properties for {server}/{database}: {exc}")

        if tables_need_rewrite:
            try:
                vol_rows = queries.get_volumetry(conn, database)
                vol_data = {r["table_id"]: r for r in vol_rows}
            except pyodbc.Error as exc:
                logger.warn(f"Cannot load volumetry for {server}/{database}: {exc}")

        # ── Tables (single file unit) ─────────────────────────────────────────

        if all_table_ids:
            b_data: dict = {"b1": [], "b2": [], "b3": [], "b4": [], "b5": [], "b6": []}
            if tables_need_rewrite:
                try:
                    b_data = queries.get_table_metadata(conn, database)
                except pyodbc.Error as exc:
                    logger.error(
                        f"Missing permission or error querying table metadata for "
                        f"{server}/{database}: {exc}"
                    )
                    any_error = True
                    # Skip tables but continue with other types
                    all_table_ids = []

            if all_table_ids:
                tables_path = db_dir / "tables.md"
                existing_tables_content = _read_optional(tables_path)

                # Load UNCHANGED tables from existing YAML
                existing_table_ctxs: dict[str, dict] = {}
                if existing_tables_content:
                    ex_yaml = _load_yaml_front_matter(existing_tables_content)
                    if ex_yaml and isinstance(ex_yaml.get("tables"), list):
                        for t in ex_yaml["tables"]:
                            tid = t.get("id", "")
                            if tid:
                                existing_table_ctxs[tid] = dict(t)

                # Build table contexts
                b1_by_id = {r["id"]: r for r in b_data["b1"]}
                table_ctxs: list[dict] = []
                for tid in all_table_ids:
                    cls = classification.get(tid, "UNCHANGED")
                    if cls in ("NEW", "MODIFIED"):
                        trow = b1_by_id.get(tid)
                        if trow is None:
                            continue
                        trow["server"] = server_lower
                        trow["database"] = database_lower
                        trow["last_extracted_at"] = wall_clock_start
                        ctx = _build_table_ctx(trow, b_data, ext_props, vol_data, wall_clock_start, server_lower, database_lower)
                    else:
                        # Use existing data from prior tables.md
                        ctx = existing_table_ctxs.get(tid)
                        if ctx is None:
                            # No prior data; need to query anyway
                            trow = b1_by_id.get(tid) or {"id": tid, "fqn": tid, "schema": "dbo", "name": tid.split(".")[-1], "sql_modify_date": ""}
                            trow["server"] = server_lower
                            trow["database"] = database_lower
                            trow["last_extracted_at"] = wall_clock_start
                            ctx = _build_table_ctx(trow, b_data, ext_props, vol_data, wall_clock_start, server_lower, database_lower)
                        else:
                            # Convert ruamel CommentedMap to plain dict recursively
                            ctx = dict(ctx)
                    table_ctxs.append(ctx)

                # Preserve Human Notes per table and file-level
                notes_map: dict[str, str] = {}
                file_notes = ""
                if existing_tables_content:
                    body_text = existing_tables_content
                    # Extract file-level notes
                    file_nr = extract_untagged(body_text)
                    if file_nr.malformed:
                        logger.warn(
                            f"[WARN] Malformed Human Notes delimiters in {tables_path}. "
                            f"Body preserved verbatim. Manual review required."
                        )
                        # Preserve entire body verbatim per spec
                        body_only = extract_body_after_yaml(existing_tables_content)
                        new_yaml_str = _build_tables_yaml_only(table_ctxs, server_lower, database_lower, wall_clock_start, notes_map, file_notes)
                        final_content = "---\n" + new_yaml_str + "---\n" + body_only
                        _safe_write(tables_path, final_content, logger, server_lower, database_lower, existing_tables_content)
                        # Skip normal table write
                        goto_next_type = True
                    else:
                        file_notes = file_nr.content
                        goto_next_type = False
                    if not file_nr.malformed:
                        for tctx in table_ctxs:
                            slug = tctx.get("id", "")
                            tr = extract_tagged(existing_tables_content, slug)
                            if tr.malformed:
                                logger.warn(
                                    f"Malformed Human Notes delimiters for table {slug} "
                                    f"in {tables_path}. Body preserved verbatim. Manual review required."
                                )
                                notes_map[slug] = ""
                            else:
                                notes_map[slug] = tr.content
                else:
                    goto_next_type = False

                if not (existing_tables_content and _load_yaml_front_matter(existing_tables_content or "") is not None and
                        any(r.malformed for r in [extract_untagged(existing_tables_content or "")])):
                    goto_next_type = False

                if not goto_next_type:
                    rendered = render_tables(table_ctxs, notes_map, file_notes)
                    _safe_write(tables_path, rendered, logger, server_lower, database_lower, existing_tables_content)

        logger.info(f"{server}/{database} — Step 5 (tables) complete")

        # ── Views ────────────────────────────────────────────────────────────

        if need_views:
            view_ids = [i for i in to_regenerate if i in current_set and current_set[i]["object_type"] == "view"]
            if view_ids:
                try:
                    v_data = queries.get_views(conn, database)
                except pyodbc.Error as exc:
                    logger.error(f"Missing permission querying views for {server}/{database}: {exc}")
                    any_error = True
                    v_data = {"c1": [], "c2": []}

                c1_by_id = {r["id"]: r for r in v_data["c1"]}
                c2_by_ref = defaultdict(list)
                for r in v_data["c2"]:
                    c2_by_ref[r["referencing_id"]].append(r)

                views_dir = db_dir / "views"
                for vid in view_ids:
                    vrow = c1_by_id.get(vid)
                    if vrow is None:
                        continue
                    out_deps = _build_dep_list(
                        c2_by_ref.get(vid, []), server_lower, database_lower,
                        linked_map, logger, vid,
                    )
                    reads_from = out_deps
                    single_multi = "single" if len({d.get("slug") for d in reads_from}) <= 1 else "multi"

                    ctx = {
                        "id":           vid,
                        "fqn":          vrow["fqn"],
                        "server":       server_lower,
                        "database":     database_lower,
                        "schema":       vrow["schema"],
                        "name":         vrow["name"],
                        "last_extracted_at": wall_clock_start,
                        "sql_modify_date":   vrow["sql_modify_date"],
                        "description":  ext_props.get(vid, {}).get("object"),
                        "alias":        ext_props.get(vid, {}).get("alias"),
                        "is_indexed":   bool(vrow.get("is_indexed")),
                        "is_schemabound": bool(vrow.get("is_schemabound")),
                        "single_or_multi_source": single_multi,
                        "reads_from":   reads_from,
                        "read_by":      [],
                        "definition":   vrow.get("view_definition", ""),
                        "dependencies": {"outbound": out_deps, "inbound": []},
                    }

                    vpath = views_dir / f"{vid}.md"
                    existing = _read_optional(vpath)
                    if existing:
                        nr = extract_untagged(existing)
                        if nr.malformed:
                            logger.warn(
                                f"Malformed Human Notes delimiters in {vpath}. "
                                f"Body preserved verbatim. Manual review required."
                            )
                            body_only = extract_body_after_yaml(existing)
                            ctx_yaml  = render_view(ctx, "")
                            fm_end    = ctx_yaml.index("\n---\n") + 5
                            new_content = ctx_yaml[:fm_end] + "\n" + body_only
                            _safe_write(vpath, new_content, logger, server_lower, database_lower, existing)
                            continue
                        notes_content = nr.content
                    else:
                        notes_content = ""

                    rendered = render_view(ctx, notes_content)
                    _safe_write(vpath, rendered, logger, server_lower, database_lower, existing)

        logger.info(f"{server}/{database} — Step 5 (views) complete")

        # ── Procedures ────────────────────────────────────────────────────────

        if need_procs:
            proc_ids = [i for i in to_regenerate if i in current_set and current_set[i]["object_type"] == "procedure"]
            if proc_ids:
                try:
                    p_data = queries.get_procedures(conn, database)
                except pyodbc.Error as exc:
                    logger.error(f"Missing permission querying procedures for {server}/{database}: {exc}")
                    any_error = True
                    p_data = {"d1": [], "d2": [], "d3": []}

                d1_by_id   = {r["id"]: r for r in p_data["d1"]}
                d2_by_proc = defaultdict(list)
                for r in p_data["d2"]:
                    d2_by_proc[r["proc_id"]].append(r)
                d3_by_ref  = defaultdict(list)
                for r in p_data["d3"]:
                    d3_by_ref[r["referencing_id"]].append(r)

                procs_dir = db_dir / "procedures"
                for pid in proc_ids:
                    prow = d1_by_id.get(pid)
                    if prow is None:
                        continue

                    all_deps_rows = d3_by_ref.get(pid, [])
                    all_deps = _build_dep_list(all_deps_rows, server_lower, database_lower, linked_map, logger, pid)

                    reads_from    = _build_dep_list(
                        [r for r in all_deps_rows if r.get("ref_type_code") in ("U", "V")],
                        server_lower, database_lower, linked_map, logger, pid
                    )
                    calls_procs   = _build_dep_list(
                        [r for r in all_deps_rows if r.get("ref_type_code") == "P"],
                        server_lower, database_lower, linked_map, logger, pid
                    )
                    calls_funcs   = _build_dep_list(
                        [r for r in all_deps_rows if r.get("ref_type_code") in ("FN", "IF", "TF")],
                        server_lower, database_lower, linked_map, logger, pid
                    )

                    params = []
                    for pr in sorted(d2_by_proc.get(pid, []), key=lambda x: x["param_order"]):
                        params.append({
                            "name":      pr["param_name"],
                            "sql_type":  pr["sql_type"],
                            "direction": pr["direction"],
                            "default":   "none",
                            "is_output": bool(pr["is_output"]),
                        })

                    out_deps = _dedup_deps(reads_from + calls_procs + calls_funcs)

                    ctx = {
                        "id":           pid,
                        "fqn":          prow["fqn"],
                        "server":       server_lower,
                        "database":     database_lower,
                        "schema":       prow["schema"],
                        "name":         prow["name"],
                        "last_extracted_at": wall_clock_start,
                        "sql_modify_date":   prow["sql_modify_date"],
                        "description":  ext_props.get(pid, {}).get("object"),
                        "alias":        ext_props.get(pid, {}).get("alias"),
                        "primary_function": None,
                        "function_tags": [],
                        "parameters":   params,
                        "reads_from":   reads_from,
                        "writes_to":    [],
                        "calls_procedures": calls_procs,
                        "calls_functions":  calls_funcs,
                        "called_by_procedures": [],
                        "invoked_by_jobs": [],
                        "definition":   prow.get("proc_definition", ""),
                        "dependencies": {"outbound": out_deps, "inbound": []},
                    }

                    ppath = procs_dir / f"{pid}.md"
                    existing = _read_optional(ppath)
                    if existing:
                        nr = extract_untagged(existing)
                        if nr.malformed:
                            logger.warn(
                                f"Malformed Human Notes delimiters in {ppath}. "
                                f"Body preserved verbatim. Manual review required."
                            )
                            body_only = extract_body_after_yaml(existing)
                            rnd = render_procedure(ctx, "")
                            fm_end = rnd.index("\n---\n") + 5
                            new_content = rnd[:fm_end] + "\n" + body_only
                            _safe_write(ppath, new_content, logger, server_lower, database_lower, existing)
                            continue
                        notes_content = nr.content
                    else:
                        notes_content = ""

                    rendered = render_procedure(ctx, notes_content)
                    _safe_write(ppath, rendered, logger, server_lower, database_lower, existing)

        logger.info(f"{server}/{database} — Step 5 (procedures) complete")

        # ── Functions ─────────────────────────────────────────────────────────

        if need_funcs:
            func_ids = [i for i in to_regenerate if i in current_set and current_set[i]["object_type"] == "function"]
            if func_ids:
                try:
                    f_data = queries.get_functions(conn, database)
                except pyodbc.Error as exc:
                    logger.error(f"Missing permission querying functions for {server}/{database}: {exc}")
                    any_error = True
                    f_data = {"e1": [], "e2": [], "e3": [], "e4": []}

                e1_by_id  = {r["id"]: r for r in f_data["e1"]}
                e2_by_func = defaultdict(list)
                for r in f_data["e2"]:
                    e2_by_func[r["func_id"]].append(r)
                e3_by_func = defaultdict(list)
                for r in f_data["e3"]:
                    e3_by_func[r["func_id"]].append(r)
                e4_by_ref  = defaultdict(list)
                for r in f_data["e4"]:
                    e4_by_ref[r["referencing_id"]].append(r)

                funcs_dir = db_dir / "functions"
                for fid in func_ids:
                    frow = e1_by_id.get(fid)
                    if frow is None:
                        continue

                    func_type = frow.get("function_type", "scalar")
                    # Parameters (parameter_id > 0)
                    params = []
                    returns_type = "UNKNOWN"
                    for pr in sorted(e2_by_func.get(fid, []), key=lambda x: x["parameter_id"]):
                        if pr["param_role"] == "return_value":
                            returns_type = pr["sql_type"]
                        else:
                            params.append({
                                "name":      pr["param_name"],
                                "sql_type":  pr["sql_type"],
                                "direction": "input",
                                "default":   "none",
                                "is_output": False,
                            })
                    if func_type in ("inline_tvf", "multi_statement_tvf"):
                        returns_type = "TABLE"

                    # TVF return schema
                    returns_schema = None
                    if func_type in ("inline_tvf", "multi_statement_tvf"):
                        cols = sorted(e3_by_func.get(fid, []), key=lambda x: x["column_id"])
                        returns_schema = [
                            {"column": c["column_name"], "sql_type": c["sql_type"], "nullable": bool(c["is_nullable"])}
                            for c in cols
                        ]

                    out_deps = _build_dep_list(e4_by_ref.get(fid, []), server_lower, database_lower, linked_map, logger, fid)

                    ctx = {
                        "id":           fid,
                        "fqn":          frow["fqn"],
                        "server":       server_lower,
                        "database":     database_lower,
                        "schema":       frow["schema"],
                        "name":         frow["name"],
                        "last_extracted_at": wall_clock_start,
                        "sql_modify_date":   frow["sql_modify_date"],
                        "description":  ext_props.get(fid, {}).get("object"),
                        "alias":        ext_props.get(fid, {}).get("alias"),
                        "function_type": func_type,
                        "parameters":   params,
                        "returns_type": returns_type,
                        "returns_table_schema": returns_schema,
                        "reads_from":   out_deps,
                        "called_by_procedures": [],
                        "called_by_views":      [],
                        "called_by_functions":  [],
                        "called_by_triggers":   [],
                        "definition":   frow.get("func_definition", ""),
                        "dependencies": {"outbound": out_deps, "inbound": []},
                    }

                    fpath = funcs_dir / f"{fid}.md"
                    existing = _read_optional(fpath)
                    if existing:
                        nr = extract_untagged(existing)
                        if nr.malformed:
                            logger.warn(f"Malformed Human Notes delimiters in {fpath}. Body preserved verbatim.")
                            body_only = extract_body_after_yaml(existing)
                            rnd = render_function(ctx, "")
                            fm_end = rnd.index("\n---\n") + 5
                            new_content = rnd[:fm_end] + "\n" + body_only
                            _safe_write(fpath, new_content, logger, server_lower, database_lower, existing)
                            continue
                        notes_content = nr.content
                    else:
                        notes_content = ""

                    rendered = render_function(ctx, notes_content)
                    _safe_write(fpath, rendered, logger, server_lower, database_lower, existing)

        logger.info(f"{server}/{database} — Step 5 (functions) complete")

        # ── Triggers ──────────────────────────────────────────────────────────

        if need_triggers:
            trig_ids = [i for i in to_regenerate if i in current_set and current_set[i]["object_type"] == "trigger"]
            if trig_ids:
                try:
                    tr_data = queries.get_triggers(conn, database)
                except pyodbc.Error as exc:
                    logger.error(f"Missing permission querying triggers for {server}/{database}: {exc}")
                    any_error = True
                    tr_data = {"f1": [], "f2": [], "f3": []}

                f1_by_id  = {r["id"]: r for r in tr_data["f1"]}
                f2_by_trig = defaultdict(list)
                for r in tr_data["f2"]:
                    f2_by_trig[r["trigger_id"]].append(r)
                f3_by_ref  = defaultdict(list)
                for r in tr_data["f3"]:
                    f3_by_ref[r["referencing_id"]].append(r)

                trigs_dir = db_dir / "triggers"
                for tid in trig_ids:
                    trow = f1_by_id.get(tid)
                    if trow is None:
                        continue

                    events = [r["trigger_event"] for r in f2_by_trig.get(tid, [])]
                    timing = "INSTEAD_OF" if trow.get("is_instead_of_trigger") else "AFTER"

                    out_deps_raw = f3_by_ref.get(tid, [])
                    out_deps = _build_dep_list(out_deps_raw, server_lower, database_lower, linked_map, logger, tid)

                    # Attached table is both outbound (trigger fires on it) and inbound
                    att_slug = trow.get("attached_to_table", "")
                    att_fqn  = trow.get("attached_to_fqn", att_slug)
                    att_dep  = {"slug": DQ(att_slug), "fqn": DQ(att_fqn), "is_cross_server": False,
                                "accessed_via_linked_server": None, "access_method": "direct"}
                    if not _slug_in_list(att_slug, out_deps):
                        out_deps = [att_dep] + out_deps

                    ctx = {
                        "id":           tid,
                        "fqn":          trow["fqn"],
                        "server":       server_lower,
                        "database":     database_lower,
                        "schema":       trow["schema"],
                        "name":         trow["name"],
                        "last_extracted_at": wall_clock_start,
                        "sql_modify_date":   trow["sql_modify_date"],
                        "description":  ext_props.get(tid, {}).get("object"),
                        "alias":        ext_props.get(tid, {}).get("alias"),
                        "attached_to_table": att_slug,
                        "trigger_events": events,
                        "trigger_timing": timing,
                        "is_enabled":   bool(trow.get("is_enabled", True)),
                        "reads_from":   out_deps,
                        "writes_to":    [],
                        "calls_procedures": [],
                        "calls_functions":  [],
                        "definition":   trow.get("trigger_definition", ""),
                        "dependencies": {"outbound": out_deps, "inbound": [att_dep]},
                    }

                    tpath = trigs_dir / f"{tid}.md"
                    existing = _read_optional(tpath)
                    if existing:
                        nr = extract_untagged(existing)
                        if nr.malformed:
                            logger.warn(f"Malformed Human Notes delimiters in {tpath}. Body preserved verbatim.")
                            body_only = extract_body_after_yaml(existing)
                            rnd = render_trigger(ctx, "")
                            fm_end = rnd.index("\n---\n") + 5
                            new_content = rnd[:fm_end] + "\n" + body_only
                            _safe_write(tpath, new_content, logger, server_lower, database_lower, existing)
                            continue
                        notes_content = nr.content
                    else:
                        notes_content = ""

                    rendered = render_trigger(ctx, notes_content)
                    _safe_write(tpath, rendered, logger, server_lower, database_lower, existing)

        logger.info(f"{server}/{database} — Step 5 (triggers) complete")

        # ── Jobs ──────────────────────────────────────────────────────────────

        # Build proc slug set for this database
        db_proc_slugs: dict[str, set] = {database_lower: {
            i for i, r in current_set.items() if r["object_type"] == "procedure"
        }}
        job_assignment = _assign_jobs_to_databases(jobs_data, db_proc_slugs, server_lower)
        assigned_job_ids = set(job_assignment.get(database_lower, []))

        g1_by_id  = {r["id"]: r for r in jobs_data["g1"]}
        g2_by_job = defaultdict(list)
        for r in jobs_data["g2"]:
            g2_by_job[r["job_id"]].append(r)
        g3_by_job = defaultdict(list)
        for r in jobs_data["g3"]:
            g3_by_job[r["job_id"]].append(r)
        g4_by_job = {r["job_id"]: r for r in jobs_data["g4"]}

        jobs_dir = db_dir / "jobs"
        for job_id in assigned_job_ids:
            jrow = g1_by_id.get(job_id)
            if jrow is None:
                continue

            steps_raw = sorted(g2_by_job.get(job_id, []), key=lambda x: x["step_order"])
            schedules_raw = g3_by_job.get(job_id, [])
            history = g4_by_job.get(job_id, {})

            steps = []
            invokes_procs = []
            for sr in steps_raw:
                cmd = sr.get("command", "") or ""
                ref_slug = None
                for pname in _parse_proc_refs_from_step(cmd):
                    for ps in db_proc_slugs[database_lower]:
                        if ps.endswith(f".{pname}"):
                            ref_slug = ps
                            break
                    if ref_slug:
                        break

                steps.append({
                    "order":              sr["step_order"],
                    "step_name":          sr["step_name"],
                    "subsystem":          sr["subsystem"],
                    "database":           sr["database_name"],
                    "command":            sr["command"],
                    "on_success_action":  sr["on_success_action"],
                    "on_success_step":    sr["on_success_step"],
                    "on_failure_action":  sr["on_failure_action"],
                    "on_failure_step":    sr["on_failure_step"],
                    "references_object_slug": ref_slug,
                })
                if ref_slug and not any(d.get("slug") == ref_slug for d in invokes_procs):
                    invokes_procs.append({
                        "slug": DQ(ref_slug),
                        "fqn":  DQ(ref_slug),
                        "is_cross_server": False,
                        "accessed_via_linked_server": None,
                        "access_method": "direct",
                    })

            schedules = []
            for sc in schedules_raw:
                freq_desc = _schedule_description(sc)
                schedules.append({
                    "name":              sc["schedule_name"],
                    "frequency_type":    sc["frequency_type"],
                    "frequency_interval": sc["freq_interval"],
                    "active_start_time": sc["active_start_time"],
                    "description":       freq_desc,
                })

            # last run
            last_outcome = None
            last_date    = None
            if history:
                last_outcome = history.get("last_run_outcome", "Unknown")
                raw_dt = history.get("last_run_datetime_local")
                if raw_dt:
                    last_date = _parse_job_datetime(raw_dt)

            ctx = {
                "id":           job_id,
                "fqn":          jrow["fqn"],
                "server":       server_lower,
                "name":         jrow["job_name"],
                "last_extracted_at": wall_clock_start,
                "sql_modify_date":   jrow["sql_modify_date"],
                "description":  jrow.get("job_description"),
                "alias":        None,
                "is_enabled":   bool(jrow.get("is_enabled")),
                "owner":        jrow.get("owner", ""),
                "category":     jrow.get("category", ""),
                "schedules":    schedules,
                "steps":        steps,
                "invokes_procedures": invokes_procs,
                "last_run_outcome":   last_outcome or "Unknown",
                "last_run_date":      last_date,
                "dependencies": {"outbound": invokes_procs, "inbound": []},
            }

            jpath = jobs_dir / f"{job_id}.md"
            existing = _read_optional(jpath)
            if existing:
                nr = extract_untagged(existing)
                if nr.malformed:
                    logger.warn(f"Malformed Human Notes delimiters in {jpath}. Body preserved verbatim.")
                    body_only = extract_body_after_yaml(existing)
                    rnd = render_job(ctx, "")
                    fm_end = rnd.index("\n---\n") + 5
                    new_content = rnd[:fm_end] + "\n" + body_only
                    _safe_write(jpath, new_content, logger, server_lower, database_lower, existing)
                    continue
                notes_content = nr.content
            else:
                notes_content = ""

            rendered = render_job(ctx, notes_content)
            _safe_write(jpath, rendered, logger, server_lower, database_lower, existing)

        logger.info(f"{server}/{database} — Step 5 (jobs) complete")

        # ── Step 6: archive REMOVED objects ──────────────────────────────────

        removed_ids = [k for k, v in classification.items() if v == "REMOVED"]
        for rid in removed_ids:
            prior_type = prior_set.get(rid, {})
            # Determine object_type from prior_set id pattern
            # Fallback: check archived YAML if we can't derive it
            obj_type = _infer_type_from_prior(rid, db_dir)
            if obj_type == "table":
                # Tables archived from tables.md (handled separately)
                _archive_table_from_catalog(rid, db_dir, wall_clock_start, logger, server_lower, database_lower)
            else:
                type_dir_map = {
                    "view": "views", "procedure": "procedures",
                    "function": "functions", "trigger": "triggers", "job": "jobs",
                }
                type_dir = type_dir_map.get(obj_type, obj_type)
                src = db_dir / type_dir / f"{rid}.md"
                archive_object(
                    src, obj_type, rid, db_dir, wall_clock_start,
                    logger, server_lower, database_lower
                )

        logger.info(f"{server}/{database} — Step 6 complete: {len(removed_ids)} objects archived")

        # ── Step 7: bidirectional dependency mirroring ────────────────────────

        slug_type_map = {i: r["object_type"] for i, r in current_set.items()}
        run_mirror_pass(db_dir, slug_type_map, logger, server_lower, database_lower)

        # ── Step 8: rebuild _index.md ─────────────────────────────────────────

        archived_objects = _scan_archive(db_dir)
        archived_count   = len(archived_objects)

        inventory_for_index = [
            {
                "id":             r["id"],
                "object_type":    r["object_type"],
                "schema":         r.get("schema", ""),
                "name":           r.get("name", r["id"].split(".")[-1]),
                "sql_modify_date": r["sql_modify_date"],
            }
            for r in inventory_rows
        ]
        # Include assigned jobs in inventory
        for job_id in assigned_job_ids:
            jrow = g1_by_id.get(job_id)
            if jrow:
                inventory_for_index.append({
                    "id":             job_id,
                    "object_type":    "job",
                    "schema":         "dbo",
                    "name":           jrow["job_name"],
                    "sql_modify_date": jrow["sql_modify_date"],
                })

        index_ctx = {
            "server":                 server_lower,
            "database":               database_lower,
            "last_full_extraction_at": wall_clock_start,
            "inventory":              inventory_for_index,
            "archived_count":         archived_count,
            "archived_objects":       archived_objects,
        }

        existing_index = _read_optional(index_path)
        index_notes = ""
        if existing_index:
            nr = extract_untagged(existing_index)
            if nr.malformed:
                logger.warn(f"Malformed Human Notes delimiters in {index_path}. Body preserved verbatim.")
            else:
                index_notes = nr.content

        rendered_index = render_index(index_ctx, index_notes)
        _safe_write(index_path, rendered_index, logger, server_lower, database_lower, existing_index)

        logger.info(f"{server}/{database} — Step 8 complete: _index.md rebuilt")

    return any_error


# ── Private helpers ───────────────────────────────────────────────────────────


def _slug_in_list(slug: str, lst: list) -> bool:
    return any(d.get("slug") == slug for d in lst)


def _schedule_description(sc: dict) -> str:
    ft  = sc.get("frequency_type", "")
    fi  = sc.get("freq_interval", 1)
    ast = sc.get("active_start_time", "000000")
    h   = ast[:2]
    m   = ast[2:4]
    if ft == "Daily":
        return f"Ejecuta cada {fi} día(s) a las {h}:{m}"
    elif ft == "Weekly":
        return f"Ejecuta semanalmente a las {h}:{m}"
    elif ft == "Monthly":
        return f"Ejecuta mensualmente el día {fi} a las {h}:{m}"
    elif ft == "Once":
        return f"Ejecuta una vez a las {h}:{m}"
    elif ft == "AgentStart":
        return "Ejecuta al iniciar el Agente SQL"
    elif ft == "IdleCPU":
        return "Ejecuta cuando la CPU está inactiva"
    return ft


def _parse_job_datetime(raw: str) -> str | None:
    """Convert 'YYYYMMDD HHMMSS' to ISO-8601 UTC string."""
    try:
        raw = raw.strip()
        date_part = raw[:8]
        time_part = raw[9:15].zfill(6)
        dt = datetime.datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _infer_type_from_prior(slug: str, db_dir: Path) -> str:
    """Infer object type by checking which subdirectory the file lives in."""
    type_map = {
        "views":      "view",
        "procedures": "procedure",
        "functions":  "function",
        "triggers":   "trigger",
        "jobs":       "job",
    }
    for subdir, obj_type in type_map.items():
        if (db_dir / subdir / f"{slug}.md").exists():
            return obj_type
    if (db_dir / "tables.md").exists():
        # Check if slug appears in tables.md YAML
        content = _read_optional(db_dir / "tables.md")
        if content and slug in content:
            return "table"
    return "unknown"


def _archive_table_from_catalog(
    slug: str,
    db_dir: Path,
    deprecated_at: str,
    logger: Logger,
    server: str,
    database: str,
) -> None:
    """Remove a table from tables.md and write its entry to _archive/table/."""
    tables_path = db_dir / "tables.md"
    if not tables_path.exists():
        return

    content = _read_optional(tables_path)
    if not content:
        return

    lines = content.split("\n")
    dash_indices = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(dash_indices) < 2:
        return

    yaml_block = "\n".join(lines[dash_indices[0] + 1 : dash_indices[1]])
    body       = "\n".join(lines[dash_indices[1] + 1:])

    try:
        data = _yaml.load(yaml_block)
    except Exception:
        return

    if not data or not isinstance(data.get("tables"), list):
        return

    table_entry = None
    new_tables  = []
    for t in data["tables"]:
        if t.get("id") == slug:
            table_entry = dict(t)
        else:
            new_tables.append(t)

    data["tables"] = new_tables

    new_yaml = "---\n" + _dump(data) + "---\n" + body

    # Write updated tables.md
    _safe_write(tables_path, new_yaml, logger, server, database, content)

    # Write archived standalone file
    if table_entry:
        table_entry["deprecated"]    = True
        table_entry["deprecated_at"] = DQ(deprecated_at)
        archive_dir = db_dir / "_archive" / "table"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{slug}.md"
        archived_yaml = "---\n" + _dump(table_entry) + "---\n\n# Archived Table: " + slug + "\n"
        _safe_write(archive_path, archived_yaml, logger, server, database)
        logger.increment_archived(server, database)


def _scan_archive(db_dir: Path) -> list[dict]:
    """Return list of {slug, object_type, deprecated_at} from _archive/ subtree."""
    archive_root = db_dir / "_archive"
    if not archive_root.exists():
        return []
    results = []
    for md_file in archive_root.rglob("*.md"):
        content = _read_optional(md_file)
        if not content:
            continue
        data = _load_yaml_front_matter(content)
        if not data:
            continue
        slug = data.get("id") or md_file.stem
        results.append({
            "slug":         slug,
            "object_type":  data.get("object_type", "unknown"),
            "deprecated_at": data.get("deprecated_at", ""),
        })
    return results


def _build_tables_yaml_only(
    table_ctxs: list[dict],
    server: str,
    database: str,
    ts: str,
    notes_map: dict,
    file_notes: str,
) -> str:
    """Build only the YAML block for tables.md (used in malformed-notes path)."""
    from regenerator.renderer import render_tables
    rendered = render_tables(table_ctxs, notes_map, file_notes)
    lines = rendered.split("\n")
    dash_indices = [i for i, l in enumerate(lines) if l.strip() == "---"]
    if len(dash_indices) < 2:
        return rendered
    return "\n".join(lines[dash_indices[0] + 1 : dash_indices[1]])
