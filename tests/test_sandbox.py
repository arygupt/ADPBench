"""The sandbox boundary: mounts, resource limits, and seed redaction."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adpbench.agent import (
    RunRecord,
    _write_host_file,
    build_manifest,
    build_sandbox_bundle,
    bundle_path,
    docker_command,
    freeze_submission,
    redact_secrets,
    render_check_sh,
)
from adpbench.problem import load_problem

PROBLEM_DIR = (
    Path(__file__).resolve().parent.parent
    / "problems"
    / "level1"
    / "001_dot_product"
)


class DockerCommandTest(unittest.TestCase):
    def test_only_the_task_dir_is_writable(self) -> None:
        cmd = docker_command(
            Path("/tmp/env"),
            Path("/tmp/bundle"),
            "adpbench-agent:latest",
            "none",
            "echo hi",
            name="c1",
        )
        joined = " ".join(cmd)
        self.assertIn("/tmp/bundle:/adpbench:ro", joined)
        self.assertIn("/tmp/env:/work", joined)
        self.assertIn("--network none", joined)
        self.assertIn("--memory 4g", joined)
        self.assertIn("--pids-limit 1024", joined)
        self.assertIn("--name c1", joined)
        self.assertEqual(cmd[-4:], ["adpbench-agent:latest", "sh", "-c", "echo hi"])


class RedactionTest(unittest.TestCase):
    def test_credentials_do_not_reach_logs(self) -> None:
        redacted = redact_secrets("OPENAI_API_KEY=hunter2 run-agent")
        self.assertNotIn("hunter2", redacted)
        self.assertIn("OPENAI_API_KEY=***", redacted)


class BundleTest(unittest.TestCase):
    def test_evaluation_seeds_are_redacted_in_the_bundle(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = build_sandbox_bundle(problem, Path(tmp) / "bundle")
            evaluate = (bundle / "adpbench" / "evaluate.py").read_text()
            self.assertIn("EVAL_SEEDS = ()", evaluate)
            self.assertNotIn("EVAL_SEEDS = (1000", evaluate)
            self.assertTrue((bundle / "adpbench" / "sim.py").is_file())
            self.assertTrue((bundle / "flows" / "synth.ys").is_file())
            relative = problem.root.relative_to(
                Path(__file__).resolve().parent.parent
            )
            problem_copy = bundle / relative
            self.assertTrue((problem_copy / "dut.py").is_file())
            self.assertTrue((problem_copy / "baseline.v").is_file())
            self.assertFalse((problem_copy / "solutions").exists())
            baseline = json.loads((problem_copy / "baseline.json").read_text())
            self.assertNotIn("seeds", baseline)
            self.assertNotIn("directed", baseline)

    def test_docker_check_sh_uses_the_mount(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        text = render_check_sh(problem, python="/usr/bin/python", sandbox="docker")
        self.assertIn("/adpbench/problems/level1/001_dot_product", text)
        self.assertNotIn("/Users", text)


class FreezeTest(unittest.TestCase):
    def test_symlinked_submission_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "run"
            dest.mkdir()
            victim = Path(tmp) / "victim"
            victim.write_text("important")
            (dest / "dut.v").symlink_to(victim)
            frozen, error = freeze_submission(dest, Path(tmp) / "run_frozen")
            self.assertIsNone(frozen)
            self.assertIn("symlink", error)
            self.assertEqual(victim.read_text(), "important")

    def test_frozen_copy_lives_outside_the_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "run"
            dest.mkdir()
            (dest / "dut.v").write_text("module dut; endmodule\n")
            frozen, error = freeze_submission(dest, Path(tmp) / "run_frozen")
            self.assertEqual(error, "")
            self.assertTrue(frozen.is_file())
            self.assertNotEqual(frozen.parent, dest)

    def test_host_file_write_never_follows_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            victim = Path(tmp) / "victim"
            victim.write_text("important")
            target = Path(tmp) / "record.json"
            target.symlink_to(victim)
            _write_host_file(target, "{}")
            self.assertEqual(victim.read_text(), "important")
            self.assertFalse(target.is_symlink())

    def test_bundle_lives_outside_the_task_directory(self) -> None:
        dest = Path("/tmp/runs/pilot/label/001/rep1")
        self.assertEqual(bundle_path(dest).parent, dest.parent)
        self.assertNotEqual(bundle_path(dest), dest)


class ManifestTest(unittest.TestCase):
    def test_manifest_captures_the_replay_facts(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "run"
            dest.mkdir()
            (dest / "dut.v").write_text("module dut; endmodule\n")
            record = RunRecord(problem=problem.name, agent_cmd="echo hi", label="m")
            record.result = {"correct": False, "metadata": {"per_case": []}}
            manifest = build_manifest(
                problem,
                record,
                dest / "dut.v",
                timeout_s=60,
                sandbox="none",
                image="",
                network="",
            )
        self.assertEqual(manifest["problem"], problem.name)
        self.assertEqual(manifest["cases"]["seeds"][-1], 1002)
        self.assertEqual(manifest["cases"]["directed"], problem.directed_cases)
        self.assertTrue(manifest["submission_sha256"])
        self.assertTrue(manifest["problem_sha256"]["dut.py"])
        self.assertIn("yosys", manifest["tools"])


if __name__ == "__main__":
    unittest.main()
