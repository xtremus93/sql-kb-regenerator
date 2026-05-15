"""MD template rendering for all 7 artifact types (Section 1 templates).

Uses ruamel.yaml for YAML front matter serialization (preserves field order,
block style).  Every render_* function receives a pre-built context dict and
returns the complete file content as a string.
"""

from __future__ import annotations

import io
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ

from regenerator.notes import inject_untagged, inject_tagged, has_content

# ── YAML instance (shared, thread-safe for single-threaded use) ───────────────

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


def _front_matter(data: Any) -> str:
    """Wrap YAML dump in --- delimiters."""
    return "---\n" + _dump(data) + "---\n"


# ── Dependency object helpers ─────────────────────────────────────────────────


def _dep_row(d: dict) -> str:
    slug = f"[[{d['slug']}]]" if d.get("slug") else "—"
    via  = d.get("accessed_via_linked_server") or "—"
    cs   = str(d.get("is_cross_server", False)).lower()
    method = d.get("access_method", "direct")
    return f"| {slug} | `{d['fqn']}` | {cs} | {via} | {method} |"


def _dep_table(deps: list[dict], label: str) -> str:
    lines = [
        f"### {label}\n",
        "| Slug | FQN | Cross-Server | Via Linked Server | Access Method |",
        "|------|-----|-------------|-------------------|---------------|",
    ]
    for d in deps:
        lines.append(_dep_row(d))
    return "\n".join(lines)


def _dep_obj(
    slug: str | None,
    fqn: str,
    is_cross_server: bool = False,
    accessed_via: str | None = None,
    method: str = "direct",
    unresolved: bool = False,
) -> dict:
    obj: dict[str, Any] = {
        "slug": slug,
        "fqn": fqn,
        "is_cross_server": is_cross_server,
        "accessed_via_linked_server": accessed_via,
        "access_method": method,
    }
    if unresolved:
        obj["unresolved"] = True
    return obj


def _col_type_str(col_type: str | None) -> str:
    return col_type or "UNKNOWN"


# ── Artifact 1: _index.md ─────────────────────────────────────────────────────


def render_index(ctx: dict, notes: str) -> str:
    """Render _index.md from context dict."""
    server   = ctx["server"]
    database = ctx["database"]
    ts       = ctx["last_full_extraction_at"]
    inventory: list[dict] = ctx["inventory"]
    archived_count: int   = ctx["archived_count"]

    counts = {
        "tables":     sum(1 for o in inventory if o["object_type"] == "table"),
        "views":      sum(1 for o in inventory if o["object_type"] == "view"),
        "procedures": sum(1 for o in inventory if o["object_type"] == "procedure"),
        "functions":  sum(1 for o in inventory if o["object_type"] == "function"),
        "triggers":   sum(1 for o in inventory if o["object_type"] == "trigger"),
        "jobs":       sum(1 for o in inventory if o["object_type"] == "job"),
        "archived":   archived_count,
    }

    inv_yaml = []
    for o in inventory:
        inv_yaml.append({
            "id":              DQ(o["id"]),
            "object_type":     o["object_type"],
            "sql_modify_date": DQ(o["sql_modify_date"]),
            "deprecated":      False,
        })

    yaml_data = {
        "id":                     DQ(f"{server}.{database}._index"),
        "object_type":            "manifest",
        "server":                 DQ(server),
        "database":               DQ(database),
        "last_full_extraction_at": DQ(ts),
        "object_counts":          counts,
        "inventory":              inv_yaml,
    }

    def _section(obj_type: str, header: str, cols: list[str]) -> str:
        items = [o for o in inventory if o["object_type"] == obj_type]
        if not items:
            return ""
        lines = [f"\n## {header}\n"]
        if obj_type == "job":
            lines.append("| Slug | Name | Last Modified | Deprecated |")
            lines.append("|------|------|---------------|------------|")
            for o in items:
                lines.append(
                    f"| [[{o['id']}]] | {o['name']} | {o['sql_modify_date']} | false |"
                )
        else:
            lines.append("| Slug | Schema | Name | Last Modified | Deprecated |")
            lines.append("|------|--------|------|---------------|------------|")
            for o in items:
                lines.append(
                    f"| [[{o['id']}]] | {o['schema']} | {o['name']} "
                    f"| {o['sql_modify_date']} | false |"
                )
        return "\n".join(lines)

    archived: list[dict] = ctx.get("archived_objects", [])

    body = f"# Índice: {database} — {server}\n"
    body += _section("table",     "Tables",     [])
    body += _section("view",      "Views",      [])
    body += _section("procedure", "Procedures", [])
    body += _section("function",  "Functions",  [])
    body += _section("trigger",   "Triggers",   [])
    body += _section("job",       "Jobs",       [])

    if archived:
        body += "\n## Archived\n\n"
        body += "| Slug | Type | Deprecated At |\n"
        body += "|------|------|---------------|\n"
        for a in archived:
            body += f"| [[{a['slug']}]] | {a['object_type']} | {a['deprecated_at']} |\n"

    body += "\n## Human Notes\n\n"
    body += inject_untagged(notes)
    body += "\n"

    return _front_matter(yaml_data) + "\n" + body


