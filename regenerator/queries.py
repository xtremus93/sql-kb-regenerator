"""All SQL extraction query functions — one function per spec Section 2 group (a through j).

Every database-scoped query is executed via sp_executesql with the database name
passed as a pyodbc parameter, satisfying the parameterised-call constraint.
Job queries (group g) and linked-server queries (group j) are server-scoped and
run directly without a USE-context wrapper.
"""

from __future__ import annotations

import pyodbc
from typing import Any


# ── Internal helpers ──────────────────────────────────────────────────────────


def _rows(cursor: pyodbc.Cursor) -> list[dict[str, Any]]:
    """Convert cursor result set to list[dict]."""
    if cursor.description is None:
        return []
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _exec_in_db(
    conn: pyodbc.Connection, inner_sql: str, database_name: str
) -> list[dict[str, Any]]:
    """Execute inner_sql in the context of database_name via sp_executesql.

    The database name is passed as a parameterised pyodbc value (?) and
    embedded inside the dynamic SQL string sent to sp_executesql, so it is
    never string-interpolated into the Python SQL template.
    """
    # The outer batch takes @db_name as a pyodbc ? parameter, builds a
    # dynamic SQL string that starts with USE [<db>], then executes it.
    outer = (
        "DECLARE @_db NVARCHAR(128) = ?;\n"
        "DECLARE @_sql NVARCHAR(MAX) = N'USE [' + @_db + N'];' + ?;\n"
        "EXEC sp_executesql @_sql;"
    )
    cursor = conn.cursor()
    cursor.execute(outer, (database_name, inner_sql))
    return _rows(cursor)


def _exec_direct(conn: pyodbc.Connection, sql: str) -> list[dict[str, Any]]:
    """Execute a server-scoped query directly (no database context switch)."""
    cursor = conn.cursor()
    cursor.execute(sql)
    return _rows(cursor)


# ── (a) Inventory ─────────────────────────────────────────────────────────────


def get_inventory(conn: pyodbc.Connection, database_name: str) -> list[dict[str, Any]]:
    """Section 2 group (a): full object inventory for one database."""
    sql = r"""
SELECT
    LOWER(@@SERVERNAME)
        + '.' + LOWER(DB_NAME())
        + '.' + LOWER(s.name)
        + '.' + LOWER(o.name)                  AS id,
    CASE o.type
        WHEN 'U'  THEN 'table'
        WHEN 'V'  THEN 'view'
        WHEN 'P'  THEN 'procedure'
        WHEN 'FN' THEN 'function'
        WHEN 'IF' THEN 'function'
        WHEN 'TF' THEN 'function'
        WHEN 'TR' THEN 'trigger'
    END                                         AS object_type,
    s.name                                      AS [schema],
    o.name                                      AS [name],
    o.type                                      AS type_code,
    CONVERT(NVARCHAR(30), o.modify_date, 126) + 'Z' AS sql_modify_date,
    CAST(0 AS BIT)                              AS deprecated
FROM sys.objects o
JOIN sys.schemas s ON o.schema_id = s.schema_id
WHERE o.type IN ('U','V','P','FN','IF','TF','TR')
  AND o.is_ms_shipped = 0
ORDER BY object_type, s.name, o.name;
"""
    return _exec_in_db(conn, sql, database_name)


# ── (b) Tables ────────────────────────────────────────────────────────────────


def get_table_metadata(conn: pyodbc.Connection, database_name: str) -> dict[str, list[dict]]:
    """Section 2 group (b): B1–B6 for tables."""

    # B1: table list
    b1 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(t.name)
                                                     AS id,
    '[' + @@SERVERNAME + '].[' + DB_NAME() + '].[' + s.name + '].[' + t.name + ']'
                                                     AS fqn,
    s.name                                           AS [schema],
    t.name                                           AS [name],
    CONVERT(NVARCHAR(30), o.modify_date, 126) + 'Z' AS sql_modify_date
