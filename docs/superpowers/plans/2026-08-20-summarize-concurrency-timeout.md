# watch-ai 요약 동시성/타임아웃 정합화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `watch-ai`의 실제 내부 동시성을 `watch-runner`가 의도한 값(`SUMMARIZE_CONCURRENCY`)과 정합시키고, 요청당 최악 처리 시간을 `watch-runner`의 클라이언트 타임아웃보다 확실히 짧게 묶어서 — 클라이언트가 포기한 요청을 서버가 계속 붙잡고 있는 상태를 없앤다.

**Architecture:** `providers/gemini.py`의 블로킹 `urllib.request` 호출을 `httpx.AsyncClient`로 교체한다(`watch-runner/executor.py`와 동일한 `set_client()` 주입 패턴). `main.py`의 하드코딩된 `Semaphore(1)`을 `AI_CONCURRENCY`(기본 2) 환경변수로 조정 가능하게 바꾸고, 요약 호출을 `asyncio.wait_for(..., SUMMARIZE_TIMEOUT_S)`(기본 100초)로 감싸 느리거나 재시도 중인 Gemini 호출이 세마포어를 최대 15분까지 붙잡는 것을 막는다. `watch-runner/main.py`의 클라이언트 타임아웃(120초)과의 결합 관계를 양쪽 코드에 주석으로 남긴다.

**Tech Stack:** Python 3.11, FastAPI, httpx(신규 의존성), pytest + pytest-asyncio(신규 dev 의존성 — `watch-runner`의 `asyncio_mode = auto` 컨벤션을 그대로 따름).

**Spec:** 없음 — 별도 브레인스토밍 문서 없이, 이 대화에서 `watch-ai/main.py`, `watch-ai/providers/gemini.py`, `watch-runner/main.py`를 직접 코드 감사한 결과로 작성. 근거는 아래 Global Constraints 참고.

## Global Constraints

