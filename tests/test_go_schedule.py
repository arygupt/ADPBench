"""Scheduled reruns: pure decisions only. No model calls, dispatches, or GitHub requests."""
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.go_pilot import claim_name, pause_name, plan_slots, read_plan, validate_plan
from scripts.go_schedule import AGENT_WORKFLOW, decide, selected_plan

ROOT = Path(__file__).resolve().parent.parent
RERUN = ROOT / "pilot/go-agent-v2-r1-t2.json"
INSIDE = datetime(2026, 9, 27, 3, tzinfo=timezone.utc)


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.plan = read_plan(RERUN)
        self.claims = {claim_name(self.plan, *slot) for slot in plan_slots(self.plan)}

    def test_agent_workflow_selects_one_plan(self):
        self.assertEqual(selected_plan((ROOT / AGENT_WORKFLOW).read_text()), "pilot/go-agent-v2-r1-free.json")
        for text in ("env:\n", "  PLAN: pilot/a.json\n  PLAN: pilot/b.json\n"):
            with self.assertRaises(ValueError):
                selected_plan(text)

    def test_rerun_plan_is_paced_and_lists_only_round_one_slots(self):
        self.assertEqual((self.plan["try"], self.plan["schedule"], self.plan["max_parallel"]), (2, "hourly", 3))
        self.assertTrue(self.plan["release_on_quota"])
        self.assertEqual(len(plan_slots(self.plan)), 43)
        self.assertEqual(claim_name(self.plan, "kimi-k3", "002_gemv"), "go-agent-v2-r1-kimi-k3-002_gemv-t2")
        self.assertEqual(claim_name({**self.plan, "try": 1}, "kimi-k3", "002_gemv"), "go-agent-v2-r1-kimi-k3-002_gemv")
        self.assertEqual(pause_name(self.plan, "99"), "go-agent-v2-r1-paused-99")

    def test_dispatches_only_when_enabled_due_idle_and_unfinished(self):
        self.assertEqual(decide(self.plan, set(), 0, INSIDE), (True, "43 unclaimed slot(s)"))
        one_left = set(self.claims) - {claim_name(self.plan, *plan_slots(self.plan)[0])}
        self.assertEqual(decide(self.plan, one_left, 0, INSIDE), (True, "1 unclaimed slot(s)"))
        waits = [
            ({**self.plan, "schedule": None}, set(), 0, INSIDE),
            ({**self.plan, "generation_enabled": False}, set(), 0, INSIDE),
            (self.plan, set(), 0, datetime(2026, 9, 27, 0, 10, tzinfo=timezone.utc)),
            (self.plan, set(), 0, datetime(2026, 9, 29, 1, tzinfo=timezone.utc)),
            (self.plan, set(), 1, INSIDE),
            (self.plan, self.claims, 0, INSIDE),
        ]
        for plan, claimed, active, now in waits:
            with self.subTest(now=now, active=active, claimed=len(claimed)):
                self.assertFalse(decide(plan, claimed, active, now)[0])

    def test_pacing_fields_are_bounded(self):
        validate_plan(self.plan)
        for change in ({"max_parallel": 0}, {"max_parallel": 15}, {"release_on_quota": "yes"},
                       {"schedule": "minutely"}, {"release_on_quota": False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_plan({**self.plan, **change})


if __name__ == "__main__":
    unittest.main()
