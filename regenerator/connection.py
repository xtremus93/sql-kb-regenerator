"""pyodbc connection factory with ODBC Driver 18/17 fallback and Windows Authentication."""

from __future__ import annotations

import pyodbc

from regenerator.logger import Logger

# Try Driver 18 first, fall back to 17 per spec constraints.
_DRIVERS = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
]


def get_connection(server: str, logger: Logger) -> pyodbc.Connection | None:
    """Return an open pyodbc connection using Windows Authentication.

    Tries ODBC Driver 18 first, then 17.  Returns None if both fail,
    after logging a [WARN] per Section 3.7.
    """
    last_error: Exception | None = None
    for driver in _DRIVERS:
        conn_str = (
            f"Driver={{{driver}}};"
            f"Server={server};"
            "Trusted_Connection=yes;"
            "TrustServerCertificate=yes;"  # required by Driver 18 when cert is self-signed
        )
        try:
            conn = pyodbc.connect(conn_str, timeout=30)
            return conn
        except pyodbc.Error as exc:
            last_error = exc

    logger.warn(
        f"Cannot connect to {server}: {last_error}. "
        f"Both '{_DRIVERS[0]}' and '{_DRIVERS[1]}' failed."
    )
    return None


def validate_select(conn: pyodbc.Connection) -> bool:
    """Execute SELECT 1 to confirm the connection is alive."""
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        return True
    except pyodbc.Error:
        return False
