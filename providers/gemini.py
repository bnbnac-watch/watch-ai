import asyncio
import logging
import os

import httpx

from providers.base import BaseProvider

logger = logging.getLogger(__name__)

_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"
_MAX_RETRIES = 3
_RETRYABLE_CODES = (429, 503)
# httpx는 timeout을 안 주면 기본 5초라 정상 요청도 끊긴다 — 최소한의 방어선.
# 지금 유일한 호출 경로(main.py의 /summarize)는 이보다 훨씬 짧은
# SUMMARIZE_TIMEOUT_S(기본 110s)로 전체를 감싸므로 이 300초가 실제로
# 발동하는 일은 없다. wait_for 없이 이 provider를 직접 부르는 호출자가
# 생기면 그때는 이 값이 유일한 상한이 된다.
_REQUEST_TIMEOUT_S = 300.0

_client: httpx.AsyncClient | None = None
_sleep = asyncio.sleep


def set_client(client: httpx.AsyncClient):
    global _client
    _client = client


class GeminiProvider(BaseProvider):
    async def generate(self, prompt: str) -> str:
        body = {"contents": [{"parts": [{"text": prompt}]}]}

        for attempt in range(_MAX_RETRIES):
            try:
                res = await _client.post(
                    _URL,
                    json=body,
                    headers={"x-goog-api-key": _API_KEY},
                    timeout=_REQUEST_TIMEOUT_S,
                )
                res.raise_for_status()
                data = res.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in _RETRYABLE_CODES and attempt < _MAX_RETRIES - 1:
                    # 전체 재시도 시간이 watch-ai/main.py의 SUMMARIZE_TIMEOUT_S보다
                    # 짧을 필요는 없다 — wait_for가 상위에서 통째로 끊어준다(Task 2).
                    wait = 5 * 3 ** attempt
                    logger.warning("%d, %d초 후 재시도 (%d/%d)", code, wait, attempt + 1, _MAX_RETRIES)
                    await _sleep(wait)
                else:
                    raise
        raise RuntimeError("Gemini API 재시도 초과")
