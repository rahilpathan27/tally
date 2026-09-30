"""Small, deterministic controls used by the payment recovery loop."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int = 3
    reset_after_seconds: float = 5.0
    failures: int = 0
    opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if monotonic() - self.opened_at >= self.reset_after_seconds:
            self.opened_at = None
            self.failures = 0
            return False
        return True

    def allow_request(self) -> bool:
        return not self.is_open

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = monotonic()


def recovery_delay_seconds(attempt: int) -> int:
    """Exponential status-check delay capped at five minutes."""
    return int(min(300, 2 ** min(max(attempt, 0), 9)))
