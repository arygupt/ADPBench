"""Offline contract tests: agent publication never executes or publishes transcripts."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adpbench.durable import atomic_json
from scripts.go_pilot import read_plan
from scripts.publish_agent import WORKFLOW, job_name, publish_agent
from scripts.publish_go import REPOSITORY
from scripts.publish_results import latest_jobs, validate_source, validate_records, stage_publication, strict_json


class AgentPublicationTest(unittest.TestCase):
    PROTOCOL = "agent-assisted-v1"
    PLAN_FIELDS = {}
    RECEIPT_FIELDS = {}

    def setUp(self):
        PROTOCOL = self.PROTOCOL
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = read_plan(Path(__file__).resolve().parent.parent / "pilot/go-core-20260922.json")
        self.plan.update(name="agent-test", protocol=PROTOCOL, max_turns=8, max_checks=3, **self.PLAN_FIELDS)
        self.plan["models"] = self.plan["models"][:1]
        self.run = {"id":123, "run_attempt":1, "head_sha":"a"*40, "status":"completed", "event":"workflow_dispatch",
                    "path":WORKFLOW, "repository":{"full_name":REPOSITORY}, "head_repository":{"full_name":REPOSITORY},
                    "head_branch":"main", "html_url":f"https://github.com/{REPOSITORY}/actions/runs/123"}
        self.policy = {"schema_version":1, "workflows":{WORKFLOW:{"name":"OpenCode Go agent runs"}}}
        self.jobs, self.paths = [], []
        self.repo = self.root / "checkout"
        atomic_json(self.repo / "site/data/evaluations.json", {"schema_version":1, "default":"pilot-001", "evaluations":[
            {"id":"pilot-001", "label":"Original", "path":"data/leaderboard.json", "protocol":"iterative"}]})
        for index, problem in enumerate(self.plan["problems"]):
            model = self.plan["models"][0]
            artifact = self.root / "artifacts" / f"go-agent-records-{model['id']}-{problem}-123"
            atomic_json(artifact / "plan.json", self.plan)
            dest = artifact / f"opencode-go-{model['id']}" / problem / "rep1"
            source = dest.parent / "rep1_frozen/dut.v"
            source.parent.mkdir(parents=True)
            source.write_text("module dut; endmodule\n")
            generation = {"model":model["id"], "problem":problem, "protocol":PROTOCOL, "outcome":"submitted",
                "turns":3, "max_turns":8, "checks":1, "max_checks":3, "max_output_tokens":8192,
                "submission_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),
                "generation_settings":{"reasoning":{"enabled":False}, "token_limit_key":"max_completion_tokens"},
                "usage":{"input_tokens":40, "output_tokens":30}, "error":"",
                "github":{"GITHUB_REPOSITORY":REPOSITORY, "GITHUB_RUN_ID":"123", "GITHUB_RUN_ATTEMPT":"1", "GITHUB_SHA":"a"*40},
                **self.RECEIPT_FIELDS}
            record = {"problem":problem, "label":f"opencode-go/{model['id']} [{PROTOCOL}]", "attempt":1,
                "group":"agent-test", "outcome":"correct", "execution_health":"completed", "audit":{"ok":True},
                "result":{"correct":True, "ratio":1.2, "cells":10, "cycles":20, "adp":200, "metadata":{"stage":"ok"}},
                "manifest":{"generation":generation, "submission_sha256":hashlib.sha256(source.read_bytes()).hexdigest()}}
            atomic_json(dest / "generation.json", generation)
            atomic_json(dest / "record.json", record)
            self.paths.append(dest)
            for offset, stage in enumerate(("Generate", "Evaluate")):
                self.jobs.append({"id":456 + index*2 + offset, "name":f"{model['id']} · {problem} / {job_name(stage, model['id'], problem, PROTOCOL)}",
                    "run_id":123, "status":"completed", "conclusion":"success", "completed_at":"2026-09-23T20:00:00Z",
                    "steps":[{"name":"Run standardized agent", "conclusion":"success"}] if stage == "Generate" else []})

    def publish(self):
        validate_source(self.run, self.jobs, self.plan, self.policy)
        validate_records(self.root / "artifacts", self.plan, self.run)
        return publish_agent(self.root / "artifacts", self.plan, self.run, self.jobs, self.root / "published", self.root / "site")

    def set_generation(self, dest, **change):
        generation = strict_json((dest / "generation.json").read_bytes())
        generation.update(change)
        atomic_json(dest / "generation.json", generation)
        record = strict_json((dest / "record.json").read_bytes())
        record["manifest"]["generation"] = generation
        atomic_json(dest / "record.json", record)

    def test_slot_artifacts_publish_separate_track_and_usage(self):
        board = self.publish()
        self.assertEqual(board["meta"]["protocol"], self.PROTOCOL)
        self.assertEqual(board["meta"]["generation_requests"], 6)
        self.assertEqual(board["meta"]["output_tokens"], 60)
        self.assertEqual(board["models"][0]["outcomes"], {"correct":2})
        self.assertEqual(board["models"][0]["scored"], 2)
        self.assertNotEqual(board["models"][0]["runs"][0]["execution"]["job_id"], board["models"][0]["runs"][1]["execution"]["job_id"])
        self.assertEqual(len(list((self.root / "published").glob("**/dut.v"))), 2)

    def test_unknown_outcome_is_null_not_incorrect(self):
        dest = self.paths[0]
        record = strict_json((dest / "record.json").read_bytes())
        record.update(outcome="scoring_interrupted", execution_health="failed", result=None, error="interrupted")
        atomic_json(dest / "record.json", record)
        board = self.publish()
        model = board["models"][0]
        self.assertEqual(model["correct"], 1)
        self.assertEqual(model["wrong_rtl"], 0)
        self.assertEqual(model["scored"], 1)
        self.assertEqual(model["unscored"], 1)
        run = next(r for r in model["runs"] if r["problem"] == "001_dot_product")
        self.assertIsNone(run["correct"])
        self.assertEqual(run["outcome"], "scoring_interrupted")

    def test_rejects_private_receipts_budget_changes_and_fabricated_pass(self):
        dest = self.paths[0]
        original = strict_json((dest / "generation.json").read_bytes())
        for delta in [{"max_turns":99}, {"turns":9}, {"outcome":"magic"}, {"max_output_tokens":999}, {"checks":4},
                      {"max_checks":4}, {"submission_sha256":"b"*64},
                      {"usage":{"output_tokens":1000000}},
                      {"messages":[{"role":"assistant", "content":"private"}]}, {"generation_settings":{}}]:
            self.set_generation(dest, **delta)
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                validate_records(self.root / "artifacts", self.plan, self.run)
            self.set_generation(dest, **original)
            current = strict_json((dest / "generation.json").read_bytes())
            current.pop("messages", None)
            atomic_json(dest / "generation.json", current)
        record = strict_json((dest / "record.json").read_bytes())
        record["outcome"] = "incorrect"
        atomic_json(dest / "record.json", record)
        with self.assertRaisesRegex(ValueError, "contradicts"):
            validate_records(self.root / "artifacts", self.plan, self.run)

    def test_rejects_missing_duplicate_foreign_and_preparation_jobs(self):
        for jobs in [self.jobs[:-1], self.jobs + [self.jobs[0]],
                     [{**j, "run_id":999} for j in self.jobs], [{**j, "steps":[]} for j in self.jobs],
                     [{**j, "name":"foreign / " + j["name"]} for j in self.jobs]]:
            with self.assertRaises(ValueError):
                validate_source(self.run, jobs, self.plan, self.policy)

    def test_hash_and_identity_mismatch_rejected_before_publication(self):
        source = self.paths[0].parent / "rep1_frozen/dut.v"
        source.write_text("module changed; endmodule")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.publish()
        self.assertFalse((self.root / "published").exists())

    def test_generation_only_backup_preserves_explicit_submission_without_inventing_score(self):
        dest = self.paths[0]
        (dest / "record.json").unlink()  # Owned temporary fixture only.
        (dest.parent / "rep1_frozen/dut.v").rename(dest / "dut.v")
        self.jobs[1]["conclusion"] = "cancelled"
        board = self.publish()
        run = next(r for r in board["models"][0]["runs"] if r["problem"] == "001_dot_product")
        self.assertEqual(run["record_origin"], "github-generation-only")
        self.assertIsNone(run["correct"])
        self.assertTrue(run["submission_sha256"])
        self.assertEqual(run["execution_health"], "failed")

    def test_completed_incorrect_remains_wrong_and_health_cannot_hide_infra(self):
        dest = self.paths[0]
        record = strict_json((dest / "record.json").read_bytes())
        record.update(outcome="incorrect")
        record["result"].update(correct=False, ratio=-1)
        record["result"]["metadata"]["stage"] = "incorrect"
        atomic_json(dest / "record.json", record)
        board = self.publish()
        self.assertEqual(board["models"][0]["wrong_rtl"], 1)
        self.assertEqual(board["models"][0]["scored"], 2)
        self.assertEqual(board["models"][0]["infra"], 0)
        record.update(outcome="scoring_interrupted", error="timeout")
        atomic_json(dest / "record.json", record)
        with self.assertRaisesRegex(ValueError, "health contradicts"):
            validate_records(self.root / "artifacts", self.plan, self.run)

    def test_stage_registers_new_protocol_without_changing_historical_catalog(self):
        args = (self.repo, self.root / "artifacts", self.plan, self.run, self.jobs, [])
        stage_publication(*args)
        catalog = strict_json((self.repo / "site/data/evaluations.json").read_bytes())
        self.assertEqual(catalog["evaluations"][0]["protocol"], self.PROTOCOL)
        self.assertEqual(catalog["evaluations"][1]["protocol"], "iterative")
        self.assertEqual(stage_publication(*args), stage_publication(*args))


class AgentV2PublicationTest(AgentPublicationTest):
    """Every v1 publication check, repeated for a v2 round, plus the receipt fields v2 adds."""

    PROTOCOL = "agent-assisted-v2"
    PLAN_FIELDS = {"attempt": 1, "try": 1, "transport_retries": 2}
    RECEIPT_FIELDS = {"attempt": 1, "try": 1, "transport_retries": [
        {"turn": 2, "retry": 1, "reason": "stream_idle_timeout", "usage_known": False}]}

    def test_round_receipt_fields_are_checked(self):
        dest = self.paths[0]
        retry = self.RECEIPT_FIELDS["transport_retries"][0]
        for delta in [{"attempt": 2}, {"try": 2}, {"transport_retries": None},
                      {"transport_retries": [{**retry, "retry": 3}]}, {"transport_retries": [{**retry, "turn": 9}]},
                      {"transport_retries": [{**retry, "reason": "provider said: <html>"}]},
                      {"transport_retries": [{**retry, "detail": "extra"}]}, {"transport_retries": [retry] * 7}]:
            self.set_generation(dest, **delta)
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                validate_records(self.root / "artifacts", self.plan, self.run)
            self.set_generation(dest, **self.RECEIPT_FIELDS)
        board = self.publish()
        run = board["models"][0]["runs"][0]
        self.assertEqual(run["generation"]["transport_retries"], self.RECEIPT_FIELDS["transport_retries"])
        self.assertEqual(board["meta"]["repetitions"], 1)

    def test_exhausted_quota_is_unscored_infrastructure(self):
        dest = self.paths[0]
        self.set_generation(dest, outcome="quota_exhausted", error="provider HTTP 429")
        (dest.parent / "rep1_frozen/dut.v").unlink()  # Owned temporary fixture only.
        record = strict_json((dest / "record.json").read_bytes())
        record.update(outcome="quota_exhausted", execution_health="failed", result=None, audit={"ok": False})
        record["manifest"]["submission_sha256"] = ""
        atomic_json(dest / "record.json", record)
        board = self.publish()
        model = board["models"][0]
        self.assertEqual((model["infra"], model["wrong_rtl"], model["unscored"]), (1, 0, 1))
        run = next(r for r in model["runs"] if r["problem"] == "001_dot_product")
        self.assertIsNone(run["correct"])
        self.assertEqual(run["outcome"], "quota_exhausted")

    def test_a_rerun_is_published_through_its_round(self):
        rerun = {**self.plan, "try": 2, "slots": [{"model": self.plan["models"][0]["id"], "problem": self.plan["problems"][0]}]}
        with self.assertRaisesRegex(ValueError, "rerun_slots"):
            publish_agent(self.root / "artifacts", rerun, self.run, self.jobs, self.root / "published", self.root / "site")


class AgentRerunPublicationTest(unittest.TestCase):
    """A try-2 wave adds finished slots next to the voided first try; nothing published changes."""

    def setUp(self):
        self.base = AgentV2PublicationTest("test_slot_artifacts_publish_separate_track_and_usage")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        base = self.base
        self.model = base.plan["models"][0]["id"]
        self.cut, self.kept = base.plan["problems"][0], base.plan["problems"][1]
        # The round's first try: the first problem was cut off by the usage limit.
        dest = base.paths[0]
        base.set_generation(dest, outcome="quota_exhausted", error="provider HTTP 429")
        (dest.parent / "rep1_frozen/dut.v").unlink()  # Owned temporary fixture only.
        record = strict_json((dest / "record.json").read_bytes())
        record.update(outcome="quota_exhausted", execution_health="failed", result=None, audit={"ok": False})
        record["manifest"]["submission_sha256"] = ""
        atomic_json(dest / "record.json", record)
        stage_publication(base.repo, base.root / "artifacts", base.plan, base.run, base.jobs, [])
        self.round = base.repo / "pilot/results/agent-test"

        self.rerun = {**base.plan, "try": 2, "release_on_quota": True,
                      "slots": [{"model": self.model, "problem": self.cut}, {"model": self.model, "problem": self.kept}]}
        self.wave = {**base.run, "id": 124, "html_url": f"https://github.com/{REPOSITORY}/actions/runs/124"}
        self.artifacts = base.root / "wave"
        self.jobs = []
        self.add_slot(self.cut, outcome="submitted")

    def add_slot(self, problem, outcome):
        """One slot of wave 124: its receipt (and a scored record unless it was released)."""
        base, model = self.base, self.model
        artifact = self.artifacts / f"go-agent-records-{model}-{problem}-124"
        atomic_json(artifact / "plan.json", self.rerun)
        dest = artifact / f"opencode-go-{model}" / problem / "rep1"
        source = dest.parent / "rep1_frozen/dut.v"
        source.parent.mkdir(parents=True)
        source.write_text("module dut(); endmodule\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        generation = strict_json((base.paths[1] / "generation.json").read_bytes())
        generation.update(problem=problem, outcome=outcome, submission_sha256=digest, **{"try": 2})
        generation["github"]["GITHUB_RUN_ID"] = "124"
        atomic_json(dest / "generation.json", generation)
        released = outcome == "quota_exhausted"
        if not released:
            record = strict_json((base.paths[1] / "record.json").read_bytes())
            record.update(problem=problem)
            record["manifest"] = {"generation": generation, "submission_sha256": digest}
            atomic_json(dest / "record.json", record)
        else:
            source.unlink()  # Owned temporary fixture only.
        for offset, stage in enumerate(("Generate", "Evaluate")):
            self.jobs.append({"id": 900 + len(self.jobs), "run_id": 124, "status": "completed",
                "name": f"{model} · {problem} / {job_name(stage, model, problem, base.PROTOCOL)}",
                "conclusion": "skipped" if released and stage == "Evaluate" else "success",
                "completed_at": "2026-09-29T20:00:00Z",
                "steps": [{"name": "Run standardized agent", "conclusion": "success"}] if stage == "Generate" else []})

    def stage(self):
        validate_source(self.wave, self.jobs, self.rerun, self.base.policy)
        return stage_publication(self.base.repo, self.artifacts, self.rerun, self.wave, self.jobs, [])

    def test_rerun_counts_in_place_of_the_voided_try(self):
        before = {path: path.read_bytes() for path in self.round.rglob("*") if path.is_file() and path.name not in {"REPORT.md", "report.json"}}
        self.stage()
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        added = self.round / f"opencode-go-{self.model}" / self.cut / "rep1-t2"
        self.assertEqual(strict_json((added / "record.json").read_bytes())["try"], 2)
        self.assertTrue((added.parent / "rep1-t2_frozen/dut.v").is_file())
        board = strict_json((self.base.repo / "site/data/agent-test/leaderboard.json").read_bytes())
        model = board["models"][0]
        self.assertEqual((model["attempts"], model["correct"], model["infra"]), (2, 2, 0))
        self.assertEqual(board["meta"]["reruns"][0]["workflow_url"], self.wave["html_url"])
        self.assertEqual(board["meta"]["workflow_url"], self.base.run["html_url"])
        receipt = strict_json((self.base.repo / "pilot/publications/run-124-attempt-1.json").read_bytes())
        self.assertEqual((receipt["try"], receipt["result_slots"], receipt["confirmed_correct"]), (2, 1, 1))
        self.assertEqual(self.stage(), self.stage())
        board_before = (self.base.repo / "site/data/agent-test/leaderboard.json").read_bytes()
        base = self.base
        stage_publication(base.repo, base.root / "artifacts", base.plan, base.run, base.jobs, [])
        self.assertEqual((self.base.repo / "site/data/agent-test/leaderboard.json").read_bytes(), board_before)

    def test_released_and_absent_slots_publish_nothing(self):
        self.jobs.clear()
        shutil.rmtree(self.artifacts)
        self.add_slot(self.cut, outcome="quota_exhausted")
        self.assertEqual(self.stage(), [])
        self.assertFalse((self.round / f"opencode-go-{self.model}" / self.cut / "rep1-t2").exists())

    def test_rerun_never_replaces_a_model_outcome_or_an_unpublished_round(self):
        self.add_slot(self.kept, outcome="submitted")
        with self.assertRaisesRegex(ValueError, "infrastructure failure"):
            self.stage()
        shutil.rmtree(self.base.repo / "pilot/results")
        with self.assertRaisesRegex(ValueError, "already published round"):
            self.stage()

    def test_a_wave_must_have_both_jobs_of_each_slot_it_ran(self):
        with self.assertRaisesRegex(ValueError, "missing or duplicate"):
            validate_source(self.wave, self.jobs[:1], self.rerun, self.base.policy)

    def test_a_re_run_attempt_publishes_the_slot_its_first_attempt_generated(self):
        # Attempt 2 found the slot claimed: its Generate skipped the agent and its
        # Evaluate was skipped under an unexpanded name, so attempt 1's jobs count.
        first = [{**job, "run_attempt": 1} for job in self.jobs]
        again = [{**first[0], "id": 990, "run_attempt": 2, "steps": [{"name": "Run standardized agent", "conclusion": "skipped"}]},
                 {**first[1], "id": 991, "run_attempt": 2, "conclusion": "skipped",
                  "name": f"{self.model} · {self.cut} / Evaluate ${{{{ inputs.model }}}} · ${{{{ inputs.problem }}}} · {self.base.PROTOCOL}"}]
        self.jobs[:] = latest_jobs(first + again)
        self.assertEqual([job["id"] for job in self.jobs], [first[0]["id"], first[1]["id"]])
        self.wave["run_attempt"] = 2
        self.stage()
        record = self.round / f"opencode-go-{self.model}" / self.cut / "rep1-t2/record.json"
        self.assertEqual(strict_json(record.read_bytes())["execution"]["job_id"], first[1]["id"])

    def test_a_receipt_from_a_later_attempt_is_rejected(self):
        artifact = self.artifacts / f"go-agent-records-{self.model}-{self.cut}-124" / f"opencode-go-{self.model}" / self.cut / "rep1"
        generation = strict_json((artifact / "generation.json").read_bytes())
        generation["github"]["GITHUB_RUN_ATTEMPT"] = "2"
        atomic_json(artifact / "generation.json", generation)
        record = strict_json((artifact / "record.json").read_bytes())
        record["manifest"]["generation"] = generation
        atomic_json(artifact / "record.json", record)
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.stage()

    def test_republishing_different_content_is_rejected(self):
        self.stage()
        source = self.artifacts / f"go-agent-records-{self.model}-{self.cut}-124" / f"opencode-go-{self.model}" / self.cut
        record = strict_json((source / "rep1/record.json").read_bytes())
        record["result"]["cells"] = 5
        record["result"]["adp"] = 100
        atomic_json(source / "rep1/record.json", record)
        with self.assertRaisesRegex(ValueError, "differs"):
            self.stage()


if __name__ == "__main__":
    unittest.main()
