"""Offline contract tests: agent publication never executes or publishes transcripts."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from adpbench.durable import atomic_json
from scripts.go_pilot import read_plan
from scripts.publish_agent import WORKFLOW, job_name, publish_agent
from scripts.publish_go import REPOSITORY
from scripts.publish_results import validate_source, validate_records, stage_publication, strict_json


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

    def test_reruns_are_not_published_over_a_round_yet(self):
        rerun = {**self.plan, "try": 2, "slots": [{"model": self.plan["models"][0]["id"], "problem": self.plan["problems"][0]}]}
        with self.assertRaisesRegex(ValueError, "rerun"):
            publish_agent(self.root / "artifacts", rerun, self.run, self.jobs, self.root / "published", self.root / "site")


if __name__ == "__main__":
    unittest.main()