# ── Artifact 2: tables.md ────────────────────────────────────────────────────


def render_tables(tables_ctx: list[dict], notes_map: dict[str, str], file_notes: str) -> str:
    """Render tables.md from a list of per-table context dicts.

    notes_map: {slug -> preserved notes content}
    file_notes: file-level Human Notes content
    """
    if not tables_ctx:
        server   = "unknown"
        database = "unknown"
        ts       = ""
    else:
        server   = tables_ctx[0]["server"]
        database = tables_ctx[0]["database"]
        ts       = tables_ctx[0]["last_extracted_at"]

    tables_yaml = []
    for t in tables_ctx:
        slug = t["id"]
        hn = notes_map.get(slug, "")
        entry: dict[str, Any] = {
            "id":                  DQ(slug),
            "fqn":                 DQ(t["fqn"]),
            "object_type":         "table",
            "alias":               t.get("alias"),
            "server":              DQ(t["server"]),
            "database":            DQ(t["database"]),
            "schema":              t["schema"],
            "name":                t["name"],
            "last_extracted_at":   DQ(t["last_extracted_at"]),
            "sql_modify_date":     DQ(t["sql_modify_date"]),
            "description":         t.get("description"),
            "human_notes_present": has_content(hn),
            "deprecated":          t.get("deprecated", False),
            "deprecated_at":       t.get("deprecated_at"),
            "primary_key":         t.get("primary_key", []),
            "foreign_keys":        t.get("foreign_keys", []),
            "unique_constraints":  t.get("unique_constraints", []),
            "indexes":             t.get("indexes", []),
            "approximate_row_count": t.get("approximate_row_count", 0),
            "data_size_mb":        t.get("data_size_mb", 0.0),
            "read_by":             t.get("read_by", []),
            "written_by":          t.get("written_by", []),
            "dependencies": {
                "outbound": t.get("dependencies", {}).get("outbound", []),
                "inbound":  t.get("dependencies", {}).get("inbound", []),
            },
        }
        tables_yaml.append(entry)

    yaml_data = {
        "file_id":         DQ(f"{server}.{database}._tables"),
        "object_type":     "table_catalog",
        "server":          DQ(server),
        "database":        DQ(database),
        "last_extracted_at": DQ(ts),
        "tables":          tables_yaml,
    }

    body = f"# Catálogo de Tablas: {database} — {server}\n\n---\n"

    for t in tables_ctx:
        slug = t["id"]
        hn   = notes_map.get(slug, "")
        body += f"\n## {slug}\n\n"
        body += f"**FQN:** `{t['fqn']}`\n"
        desc = t.get("description") or ""
        body += f"**Descripción:** {desc}\n"

        # Columns table
        body += "\n### Columns\n\n"
        body += "| Column | Type | Nullable | Default | Notes |\n"
        body += "|--------|------|----------|---------|-------|\n"
        for col in t.get("columns", []):
            nullable = "YES" if col.get("is_nullable") else "NO"
            default  = col.get("column_default") or "—"
            notes_c  = col.get("column_notes") or ""
            body += (
                f"| {col['column_name']} | {col.get('sql_type','')} "
                f"| {nullable} | {default} | {notes_c} |\n"
            )

        # Dependencies
        out_deps = t.get("dependencies", {}).get("outbound", [])
        in_deps  = t.get("dependencies", {}).get("inbound",  [])

        body += "\n### Dependencies\n\n"
        body += "#### Outbound\n\n"
        body += "| Slug | FQN | Cross-Server | Via Linked Server | Access Method |\n"
        body += "|------|-----|-------------|-------------------|---------------|\n"
        for d in out_deps:
            body += _dep_row(d) + "\n"

        body += "\n#### Inbound\n\n"
        body += "| Slug | FQN | Cross-Server | Via Linked Server | Access Method |\n"
        body += "|------|-----|-------------|-------------------|---------------|\n"
        for d in in_deps:
            body += _dep_row(d) + "\n"

        # Per-table Human Notes
        body += "\n### Human Notes\n\n"
        body += inject_tagged(hn, slug) + "\n"
        body += "\n---\n"

    body += "\n## File-Level Human Notes\n\n"
    body += inject_untagged(file_notes) + "\n"

    return _front_matter(yaml_data) + "\n" + body


