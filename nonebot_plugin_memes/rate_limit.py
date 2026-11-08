from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class GenerationRateLimitDecision:
    allowed: bool
    should_prompt: bool = False


class GenerationRateLimiter:
    """限制表情生成频率，并在每轮超限时只提示一次。"""

    def __init__(
        self,
        max_calls: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_calls <= 0 or window_seconds <= 0:
            raise ValueError("限流次数和窗口必须大于 0")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    def check(
        self, key: str, now: float | None = None
    ) -> GenerationRateLimitDecision:
        now = self._clock() if now is None else now
        blocked_until = self._blocked_until.get(key)
        if blocked_until is not None:
            if now < blocked_until:
                return GenerationRateLimitDecision(allowed=False)
            self._blocked_until.pop(key, None)

        events = self._events[key]
        threshold = now - self.window_seconds
        while events and events[0] <= threshold:
            events.popleft()

        if len(events) >= self.max_calls:
            self._events.pop(key, None)
            self._blocked_until[key] = now + self.window_seconds
            return GenerationRateLimitDecision(allowed=False, should_prompt=True)

        events.append(now)
        if len(self._events) > 4096:
            self._prune(now)
        return GenerationRateLimitDecision(allowed=True)

    def _prune(self, now: float) -> None:
        threshold = now - self.window_seconds
        for key, events in list(self._events.items()):
            if not events or events[-1] <= threshold:
                self._events.pop(key, None)
        for key, blocked_until in list(self._blocked_until.items()):
            if blocked_until <= now:
                self._blocked_until.pop(key, None)
