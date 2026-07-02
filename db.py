import os
import asyncpg

_pool: asyncpg.Pool | None = None


async def init():
    global _pool
    _pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])


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
