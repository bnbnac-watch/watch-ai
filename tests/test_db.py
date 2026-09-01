import uuid

import db


async def test_create_job_inserts_row(fake_pool, fake_conn, monkeypatch):
    monkeypatch.setattr(db, "_pool", fake_pool)
    job_id = uuid.uuid4()

    await db.create_job(job_id, "summarize", {"url": "https://x"})

    query, args = fake_conn.execute_calls[0]
    assert "INSERT INTO async_jobs" in query
    assert args == (job_id, "summarize", {"url": "https://x"})


async def test_complete_job_updates_and_notifies(fake_pool, fake_conn, monkeypatch):
    monkeypatch.setattr(db, "_pool", fake_pool)
    job_id = uuid.uuid4()

    await db.complete_job(job_id, {"result": "요약"})

    update_query, update_args = fake_conn.execute_calls[0]
    assert "status = 'done'" in update_query
    assert update_args == (job_id, {"result": "요약"})
    notify_query, notify_args = fake_conn.execute_calls[1]
    assert "pg_notify" in notify_query
    assert notify_args == (str(job_id),)


async def test_fail_job_marks_retryable_flag(fake_pool, fake_conn, monkeypatch):
    monkeypatch.setattr(db, "_pool", fake_pool)
    job_id = uuid.uuid4()

    await db.fail_job(job_id, "자막 없음", retryable=False)

    update_query, update_args = fake_conn.execute_calls[0]
    assert "status = 'failed'" in update_query
    assert update_args == (job_id, "자막 없음", False)


async def test_sweep_stale_jobs_returns_count(fake_pool, fake_conn, monkeypatch):
    monkeypatch.setattr(db, "_pool", fake_pool)
    fake_conn.fetch_return = [{"id": uuid.uuid4()}, {"id": uuid.uuid4()}]

    count = await db.sweep_stale_jobs(3600)

    assert count == 2