# ── Artifact 3: views/{slug}.md ──────────────────────────────────────────────


def render_view(ctx: dict, notes: str) -> str:
    yaml_data = {
        "id":                  DQ(ctx["id"]),
        "fqn":                 DQ(ctx["fqn"]),
        "object_type":         "view",
        "alias":               ctx.get("alias"),
        "server":              DQ(ctx["server"]),
        "database":            DQ(ctx["database"]),
        "schema":              ctx["schema"],
        "name":                ctx["name"],
        "last_extracted_at":   DQ(ctx["last_extracted_at"]),
        "sql_modify_date":     DQ(ctx["sql_modify_date"]),
        "description":         ctx.get("description"),
        "human_notes_present": has_content(notes),
        "deprecated":          ctx.get("deprecated", False),
        "deprecated_at":       ctx.get("deprecated_at"),
        "is_indexed":          ctx.get("is_indexed", False),
        "is_schemabound":      ctx.get("is_schemabound", False),
        "single_or_multi_source": ctx.get("single_or_multi_source", "single"),
        "reads_from":          ctx.get("reads_from", []),
        "read_by":             ctx.get("read_by", []),
        "dependencies": {
            "outbound": ctx.get("dependencies", {}).get("outbound", []),
            "inbound":  ctx.get("dependencies", {}).get("inbound", []),
        },
    }

    out_deps = ctx.get("dependencies", {}).get("outbound", [])
    in_deps  = ctx.get("dependencies", {}).get("inbound",  [])

    name = ctx["name"]
    fqn  = ctx["fqn"]
    desc = ctx.get("description") or ""
    src  = ctx.get("definition", "")

    body  = f"# {name}\n\n"
    body += f"**FQN:** `{fqn}`\n"
    body += f"**Descripción:** {desc}\n"
    body += "\n## Source\n\n~~~sql\n"
    body += src
    body += "\n~~~\n"
    body += "\n## Dependencies\n\n"
    body += _dep_table(out_deps, "Outbound") + "\n\n"
    body += _dep_table(in_deps,  "Inbound")  + "\n\n"
    body += "## Human Notes\n\n"
    body += inject_untagged(notes) + "\n"

    return _front_matter(yaml_data) + "\n" + body


# ── Artifact 4: procedures/{slug}.md ────────────────────────────────────────


def render_procedure(ctx: dict, notes: str) -> str:
    yaml_data = {
        "id":                  DQ(ctx["id"]),
        "fqn":                 DQ(ctx["fqn"]),
        "object_type":         "procedure",
        "alias":               ctx.get("alias"),
        "server":              DQ(ctx["server"]),
        "database":            DQ(ctx["database"]),
        "schema":              ctx["schema"],
        "name":                ctx["name"],
        "last_extracted_at":   DQ(ctx["last_extracted_at"]),
        "sql_modify_date":     DQ(ctx["sql_modify_date"]),
        "description":         ctx.get("description"),
        "human_notes_present": has_content(notes),
        "deprecated":          ctx.get("deprecated", False),
        "deprecated_at":       ctx.get("deprecated_at"),
        "primary_function":    ctx.get("primary_function"),
        "function_tags":       ctx.get("function_tags", []),
        "parameters":          ctx.get("parameters", []),
        "reads_from":          ctx.get("reads_from", []),
        "writes_to":           ctx.get("writes_to", []),
        "calls_procedures":    ctx.get("calls_procedures", []),
        "calls_functions":     ctx.get("calls_functions", []),
        "called_by_procedures": ctx.get("called_by_procedures", []),
        "invoked_by_jobs":     ctx.get("invoked_by_jobs", []),
        "dependencies": {
            "outbound": ctx.get("dependencies", {}).get("outbound", []),
            "inbound":  ctx.get("dependencies", {}).get("inbound",  []),
        },
    }

    out_deps = ctx.get("dependencies", {}).get("outbound", [])
    in_deps  = ctx.get("dependencies", {}).get("inbound",  [])

    name = ctx["name"]
    fqn  = ctx["fqn"]
    pf   = ctx.get("primary_function") or "—"
    desc = ctx.get("description") or ""
    src  = ctx.get("definition", "")

    body  = f"# {name}\n\n"
    body += f"**FQN:** `{fqn}`\n"
    body += f"**Función primaria:** {pf}\n"
    body += f"**Descripción:** {desc}\n"
    body += "\n## Source\n\n~~~sql\n"
    body += src
    body += "\n~~~\n"
    body += "\n## Dependencies\n\n"
    body += _dep_table(out_deps, "Outbound") + "\n\n"
    body += _dep_table(in_deps,  "Inbound")  + "\n\n"
    body += "## Human Notes\n\n"
    body += inject_untagged(notes) + "\n"

    return _front_matter(yaml_data) + "\n" + body