FROM sys.tables t
JOIN sys.objects o  ON t.object_id = o.object_id
JOIN sys.schemas s  ON t.schema_id = s.schema_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name;
""", database_name)

    # B2: columns
    b2 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(t.name)
                                                     AS table_id,
    c.column_id                                      AS column_order,
    c.name                                           AS column_name,
    UPPER(tp.name)
        + CASE
            WHEN tp.name IN ('nvarchar','varchar','char','nchar')
                THEN '(' + CASE c.max_length WHEN -1 THEN 'MAX'
                           ELSE CAST(
                              CASE WHEN tp.name IN ('nvarchar','nchar')
                                   THEN c.max_length/2
                                   ELSE c.max_length END
                           AS NVARCHAR(10)) END + ')'
            WHEN tp.name IN ('decimal','numeric')
                THEN '(' + CAST(c.precision AS NVARCHAR(5)) + ',' + CAST(c.scale AS NVARCHAR(5)) + ')'
            ELSE ''
          END                                        AS sql_type,
    c.is_nullable                                    AS is_nullable,
    OBJECT_DEFINITION(c.default_object_id)           AS column_default,
    ep.value                                         AS column_notes
FROM sys.tables t
JOIN sys.objects  o  ON t.object_id = o.object_id
JOIN sys.schemas  s  ON t.schema_id = s.schema_id
JOIN sys.columns  c  ON t.object_id = c.object_id
JOIN sys.types   tp  ON c.user_type_id = tp.user_type_id
LEFT JOIN sys.extended_properties ep
    ON  ep.major_id   = c.object_id
    AND ep.minor_id   = c.column_id
    AND ep.class      = 1
    AND ep.name       = N'MS_Description'
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, c.column_id;
""", database_name)

    # B3: primary keys
    b3 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(t.name)
                                                     AS table_id,
    kc.name                                          AS pk_name,
    c.name                                           AS pk_column,
    ic.key_ordinal                                   AS key_ordinal
FROM sys.tables t
JOIN sys.objects       o  ON t.object_id = o.object_id
JOIN sys.schemas       s  ON t.schema_id = s.schema_id
JOIN sys.key_constraints kc
    ON kc.parent_object_id = t.object_id
    AND kc.type = 'PK'
JOIN sys.index_columns ic ON ic.object_id = t.object_id AND ic.index_id = kc.unique_index_id
JOIN sys.columns       c  ON c.object_id = t.object_id  AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, ic.key_ordinal;
""", database_name)

    # B4: foreign keys
    b4 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(ps.name) + '.' + LOWER(pt.name)
                                                     AS table_id,
    fk.name                                          AS fk_name,
    pc.name                                          AS fk_column,
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(rs.name) + '.' + LOWER(rt.name)
                                                     AS references_slug,
    rc.name                                          AS references_column
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables  pt ON pt.object_id = fk.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id   AND pc.column_id = fkc.parent_column_id
JOIN sys.tables  rt ON rt.object_id = fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
ORDER BY ps.name, pt.name, fk.name;
""", database_name)

    # B5: unique constraints
    b5 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(t.name)
                                                     AS table_id,
    kc.name                                          AS uc_name,
    c.name                                           AS uc_column,
    ic.key_ordinal                                   AS key_ordinal
FROM sys.tables t
JOIN sys.schemas        s  ON t.schema_id = s.schema_id
JOIN sys.key_constraints kc
    ON kc.parent_object_id = t.object_id
    AND kc.type = 'UQ'
JOIN sys.index_columns  ic ON ic.object_id = t.object_id AND ic.index_id = kc.unique_index_id
JOIN sys.columns        c  ON c.object_id  = t.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, kc.name, ic.key_ordinal;
""", database_name)

    # B6: indexes
    b6 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(t.name)
                                                     AS table_id,
    i.name                                           AS index_name,
    i.is_unique                                      AS is_unique,
    CAST(CASE WHEN i.type = 1 THEN 1 ELSE 0 END AS BIT) AS is_clustered,
    c.name                                           AS column_name,
    ic.key_ordinal                                   AS key_ordinal,
    ic.is_included_column                            AS is_included_column
FROM sys.tables t
JOIN sys.schemas        s  ON t.schema_id = s.schema_id
JOIN sys.indexes        i  ON i.object_id  = t.object_id AND i.type IN (1,2)
JOIN sys.index_columns  ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns        c  ON c.object_id  = t.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0
  AND i.is_hypothetical = 0
ORDER BY s.name, t.name, i.name, ic.is_included_column, ic.key_ordinal;
""", database_name)

    return {"b1": b1, "b2": b2, "b3": b3, "b4": b4, "b5": b5, "b6": b6}


