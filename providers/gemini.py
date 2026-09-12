import asyncio
import logging
import os
import random

import httpx

from providers.base import BaseProvider

logger = logging.getLogger(__name__)

_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"
_MAX_RETRIES = 5
_RETRYABLE_CODES = (429, 503)
# 503은 Gemini 쪽 일시적 과부하라 몇십 초 안에 풀리는 경우가 대부분이다.
# 여기서 포기하면 watch-runner가 pending_summaries에 넣고 다음 크롤 사이클
# (유튜브 채널 기준 12시간)까지 미루므로, 재시도를 짧게 끊는 비용이 매우 크다.
# 지수 백오프 + full jitter(50~100%)로 동시 재시도가 겹치는 것도 피한다.
# 최악 백오프 합 5+10+20+40=75초 — SUMMARIZE_TIMEOUT_S 안에 들어와야 한다.
_BACKOFF_BASE_S = 5
_BACKOFF_CAP_S = 60
# httpx는 timeout을 안 주면 기본 5초라 정상 요청도 끊긴다 — 최소한의 방어선.
# 지금 유일한 호출 경로(main.py의 /summarize)는 이보다 짧은
# SUMMARIZE_TIMEOUT_S(기본 240s)로 전체를 감싸므로 이 300초가 실제로
# 발동하는 일은 없다. wait_for 없이 이 provider를 직접 부르는 호출자가
# 생기면 그때는 이 값이 유일한 상한이 된다.
_REQUEST_TIMEOUT_S = 300.0

_client: httpx.AsyncClient | None = None
_sleep = asyncio.sleep
_random = random.random


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
                    wait = min(_BACKOFF_BASE_S * 2 ** attempt, _BACKOFF_CAP_S) * (0.5 + 0.5 * _random())
                    logger.warning("%d, %.1f초 후 재시도 (%d/%d)", code, wait, attempt + 1, _MAX_RETRIES)
                    await _sleep(wait)
                else:
                    raise
        raise RuntimeError("Gemini API 재시도 초과")
