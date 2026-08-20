import httpx
import pytest

from providers import gemini
from providers.gemini import GeminiProvider


def _client_with_responses(responses):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        resp = responses[calls["n"]]
        calls["n"] += 1
        return resp

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_generate_returns_text_on_success():
    resp = httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "요약 결과"}]}}]
    })
    client, calls = _client_with_responses([resp])
    gemini.set_client(client)

    result = await GeminiProvider().generate("프롬프트")

    assert result == "요약 결과"
    assert calls["n"] == 1


async def test_generate_retries_on_503_then_succeeds(monkeypatch):
    async def fast_sleep(_):
        return

    monkeypatch.setattr(gemini.asyncio, "sleep", fast_sleep)

    responses = [
        httpx.Response(503, json={"error": "unavailable"}),
        httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "재시도 성공"}]}}]
        }),
    ]
    client, calls = _client_with_responses(responses)
    gemini.set_client(client)

    result = await GeminiProvider().generate("프롬프트")

    assert result == "재시도 성공"
    assert calls["n"] == 2


async def test_generate_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(gemini, "_MAX_RETRIES", 2)

    async def fast_sleep(_):
        return

    monkeypatch.setattr(gemini.asyncio, "sleep", fast_sleep)

    responses = [
        httpx.Response(503, json={"error": "unavailable"}),
        httpx.Response(503, json={"error": "unavailable"}),
    ]
    client, calls = _client_with_responses(responses)
    gemini.set_client(client)

    with pytest.raises(httpx.HTTPStatusError):
        await GeminiProvider().generate("프롬프트")

    assert calls["n"] == 2
