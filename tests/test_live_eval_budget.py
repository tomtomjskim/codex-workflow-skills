import unittest
import math
import threading
import time

from scripts.live_eval.budget import Budget, BudgetExceeded, BudgetPolicy


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class DetectConcurrentClock:
    def __init__(self):
        self.concurrent = False
        self._entered = threading.Lock()

    def __call__(self):
        if not self._entered.acquire(blocking=False):
            self.concurrent = True
            raise RuntimeError("clock called concurrently")
        try:
            time.sleep(0.002)
            return 100.0
        finally:
            self._entered.release()


class BudgetTests(unittest.TestCase):
    def assertClockRejected(self, operation):
        try:
            operation()
        except Exception as error:
            self.assertIsInstance(error, RuntimeError)
        else:
            self.fail("invalid clock sample was accepted")

    def test_targeted_budget_blocks_sixth_call_without_off_by_one(self):
        budget = Budget.targeted()

        for _ in range(5):
            self.assertEqual(budget.next_decision(), "allowed")
            budget.consume_call()

        self.assertEqual(budget.next_decision(), "blocked_budget")

    def test_timeout_uses_injected_monotonic_clock(self):
        clock = FakeClock()
        budget = Budget(BudgetPolicy(5, 10.0, 1, 1024), clock=clock)

        clock.now += 10.0

        self.assertEqual(budget.next_decision(), "blocked_timeout")

    def test_concurrency_blocks_until_slot_is_released(self):
        budget = Budget(BudgetPolicy(5, 10.0, 1, 1024), clock=FakeClock())
        budget.acquire()

        self.assertEqual(budget.next_decision(), "blocked_concurrency")
        budget.release()
        self.assertEqual(budget.next_decision(), "allowed")

    def test_invalid_policy_is_rejected(self):
        invalid_policies = (
            (0, 10.0, 1, 1024),
            (5, 0.0, 1, 1024),
            (5, 10.0, 0, 1024),
            (5, 10.0, 1, 0),
            (1.5, 10.0, 1, 1024),
            (5, 10.0, 1.5, 1024),
            (5, 10.0, 1, 1.5),
            (True, 10.0, 1, 1024),
            (5, math.inf, 1, 1024),
            (5, math.nan, 1, 1024),
            (5, "10", 1, 1024),
        )

        for values in invalid_policies:
            with self.subTest(values=values), self.assertRaises(ValueError):
                BudgetPolicy(*values)

    def test_snapshot_is_immutable_and_detached(self):
        budget = Budget.targeted()
        snapshot = budget.snapshot()

        with self.assertRaises((AttributeError, TypeError)):
            snapshot.calls_used = 3
        budget.consume_call()

        self.assertEqual(snapshot.calls_used, 0)
        self.assertEqual(budget.snapshot().calls_used, 1)

    def test_clock_samples_must_be_numeric_finite_and_nondecreasing(self):
        invalid_initial = (True, "100", math.nan, math.inf, -math.inf, 10 ** 1000)
        for value in invalid_initial:
            with self.subTest(initial=value):
                self.assertClockRejected(
                    lambda value=value: Budget.targeted(clock=lambda: value)
                )

        for value in (True, "100", math.nan, math.inf, -math.inf, 10 ** 1000, 99.0):
            with self.subTest(next=value):
                samples = iter((100.0, value))
                budget = Budget.targeted(clock=lambda: next(samples))
                self.assertClockRejected(budget.next_decision)

    def test_clock_anomaly_permanently_poisons_budget(self):
        clock = FakeClock()
        budget = Budget.targeted(clock=clock)
        clock.now = 99.0
        self.assertClockRejected(budget.next_decision)
        clock.now = 101.0
        self.assertClockRejected(budget.next_decision)
        self.assertClockRejected(budget.snapshot)

    def test_clock_calls_are_serialized_by_budget_lock(self):
        clock = DetectConcurrentClock()
        budget = Budget.targeted(clock=clock)
        start = threading.Barrier(9)
        errors = []
        errors_lock = threading.Lock()

        def decide():
            start.wait()
            try:
                budget.next_decision()
            except Exception as error:
                with errors_lock:
                    errors.append(error)

        threads = [threading.Thread(target=decide) for _ in range(8)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join()

        self.assertFalse(clock.concurrent)
        self.assertEqual(errors, [])

    def test_acquire_call_atomically_reserves_call_and_slot(self):
        budget = Budget(BudgetPolicy(3, 10.0, 1, 1024), clock=FakeClock())
        self.assertTrue(hasattr(budget, "acquire_call"))

        lease = budget.acquire_call()
        snapshot = budget.snapshot()
        self.assertEqual(snapshot.calls_used, 1)
        self.assertEqual(snapshot.active, 1)
        with self.assertRaises(BudgetExceeded):
            budget.acquire_call()

        lease.release()
        self.assertEqual(budget.snapshot().active, 0)
        with self.assertRaises(RuntimeError):
            lease.release()

    def test_acquire_call_context_releases_exactly_once(self):
        budget = Budget(BudgetPolicy(3, 10.0, 1, 1024), clock=FakeClock())
        self.assertTrue(hasattr(budget, "acquire_call"))

        with budget.acquire_call():
            self.assertEqual(budget.snapshot().active, 1)

        snapshot = budget.snapshot()
        self.assertEqual(snapshot.active, 0)
        self.assertEqual(snapshot.calls_used, 1)

    def test_barrier_concurrency_never_exceeds_policy(self):
        workers = 10
        concurrency = 3
        budget = Budget(BudgetPolicy(20, 10.0, concurrency, 1024), clock=FakeClock())
        self.assertTrue(hasattr(budget, "acquire_call"))
        start = threading.Barrier(workers + 1)
        attempted = threading.Barrier(workers + 1)
        release_allowed = threading.Event()
        leases = []
        blocked = []
        result_lock = threading.Lock()

        def acquire():
            start.wait()
            lease = None
            try:
                lease = budget.acquire_call()
            except BudgetExceeded as error:
                with result_lock:
                    blocked.append(error.decision)
            else:
                with result_lock:
                    leases.append(lease)
            attempted.wait()
            if lease is not None:
                release_allowed.wait()
                lease.release()

        threads = [threading.Thread(target=acquire) for _ in range(workers)]
        for thread in threads:
            thread.start()
        start.wait()
        attempted.wait()
        snapshot = budget.snapshot()
        release_allowed.set()
        for thread in threads:
            thread.join()

        self.assertEqual(len(leases), concurrency)
        self.assertEqual(len(blocked), workers - concurrency)
        self.assertEqual(snapshot.calls_used, concurrency)
        self.assertEqual(snapshot.active, concurrency)
        self.assertEqual(budget.snapshot().active, 0)

    def test_legacy_double_release_is_rejected(self):
        budget = Budget(BudgetPolicy(3, 10.0, 1, 1024), clock=FakeClock())
        budget.acquire()
        budget.release()

        with self.assertRaises(RuntimeError):
            budget.release()


if __name__ == "__main__":
    unittest.main()