# ── (c) Views ─────────────────────────────────────────────────────────────────


def get_views(conn: pyodbc.Connection, database_name: str) -> dict[str, list[dict]]:
    """Section 2 group (c): C1–C2 for views."""

    c1 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(v.name)
                                                      AS id,
    '[' + @@SERVERNAME + '].[' + DB_NAME() + '].[' + s.name + '].[' + v.name + ']'
                                                      AS fqn,
    s.name                                            AS [schema],
    v.name                                            AS [name],
    CONVERT(NVARCHAR(30), o.modify_date, 126) + 'Z'  AS sql_modify_date,
    m.is_schema_bound                                 AS is_schemabound,
    CAST(CASE WHEN EXISTS (
        SELECT 1 FROM sys.indexes i
        WHERE i.object_id = v.object_id AND i.type = 1
    ) THEN 1 ELSE 0 END AS BIT)                       AS is_indexed,
    m.definition                                      AS view_definition
FROM sys.views v
JOIN sys.objects     o  ON v.object_id = o.object_id
JOIN sys.schemas     s  ON v.schema_id = s.schema_id
JOIN sys.sql_modules m  ON v.object_id = m.object_id
WHERE v.is_ms_shipped = 0
ORDER BY s.name, v.name;
""", database_name)

    c2 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(rs.name) + '.' + LOWER(ro.name)
                                                      AS referencing_id,
    sed.referenced_server_name                        AS ref_server_name,
    sed.referenced_database_name                      AS ref_database_name,
    sed.referenced_schema_name                        AS ref_schema_name,
    sed.referenced_entity_name                        AS ref_object_name,
    sed.is_cross_server                               AS is_cross_server,
    CASE
        WHEN sed.is_cross_server = 1 THEN
            LOWER(ISNULL(srv.data_source, sed.referenced_server_name))
        ELSE LOWER(@@SERVERNAME)
    END + '.'
    + LOWER(ISNULL(sed.referenced_database_name, DB_NAME())) + '.'
    + LOWER(ISNULL(sed.referenced_schema_name, 'dbo'))    + '.'
    + LOWER(sed.referenced_entity_name)               AS referenced_slug,
    '[' + ISNULL(sed.referenced_server_name, @@SERVERNAME) + '].['
    + ISNULL(sed.referenced_database_name, DB_NAME()) + '].['
    + ISNULL(sed.referenced_schema_name, 'dbo')     + '].['
    + sed.referenced_entity_name                      + ']' AS referenced_fqn,
    srv.name                                          AS linked_server_alias
FROM sys.views v
JOIN sys.objects ro ON v.object_id = ro.object_id
JOIN sys.schemas rs ON v.schema_id = rs.schema_id
JOIN sys.sql_expression_dependencies sed
    ON sed.referencing_id = v.object_id
    AND sed.referenced_minor_id = 0
LEFT JOIN sys.servers srv
    ON srv.name = sed.referenced_server_name
WHERE v.is_ms_shipped = 0
ORDER BY referencing_id, referenced_slug;
""", database_name)

    return {"c1": c1, "c2": c2}


# ── (d) Procedures ────────────────────────────────────────────────────────────


def get_procedures(conn: pyodbc.Connection, database_name: str) -> dict[str, list[dict]]:
    """Section 2 group (d): D1–D3 for stored procedures."""

    d1 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(p.name)
                                                      AS id,
    '[' + @@SERVERNAME + '].[' + DB_NAME() + '].[' + s.name + '].[' + p.name + ']'
                                                      AS fqn,
    s.name                                            AS [schema],
    p.name                                            AS [name],
    CONVERT(NVARCHAR(30), o.modify_date, 126) + 'Z'  AS sql_modify_date,
    m.definition                                      AS proc_definition
