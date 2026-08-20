import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

import db
from providers import gemini
from providers.gemini import GeminiProvider
from summarizers.base import BaseSummarizer
from summarizers.transcript import TranscriptSummarizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RPD_LIMIT = int(os.getenv("RPD_LIMIT", "1500"))
SUMMARIZER_TYPE = os.getenv("SUMMARIZER", "transcript")
AI_CONCURRENCY = int(os.getenv("AI_CONCURRENCY", "2"))
# watch-runner/main.py의 _summarize() 클라이언트 타임아웃(120s)보다 반드시 작아야 한다 —
# 그래야 watch-ai가 스스로 포기하는 시점이 runner가 포기하는 시점보다 항상 먼저 온다.
SUMMARIZE_TIMEOUT_S = float(os.getenv("SUMMARIZE_TIMEOUT_S", "100"))


def _build_summarizer() -> BaseSummarizer:
    provider = GeminiProvider()
    if SUMMARIZER_TYPE == "transcript":
        return TranscriptSummarizer(provider)
    raise ValueError(f"알 수 없는 SUMMARIZER: {SUMMARIZER_TYPE}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    async with httpx.AsyncClient() as client:
        gemini.set_client(client)
        app.state.summarizer = _build_summarizer()
        app.state.semaphore = asyncio.Semaphore(AI_CONCURRENCY)
        yield


app = FastAPI(lifespan=lifespan)


class SummarizeRequest(BaseModel):
    url: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/summarize")
async def summarize_video(req: SummarizeRequest, request: Request):
    async with request.app.state.semaphore:
        count = await db.increment_usage()
        if count > RPD_LIMIT:
            logger.warning("RPD 한도 초과 (오늘 %d회)", count)
            raise HTTPException(status_code=429, detail="RPD 한도 초과")

        try:
            result = await asyncio.wait_for(
                request.app.state.summarizer.summarize(req.url),
                timeout=SUMMARIZE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error("요약 시간 초과 (%s, %.0fs)", req.url, SUMMARIZE_TIMEOUT_S)
            raise HTTPException(status_code=504, detail="요약 시간 초과")

        if result is None:
            raise HTTPException(status_code=404, detail="자막 없음")

        logger.info("요약 완료: %s (오늘 %d회)", req.url, count)
        return {"result": result}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080)