- 확인된 사실(코드 직접 확인, 2026-08-20): `watch-runner/main.py:19,23`는 `SUMMARIZE_CONCURRENCY`(기본 4)로 최대 4개의 `/summarize` 요청을 동시에 보내도록 설계돼 있다(`_summarize_sem = asyncio.Semaphore(SUMMARIZE_CONCURRENCY)`). 하지만 `watch-ai/main.py:33`는 자체적으로 `asyncio.Semaphore(1)`을 걸어 요청을 완전히 직렬화한다 — "동시 4개"라는 설정이 실제로는 항상 1개로 눌린다.
- 확인된 사실: `watch-ai`의 세마포어는 `summarizer.summarize()` 호출 전체(Gemini 재시도 루프 포함)를 감싼다. `providers/gemini.py`의 재시도는 attempt당 300초 타임아웃 × 최대 3회 + 백오프(5초, 15초)로, 최악의 경우 세마포어를 약 920초(~15분) 붙잡을 수 있다.
- 확인된 사실: `watch-runner/main.py:48`의 클라이언트 호출은 `timeout=120`(초)이다. watch-ai가 최악의 경우(~15분) 세마포어 뒤에서 대기/재시도하는 동안, runner는 120초 만에 자기 쪽 요청을 포기한다. `_summarize()`(`watch-runner/main.py:45-53`)는 예외를 삼키고 `None`을 반환하므로 파이프라인 자체는 죽지 않지만 요약이 조용히 누락되고, watch-ai 서버 쪽은 클라이언트가 이미 포기한 요청을 계속 처리하며 세마포어 슬롯을 낭비한다(FastAPI가 클라이언트 연결 종료를 자동 감지하지 않음).
- `urllib.request.urlopen`은 동기 블로킹 호출이라 `loop.run_in_executor`의 스레드에서 실행된다. 이 실행을 감싸는 `asyncio.wait_for`가 타임아웃돼도 **executor 스레드 자체는 멈추지 않고 백그라운드에서 계속 돈다** — `wait_for`는 코루틴이 그 결과를 기다리는 것만 그만둘 뿐이다. 그래서 이 계획은 타임아웃을 걸기 전에 먼저 전송 계층을 `httpx.AsyncClient`로 바꾼다(Task 1). httpx는 진짜 `asyncio` 취소를 지원해서 `wait_for` 타임아웃이 실제로 네트워크 호출을 끊는다. 순서를 반대로 하면(타임아웃만 먼저 추가) 눈에 보이는 증상은 사라져도 스레드 풀이 서서히 고갈되는 새로운 잠재 결함을 심는 꼴이 된다.
- `AI_CONCURRENCY` 기본값은 `SUMMARIZE_CONCURRENCY`의 4가 아니라 **2**로 시작한다 — Gemini API 자체의 초당/분당 rate limit 여유를 이 계획 시점에 알 수 없는 상태에서 runner 쪽 설정(4)에 무조건 맞추면 429 폭증 위험이 있다. 배포 후 429 발생률을 보고 운영자가 `.env`에서 튜닝한다.
- `SUMMARIZE_TIMEOUT_S`(기본 100)는 반드시 `watch-runner`의 `_summarize()` 클라이언트 타임아웃(120)보다 작아야 한다 — 이 불변식이 깨지면(둘 중 하나만 바꾸면) 이번에 고친 문제가 그대로 재발한다. Task 4에서 양쪽 코드에 상호 참조 주석을 남긴다.
- `watch-ai`에는 기존 pytest 인프라가 없다 — `watch-admin` 신설 때 도입한 것과 동일한 패턴(pytest + pytest-asyncio, `pythonpath = .`, `asyncio_mode = auto`)을 그대로 가져온다.
- DB 실접속 없이 테스트하기 위해 `watch-runner/tests/conftest.py`의 `FakeConn`/`FakePool` 패턴을 그대로 재사용한다 — `watch-ai/db.py`의 `_pool.acquire()` 셰이프가 동일하다.
- `summarizers/transcript.py`의 `TranscriptSummarizer.summarize(url) -> str | None` 시그니처는 이 계획에서 변경하지 않는다 — `GeminiProvider.generate(prompt) -> str` 시그니처도 마찬가지로 그대로 유지한다(내부 전송 계층만 바뀐다).

---

## Task 1: `providers/gemini.py`를 `httpx.AsyncClient`로 전환

**Files:**
- Modify: `watch-ai/providers/gemini.py`
- Modify: `watch-ai/requirements.txt`
- Create: `watch-ai/requirements-dev.txt`
- Create: `watch-ai/pytest.ini`
- Create: `watch-ai/tests/test_gemini.py`

**Interfaces:**
- Produces: `providers.gemini.set_client(client: httpx.AsyncClient)` (모듈 레벨 함수), `GeminiProvider.generate(prompt: str) -> str` (시그니처 불변, 내부만 httpx로 교체).

- [ ] **Step 1: 테스트 인프라 파일 생성**

`watch-ai/requirements.txt`에 `httpx` 추가:

```
fastapi
uvicorn
asyncpg
youtube-transcript-api
httpx
```

`watch-ai/requirements-dev.txt` 신규 생성:

```
-r requirements.txt
pytest
pytest-asyncio
```

`watch-ai/pytest.ini` 신규 생성:

```ini
[pytest]
asyncio_mode = auto
pythonpath = .
```

- [ ] **Step 2: 실패하는 테스트 작성**

`watch-ai/tests/test_gemini.py` 신규 생성:

```python
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
```

- [ ] **Step 3: 테스트 실패 확인**

Run:
```bash
cd watch-ai
pip install -r requirements-dev.txt
pytest tests/test_gemini.py -v
```

Expected: FAIL — `providers.gemini`에 `set_client`가 없음(`AttributeError`), 또는 기존 코드가 `httpx` 대신 `urllib.request`를 사용해 `_client`를 참조하지 않음.

- [ ] **Step 4: `providers/gemini.py` 재작성**

```python
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
```