FROM sys.procedures p
JOIN sys.objects     o  ON p.object_id = o.object_id
JOIN sys.schemas     s  ON p.schema_id = s.schema_id
JOIN sys.sql_modules m  ON p.object_id = m.object_id
WHERE p.is_ms_shipped = 0
ORDER BY s.name, p.name;
""", database_name)

    d2 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(pr.name)
                                                      AS proc_id,
    pa.parameter_id                                   AS param_order,
    pa.name                                           AS param_name,
    UPPER(tp.name)
        + CASE
            WHEN tp.name IN ('nvarchar','varchar','char','nchar')
                THEN '(' + CASE pa.max_length WHEN -1 THEN 'MAX'
                           ELSE CAST(
                              CASE WHEN tp.name IN ('nvarchar','nchar')
                                   THEN pa.max_length/2 ELSE pa.max_length END
                           AS NVARCHAR(10)) END + ')'
            WHEN tp.name IN ('decimal','numeric')
                THEN '(' + CAST(pa.precision AS NVARCHAR(5)) + ',' + CAST(pa.scale AS NVARCHAR(5)) + ')'
            ELSE ''
          END                                         AS sql_type,
    pa.is_output                                      AS is_output,
    CASE pa.is_output WHEN 1 THEN 'output' ELSE 'input' END AS direction
FROM sys.procedures pr
JOIN sys.schemas    s   ON pr.schema_id = s.schema_id
JOIN sys.parameters pa  ON pa.object_id = pr.object_id AND pa.parameter_id > 0
JOIN sys.types      tp  ON pa.user_type_id = tp.user_type_id
WHERE pr.is_ms_shipped = 0
ORDER BY proc_id, pa.parameter_id;
""", database_name)

    d3 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(rs.name) + '.' + LOWER(ro.name)
                                                      AS referencing_id,
    sed.referenced_server_name                        AS ref_server_name,
    sed.referenced_database_name                      AS ref_database_name,
    sed.referenced_schema_name                        AS ref_schema_name,
    sed.referenced_entity_name                        AS ref_object_name,
    ISNULL(robj.type, 'UNKNOWN')                      AS ref_type_code,
    sed.is_cross_server                               AS is_cross_server,
    CASE
        WHEN sed.is_cross_server = 1 THEN
            LOWER(ISNULL(srv.data_source, sed.referenced_server_name))
        ELSE LOWER(@@SERVERNAME)
    END + '.'
    + LOWER(ISNULL(sed.referenced_database_name, DB_NAME())) + '.'
    + LOWER(ISNULL(sed.referenced_schema_name, 'dbo'))     + '.'
    + LOWER(sed.referenced_entity_name)               AS referenced_slug,
    '[' + ISNULL(sed.referenced_server_name, @@SERVERNAME) + '].['
    + ISNULL(sed.referenced_database_name, DB_NAME()) + '].['
    + ISNULL(sed.referenced_schema_name, 'dbo')     + '].['
    + sed.referenced_entity_name + ']'               AS referenced_fqn,
    srv.name                                          AS linked_server_alias,
    CASE
        WHEN sed.is_cross_server = 1
            AND EXISTS (SELECT 1 FROM sys.servers s2 WHERE s2.name = sed.referenced_server_name
                        AND s2.is_linked = 1)
        THEN 'linked_server'
        ELSE 'direct'
    END                                               AS access_method_hint
FROM sys.procedures pr
JOIN sys.objects ro2 ON pr.object_id = ro2.object_id
JOIN sys.schemas  rs ON pr.schema_id = rs.schema_id
JOIN sys.sql_expression_dependencies sed ON sed.referencing_id = pr.object_id
    AND sed.referenced_minor_id = 0
