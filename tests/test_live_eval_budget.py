import unittest
import math

from scripts.live_eval.budget import Budget, BudgetPolicy


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class BudgetTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