- [ ] **Step 5: 테스트 통과 확인**

Run:
```bash
pytest tests/test_gemini.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add providers/gemini.py requirements.txt requirements-dev.txt pytest.ini tests/test_gemini.py
git commit -m "fix: Gemini 호출을 httpx.AsyncClient로 전환 (실제 취소 가능하게)"
```

---

## Task 2: `main.py` — `AI_CONCURRENCY` + `SUMMARIZE_TIMEOUT_S` 도입

**Files:**
- Modify: `watch-ai/main.py`
- Create: `watch-ai/tests/conftest.py`
- Create: `watch-ai/tests/test_main.py`

**Interfaces:**
- Consumes: `providers.gemini.set_client`(Task 1), `GeminiProvider`(Task 1), `db.increment_usage() -> int`(기존).
- Produces: `/summarize` 엔드포인트가 `app.state.semaphore`(용량 `AI_CONCURRENCY`, 기본 2)로 동시성을 제한하고, 각 요청을 `SUMMARIZE_TIMEOUT_S`(기본 100초)로 묶어 초과 시 `504`를 반환.

- [ ] **Step 1: `conftest.py` 작성 (watch-runner 패턴 재사용)**

`watch-ai/tests/conftest.py` 신규 생성:

```python
import pytest


class FakeConn:
    def __init__(self):
        self.fetchrow_return = None
        self.fetchrow_calls = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.fetchrow_return


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)


@pytest.fixture
def fake_conn():
    return FakeConn()


@pytest.fixture
def fake_pool(fake_conn):
    return FakePool(fake_conn)
```

- [ ] **Step 2: 실패하는 테스트 작성**

`watch-ai/tests/test_main.py` 신규 생성:

```python
import asyncio

import httpx
import pytest

import db
import main


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
```

- [ ] **Step 3: 테스트 실패 확인**

Run:
```bash
pytest tests/test_main.py -v
```

Expected: FAIL — `main`에 `SUMMARIZE_TIMEOUT_S`가 없거나(`AttributeError`), 현재 세마포어가 `Semaphore(1)`로 하드코딩돼 있어 `assert fake.max_concurrent == 2`가 실패, 또는 타임아웃 처리 부재로 `504` 대신 다른 상태코드/무한 대기.

- [ ] **Step 4: `main.py` 수정**

```python
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
```

- [ ] **Step 5: 테스트 통과 확인**

Run:
```bash
pytest tests/ -v
```

Expected: PASS (모든 테스트 — Task 1의 3개 + Task 2의 2개)

- [ ] **Step 6: Commit**

```bash
git add main.py tests/conftest.py tests/test_main.py
git commit -m "fix: AI_CONCURRENCY/SUMMARIZE_TIMEOUT_S 도입으로 요약 요청 직렬화·무한대기 제거"
```

---

## Task 3: `watch-infra` — 환경변수 배관 + README 문서화

**Files:**
- Modify: `watch-infra/docker-compose.yml`
- Modify: `watch-infra/.env.example`
- Modify: `watch-infra/README.md`

**Interfaces:**
- Consumes: Task 2에서 정의한 `AI_CONCURRENCY`, `SUMMARIZE_TIMEOUT_S` 환경변수 이름 그대로 사용.

- [ ] **Step 1: `docker-compose.yml`의 `watch-ai` 서비스에 환경변수 추가**

`watch-infra/docker-compose.yml`의 `watch-ai` 서비스 블록(`environment:` 아래)에 추가:

```yaml
  watch-ai:
    image: watch-ai
    restart: unless-stopped
    init: true
    environment:
      - DATABASE_URL=${DATABASE_URL}
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - GEMINI_MODEL=${GEMINI_MODEL:-gemini-3.5-flash}
      - RPD_LIMIT=${RPD_LIMIT:-1500}
      - SUMMARIZER=${SUMMARIZER:-transcript}
      - AI_CONCURRENCY=${AI_CONCURRENCY:-2}
      - SUMMARIZE_TIMEOUT_S=${SUMMARIZE_TIMEOUT_S:-100}
```

