"""
SQLite metadata database — stores etl_run_log only.

Separate from DuckDB so run-log writes never block analytical data writes.
SQLite WAL mode allows concurrent readers + 1 writer with no blocking.
"""

import asyncio
import sqlite3
import os
from datetime import datetime, timezone
import structlog

log = structlog.get_logger()

_META_DB_PATH: str = ""


def _get_path() -> str:
    if _META_DB_PATH:
        return _META_DB_PATH
    duckdb_path = os.environ.get("DUCKDB_PATH", "/data/sp_api.duckdb")
    return duckdb_path.replace(".duckdb", "_meta.db")


def _new_conn() -> sqlite3.Connection:
    path = _get_path()
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent readers
    conn.execute("PRAGMA synchronous=NORMAL") # fast writes
    conn.row_factory = sqlite3.Row
    return conn


def init_meta_db() -> None:
    """Create the etl_run_log table if it doesn't exist. Called at startup."""
    conn = _new_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS etl_run_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                pipeline      TEXT NOT NULL,
                started_at    TEXT NOT NULL,
                finished_at   TEXT,
                status        TEXT NOT NULL DEFAULT 'running',
                rows_written  INTEGER,
                error_message TEXT,
                params_json   TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_run_log_pipeline ON etl_run_log(pipeline, started_at DESC)")
        conn.commit()
        log.info("sqlite_meta.schema_ready", path=_get_path())
    finally:
        conn.close()


# ─── Async helpers (all run in thread pool) ──────────────────────────────────

async def insert_run(pipeline: str, params_json: str) -> int:
    def _do():
        conn = _new_conn()
        try:
            cur = conn.execute("""
                INSERT INTO etl_run_log (pipeline, started_at, status, params_json)
                VALUES (?, ?, 'running', ?)
            """, [pipeline, datetime.now(timezone.utc).isoformat(), params_json])
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    return await asyncio.to_thread(_do)


async def update_run_success(run_id: int, rows_written: int) -> None:
    def _do():
        conn = _new_conn()
        try:
            conn.execute("""
                UPDATE etl_run_log
                SET status='success', finished_at=?, rows_written=?
                WHERE id=?
            """, [datetime.now(timezone.utc).isoformat(), rows_written, run_id])
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_do)


async def update_run_error(run_id: int, error: str) -> None:
    def _do():
        conn = _new_conn()
        try:
            conn.execute("""
                UPDATE etl_run_log
                SET status='error', finished_at=?, error_message=?
                WHERE id=?
            """, [datetime.now(timezone.utc).isoformat(), error[:2000], run_id])
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(_do)


async def get_latest(pipeline: str) -> dict:
    def _do():
        conn = _new_conn()
        try:
            row = conn.execute("""
                SELECT id, pipeline, started_at, finished_at, status,
                       rows_written, error_message
                FROM etl_run_log
                WHERE pipeline = ?
                ORDER BY started_at DESC
                LIMIT 1
            """, [pipeline]).fetchone()
            if row is None:
                return {"status": "no_runs", "message": f"No runs found for pipeline: {pipeline}"}
            return dict(row)
        finally:
            conn.close()
    return await asyncio.to_thread(_do)


async def get_recent(pipeline: str, limit: int = 5) -> list[dict]:
    def _do():
        conn = _new_conn()
        try:
            rows = conn.execute("""
                SELECT id, pipeline, started_at, finished_at, status,
                       rows_written, error_message
                FROM etl_run_log
                WHERE pipeline = ?
                ORDER BY started_at DESC
                LIMIT ?
            """, [pipeline, limit]).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    return await asyncio.to_thread(_do)
