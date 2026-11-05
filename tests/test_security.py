"""Tests for the security module: permissions and budget enforcement."""

import time
import unittest

from local_agent.security.budget import BudgetExhaustedError, BudgetTracker
from local_agent.security.permissions import (
    PermissionDeniedError,
    PermissionPolicy,
    PermissionTier,
    TIER_LABELS,
)


class TestPermissionTier(unittest.TestCase):
    def test_tier_ordering(self):
        self.assertLess(PermissionTier.READ_ONLY, PermissionTier.WORKSPACE_WRITE)
        self.assertLess(PermissionTier.WORKSPACE_WRITE, PermissionTier.NETWORK)
        self.assertLess(PermissionTier.NETWORK, PermissionTier.SYSTEM)
        self.assertLess(PermissionTier.SYSTEM, PermissionTier.DESTRUCTIVE)

    def test_all_tiers_have_labels(self):
        for tier in PermissionTier:
            self.assertIn(tier, TIER_LABELS)


class TestPermissionPolicy(unittest.TestCase):
    def test_default_auto_approves_workspace_write(self):
        policy = PermissionPolicy()
        self.assertTrue(policy.is_allowed(PermissionTier.READ_ONLY))
        self.assertTrue(policy.is_allowed(PermissionTier.WORKSPACE_WRITE))

    def test_default_blocks_network_and_above(self):
        policy = PermissionPolicy()
        self.assertFalse(policy.is_allowed(PermissionTier.NETWORK))
        self.assertFalse(policy.is_allowed(PermissionTier.SYSTEM))
        self.assertFalse(policy.is_allowed(PermissionTier.DESTRUCTIVE))

    def test_human_approval_unlocks_destructive(self):
        policy = PermissionPolicy()
        self.assertTrue(policy.is_allowed(PermissionTier.DESTRUCTIVE, human_approved=True))

    def test_custom_auto_approve_level(self):
        policy = PermissionPolicy(auto_approve_up_to=PermissionTier.NETWORK)
        self.assertTrue(policy.is_allowed(PermissionTier.NETWORK))
        self.assertFalse(policy.is_allowed(PermissionTier.SYSTEM))

    def test_needs_approval(self):
        policy = PermissionPolicy()
        self.assertFalse(policy.needs_approval(PermissionTier.READ_ONLY))
        self.assertTrue(policy.needs_approval(PermissionTier.DESTRUCTIVE))
        self.assertTrue(policy.needs_approval(PermissionTier.NETWORK))


class TestPermissionDeniedError(unittest.TestCase):
    def test_error_message(self):
        err = PermissionDeniedError("rm_file", PermissionTier.DESTRUCTIVE)
        self.assertIn("rm_file", str(err))
        self.assertIn("Destructive", str(err))
        self.assertEqual(err.tool_name, "rm_file")
        self.assertEqual(err.tier, PermissionTier.DESTRUCTIVE)


class TestBudgetTracker(unittest.TestCase):
    def test_step_limit(self):
        budget = BudgetTracker(max_steps=3, max_wall_time_seconds=600, max_repeated_actions=10)
        budget.start()
        budget.record_step("a")
        budget.record_step("b")
        budget.record_step("c")
        with self.assertRaises(BudgetExhaustedError) as ctx:
            budget.check()
        self.assertIn("Step limit", ctx.exception.reason)

    def test_repeated_action_limit(self):
        budget = BudgetTracker(max_steps=100, max_wall_time_seconds=600, max_repeated_actions=2)
        budget.start()
        budget.record_step("same_action")
        budget.record_step("same_action")
        with self.assertRaises(BudgetExhaustedError) as ctx:
            budget.check("same_action")
        self.assertIn("Repeated-action", ctx.exception.reason)

    def test_within_budget(self):
        budget = BudgetTracker(max_steps=10, max_wall_time_seconds=600, max_repeated_actions=5)
        budget.start()
        budget.record_step("action_1")
        budget.check("action_1")  # Should not raise.

    def test_summary(self):
        budget = BudgetTracker(max_steps=10, max_wall_time_seconds=600)
        budget.start()
        budget.record_step("a")
        budget.record_step("b")
        s = budget.summary()
        self.assertEqual(s["steps"], 2)
        self.assertEqual(s["max_steps"], 10)

    def test_remaining_steps(self):
        budget = BudgetTracker(max_steps=5)
        budget.start()
        budget.record_step("a")
        budget.record_step("b")
        self.assertEqual(budget.remaining_steps, 3)


if __name__ == "__main__":
    unittest.main()