- [ ] **Step 2: `.env.example`에 문서화**

`watch-infra/.env.example`의 `# watch-ai` 섹션에 추가:

```
# watch-ai
RPD_LIMIT=1500
SUMMARIZER=transcript
# Gemini 요약 요청 내부 동시성. watch-runner의 SUMMARIZE_CONCURRENCY(기본 4)와
# 독립적으로 동작 — Gemini rate limit 여유를 보고 조정할 것 (기본은 보수적으로 2).
AI_CONCURRENCY=2
# 요약 요청 1건당 최대 처리 시간(초). watch-runner/main.py의 _summarize() 클라이언트
# 타임아웃(120s)보다 반드시 작아야 한다 — 그래야 watch-ai가 스스로 포기하는 시점이
# runner가 포기하는 시점보다 항상 먼저 온다.
SUMMARIZE_TIMEOUT_S=100
```

- [ ] **Step 3: README "왜 이런 구조인가" 섹션에 항목 추가**

`watch-infra/README.md`의 동시성 제어 단락(현재 50행 근처, `**동시성 제어**: ...` 문단) 뒤에 추가:

```markdown
**`watch-ai`가 자체 `AI_CONCURRENCY`를 갖는 이유**
`watch-runner`는 `SUMMARIZE_CONCURRENCY`(기본 4)로 `/summarize` 요청을 최대 4개까지 동시에 보내도록 설계돼 있지만, 예전 `watch-ai`는 자체적으로 `Semaphore(1)`을 걸어 항상 1개로 직렬화하고 있었다 — runner의 동시성 설정이 사실상 무의미했다. 게다가 그 세마포어를 Gemini 재시도 루프 전체(최악 ~15분) 동안 쥐고 있어서, runner의 클라이언트 타임아웃(120초)이 먼저 끊기고 watch-ai는 이미 버려진 요청을 계속 붙잡은 채 세마포어 슬롯을 낭비하는 구조였다. `AI_CONCURRENCY`로 내부 동시성을 명시적으로 노출하고, `SUMMARIZE_TIMEOUT_S`(반드시 runner의 120초보다 작게)로 요청당 처리 시간에 상한을 걸어 이 불일치를 없앴다.
```

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml .env.example README.md
git commit -m "fix: watch-ai AI_CONCURRENCY/SUMMARIZE_TIMEOUT_S 배관"
```

---

## Task 4: `watch-runner` — 타임아웃 결합 관계 주석

**Files:**
- Modify: `watch-runner/main.py:45-53`

**Interfaces:**
- Consumes: 없음(로직 변경 없음, 주석만 추가)

- [ ] **Step 1: `_summarize()`에 상호 참조 주석 추가**

`watch-runner/main.py`의 `_summarize` 함수를 수정:

```python
async def _summarize(url: str) -> str | None:
    async with _summarize_sem:
        try:
            # watch-ai의 SUMMARIZE_TIMEOUT_S(기본 100s)보다 반드시 커야 한다 — 안 그러면
            # 이 타임아웃이 watch-ai가 스스로 포기하기 전에 먼저 끊어버려서, watch-ai
            # 서버 쪽에 아무도 기다리지 않는 요청만 남기고 세마포어 슬롯을 낭비하게 된다.
            res = await _http_client.post(f"{WATCH_AI_URL}/summarize", json={"url": url}, timeout=120)
            res.raise_for_status()
            return res.json().get("result")
        except Exception as e:
            logger.error("watch-ai 호출 실패 (%s): %s", url, e)
            return None
```

- [ ] **Step 2: 기존 테스트 확인 (회귀 없음)**

Run:
```bash
cd watch-runner
pytest -v
```

Expected: PASS (주석만 추가했으므로 기존 테스트 결과에 변화 없음)

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "docs: watch-ai SUMMARIZE_TIMEOUT_S와의 타임아웃 결합 관계 주석 추가"
```
