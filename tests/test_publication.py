"""No network/model calls: exercise the privileged publication trust boundary."""
import io
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from adpbench.durable import atomic_json
from scripts.publish_results import (
    REPOSITORY, MAX_FILES, extract_archive, strict_json, validate_source,
    validate_records, stage_publication, open_results_pr, register, source_plan_path,
)
from tests.test_publish_go import PublishGoTest


class PublicationTest(PublishGoTest):
    def setUp(self):
        super().setUp()
        self.run.update(head_branch="main", head_repository={"full_name": REPOSITORY})
        self.jobs[0].update(run_id=123, steps=[{"name":"Generate two single-shot submissions (no retries or fallback)", "conclusion":"success"}])
        self.policy = {"schema_version":1, "workflows": {".github/workflows/go-core.yml": {"name":"OpenCode Go model runs"}}}
        self.catalog = {"schema_version":1, "default":"pilot-001", "evaluations":[{"id":"pilot-001", "label":"Original", "path":"data/leaderboard.json", "protocol":"iterative"}]}
        self.repo = self.root / "checkout"
        atomic_json(self.repo / "site/data/evaluations.json", self.catalog)
        for path in self.artifact.glob("**/generation.json"):
            generation = strict_json(path.read_bytes())
            generation.update(max_output_tokens=8192, generation_settings={"reasoning":{"enabled":False}, "token_limit_key":"max_completion_tokens"})
            atomic_json(path, generation)
            record_path = path.parent / "record.json"
            record = strict_json(record_path.read_bytes())
            record.update(group=self.plan["name"], audit={"ok":True})
            record["manifest"]["generation"] = generation
            atomic_json(record_path, record)

    def test_only_completed_same_repo_main_generations_are_accepted(self):
        validate_source(self.run, self.jobs, self.plan, self.policy)
        for change in [dict(head_branch="topic"), dict(event="pull_request"), dict(status="in_progress"),
                       dict(path=".github/workflows/other.yml"), dict(head_sha="main"), dict(run_attempt=0),
                       dict(repository={"full_name":"attacker/repo"}), dict(head_repository={"full_name":"attacker/repo"}),
                       dict(html_url="https://attacker.invalid")]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_source({**self.run, **change}, self.jobs, self.plan, self.policy)

    def test_preparation_only_skipped_duplicate_or_other_run_jobs_rejected(self):
        for jobs in [[], self.jobs * 2, [{**self.jobs[0], "steps":[]}],
                     [{**self.jobs[0], "conclusion":"skipped"}], [{**self.jobs[0], "run_id":999}]]:
            with self.assertRaises(ValueError):
                validate_source(self.run, jobs, self.plan, self.policy)

    def test_separate_generation_and_scoring_jobs_keep_provenance_checks(self):
        generation = {**self.jobs[0], "id":456, "name":"Generate mimo-v2.5 · two single-shot submissions"}
        evaluated = {**self.jobs[0], "steps":[]}
        validate_source(self.run,[evaluated,generation],self.plan,self.policy)
        for invalid in [[evaluated,generation,generation],
                        [evaluated,{**generation,"run_id":999}],
                        [evaluated,{**generation,"steps":[]}]]:
            with self.assertRaises(ValueError):
                validate_source(self.run,invalid,self.plan,self.policy)

    def test_interrupted_generation_is_not_mistaken_for_preparation_only(self):
        step = {"name":"Generate two single-shot submissions (no retries or fallback)",
                "conclusion":"cancelled", "started_at":"2026-09-22T00:00:00Z"}
        job = {**self.jobs[0], "conclusion":"cancelled", "steps":[step]}
        validate_source(self.run,[job],self.plan,self.policy)
        step.pop("started_at")
        with self.assertRaises(ValueError):
            validate_source(self.run,[job],self.plan,self.policy)

    def test_rejects_budget_changes_and_passing_error_records(self):
        validate_records(self.root / "artifacts", self.plan, self.run)
        path = self.artifact / "opencode-go-mimo-v2.5/001_dot_product/rep1/record.json"
        original = strict_json(path.read_bytes())
        for change in [dict(error="provider rejected"), dict(audit={"ok":False}), dict(attempt=2),
                       dict(group="other"), dict(api_key="never publish")]:
            atomic_json(path, {**original, **change})
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_records(self.root / "artifacts", self.plan, self.run)
        atomic_json(path, original)
        gen = path.parent / "generation.json"
        original_gen = strict_json(gen.read_bytes())
        for change in [dict(max_output_tokens=99999), dict(generation_settings={})]:
            atomic_json(gen, {**original_gen, **change})
            with self.assertRaises(ValueError):
                validate_records(self.root / "artifacts", self.plan, self.run)

    def test_immutable_publication_is_idempotent_and_registers_dataset(self):
        args = (self.repo, self.root / "artifacts", self.plan, self.run, self.jobs, [])
        paths = stage_publication(*args)
        before = {str(p):p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(stage_publication(*args), paths)
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.repo.rglob("*") if p.is_file()})
        catalog = strict_json((self.repo / "site/data/evaluations.json").read_bytes())
        self.assertEqual(catalog["default"], self.plan["name"])
        self.assertEqual(len(catalog["evaluations"]), 2)
        source = self.repo / "pilot/results" / self.plan["name"] / "opencode-go-mimo-v2.5/001_dot_product/rep1_frozen/dut.v"
        source.write_text("tampered test fixture")
        with self.assertRaisesRegex(ValueError, "immutable results differ"):
            stage_publication(*args)

    def test_existing_pr_is_never_overwritten_or_reopened(self):
        with patch("scripts.publish_results.command", return_value=b'[{"url":"https://github.com/arygupt/ADPBench/pull/1","state":"CLOSED"}]') as call:
            self.assertTrue(open_results_pr(self.repo, [], self.run).endswith("/1"))
            self.assertEqual(call.call_count, 1)

    def test_generation_backup_keeps_rtl_without_claiming_score(self):
        self.jobs[0]["conclusion"] = "cancelled"
        for source in self.sources:
            dest = source.parent.parent / "rep1"
            (dest / "record.json").unlink()  # Owned temporary test fixtures only.
            source.rename(dest / "dut.v")
        stage_publication(self.repo, self.root / "artifacts", self.plan, self.run, self.jobs, [])
        record = strict_json((self.repo / "pilot/results" / self.plan["name"] / "opencode-go-mimo-v2.5/001_dot_product/rep1/record.json").read_bytes())
        self.assertIsNone(record["result"])
        self.assertEqual(record["record_origin"], "github-generation-only")
        self.assertTrue(record["manifest"]["submission_sha256"])

    def test_per_model_output_limits_are_validated_and_exported(self):
        self.plan.update(output_budget="provider_max", max_output_tokens={"mimo-v2.5":128000})
        atomic_json(self.artifact / "plan.json", self.plan)
        for path in self.artifact.glob("**/generation.json"):
            generation = strict_json(path.read_bytes())
            generation["max_output_tokens"] = 128000
            atomic_json(path, generation)
            record_path = path.parent / "record.json"
            record = strict_json(record_path.read_bytes())
            record["manifest"]["generation"] = generation
            atomic_json(record_path, record)
        stage_publication(self.repo, self.root / "artifacts", self.plan, self.run, self.jobs, [])
        board = strict_json((self.repo / "site/data" / self.plan["name"] / "leaderboard.json").read_bytes())
        self.assertEqual(board["meta"]["output_budget"], "provider_max")
        self.assertEqual(board["meta"]["max_output_tokens"], {"mimo-v2.5":128000})
        generation["max_output_tokens"] = 384000
        atomic_json(path, generation)
        with self.assertRaisesRegex(ValueError, "budget differs"):
            validate_records(self.root / "artifacts", self.plan, self.run)


