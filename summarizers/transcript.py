import re
import logging
from youtube_transcript_api import YouTubeTranscriptApi

from providers.base import BaseProvider
from summarizers.base import BaseSummarizer

logger = logging.getLogger(__name__)

_VIDEO_URL_BASE = "https://youtu.be/{vid}"

_PROMPT_TEMPLATE = (
    "아래는 YouTube 영상({url})의 영어 자막입니다. 타임스탬프 [MM:SS] 포함.\n"
    "한국어로 요약해줘.\n\n"
    "형식 규칙:\n"
    "- 시간대별로 번호 매긴 섹션 제목 구성 (예: '1. 챕터명')\n"
    "- 각 섹션 안에 불릿 포인트로 핵심 내용만\n"
    "- 각 항목 끝에 해당 시점 타임스탬프를 인라인으로 표시: [MM:SS]({url}?t=초)\n"
    "  - MM:SS는 분:초, 초는 전체 초로 변환한 숫자\n"
    "  - 예시: [06:21]({url}?t=381)\n"
    "- 광고 섹션은 완전히 생략\n"
    "- 별도 링크 항목은 만들지 말 것\n\n"
    "자막:\n{transcript}"
)


def _extract_video_id(url: str) -> str:
    m = re.search(r"(?:youtu\.be/|[?&]v=)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else url


def _to_mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _build_transcript_text(entries) -> str:
    return "\n".join(f"[{_to_mmss(e.start)}] {e.text}" for e in entries)


class TranscriptSummarizer(BaseSummarizer):
    def __init__(self, provider: BaseProvider):
        self._provider = provider

    async def summarize(self, url: str) -> str | None:
        vid = _extract_video_id(url)
        video_url = _VIDEO_URL_BASE.format(vid=vid)
        try:
            entries = list(YouTubeTranscriptApi().fetch(vid, languages=["ko", "en"]))
        except Exception as e:
            logger.warning("자막 없음 (%s): %s", vid, e)
            return None

        transcript = _build_transcript_text(entries)
        prompt = _PROMPT_TEMPLATE.format(url=video_url, transcript=transcript)
        logger.info("요약 요청: %s (자막 %d줄)", vid, len(entries))
        return await self._provider.generate(prompt)