# ── Artifact 5: functions/{slug}.md ──────────────────────────────────────────


def render_function(ctx: dict, notes: str) -> str:
    yaml_data = {
        "id":                  DQ(ctx["id"]),
        "fqn":                 DQ(ctx["fqn"]),
        "object_type":         "function",
        "alias":               ctx.get("alias"),
        "server":              DQ(ctx["server"]),
        "database":            DQ(ctx["database"]),
        "schema":              ctx["schema"],
        "name":                ctx["name"],
        "last_extracted_at":   DQ(ctx["last_extracted_at"]),
        "sql_modify_date":     DQ(ctx["sql_modify_date"]),
        "description":         ctx.get("description"),
        "human_notes_present": has_content(notes),
        "deprecated":          ctx.get("deprecated", False),
        "deprecated_at":       ctx.get("deprecated_at"),
        "function_type":       ctx.get("function_type", "scalar"),
        "parameters":          ctx.get("parameters", []),
        "returns_type":        ctx.get("returns_type", "UNKNOWN"),
        "returns_table_schema": ctx.get("returns_table_schema"),
        "reads_from":          ctx.get("reads_from", []),
        "called_by_procedures": ctx.get("called_by_procedures", []),
        "called_by_views":     ctx.get("called_by_views", []),
        "called_by_functions": ctx.get("called_by_functions", []),
        "called_by_triggers":  ctx.get("called_by_triggers", []),
        "dependencies": {
            "outbound": ctx.get("dependencies", {}).get("outbound", []),
            "inbound":  ctx.get("dependencies", {}).get("inbound",  []),
        },
    }

    out_deps = ctx.get("dependencies", {}).get("outbound", [])
    in_deps  = ctx.get("dependencies", {}).get("inbound",  [])

    name = ctx["name"]
    fqn  = ctx["fqn"]
    ft   = ctx.get("function_type", "scalar")
    rt   = ctx.get("returns_type", "UNKNOWN")
    desc = ctx.get("description") or ""
    src  = ctx.get("definition", "")

    body  = f"# {name}\n\n"
    body += f"**FQN:** `{fqn}`\n"
    body += f"**Tipo:** {ft} | **Retorna:** {rt}\n"
    body += f"**Descripción:** {desc}\n"
    body += "\n## Source\n\n~~~sql\n"
    body += src
    body += "\n~~~\n"
    body += "\n## Dependencies\n\n"
    body += _dep_table(out_deps, "Outbound") + "\n\n"
    body += _dep_table(in_deps,  "Inbound")  + "\n\n"
    body += "## Human Notes\n\n"
    body += inject_untagged(notes) + "\n"

    return _front_matter(yaml_data) + "\n" + body


# ── Artifact 6: triggers/{slug}.md ───────────────────────────────────────────


def render_trigger(ctx: dict, notes: str) -> str:
    yaml_data = {
        "id":                  DQ(ctx["id"]),
        "fqn":                 DQ(ctx["fqn"]),
        "object_type":         "trigger",
        "alias":               ctx.get("alias"),
        "server":              DQ(ctx["server"]),
        "database":            DQ(ctx["database"]),
        "schema":              ctx["schema"],
        "name":                ctx["name"],
        "last_extracted_at":   DQ(ctx["last_extracted_at"]),
        "sql_modify_date":     DQ(ctx["sql_modify_date"]),
        "description":         ctx.get("description"),
        "human_notes_present": has_content(notes),
        "deprecated":          ctx.get("deprecated", False),
        "deprecated_at":       ctx.get("deprecated_at"),
        "attached_to_table":   DQ(ctx.get("attached_to_table", "")),
        "trigger_events":      ctx.get("trigger_events", []),
        "trigger_timing":      ctx.get("trigger_timing", "AFTER"),
        "is_enabled":          ctx.get("is_enabled", True),
        "reads_from":          ctx.get("reads_from", []),
        "writes_to":           ctx.get("writes_to", []),
        "calls_procedures":    ctx.get("calls_procedures", []),
        "calls_functions":     ctx.get("calls_functions", []),
        "dependencies": {
            "outbound": ctx.get("dependencies", {}).get("outbound", []),
            "inbound":  ctx.get("dependencies", {}).get("inbound",  []),
        },
    }

    out_deps = ctx.get("dependencies", {}).get("outbound", [])
    in_deps  = ctx.get("dependencies", {}).get("inbound",  [])

    name    = ctx["name"]
    fqn     = ctx["fqn"]
    att     = ctx.get("attached_to_table", "")
    events  = ", ".join(ctx.get("trigger_events", []))
    timing  = ctx.get("trigger_timing", "AFTER")
    desc    = ctx.get("description") or ""
    src     = ctx.get("definition", "")

    body  = f"# {name}\n\n"
    body += f"**FQN:** `{fqn}`\n"
    body += f"**Attached to:** [[{att}]] | **Events:** {events} | **Timing:** {timing}\n"
    body += f"**Descripción:** {desc}\n"
    body += "\n## Source\n\n~~~sql\n"
    body += src
    body += "\n~~~\n"
    body += "\n## Dependencies\n\n"
    body += _dep_table(out_deps, "Outbound") + "\n\n"
    body += _dep_table(in_deps,  "Inbound")  + "\n\n"
    body += "## Human Notes\n\n"
    body += inject_untagged(notes) + "\n"

    return _front_matter(yaml_data) + "\n" + body


