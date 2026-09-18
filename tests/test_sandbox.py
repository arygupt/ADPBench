"""The sandbox boundary: mounts, resource limits, and seed redaction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from adpbench.agent import (
    RunRecord,
    build_manifest,
    build_sandbox_bundle,
    docker_command,
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
        self.assertEqual(cmd[-4:], ["adpbench-agent:latest", "sh", "-lc", "echo hi"])


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
            self.assertTrue((bundle / relative / "dut.py").is_file())

    def test_docker_check_sh_uses_the_mount(self) -> None:
        problem = load_problem(PROBLEM_DIR)
        text = render_check_sh(problem, python="/usr/bin/python", sandbox="docker")
        self.assertIn("/adpbench/problems/level1/001_dot_product", text)
        self.assertNotIn("/Users", text)


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
                problem, record, dest, timeout_s=60, sandbox="none", image="", network=""
            )
        self.assertEqual(manifest["problem"], problem.name)
        self.assertEqual(manifest["cases"]["seeds"][-1], 1002)
        self.assertEqual(manifest["cases"]["directed"], problem.directed_cases)
        self.assertTrue(manifest["submission_sha256"])
        self.assertTrue(manifest["problem_sha256"]["dut.py"])
        self.assertIn("yosys", manifest["tools"])


if __name__ == "__main__":
    unittest.main()
