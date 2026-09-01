import asyncio
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest

import db
import main
from providers import gemini


class _FakeSummarizer:
    def __init__(self, delay=0.0, result="요약"):
        self.delay = delay
        self.result = result
        self.calls = 0
        self.max_concurrent = 0
        self._current = 0

    async def summarize(self, url):
        self.calls += 1
        self._current += 1
        self.max_concurrent = max(self.max_concurrent, self._current)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.result
        finally:
            self._current -= 1


@pytest.fixture
def client(monkeypatch, fake_pool, fake_conn):
    fake_conn.fetchrow_return = {"request_count": 1}
    monkeypatch.setattr(db, "_pool", fake_pool)
    transport = httpx.ASGITransport(app=main.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_process_job_respects_semaphore_capacity(fake_pool, fake_conn, monkeypatch):
    monkeypatch.setattr(db, "_pool", fake_pool)
    fake_conn.fetchrow_return = {"request_count": 1}
    fake = _FakeSummarizer(delay=0.05)
    semaphore = asyncio.Semaphore(2)

    await asyncio.gather(*[
        main._process_job(uuid.uuid4(), f"https://x/{i}", fake, semaphore)
        for i in range(4)
    ])

    assert fake.max_concurrent == 2
    done_calls = [c for c in fake_conn.execute_calls if "status = 'done'" in c[0]]
    assert len(done_calls) == 4


async def test_process_job_marks_retryable_on_timeout(fake_pool, fake_conn, monkeypatch):
    monkeypatch.setattr(db, "_pool", fake_pool)
    fake_conn.fetchrow_return = {"request_count": 1}
    fake = _FakeSummarizer(delay=0.2)
    monkeypatch.setattr(main, "SUMMARIZE_TIMEOUT_S", 0.05)
    semaphore = asyncio.Semaphore(1)

    await main._process_job(uuid.uuid4(), "https://x", fake, semaphore)

    query, args = fake_conn.execute_calls[0]
    assert "status = 'failed'" in query
    assert args[2] is True  # retryable
    assert semaphore.locked() is False


async def test_process_job_marks_not_retryable_when_no_captions(fake_pool, fake_conn, monkeypatch):
    monkeypatch.setattr(db, "_pool", fake_pool)
    fake_conn.fetchrow_return = {"request_count": 1}
    fake = _FakeSummarizer(result=None)

    await main._process_job(uuid.uuid4(), "https://x", fake, asyncio.Semaphore(1))

    query, args = fake_conn.execute_calls[0]
    assert "status = 'failed'" in query
    assert args[2] is False  # retryable


async def test_process_job_marks_retryable_when_rpd_exceeded(fake_pool, fake_conn, monkeypatch):
    monkeypatch.setattr(db, "_pool", fake_pool)
    fake_conn.fetchrow_return = {"request_count": 9999}
    fake = _FakeSummarizer()

    await main._process_job(uuid.uuid4(), "https://x", fake, asyncio.Semaphore(1))

    query, args = fake_conn.execute_calls[0]
    assert "status = 'failed'" in query
    assert args[1] == "RPD 한도 초과"
    assert args[2] is True  # retryable


async def test_summarize_endpoint_returns_job_id_immediately(client):
    async with client:
        main.app.state.summarizer = _FakeSummarizer()
        main.app.state.semaphore = asyncio.Semaphore(2)
        main.app.state.background_tasks = set()
        res = await client.post("/summarize", json={"url": "https://x"})

    assert res.status_code == 202
    uuid.UUID(res.json()["job_id"])  # 유효한 UUID 문자열인지 확인


async def test_summarize_endpoint_tracks_background_task(client):
    fake = _FakeSummarizer(delay=0.05)
    async with client:
        main.app.state.summarizer = fake
        main.app.state.semaphore = asyncio.Semaphore(2)
        main.app.state.background_tasks = set()
        res = await client.post("/summarize", json={"url": "https://x"})

        # 백그라운드 작업이 끝나기 전이므로 강한 참조가 집합에 남아 있어야 한다
        # (asyncio.create_task의 결과를 저장하지 않으면 GC 대상이 되어 job이 유실될 수 있음).
        assert len(main.app.state.background_tasks) >= 1

    assert res.status_code == 202


async def test_lifespan_wires_semaphore_and_gemini_client(monkeypatch):
    monkeypatch.setattr(db, "init", AsyncMock())
    monkeypatch.setattr(gemini, "_client", None)
    async with main.lifespan(main.app):
        assert main.app.state.semaphore._value == main.AI_CONCURRENCY
        assert gemini._client is not None
