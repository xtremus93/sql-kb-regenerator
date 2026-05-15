"""Entry point: python -m regenerator

CLI arguments:
  --server SERVER       SQL Server instance name (repeatable; paired with --database)
  --database DATABASE   Target database name (repeatable; paired with --server)
  --mode {full,incremental}
  --output-root PATH

The Nth --server is paired with the Nth --database.
Mismatched counts abort immediately before any connection is attempted.
"""

from __future__ import annotations

import argparse
import sys
import datetime
from pathlib import Path

from regenerator.logger import Logger
from regenerator.connection import get_connection, validate_select
from regenerator.algorithm import run


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m regenerator",
        description="SQL Server Knowledge-Base Regenerator",
    )
    parser.add_argument(
        "--server",
        action="append",
        dest="servers",
        metavar="SERVER",
        required=True,
        help="SQL Server instance name (repeatable; paired with --database)",
    )
    parser.add_argument(
        "--database",
        action="append",
        dest="databases",
        metavar="DATABASE",
        required=True,
        help="Target database name (repeatable; paired with --server)",
    )
    parser.add_argument(
        "--mode",
        choices=["full", "incremental"],
        required=True,
        help="full = reclassify all UNCHANGED as MODIFIED; incremental = skip UNCHANGED",
    )
    parser.add_argument(
        "--output-root",
        required=True,
        metavar="PATH",
        help="Root directory for all output files",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    servers:   list[str] = args.servers
    databases: list[str] = args.databases

    # Validate pairing before touching any server (Section 3 — argparse constraint)
    if len(servers) != len(databases):
        print(
            f"[ERROR] Mismatched --server / --database count: "
            f"{len(servers)} server(s) vs {len(databases)} database(s). "
            f"Each --server must be paired with exactly one --database.",
            file=sys.stderr,
        )
        return 1

    targets   = list(zip(servers, databases))
    output_root = Path(args.output_root)
    mode      = args.mode
    logger    = Logger()

    # Step 1: record wall-clock UTC timestamp at run start (before any queries)
    wall_clock_start = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Establish connections — one per unique server
    unique_servers  = list(dict.fromkeys(s.lower() for s, _ in targets))
    connections: dict[str, object] = {}

    for srv in unique_servers:
        conn = get_connection(srv, logger)
        if conn is None:
            # get_connection already logged [WARN]
            # Log warn for each database under this server
            for s, d in targets:
                if s.lower() == srv:
                    logger.warn(
                        f"Cannot connect to {srv}: skipping {srv}/{d.lower()}."
                    )
            continue

        if not validate_select(conn):
            logger.warn(
                f"Connection to {srv} established but SELECT 1 failed. "
                f"Skipping all databases under {srv}."
            )
            conn.close()
            continue

        connections[srv] = conn
        logger.info(f"Step 1 complete: connected to {srv}")

    logger.info(f"Step 1 complete: {len(connections)}/{len(unique_servers)} servers connected")

    # Run the 8-step pipeline
    any_error = run(
        targets=targets,
        mode=mode,
        output_root=output_root,
        connections=connections,  # type: ignore[arg-type]
        logger=logger,
        wall_clock_start=wall_clock_start,
    )

    # Close all connections
    for conn in connections.values():
        try:
            conn.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    # Emit final summary
    logger.summary()

    # Exit code 0 only if no warnings or errors (Section — success criteria)
    if any_error or logger.has_issues:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