LEFT JOIN sys.objects robj ON robj.object_id = sed.referenced_id
LEFT JOIN sys.servers srv  ON srv.name = sed.referenced_server_name
WHERE pr.is_ms_shipped = 0
ORDER BY referencing_id, referenced_slug;
""", database_name)

    return {"d1": d1, "d2": d2, "d3": d3}


# ── (e) Functions ─────────────────────────────────────────────────────────────


def get_functions(conn: pyodbc.Connection, database_name: str) -> dict[str, list[dict]]:
    """Section 2 group (e): E1–E4 for functions."""

    e1 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(o.name)
                                                      AS id,
    '[' + @@SERVERNAME + '].[' + DB_NAME() + '].[' + s.name + '].[' + o.name + ']'
                                                      AS fqn,
    s.name                                            AS [schema],
    o.name                                            AS [name],
    CASE o.type
        WHEN 'FN' THEN 'scalar'
        WHEN 'IF' THEN 'inline_tvf'
        WHEN 'TF' THEN 'multi_statement_tvf'
    END                                               AS function_type,
    CONVERT(NVARCHAR(30), o.modify_date, 126) + 'Z'  AS sql_modify_date,
    m.definition                                      AS func_definition
FROM sys.objects o
JOIN sys.schemas     s  ON o.schema_id = s.schema_id
JOIN sys.sql_modules m  ON o.object_id = m.object_id
WHERE o.type IN ('FN','IF','TF')
  AND o.is_ms_shipped = 0
ORDER BY s.name, o.name;
""", database_name)

    e2 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(o.name)
                                                      AS func_id,
    pa.parameter_id,
    pa.name                                           AS param_name,
    UPPER(tp.name)
        + CASE
            WHEN tp.name IN ('nvarchar','varchar','char','nchar')
                THEN '(' + CASE pa.max_length WHEN -1 THEN 'MAX'
                           ELSE CAST(
                              CASE WHEN tp.name IN ('nvarchar','nchar')
                                   THEN pa.max_length/2 ELSE pa.max_length END
                           AS NVARCHAR(10)) END + ')'
            WHEN tp.name IN ('decimal','numeric')
                THEN '(' + CAST(pa.precision AS NVARCHAR(5)) + ',' + CAST(pa.scale AS NVARCHAR(5)) + ')'
            ELSE ''
          END                                         AS sql_type,
    pa.is_output,
    CASE WHEN pa.parameter_id = 0 THEN 'return_value' ELSE 'input' END AS param_role
FROM sys.objects o
JOIN sys.schemas    s   ON o.schema_id = s.schema_id
JOIN sys.parameters pa  ON pa.object_id = o.object_id
JOIN sys.types      tp  ON pa.user_type_id = tp.user_type_id
WHERE o.type IN ('FN','IF','TF')
  AND o.is_ms_shipped = 0
ORDER BY func_id, pa.parameter_id;
""", database_name)

    e3 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(o.name)
                                                      AS func_id,
    c.column_id,
    c.name                                            AS column_name,
    UPPER(tp.name)
        + CASE
            WHEN tp.name IN ('nvarchar','varchar','char','nchar')
                THEN '(' + CASE c.max_length WHEN -1 THEN 'MAX'
                           ELSE CAST(
                              CASE WHEN tp.name IN ('nvarchar','nchar')
                                   THEN c.max_length/2 ELSE c.max_length END
                           AS NVARCHAR(10)) END + ')'
            WHEN tp.name IN ('decimal','numeric')
                THEN '(' + CAST(c.precision AS NVARCHAR(5)) + ',' + CAST(c.scale AS NVARCHAR(5)) + ')'
            ELSE ''
          END                                         AS sql_type,
    c.is_nullable
FROM sys.objects o
JOIN sys.schemas s ON o.schema_id = s.schema_id
JOIN sys.columns c ON c.object_id = o.object_id
JOIN sys.types  tp ON c.user_type_id = tp.user_type_id
WHERE o.type IN ('IF','TF')
  AND o.is_ms_shipped = 0
ORDER BY func_id, c.column_id;
""", database_name)

    e4 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(o.name)
                                                      AS referencing_id,
    sed.referenced_server_name,
    sed.referenced_database_name,
    sed.referenced_schema_name,
    sed.referenced_entity_name,
    ISNULL(robj.type, 'UNKNOWN')                      AS ref_type_code,
    sed.is_cross_server,
    CASE WHEN sed.is_cross_server = 1
         THEN LOWER(ISNULL(srv.data_source, sed.referenced_server_name))
         ELSE LOWER(@@SERVERNAME) END + '.'
    + LOWER(ISNULL(sed.referenced_database_name, DB_NAME())) + '.'
    + LOWER(ISNULL(sed.referenced_schema_name, 'dbo'))    + '.'
    + LOWER(sed.referenced_entity_name)               AS referenced_slug,
    '[' + ISNULL(sed.referenced_server_name, @@SERVERNAME) + '].['
    + ISNULL(sed.referenced_database_name, DB_NAME()) + '].['
    + ISNULL(sed.referenced_schema_name, 'dbo')     + '].['
    + sed.referenced_entity_name + ']'               AS referenced_fqn,
    srv.name                                          AS linked_server_alias
