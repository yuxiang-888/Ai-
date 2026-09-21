"""Database access for the suchen/FULIN web service.

Two backends are supported so the same code can run on a laptop and on a
free cloud host:

* **SQLite** (default) -- local desktop / portable deployment.
* **PostgreSQL** -- set ``DATABASE_URL`` (or ``FULIN_DATABASE_URL``) to a
  ``postgres://`` / ``postgresql://`` connection string. This is what the
  free-tier cloud deployment uses, because serverless hosts have no writable
  disk for a SQLite file.

Usage::

    from .db import connect

    with connect() as connection:
        rows = connection.fetch_all("SELECT * FROM users WHERE email = ?", (email,))

Placeholders are always written with ``?`` (SQLite style). On PostgreSQL they
are rewritten to ``%s`` automatically.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

ROOT = Path(__file__).resolve().parents[2]

SQLITE_PATH = Path(
    os.getenv("FULIN_DATABASE", ROOT / "data" / "fulin_editor.sqlite3")
).resolve()

DATABASE_URL = (
    os.getenv("DATABASE_URL", "").strip()
    or os.getenv("FULIN_DATABASE_URL", "").strip()
)


def backend() -> str:
    """Return ``postgres`` or ``sqlite``."""
    return "postgres" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite"


def _rewrite(sql: str) -> str:
    if backend() == "sqlite":
        return sql
    return sql.replace("?", "%s")


class CompatCursor:
    """Cursor wrapper that always yields dict rows."""

    def __init__(self, cursor: Any, *, postgres: bool) -> None:
        self._cursor = cursor
        self._postgres = postgres

    def fetchone(self) -> dict | None:
        row = self._cursor.fetchone()
        if row is None:
            return None
        return dict(row) if self._postgres else row

    def fetchall(self) -> list[dict]:
        rows = self._cursor.fetchall()
        return [dict(row) for row in rows] if self._postgres else list(rows)

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount or 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class CompatConnection:
    """Thin adapter exposing the small subset of DB-API that the app uses."""

    def __init__(self, connection: Any, *, postgres: bool) -> None:
        self._connection = connection
        self._postgres = postgres

    @property
    def raw(self) -> Any:
        return self._connection

    @property
    def postgres(self) -> bool:
        return self._postgres

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> CompatCursor:
        cursor = self._connection.execute(_rewrite(sql), params)
        return CompatCursor(cursor, postgres=self._postgres)

    def executescript(self, script: str) -> None:
        """Run a DDL script on either backend.

        PostgreSQL has no ``executescript``; the schema scripts only contain
        plain statements separated by semicolons, so splitting is safe here.
        """
        for statement in script.split(";"):
            stripped = statement.strip()
            if not stripped:
                continue
            self._connection.execute(_rewrite(stripped))
        if not self._postgres:
            self._connection.commit()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def table_columns(self, table: str) -> set[str]:
        """Return the column names of an existing table (empty set if absent)."""
        if self._postgres:
            rows = self.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                (table,),
            ).fetchall()
            return {str(row["column_name"]) for row in rows}
        rows = self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {row[1] for row in rows}


@contextmanager
def connect() -> Iterator[CompatConnection]:
    """Open a connection. SQLite files are created on demand."""
    if backend() == "postgres":
        import psycopg  # type: ignore[import-not-found]

        connection = psycopg.connect(DATABASE_URL)
        try:
            connection.autocommit = False
            yield CompatConnection(connection, postgres=True)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return

    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(SQLITE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield CompatConnection(connection, postgres=False)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def describe() -> dict[str, Any]:
    """Human readable backend description for the health endpoint."""
    if backend() == "postgres":
        safe = DATABASE_URL
        if "@" in safe:
            safe = safe.split("@", 1)[1]
        return {"backend": "postgres", "target": safe, "file": None}
    return {"backend": "sqlite", "target": str(SQLITE_PATH), "file": str(SQLITE_PATH)}
