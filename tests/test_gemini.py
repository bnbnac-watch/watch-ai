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

    monkeypatch.setattr(gemini, "_sleep", fast_sleep)

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

    monkeypatch.setattr(gemini, "_sleep", fast_sleep)

    responses = [
        httpx.Response(503, json={"error": "unavailable"}),
        httpx.Response(503, json={"error": "unavailable"}),
    ]
    client, calls = _client_with_responses(responses)
    gemini.set_client(client)

    with pytest.raises(httpx.HTTPStatusError):
        await GeminiProvider().generate("프롬프트")

    assert calls["n"] == 2


async def test_api_key_sent_as_header_not_leaked_in_url_or_errors(monkeypatch):
    monkeypatch.setattr(gemini, "_API_KEY", "SUPER_SECRET_KEY_123")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(503, json={"error": "unavailable"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gemini.set_client(client)
    monkeypatch.setattr(gemini, "_MAX_RETRIES", 1)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await GeminiProvider().generate("프롬프트")

    assert "SUPER_SECRET_KEY_123" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "SUPER_SECRET_KEY_123"
    assert "SUPER_SECRET_KEY_123" not in str(exc_info.value)


def _capture_sleeps(monkeypatch) -> list:
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(gemini, "_sleep", fake_sleep)
    return waits


async def test_generate_retries_up_to_five_times_by_default(monkeypatch):
    _capture_sleeps(monkeypatch)

    responses = [httpx.Response(503, json={"error": "unavailable"}) for _ in range(4)]
    responses.append(httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": "5회차 성공"}]}}]
    }))
    client, calls = _client_with_responses(responses)
    gemini.set_client(client)

    assert await GeminiProvider().generate("프롬프트") == "5회차 성공"
    assert calls["n"] == 5


async def test_backoff_grows_exponentially_and_is_capped(monkeypatch):
    waits = _capture_sleeps(monkeypatch)
    monkeypatch.setattr(gemini, "_random", lambda: 1.0)

    client, _ = _client_with_responses(
        [httpx.Response(503, json={"error": "unavailable"}) for _ in range(gemini._MAX_RETRIES)]
    )
    gemini.set_client(client)

    with pytest.raises(httpx.HTTPStatusError):
        await GeminiProvider().generate("프롬프트")

    assert waits == [5, 10, 20, 40]


async def test_backoff_applies_jitter(monkeypatch):
    waits = _capture_sleeps(monkeypatch)
    monkeypatch.setattr(gemini, "_random", lambda: 0.0)

    client, _ = _client_with_responses(
        [httpx.Response(503, json={"error": "unavailable"}) for _ in range(gemini._MAX_RETRIES)]
    )
    gemini.set_client(client)

    with pytest.raises(httpx.HTTPStatusError):
        await GeminiProvider().generate("프롬프트")

    assert waits == [2.5, 5, 10, 20]


def test_worst_case_backoff_fits_summarize_timeout_budget():
    """전체 백오프 합이 SUMMARIZE_TIMEOUT_S(권장 240s) 안에 여유 있게 들어와야 한다.
    들어오지 않으면 재시도가 끝나기 전에 상위 wait_for가 먼저 job을 끊는다."""
    worst = sum(
        min(gemini._BACKOFF_BASE_S * 2 ** attempt, gemini._BACKOFF_CAP_S)
        for attempt in range(gemini._MAX_RETRIES - 1)
    )
    assert worst <= 80
