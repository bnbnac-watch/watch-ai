import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import db
import summarizer
from providers.gemini import GeminiProvider

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_semaphore = asyncio.Semaphore(1)
_provider = GeminiProvider()

RPD_LIMIT = 1500


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield


app = FastAPI(lifespan=lifespan)


class SummarizeRequest(BaseModel):
    url: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/summarize")
async def summarize_video(req: SummarizeRequest):
    async with _semaphore:
        count = await db.increment_usage()
        if count > RPD_LIMIT:
            logger.warning("RPD 한도 초과 (오늘 %d회)", count)
            raise HTTPException(status_code=429, detail="RPD 한도 초과")

        result = await summarizer.summarize(req.url, _provider)
        if result is None:
            raise HTTPException(status_code=404, detail="자막 없음")

        logger.info("요약 완료: %s (오늘 %d회)", req.url, count)
        return {"result": result}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8080)
