"""In-process observability counters and provider cost estimation helpers."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock


class MetricsRegistry:
    """Thread-safe counters exposed via /api/v1/health/metrics."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._estimated_usd: float = 0.0

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def add_cost(self, usd: float) -> None:
        with self._lock:
            self._estimated_usd += max(0.0, usd)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "estimated_provider_cost_usd": round(self._estimated_usd, 6),
                "cost_note": "Configured estimate based on env rate settings, not live provider billing",
            }


metrics = MetricsRegistry()
