import asyncio
import logging
import os

import httpx

from providers.base import BaseProvider

logger = logging.getLogger(__name__)

_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent?key={_API_KEY}"
_MAX_RETRIES = 3
_RETRYABLE_CODES = (429, 503)
_REQUEST_TIMEOUT_S = 300.0

_client: httpx.AsyncClient | None = None


def set_client(client: httpx.AsyncClient):
    global _client
    _client = client


class GeminiProvider(BaseProvider):
    async def generate(self, prompt: str) -> str:
        body = {"contents": [{"parts": [{"text": prompt}]}]}

        for attempt in range(_MAX_RETRIES):
            try:
                res = await _client.post(_URL, json=body, timeout=_REQUEST_TIMEOUT_S)
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
                    await asyncio.sleep(wait)
                else:
                    raise
        raise RuntimeError("Gemini API 재시도 초과")
