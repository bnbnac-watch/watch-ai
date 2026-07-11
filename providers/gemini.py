import asyncio
import json
import logging
import os
import urllib.request

from providers.base import BaseProvider

logger = logging.getLogger(__name__)

_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent?key={_API_KEY}"
_MAX_RETRIES = 3
_RETRYABLE_CODES = (429, 503)


class GeminiProvider(BaseProvider):
    async def generate(self, prompt: str) -> str:
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        encoded = json.dumps(body).encode()

        for attempt in range(_MAX_RETRIES):
            try:
                req = urllib.request.Request(
                    _URL,
                    data=encoded,
                    headers={"Content-Type": "application/json"},
                )
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(None, self._call, req)
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                if e.code in _RETRYABLE_CODES and attempt < _MAX_RETRIES - 1:
                    # runner의 요청 timeout이 120초라 총 대기(5+15=20초)를 그 안에 유지
                    wait = 5 * 3 ** attempt
                    logger.warning("%d %s, %d초 후 재시도 (%d/%d)", e.code, e.reason, wait, attempt + 1, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                else:
                    raise
        raise RuntimeError("Gemini API 재시도 초과")

    @staticmethod
    def _call(req: urllib.request.Request) -> dict:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