# ── Artifact 7: jobs/{slug}.md ────────────────────────────────────────────────


def render_job(ctx: dict, notes: str) -> str:
    yaml_data = {
        "id":                  DQ(ctx["id"]),
        "fqn":                 DQ(ctx["fqn"]),
        "object_type":         "job",
        "alias":               ctx.get("alias"),
        "server":              DQ(ctx["server"]),
        "database":            "msdb",
        "schema":              "dbo",
        "name":                ctx["name"],
        "last_extracted_at":   DQ(ctx["last_extracted_at"]),
        "sql_modify_date":     DQ(ctx["sql_modify_date"]),
        "description":         ctx.get("description"),
        "human_notes_present": has_content(notes),
        "deprecated":          ctx.get("deprecated", False),
        "deprecated_at":       ctx.get("deprecated_at"),
        "is_enabled":          ctx.get("is_enabled", True),
        "owner":               ctx.get("owner", ""),
        "category":            ctx.get("category", ""),
        "schedules":           ctx.get("schedules", []),
        "steps":               ctx.get("steps", []),
        "invokes_procedures":  ctx.get("invokes_procedures", []),
        "last_run_outcome":    ctx.get("last_run_outcome", "Unknown"),
        "last_run_date":       ctx.get("last_run_date"),
        "dependencies": {
            "outbound": ctx.get("dependencies", {}).get("outbound", []),
            "inbound":  [],
        },
    }

    out_deps = ctx.get("dependencies", {}).get("outbound", [])

    name    = ctx["name"]
    fqn     = ctx["fqn"]
    desc    = ctx.get("description") or ""
    enabled = "Habilitado" if ctx.get("is_enabled") else "Deshabilitado"
    outcome = ctx.get("last_run_outcome", "Unknown")
    lrd     = ctx.get("last_run_date") or "—"
    steps   = ctx.get("steps", [])

    body  = f"# {name}\n\n"
    body += f"**FQN:** `{fqn}`\n"
    body += f"**Descripción:** {desc}\n"
    body += f"**Estado:** {enabled} | **Último resultado:** {outcome} ({lrd})\n"

    body += "\n## Steps\n\n"
    body += "| Order | Name | Subsystem | References | On Success | On Failure |\n"
    body += "|-------|------|-----------|------------|------------|------------|\n"
    for s in steps:
        ref_slug = s.get("references_object_slug")
        ref = f"[[{ref_slug}]]" if ref_slug else "—"
        on_s = s.get("on_success_action", "")
        on_s_step = s.get("on_success_step")
        if on_s == "GoToStep" and on_s_step is not None:
            on_s += f" {on_s_step}"
        on_f = s.get("on_failure_action", "")
        on_f_step = s.get("on_failure_step")
        if on_f == "GoToStep" and on_f_step is not None:
            on_f += f" {on_f_step}"
        body += (
            f"| {s.get('order','')} | {s.get('step_name','')} | "
            f"{s.get('subsystem','')} | {ref} | {on_s} | {on_f} |\n"
        )

    body += "\n## Dependencies\n\n"
    body += _dep_table(out_deps, "Outbound") + "\n\n"
    body += "### Inbound\n\n"
    body += "| Slug | FQN | Cross-Server | Via Linked Server | Access Method |\n"
    body += "|------|-----|-------------|-------------------|---------------|\n"
    body += "\n## Human Notes\n\n"
    body += inject_untagged(notes) + "\n"

    return _front_matter(yaml_data) + "\n" + body