FROM sys.objects o
JOIN sys.schemas s ON o.schema_id = s.schema_id
JOIN sys.sql_expression_dependencies sed ON sed.referencing_id = o.object_id
    AND sed.referenced_minor_id = 0
LEFT JOIN sys.objects robj ON robj.object_id = sed.referenced_id
LEFT JOIN sys.servers srv  ON srv.name = sed.referenced_server_name
WHERE o.type IN ('FN','IF','TF')
  AND o.is_ms_shipped = 0
ORDER BY referencing_id, referenced_slug;
""", database_name)

    return {"e1": e1, "e2": e2, "e3": e3, "e4": e4}


# ── (f) Triggers ──────────────────────────────────────────────────────────────


def get_triggers(conn: pyodbc.Connection, database_name: str) -> dict[str, list[dict]]:
    """Section 2 group (f): F1–F3 for DML triggers."""

    f1 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(os.name) + '.' + LOWER(tr.name)
                                                      AS id,
    '[' + @@SERVERNAME + '].[' + DB_NAME() + '].[' + os.name + '].[' + tr.name + ']'
                                                      AS fqn,
    os.name                                           AS [schema],
    tr.name                                           AS [name],
    CONVERT(NVARCHAR(30), o.modify_date, 126) + 'Z'  AS sql_modify_date,
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(ps.name) + '.' + LOWER(pt.name)
                                                      AS attached_to_table,
    '[' + @@SERVERNAME + '].[' + DB_NAME() + '].[' + ps.name + '].[' + pt.name + ']'
                                                      AS attached_to_fqn,
    tr.is_instead_of_trigger,
    CAST(CASE WHEN tr.is_disabled = 0 THEN 1 ELSE 0 END AS BIT) AS is_enabled,
    m.definition                                      AS trigger_definition
FROM sys.triggers tr
JOIN sys.objects  o   ON tr.object_id = o.object_id
JOIN sys.objects  pt  ON pt.object_id = tr.parent_id
JOIN sys.schemas  ps  ON ps.schema_id = pt.schema_id
JOIN sys.schemas  os  ON os.schema_id = o.schema_id
JOIN sys.sql_modules m ON tr.object_id = m.object_id
WHERE tr.parent_class = 1
  AND o.is_ms_shipped = 0
ORDER BY os.name, tr.name;
""", database_name)

    f2 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(os.name) + '.' + LOWER(tr.name)
                                                      AS trigger_id,
    te.type_desc                                      AS trigger_event
FROM sys.triggers    tr
JOIN sys.objects     o  ON tr.object_id = o.object_id
JOIN sys.schemas    os  ON os.schema_id = o.schema_id
JOIN sys.trigger_events te ON te.object_id = tr.object_id
WHERE tr.parent_class = 1
  AND o.is_ms_shipped = 0
ORDER BY trigger_id, te.type_desc;
""", database_name)

    f3 = _exec_in_db(conn, r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(os.name) + '.' + LOWER(tr.name)
                                                      AS referencing_id,
    sed.referenced_server_name,
    sed.referenced_database_name,
    sed.referenced_schema_name,
    sed.referenced_entity_name,
    ISNULL(robj.type, 'UNKNOWN')                      AS ref_type_code,
    sed.is_cross_server,
    CASE WHEN sed.is_cross_server = 1
         THEN LOWER(ISNULL(srv.data_source, sed.referenced_server_name))
         ELSE LOWER(@@SERVERNAME) END + '.'
    + LOWER(ISNULL(sed.referenced_database_name, DB_NAME())) + '.'
    + LOWER(ISNULL(sed.referenced_schema_name, 'dbo'))    + '.'
    + LOWER(sed.referenced_entity_name)               AS referenced_slug,
    '[' + ISNULL(sed.referenced_server_name, @@SERVERNAME) + '].['
    + ISNULL(sed.referenced_database_name, DB_NAME()) + '].['
    + ISNULL(sed.referenced_schema_name, 'dbo')     + '].['
    + sed.referenced_entity_name + ']'               AS referenced_fqn,
    srv.name                                          AS linked_server_alias
FROM sys.triggers tr
JOIN sys.objects  o  ON tr.object_id = o.object_id
JOIN sys.schemas os  ON os.schema_id = o.schema_id
JOIN sys.sql_expression_dependencies sed ON sed.referencing_id = tr.object_id
    AND sed.referenced_minor_id = 0
LEFT JOIN sys.objects robj ON robj.object_id = sed.referenced_id
LEFT JOIN sys.servers srv  ON srv.name = sed.referenced_server_name
WHERE tr.parent_class = 1
  AND o.is_ms_shipped = 0
ORDER BY referencing_id, referenced_slug;
""", database_name)

    return {"f1": f1, "f2": f2, "f3": f3}


