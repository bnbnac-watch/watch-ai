import os

import httpx

from providers.gemini import GeminiProvider
from summarizers.base import BaseSummarizer
from summarizers.transcript import TranscriptSummarizer

SUMMARIZER_TYPE = os.getenv("SUMMARIZER", "transcript")


def build_summarizer(client: httpx.AsyncClient) -> BaseSummarizer:
    provider = GeminiProvider(client)
    if SUMMARIZER_TYPE == "transcript":
        return TranscriptSummarizer(provider)
    raise ValueError(f"알 수 없는 SUMMARIZER: {SUMMARIZER_TYPE}")
