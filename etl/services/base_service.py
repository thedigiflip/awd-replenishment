"""
Base ETL Service — shared run logging helpers.
All pipeline services inherit from this.

Run logs use SQLite (core/db_meta.py) — completely separate from DuckDB,
so metadata writes never contend with analytical data writes.
"""

import json
import structlog

from core import db_meta

log = structlog.get_logger()


class BaseService:
    pipeline: str = "base"

    async def start_run(self, **params) -> int:
        """Insert a 'running' record and return the run_id."""
        return await db_meta.insert_run(
            pipeline=self.pipeline,
            params_json=json.dumps(params, default=str),
        )

    async def finish_run(self, run_id: int, rows_written: int) -> None:
        await db_meta.update_run_success(run_id, rows_written)

    async def fail_run(self, run_id: int, error: str) -> None:
        await db_meta.update_run_error(run_id, error)

    async def get_recent_runs(self, pipeline: str, limit: int = 5) -> list[dict]:
        return await db_meta.get_recent(pipeline, limit)

    async def get_latest_run(self, pipeline: str) -> dict:
        return await db_meta.get_latest(pipeline)
