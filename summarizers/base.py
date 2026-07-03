from abc import ABC, abstractmethod


class BaseSummarizer(ABC):
    @abstractmethod
    async def summarize(self, url: str) -> str | None:
        ...
