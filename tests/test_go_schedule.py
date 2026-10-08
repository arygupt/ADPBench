"""Scheduled reruns: pure decisions only. No model calls, dispatches, or GitHub requests."""
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.go_pilot import claim_name, pause_name, plan_slots, read_plan, validate_plan
from scripts.go_schedule import AGENT_WORKFLOW, ROUND_TITLE, decide, selected_plan, to_publish

ROOT = Path(__file__).resolve().parent.parent
ROUND = ROOT / "pilot/go-agent-v2-r2.json"
INSIDE = datetime(2026, 10, 9, 3, tzinfo=timezone.utc)


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.plan = read_plan(ROUND)
        self.claims = {claim_name(self.plan, *slot) for slot in plan_slots(self.plan)}

    def test_agent_workflow_selects_one_plan(self):
        self.assertEqual(selected_plan((ROOT / AGENT_WORKFLOW).read_text()), "pilot/go-agent-v2-r2.json")
        for text in ("env:\n", "  PLAN: pilot/a.json\n  PLAN: pilot/b.json\n"):
            with self.assertRaises(ValueError):
                selected_plan(text)

    def test_slot_workflow_runs_the_selected_plan(self):
        # A second literal here once ran every free slot against the -t2 plan.
        slot = (ROOT / ".github/workflows/go-agent-slot.yml").read_text()
        self.assertIn("  PLAN: ${{ inputs.plan }}\n", slot)
        self.assertNotRegex(slot, r"PLAN: pilot/")
        self.assertIn("plan: ${{ needs.prepare.outputs.plan }}", (ROOT / AGENT_WORKFLOW).read_text())

    def test_round_two_is_paced_and_repeats_round_one(self):
        self.assertEqual((self.plan["attempt"], self.plan["try"], self.plan["schedule"], self.plan["max_parallel"]), (2, 1, "hourly", 3))
        self.assertTrue(self.plan["release_on_quota"])
        self.assertEqual(len(plan_slots(self.plan)), 56)
        self.assertEqual(claim_name(self.plan, "kimi-k3", "002_gemv"), "go-agent-v2-r2-kimi-k3-002_gemv")
        self.assertEqual(claim_name({**self.plan, "try": 2}, "kimi-k3", "002_gemv"), "go-agent-v2-r2-kimi-k3-002_gemv-t2")
        self.assertEqual(pause_name(self.plan, "99"), "go-agent-v2-r2-paused-99")
        # Every setting a result depends on is round 1's; only the window and pacing differ.
        window = {"name", "attempt", "max_parallel", "release_on_quota", "schedule", "expires_at"}
        first = read_plan(ROOT / "pilot/go-agent-v2-r1.json")
        strip = lambda plan: {**{k: v for k, v in plan.items() if k not in window},
                              "models": [{k: v for k, v in m.items() if k != "not_before"} for m in plan["models"]]}
        self.assertEqual(strip(self.plan), strip(first))

    def test_dispatches_only_when_enabled_due_idle_and_unfinished(self):
        self.assertEqual(decide(self.plan, set(), 0, INSIDE), (True, "56 unclaimed slot(s)"))
        one_left = set(self.claims) - {claim_name(self.plan, *plan_slots(self.plan)[0])}
        self.assertEqual(decide(self.plan, one_left, 0, INSIDE), (True, "1 unclaimed slot(s)"))
        waits = [
            ({**self.plan, "schedule": None}, set(), 0, INSIDE),
            ({**self.plan, "generation_enabled": False}, set(), 0, INSIDE),
            (self.plan, set(), 0, datetime(2026, 10, 8, 17, 40, tzinfo=timezone.utc)),
            (self.plan, set(), 0, datetime(2026, 10, 10, 17, 45, tzinfo=timezone.utc)),
            (self.plan, set(), 1, INSIDE),
            (self.plan, self.claims, 0, INSIDE),
        ]
        for plan, claimed, active, now in waits:
            with self.subTest(now=now, active=active, claimed=len(claimed)):
                self.assertFalse(decide(plan, claimed, active, now)[0])

    def test_publishes_one_finished_wave_at_a_time(self):
        def wave(run_id, created, status="completed", **change):
            return {"databaseId": run_id, "status": status, "event": "workflow_dispatch", "headBranch": "main",
                    "displayTitle": ROUND_TITLE, "createdAt": created, **change}

        def publication(run_id, status="completed", conclusion="success"):
            return {"displayTitle": f"Publish results · source run {run_id}", "status": status,
                    "conclusion": conclusion, "event": "workflow_dispatch"}

        waves = [wave(3, "2026-10-09T02:00:00Z"), wave(2, "2026-10-08T20:00:00Z"),
                 wave(4, "2026-10-09T04:00:00Z", status="in_progress"),
                 wave(1, "2026-10-05T06:00:00Z"),  # an earlier plan's wave
                 wave(5, "2026-10-09T05:00:00Z", event="pull_request"),
                 wave(6, "2026-10-09T05:00:00Z", displayTitle="OpenCode Go · agent-assisted-v2 · compatibility canary")]
        ci = {"displayTitle": "Publish results · source run ", "status": "in_progress", "event": "pull_request"}
        self.assertEqual(to_publish(self.plan, waves, [ci], []), (2, "2 finished wave(s) to publish"))
        self.assertEqual(to_publish(self.plan, waves, [publication(2)], [])[0], 3)
        # A failed publication is left for review, never retried.
        self.assertEqual(to_publish(self.plan, waves, [publication(2, conclusion="failure")], [])[0], 3)
        for publications, branches in [([publication(2), publication(3)], []),
                                       ([publication(2, status="in_progress", conclusion="")], []),
                                       ([publication(2)], ["automation/results-2-1"])]:
            with self.subTest(publications=publications, branches=branches):
                self.assertIsNone(to_publish(self.plan, waves, publications, branches)[0])
        # Old open results PRs from other plans never block this one.
        self.assertEqual(to_publish(self.plan, waves, [publication(2)], ["automation/results-1-1"])[0], 3)

    def test_pacing_fields_are_bounded(self):
        validate_plan(self.plan)
        for change in ({"max_parallel": 0}, {"max_parallel": 15}, {"release_on_quota": "yes"},
                       {"schedule": "minutely"}, {"release_on_quota": False}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_plan({**self.plan, **change})


if __name__ == "__main__":
    unittest.main()
