# watch-ai

YouTube 영상 자막을 Gemini로 요약하는 서비스. `watch-runner`가 `post_process.type == "summarize"`인 새 아이템에 대해서만 호출한다.

## API

### POST /summarize

```json
{"url": "https://www.youtube.com/watch?v=..."}
```

응답:
```json
{"result": "1. 챕터명\n- 핵심 내용 [06:21](https://youtu.be/xxxx?t=381)\n..."}
```

- 자막이 없으면 `404` (`watch-runner`는 이 경우 summary 없이 URL만 발송)
- 일일 요청 한도(`RPD_LIMIT`) 초과 시 `429`

### GET /health

`{"status": "ok"}`

## 요약 방식 (자막 기반)

`TranscriptSummarizer`가 `youtube-transcript-api`로 자막을 추출해 Gemini에 텍스트로 전달한다. 영상을 Gemini에 직접(fileData) 넘기는 방식보다 토큰 소모가 수십 배 적다(실측: 93분 영상 자막 36,941 토큰 vs 8분 영상 직접 전달 44,242 토큰) — 대상이 IT 개발 영상이라 시각 정보 손실 영향이 적다고 판단해 자막 방식을 택했다.

```
summarizer.summarize(url)
├── youtube-transcript-api로 자막 추출 (ko, en 순으로 시도)
│   ├── 성공 → 타임스탬프 포함 프롬프트 구성 → GeminiProvider.generate()
│   └── 실패(자막 없음) → None 반환
└── 요약 텍스트 반환
```

프롬프트는 시간대별 섹션 + 불릿 + 인라인 타임스탬프 링크(`[MM:SS](url?t=초)`) 형식을 강제한다. Discord에서 타임스탬프 링크가 그대로 클릭 가능한 마크다운 링크로 렌더된다.

## 구조 (확장 포인트)

```
providers/base.py      BaseProvider.generate(prompt: str) -> str        # LLM 호출만
providers/gemini.py     GeminiProvider                                   # 현재 유일한 구현
summarizers/base.py     BaseSummarizer.summarize(url: str) -> str | None
summarizers/transcript.py  TranscriptSummarizer(provider)                # 현재 사용 중
```

`main.py`는 `SUMMARIZER` 환경변수로 어떤 `BaseSummarizer` 구현을 쓸지 결정하고, 이후로는 `summarizer.summarize(url)`에만 의존한다. 자막 없는 영상까지 처리하려는 `GeminiNativeSummarizer`(Gemini fileData API 직접 호출)가 계획되어 있으나 미구현 — 구글 쪽 토큰 제한 완화를 기다리는 중이다. `BaseProvider`에는 Gemini 전용 메서드를 추가하지 않기로 했다 — provider 인터페이스가 특정 벤더 기능으로 오염되는 것을 막기 위함이며, native 방식은 provider 계층을 거치지 않고 `GeminiNativeSummarizer`가 API를 직접 호출하는 형태로 구현될 예정이다.

## Rate Limit / RPD 방어

- Gemini 429(TPM/RPM 초과) 시 exponential backoff로 최대 3회 재시도(`GeminiProvider`)
- 프로세스 내부에서 `asyncio.Semaphore(1)`로 동시 요청을 1개로 직렬화 — `watch-runner`의 `SUMMARIZE_CONCURRENCY`와 별개로 watch-ai 자체도 한 번에 하나만 처리한다
- 일일 요청 수는 `ai_usage` 테이블(`date` PK, `request_count`)에 저장해 프로세스 재시작에도 카운터가 유지되게 하는 게 의도다

**미해결 버그**: HC4 실제 DB에 `ai_usage` 테이블 자체가 없다(`relation "ai_usage" does not exist`). `db.increment_usage()`는 `/summarize` 호출마다 이 테이블에 INSERT를 시도하므로, 지금 상태로는 **`/summarize` 요청이 매번 예외로 실패한다** — RPD 방어 로직이 죽어있는 정도가 아니라 요약 기능 자체가 동작하지 않을 가능성이 높다. `CREATE TABLE ai_usage (date DATE PRIMARY KEY, request_count INTEGER NOT NULL DEFAULT 0)`를 HC4에 적용해야 한다.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `DATABASE_URL` | (필수) | `ai_usage` 테이블 접근용 |
| `GEMINI_API_KEY` | (필수) | |
| `RPD_LIMIT` | 1500 | 일일 요청 한도 (추정치, AI Studio 콘솔 확인 필요) |
| `SUMMARIZER` | `transcript` | `_build_summarizer()`가 참조 — 현재 `transcript` 외 값은 에러 |

사용 모델: `gemini-3.5-flash` (RPM 10 / TPM 250,000 제한 하에서 동작).

## 포트

| 포트 | 용도 |
|---|---|
| 8080 | FastAPI — 컴포즈 내부에서만 노출 |