# ── (g) Jobs ──────────────────────────────────────────────────────────────────


def get_jobs(conn: pyodbc.Connection) -> dict[str, list[dict]]:
    """Section 2 group (g): G1–G4 for SQL Agent jobs (queries msdb directly)."""

    g1 = _exec_direct(conn, """
SELECT
    LOWER(@@SERVERNAME) + '.msdb.dbo.' + LOWER(REPLACE(j.name, ' ', '_'))
                                                      AS id,
    '[' + @@SERVERNAME + '].[msdb].[dbo].[' + j.name + ']'
                                                      AS fqn,
    j.name                                            AS job_name,
    j.enabled                                         AS is_enabled,
    SUSER_SNAME(j.owner_sid)                          AS owner,
    c.name                                            AS category,
    CONVERT(NVARCHAR(30), j.date_modified, 126) + 'Z' AS sql_modify_date,
    j.description                                     AS job_description
FROM msdb.dbo.sysjobs j
JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id
ORDER BY j.name;
""")

    g2 = _exec_direct(conn, """
SELECT
    LOWER(@@SERVERNAME) + '.msdb.dbo.' + LOWER(REPLACE(j.name, ' ', '_'))
                                                      AS job_id,
    s.step_id                                         AS step_order,
    s.step_name,
    s.subsystem,
    s.database_name,
    s.command,
    CASE s.on_success_action
        WHEN 1 THEN 'QuitWithSuccess'
        WHEN 2 THEN 'QuitWithFailure'
        WHEN 3 THEN 'GoToNextStep'
        WHEN 4 THEN 'GoToStep'
    END                                               AS on_success_action,
    CASE WHEN s.on_success_action = 4 THEN s.on_success_step_id ELSE NULL END
                                                      AS on_success_step,
    CASE s.on_fail_action
        WHEN 1 THEN 'QuitWithSuccess'
        WHEN 2 THEN 'QuitWithFailure'
        WHEN 3 THEN 'GoToNextStep'
        WHEN 4 THEN 'GoToStep'
    END                                               AS on_failure_action,
    CASE WHEN s.on_fail_action = 4 THEN s.on_fail_step_id ELSE NULL END
                                                      AS on_failure_step
FROM msdb.dbo.sysjobsteps s
JOIN msdb.dbo.sysjobs      j ON j.job_id = s.job_id
ORDER BY job_id, s.step_id;
""")

    g3 = _exec_direct(conn, """
SELECT
    LOWER(@@SERVERNAME) + '.msdb.dbo.' + LOWER(REPLACE(j.name, ' ', '_'))
                                                      AS job_id,
    sc.name                                           AS schedule_name,
    CASE sc.freq_type
        WHEN   1 THEN 'Once'
        WHEN   4 THEN 'Daily'
        WHEN   8 THEN 'Weekly'
        WHEN  16 THEN 'Monthly'
        WHEN  64 THEN 'AgentStart'
        WHEN 128 THEN 'IdleCPU'
        ELSE CAST(sc.freq_type AS NVARCHAR(10))
    END                                               AS frequency_type,
    sc.freq_interval,
    RIGHT('000000' + CAST(sc.active_start_time AS NVARCHAR(6)), 6)
                                                      AS active_start_time
FROM msdb.dbo.sysjobschedules  js
JOIN msdb.dbo.sysschedules      sc ON sc.schedule_id = js.schedule_id
JOIN msdb.dbo.sysjobs            j ON  j.job_id       = js.job_id
ORDER BY job_id, sc.name;
""")

    g4 = _exec_direct(conn, """
SELECT
    LOWER(@@SERVERNAME) + '.msdb.dbo.' + LOWER(REPLACE(j.name, ' ', '_'))
                                                      AS job_id,
    CASE h.run_status
        WHEN 0 THEN 'Failed'
        WHEN 1 THEN 'Succeeded'
        WHEN 3 THEN 'Canceled'
        ELSE 'Unknown'
    END                                               AS last_run_outcome,
    CONVERT(NVARCHAR(8), h.run_date) + ' '
    + RIGHT('000000' + CAST(h.run_time AS NVARCHAR(6)), 6)
                                                      AS last_run_datetime_local
FROM msdb.dbo.sysjobs j
OUTER APPLY (
    SELECT TOP 1 run_status, run_date, run_time
    FROM msdb.dbo.sysjobhistory
    WHERE job_id = j.job_id
      AND step_id = 0
    ORDER BY instance_id DESC
) h
ORDER BY job_id;
""")

    return {"g1": g1, "g2": g2, "g3": g3, "g4": g4}