class ArchiveTest(unittest.TestCase):
    def test_plan_is_bound_to_the_source_workflow_not_the_latest_default(self):
        old = "pilot/go-core-20260922.json"
        new = "pilot/go-core-provider-max-20260922.json"
        for path in (old, new):
            self.assertEqual(source_plan_path(f"env:\n  PLAN: {path}\n", [old, new]), path)
        for source in ["env:\n  PLAN: ${{ inputs.plan }}\n", "env:\n  PLAN: pilot/unreviewed.json\n",
                       f"env:\n  PLAN: {old}\n  PLAN: {new}\n", "env:\n  PLAN: ../../private.json\n"]:
            with self.assertRaises(ValueError):
                source_plan_path(source, [old, new])

    def archive(self, entries):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for name, content in entries:
                z.writestr(name, content)
        return out.getvalue()

    def test_safe_archive_extracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "artifact"
            extract_archive(self.archive([("plan.json", '{}'), ("model/rep1_frozen/dut.v", "module dut; endmodule")]), target)
            self.assertEqual((target / "plan.json").read_text(), "{}")

    def test_larger_budgets_do_not_disable_archive_resource_limits(self):
        with tempfile.TemporaryDirectory() as tmp, patch("scripts.publish_results.MAX_FILE", 8), self.assertRaises(ValueError):
            extract_archive(self.archive([("response.txt", "x" * 9)]), Path(tmp) / "artifact")

    def test_traversal_absolute_symlink_duplicate_and_invalid_json_rejected(self):
        link = zipfile.ZipInfo("source.v")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entries in [[("../escape", "x")], [("/absolute", "x")], [("a\\b", "x")],
                        [("same", "a"), ("SAME", "b")], [(link, "../escape")],
                        [("record.json", '{"correct":true,"correct":false}')], [("record.json", '{"ratio":NaN}')],
                        [(str(i), "x") for i in range(MAX_FILES + 1)]]:
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                target = Path(tmp) / "artifact"
                extract_archive(self.archive(entries), target)
                self.assertFalse(target.exists())

    def test_json_must_be_object_without_duplicates_or_nonfinite_values(self):
        for raw in [b'[]', b'{"a":1,"a":2}', b'{"score":Infinity}', b'{"score":1e999}']:
            with self.assertRaises(ValueError):
                strict_json(raw)

    def test_catalog_rejects_conflicting_existing_path(self):
        catalog = {"schema_version":1,"default":"batch","evaluations":[{"id":"batch","path":"evil","protocol":"single-shot"}]}
        with self.assertRaises(ValueError):
            register(catalog, {"name":"batch"})


if __name__ == "__main__":
    unittest.main()
