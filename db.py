import json
import os
import uuid

import asyncpg

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection):
    # asyncpg는 JSONB를 str로 반환하므로 dict로 자동 변환하는 코덱 등록
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def init():
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], init=_init_conn)


async def increment_usage() -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ai_usage (date, request_count) VALUES (CURRENT_DATE, 1)
            ON CONFLICT (date) DO UPDATE SET request_count = ai_usage.request_count + 1
            RETURNING request_count
            """
        )
        return row["request_count"]


async def create_job(job_id: uuid.UUID, kind: str, payload: dict) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO async_jobs (id, kind, payload) VALUES ($1, $2, $3)",
            job_id, kind, payload,
        )


async def complete_job(job_id: uuid.UUID, result: dict) -> None:
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE async_jobs SET status = 'done', result = $2, finished_at = now() WHERE id = $1",
                job_id, result,
            )
            await conn.execute("SELECT pg_notify('async_job_done', $1)", str(job_id))


async def fail_job(job_id: uuid.UUID, error: str, retryable: bool) -> None:
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE async_jobs SET status = 'failed', error = $2, retryable = $3, finished_at = now() WHERE id = $1",
                job_id, error, retryable,
            )
            await conn.execute("SELECT pg_notify('async_job_done', $1)", str(job_id))


async def sweep_stale_jobs(older_than_seconds: int = 3600) -> int:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE async_jobs SET status = 'failed', error = 'stale: no worker completed this job', "
            "retryable = true, finished_at = now() "
            "WHERE status = 'pending' AND created_at < now() - ($1 || ' seconds')::interval "
            "RETURNING id",
            str(older_than_seconds),
        )
        return len(rows)
