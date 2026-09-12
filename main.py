import asyncio
import contextlib
import logging
import os
import uuid
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel

import db
from providers import gemini
from providers.gemini import GeminiProvider
from summarizers.base import BaseSummarizer
from summarizers.transcript import TranscriptSummarizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 URL(쿼리 포함) 로그 노출 방지
logger = logging.getLogger(__name__)

RPD_LIMIT = int(os.getenv("RPD_LIMIT", "1500"))
SUMMARIZER_TYPE = os.getenv("SUMMARIZER", "transcript")
AI_CONCURRENCY = int(os.getenv("AI_CONCURRENCY", "2"))
# SUMMARIZE_TIMEOUT_S은 watch-runner의 job 대기 상한(300s)보다 작아야 한다 —
# 그래야 이 서비스가 스스로 포기하는 시점이 runner가 포기하는 시점보다 항상 먼저 온다.
SUMMARIZE_TIMEOUT_S = float(os.getenv("SUMMARIZE_TIMEOUT_S", "240"))
SWEEP_INTERVAL_SECONDS = 3600
# watch-runner의 wait_for_job 타임아웃(300s)보다 훨씬 커야 함 —
# 안 그러면 아직 기다리는 job을 스윕이 먼저 failed 처리할 수 있음
STALE_JOB_SECONDS = 3600


def _build_summarizer() -> BaseSummarizer:
    provider = GeminiProvider()
    if SUMMARIZER_TYPE == "transcript":
        return TranscriptSummarizer(provider)
    raise ValueError(f"알 수 없는 SUMMARIZER: {SUMMARIZER_TYPE}")


async def _sweep_loop():
    while True:
        try:
            count = await db.sweep_stale_jobs(STALE_JOB_SECONDS)
            if count:
                logger.warning("오래된 pending job %d개 정리", count)
        except Exception as exc:
            logger.warning("job 정리 스윕 실패: %s", exc)
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    async with httpx.AsyncClient() as client:
        gemini.set_client(client)
        app.state.summarizer = _build_summarizer()
        app.state.semaphore = asyncio.Semaphore(AI_CONCURRENCY)
        app.state.background_tasks = set()
        sweep_task = asyncio.create_task(_sweep_loop())
        yield
        sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task
        if app.state.background_tasks:
            await asyncio.wait(app.state.background_tasks, timeout=15)


app = FastAPI(lifespan=lifespan)


class SummarizeRequest(BaseModel):
    url: str


@app.get("/health")
async def health():
    return {"status": "ok"}


async def _process_job(job_id: uuid.UUID, url: str, summarizer, semaphore: asyncio.Semaphore):
    async with semaphore:
        try:
            count = await db.increment_usage()
            if count > RPD_LIMIT:
                logger.warning("RPD 한도 초과 (오늘 %d회)", count)
                await db.fail_job(job_id, "RPD 한도 초과", retryable=True)
                return

            try:
                result = await asyncio.wait_for(
                    summarizer.summarize(url), timeout=SUMMARIZE_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                logger.error("요약 시간 초과 (%s, %.0fs)", url, SUMMARIZE_TIMEOUT_S)
                await db.fail_job(job_id, "요약 시간 초과", retryable=True)
                return

            if result is None:
                await db.fail_job(job_id, "자막 없음", retryable=False)
                return

            logger.info("요약 완료: %s (오늘 %d회)", url, count)
            await db.complete_job(job_id, {"result": result})
        except Exception as exc:
            logger.exception("요약 처리 중 예상치 못한 오류 (%s)", url)
            await db.fail_job(job_id, f"처리 실패: {exc}", retryable=True)


@app.post("/summarize", status_code=202)
async def summarize_video(req: SummarizeRequest, request: Request):
    job_id = uuid.uuid4()
    await db.create_job(job_id, "summarize", {"url": req.url})
    task = asyncio.create_task(
        _process_job(job_id, req.url, request.app.state.summarizer, request.app.state.semaphore)
    )
    request.app.state.background_tasks.add(task)
    task.add_done_callback(request.app.state.background_tasks.discard)
    return {"job_id": str(job_id)}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080)
