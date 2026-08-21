import asyncio
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


async def test_summarize_respects_semaphore_capacity(client):
    fake = _FakeSummarizer(delay=0.05)
    async with client:
        main.app.state.summarizer = fake
        main.app.state.semaphore = asyncio.Semaphore(2)
        results = await asyncio.gather(*[
            client.post("/summarize", json={"url": f"https://x/{i}"})
            for i in range(4)
        ])

    assert all(r.status_code == 200 for r in results)
    assert fake.max_concurrent == 2


async def test_summarize_times_out_and_releases_semaphore(monkeypatch, client):
    fake = _FakeSummarizer(delay=0.2)
    monkeypatch.setattr(main, "SUMMARIZE_TIMEOUT_S", 0.05)
    async with client:
        main.app.state.summarizer = fake
        main.app.state.semaphore = asyncio.Semaphore(1)

        res = await client.post("/summarize", json={"url": "https://x"})

        assert res.status_code == 504
        assert main.app.state.semaphore.locked() is False


async def test_lifespan_wires_semaphore_and_gemini_client(monkeypatch):
    monkeypatch.setattr(db, "init", AsyncMock())
    monkeypatch.setattr(gemini, "_client", None)
    async with main.lifespan(main.app):
        assert main.app.state.semaphore._value == main.AI_CONCURRENCY
        assert gemini._client is not None
