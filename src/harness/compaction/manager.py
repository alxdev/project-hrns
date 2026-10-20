from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from harness.api.types import Message
from harness.provider.base import Provider

from .strategies import CompactionStrategy, NoCompaction, SlidingWindow, Summarize


@dataclass(frozen=True)
class CompactionReport:
    strategy: str
    before_count: int
    after_count: int

    @property
    def changed(self) -> bool:
        return self.before_count != self.after_count


class CompactionManager:
    def __init__(
        self,
        provider: Provider,
        strategy: CompactionStrategy | None = None,
        sliding_keep_last: int = 10,
        summarize_threshold: int = 20,
        summarize_keep_recent: int = 6,
    ) -> None:
        self.provider = provider
        self.strategy = strategy or NoCompaction()
        self.sliding_keep_last = sliding_keep_last
        self.summarize_threshold = summarize_threshold
        self.summarize_keep_recent = summarize_keep_recent
        self.verbose = False

    async def compact(self, messages: Sequence[Message]) -> list[Message]:
        return await self.strategy.compact(messages)

    async def run(
        self, messages: Sequence[Message], mode: str | None = None
    ) -> tuple[list[Message], CompactionReport]:
        strategy = self._strategy_for(mode) if mode else self.strategy
        before_count = len(messages)
        result = await strategy.compact(messages)
        return result, CompactionReport(self._strategy_name(strategy), before_count, len(result))

    def set_mode(self, mode: str) -> None:
        self.strategy = self._strategy_for(mode)

    def set_provider(self, provider: Provider) -> None:
        self.provider = provider
        if isinstance(self.strategy, Summarize):
            self.strategy.provider = provider

    @property
    def strategy_name(self) -> str:
        return self._strategy_name(self.strategy)

    @staticmethod
    def _strategy_name(strategy: CompactionStrategy) -> str:
        name = type(strategy).__name__
        return {
            "NoCompaction": "none",
            "SlidingWindow": "sliding",
            "Summarize": "summarize",
        }.get(name, name)

    def _strategy_for(self, mode: str) -> CompactionStrategy:
        normalized = mode.lower()
        if normalized == "none":
            return NoCompaction()
        if normalized == "sliding":
            return SlidingWindow(self.sliding_keep_last)
        if normalized == "summarize":
            return Summarize(self.provider, self.summarize_threshold, self.summarize_keep_recent)
        raise ValueError(f"unknown compaction strategy: {mode}")
