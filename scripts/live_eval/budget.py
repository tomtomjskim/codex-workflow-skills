"""Deterministic resource budgets for bounded live evaluations."""

from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import Callable, Optional, Union


class BudgetDecision(str, Enum):
    ALLOWED = "allowed"
    BLOCKED_BUDGET = "blocked_budget"
    BLOCKED_TIMEOUT = "blocked_timeout"
    BLOCKED_CONCURRENCY = "blocked_concurrency"


class BudgetExceeded(RuntimeError):
    """Raised when a caller attempts an operation rejected by the budget."""

    def __init__(self, decision: BudgetDecision):
        super().__init__(decision.value)
        self.decision = decision


@dataclass(frozen=True)
class BudgetPolicy:
    max_calls: int
    max_seconds: float
    concurrency: int
    max_raw_bytes: int

    def __post_init__(self) -> None:
        if isinstance(self.max_calls, bool) or not isinstance(self.max_calls, int) or self.max_calls <= 0:
            raise ValueError("max_calls must be a positive integer")
        if (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not math.isfinite(self.max_seconds)
            or self.max_seconds <= 0
        ):
            raise ValueError("max_seconds must be positive")
        if isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int) or self.concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        if (
            isinstance(self.max_raw_bytes, bool)
            or not isinstance(self.max_raw_bytes, int)
            or self.max_raw_bytes <= 0
        ):
            raise ValueError("max_raw_bytes must be a positive integer")


@dataclass(frozen=True)
class BudgetSnapshot:
    max_calls: int
    max_seconds: float
    concurrency: int
    max_raw_bytes: int
    calls_used: int
    active: int
    elapsed_seconds: float
    decision: BudgetDecision


class Budget:
    """Tracks call, elapsed-time, and concurrency limits for one run."""

    TARGETED_MAX_CALLS = 5
    TARGETED_MAX_SECONDS = 300.0
    TARGETED_CONCURRENCY = 1
    TARGETED_MAX_RAW_BYTES = 1024 * 1024

    def __init__(
        self,
        policy: Union[BudgetPolicy, int],
        max_seconds: Optional[float] = None,
        concurrency: Optional[int] = None,
        max_raw_bytes: int = TARGETED_MAX_RAW_BYTES,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(policy, BudgetPolicy):
            if max_seconds is not None or concurrency is not None:
                raise TypeError("policy cannot be combined with individual limits")
            resolved = policy
        else:
            if max_seconds is None or concurrency is None:
                raise TypeError("max_seconds and concurrency are required")
            resolved = BudgetPolicy(policy, max_seconds, concurrency, max_raw_bytes)
        if not callable(clock):
            raise TypeError("clock must be callable")

        self._policy = resolved
        self._clock = clock
        self._started_at = clock()
        self._calls_used = 0
        self._active = 0

    @classmethod
    def targeted(cls, *, clock: Callable[[], float] = time.monotonic) -> "Budget":
        return cls(
            BudgetPolicy(
                cls.TARGETED_MAX_CALLS,
                cls.TARGETED_MAX_SECONDS,
                cls.TARGETED_CONCURRENCY,
                cls.TARGETED_MAX_RAW_BYTES,
            ),
            clock=clock,
        )

    @property
    def policy(self) -> BudgetPolicy:
        return self._policy

    @property
    def calls_used(self) -> int:
        return self._calls_used

    def _elapsed(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    def next_decision(self) -> BudgetDecision:
        if self._elapsed() >= self._policy.max_seconds:
            return BudgetDecision.BLOCKED_TIMEOUT
        if self._calls_used >= self._policy.max_calls:
            return BudgetDecision.BLOCKED_BUDGET
        if self._active >= self._policy.concurrency:
            return BudgetDecision.BLOCKED_CONCURRENCY
        return BudgetDecision.ALLOWED

    def consume_call(self) -> None:
        decision = self.next_decision()
        if decision is not BudgetDecision.ALLOWED:
            raise BudgetExceeded(decision)
        self._calls_used += 1

    def acquire(self) -> None:
        decision = self.next_decision()
        if decision is not BudgetDecision.ALLOWED:
            raise BudgetExceeded(decision)
        self._active += 1

    def release(self) -> None:
        if self._active == 0:
            raise RuntimeError("no active slot to release")
        self._active -= 1

    def snapshot(self) -> BudgetSnapshot:
        elapsed = self._elapsed()
        if elapsed >= self._policy.max_seconds:
            decision = BudgetDecision.BLOCKED_TIMEOUT
        elif self._calls_used >= self._policy.max_calls:
            decision = BudgetDecision.BLOCKED_BUDGET
        elif self._active >= self._policy.concurrency:
            decision = BudgetDecision.BLOCKED_CONCURRENCY
        else:
            decision = BudgetDecision.ALLOWED
        return BudgetSnapshot(
            max_calls=self._policy.max_calls,
            max_seconds=self._policy.max_seconds,
            concurrency=self._policy.concurrency,
            max_raw_bytes=self._policy.max_raw_bytes,
            calls_used=self._calls_used,
            active=self._active,
            elapsed_seconds=elapsed,
            decision=decision,
        )