# ── (h) Extended Properties ────────────────────────────────────────────────────


def get_extended_properties(
    conn: pyodbc.Connection, database_name: str
) -> list[dict[str, Any]]:
    """Section 2 group (h): MS_Description at object and column level."""
    sql = r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(o.name)
                                                      AS object_id_slug,
    'object'                                          AS ep_level,
    NULL                                              AS column_name,
    CAST(ep.value AS NVARCHAR(4000))                  AS description
FROM sys.objects o
JOIN sys.schemas s ON o.schema_id = s.schema_id
JOIN sys.extended_properties ep
    ON  ep.major_id = o.object_id
    AND ep.minor_id = 0
    AND ep.class    = 1
    AND ep.name     = N'MS_Description'
WHERE o.type IN ('U','V','P','FN','IF','TF','TR')
  AND o.is_ms_shipped = 0

UNION ALL

SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(o.name)
                                                      AS object_id_slug,
    'column'                                          AS ep_level,
    c.name                                            AS column_name,
    CAST(ep.value AS NVARCHAR(4000))                  AS description
FROM sys.objects o
JOIN sys.schemas s  ON o.schema_id = s.schema_id
JOIN sys.columns c  ON c.object_id = o.object_id
JOIN sys.extended_properties ep
    ON  ep.major_id = o.object_id
    AND ep.minor_id = c.column_id
    AND ep.class    = 1
    AND ep.name     = N'MS_Description'
WHERE o.type IN ('U','V','TR')
  AND o.is_ms_shipped = 0
ORDER BY object_id_slug, ep_level, column_name;
"""
    return _exec_in_db(conn, sql, database_name)


# ── (i) Volumetry ─────────────────────────────────────────────────────────────


def get_volumetry(conn: pyodbc.Connection, database_name: str) -> list[dict[str, Any]]:
    """Section 2 group (i): row counts and storage sizes for tables."""
    sql = r"""
SELECT
    LOWER(@@SERVERNAME) + '.' + LOWER(DB_NAME()) + '.' + LOWER(s.name) + '.' + LOWER(t.name)
                                                      AS table_id,
    SUM(p.row_count)                                  AS approximate_row_count,
    CAST(SUM(p.used_page_count) * 8.0 / 1024.0 AS DECIMAL(18,2))
                                                      AS data_size_mb
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.dm_db_partition_stats p
    ON p.object_id = t.object_id
    AND p.index_id IN (0, 1)
WHERE t.is_ms_shipped = 0
GROUP BY s.name, t.name
ORDER BY table_id;
"""
    return _exec_in_db(conn, sql, database_name)


# ── (j) Cross-Server Reference Resolution ────────────────────────────────────


def get_linked_servers(conn: pyodbc.Connection) -> list[dict[str, Any]]:
    """Section 2 group (j): linked server alias → real server address mapping."""
    return _exec_direct(conn, """
SELECT
    srv.server_id,
    srv.name                                          AS linked_server_alias,
    srv.data_source                                   AS real_server_address,
    LOWER(srv.data_source)                            AS real_server_slug_prefix,
    srv.product,
    srv.provider,
    srv.is_linked,
    srv.is_remote_login_enabled,
    srv.modify_date
FROM sys.servers srv
WHERE srv.is_linked = 1
ORDER BY srv.name;
""")
