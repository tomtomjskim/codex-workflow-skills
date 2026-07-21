"""Deterministic resource budgets for bounded live evaluations."""

from dataclasses import dataclass
from enum import Enum
import math
import threading
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


class BudgetClockError(RuntimeError):
    """Raised after an invalid or regressing budget clock sample."""


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
    TARGETED_MAX_SECONDS = 600.0
    TARGETED_CONCURRENCY = 1
    TARGETED_MAX_RAW_BYTES = 1024 * 1024
    RELEASE_MAX_CALLS = 30
    RELEASE_MAX_SECONDS = 2700.0
    RELEASE_CONCURRENCY = 2
    RELEASE_MAX_RAW_BYTES = 1024 * 1024

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
        self._lock = threading.RLock()
        self._clock_poisoned = False
        self._last_clock = None  # type: Optional[float]
        self._started_at = self._clock_value_locked()
        self._calls_used = 0
        self._legacy_active = 0
        self._lease_active = 0
        self._lease_tokens = set()

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

    @classmethod
    def release_suite(
        cls, *, clock: Callable[[], float] = time.monotonic
    ) -> "Budget":
        return cls(
            BudgetPolicy(
                cls.RELEASE_MAX_CALLS,
                cls.RELEASE_MAX_SECONDS,
                cls.RELEASE_CONCURRENCY,
                cls.RELEASE_MAX_RAW_BYTES,
            ),
            clock=clock,
        )

    @property
    def policy(self) -> BudgetPolicy:
        return self._policy

    @property
    def calls_used(self) -> int:
        with self._lock:
            return self._calls_used

    def _clock_value_locked(self) -> float:
        if self._clock_poisoned:
            raise BudgetClockError("budget clock is invalid") from None
        try:
            sample = self._clock()
        except Exception:
            self._clock_poisoned = True
            raise BudgetClockError("budget clock is invalid") from None
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            self._clock_poisoned = True
            raise BudgetClockError("budget clock is invalid") from None
        try:
            resolved = float(sample)
        except (OverflowError, TypeError, ValueError):
            self._clock_poisoned = True
            raise BudgetClockError("budget clock is invalid") from None
        if (
            not math.isfinite(resolved)
            or (self._last_clock is not None and resolved < self._last_clock)
        ):
            self._clock_poisoned = True
            raise BudgetClockError("budget clock is invalid") from None
        self._last_clock = resolved
        return resolved

    def _elapsed_locked(self) -> float:
        return self._clock_value_locked() - self._started_at

    def _decision_locked(self, elapsed: float) -> BudgetDecision:
        if elapsed >= self._policy.max_seconds:
            return BudgetDecision.BLOCKED_TIMEOUT
        if self._calls_used >= self._policy.max_calls:
            return BudgetDecision.BLOCKED_BUDGET
        if self._legacy_active + self._lease_active >= self._policy.concurrency:
            return BudgetDecision.BLOCKED_CONCURRENCY
        return BudgetDecision.ALLOWED

    def next_decision(self) -> BudgetDecision:
        with self._lock:
            return self._decision_locked(self._elapsed_locked())

    def consume_call(self) -> None:
        with self._lock:
            decision = self._decision_locked(self._elapsed_locked())
            if decision is not BudgetDecision.ALLOWED:
                raise BudgetExceeded(decision)
            self._calls_used += 1

    def acquire(self) -> None:
        with self._lock:
            decision = self._decision_locked(self._elapsed_locked())
            if decision is not BudgetDecision.ALLOWED:
                raise BudgetExceeded(decision)
            self._legacy_active += 1

    def acquire_call(self) -> "_BudgetLease":
        """Atomically reserve one call and one active slot."""
        with self._lock:
            decision = self._decision_locked(self._elapsed_locked())
            if decision is not BudgetDecision.ALLOWED:
                raise BudgetExceeded(decision)
            token = object()
            self._calls_used += 1
            self._lease_active += 1
            self._lease_tokens.add(token)
            return _BudgetLease(self, token)

    def release(self) -> None:
        with self._lock:
            if self._legacy_active == 0:
                raise RuntimeError("no active slot to release")
            self._legacy_active -= 1

    def _release_lease(self, lease: "_BudgetLease", allow_released: bool) -> None:
        with self._lock:
            if lease._released:
                if allow_released:
                    return
                raise RuntimeError("budget lease already released")
            if lease._token not in self._lease_tokens:
                raise RuntimeError("budget lease is not active")
            self._lease_tokens.remove(lease._token)
            self._lease_active -= 1
            lease._released = True

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            elapsed = self._elapsed_locked()
            decision = self._decision_locked(elapsed)
            return BudgetSnapshot(
                max_calls=self._policy.max_calls,
                max_seconds=self._policy.max_seconds,
                concurrency=self._policy.concurrency,
                max_raw_bytes=self._policy.max_raw_bytes,
                calls_used=self._calls_used,
                active=self._legacy_active + self._lease_active,
                elapsed_seconds=elapsed,
                decision=decision,
            )


class _BudgetLease:
    """Single-use release handle returned by ``Budget.acquire_call``."""

    def __init__(self, budget: Budget, token: object) -> None:
        self._budget = budget
        self._token = token
        self._released = False

    def release(self) -> None:
        self._budget._release_lease(self, allow_released=False)

    def __enter__(self) -> "_BudgetLease":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self._budget._release_lease(self, allow_released=True)
