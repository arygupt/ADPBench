"""Subscription CLI agents: credentials into the sandbox, usage limits, resume."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adpbench.agent import (
    Mount,
    check_host_access,
    docker_command,
    match_infra_failure,
)
from adpbench.pilot import PilotAgent, PilotConfig, _plan_cells, cell_destination, load_pilot, run_pilot
from adpbench.report import load_runs, summarize

REPO = Path(__file__).resolve().parent.parent
PROBLEM_DIR = REPO / "problems" / "level1" / "001_dot_product"
HAS_TOOLS = all(shutil.which(tool) for tool in ("yosys", "iverilog", "vvp"))
requires_tools = unittest.skipUnless(HAS_TOOLS, "yosys and iverilog are required")


def docker(**kwargs) -> list[str]:
    return docker_command(
        Path("/tmp/env"), Path("/tmp/bundle"), "img", "bridge", ["true"], **kwargs
    )


class SandboxAccessTest(unittest.TestCase):
    def test_credentials_pass_by_name_only(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-secret"}):
            cmd = docker(env_names=["CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", cmd)
        self.assertNotIn("sk-secret", " ".join(cmd))

    def test_login_directory_is_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmd = docker(mounts=[Mount(tmp, "/home/agent/.codex")])
            self.assertIn(f"{Path(tmp).resolve()}:/home/agent/.codex", cmd)

    def test_mounts_cannot_cover_the_harness(self) -> None:
        for target in ("/work", "/work/x", "/adpbench", "/", "relative"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                docker(mounts=[Mount("/tmp", target)])

    def test_malformed_env_names_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            docker(env_names=["A=b"])

    def test_missing_credentials_fail_before_the_run(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            check_host_access(["CLAUDE_CODE_OAUTH_TOKEN"], [])
        with self.assertRaises(ValueError):
            check_host_access([], [Mount("/does/not/exist", "/home/agent/.codex")])

    def test_mount_spec_parses(self) -> None:
        self.assertEqual(Mount.parse("~/a:/b:ro"), Mount("~/a", "/b", read_only=True))
        with self.assertRaises(ValueError):
            Mount.parse("/a")


class InfraFailureTest(unittest.TestCase):
    def test_matching_line_is_reported(self) -> None:
        log = "thinking\nError: You've hit your usage limit. Try again at 5pm.\nbye"
        found = match_infra_failure(log, [r"usage limit"])
        self.assertEqual(found, "Error: You've hit your usage limit. Try again at 5pm.")

    def test_no_pattern_no_failure(self) -> None:
        self.assertEqual(match_infra_failure("usage limit", []), "")

    def test_secrets_are_redacted(self) -> None:
        found = match_infra_failure("401 bad API_KEY=abc", [r"401"])
        self.assertNotIn("abc", found)


class PlanTest(unittest.TestCase):
    def test_round_order_finishes_each_repetition_first(self) -> None:
        config = PilotConfig(
            name="x",
            problems=["p1", "p2"],
            agents=[PilotAgent("a", "true"), PilotAgent("b", "true")],
            repetitions=2,
            order="round",
        )
        cells = [(c.attempt, c.problem, c.agent.label) for c in _plan_cells(config)]
        self.assertEqual(cells[:4], [(1, "p1", "a"), (1, "p1", "b"), (1, "p2", "a"), (1, "p2", "b")])
        self.assertTrue(all(attempt == 2 for attempt, _, _ in cells[4:]))

    def test_config_parses_credentials(self) -> None:
        config = {
            "name": "cli",
            "problems": ["001_dot_product"],
            "order": "round",
            "sandbox": {"mode": "docker", "image": "img"},
            "agents": [
                {
                    "label": "codex",
                    "cmd": "codex exec hi",
                    "mounts": [{"host": "~/.adpbench/codex", "container": "/home/agent/.codex"}],
                    "env": ["X_TOKEN"],
                    "infra_patterns": ["usage limit"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilot.json"
            path.write_text(json.dumps(config))
            agent = load_pilot(path).agents[0]
        self.assertEqual(agent.env, ("X_TOKEN",))
        self.assertEqual(agent.mounts[0].container, "/home/agent/.codex")
        self.assertEqual(agent.infra_patterns, ("usage limit",))

    def test_credentials_without_docker_are_refused(self) -> None:
        config = {
            "name": "cli",
            "problems": ["001_dot_product"],
            "agents": [{"label": "c", "cmd": "true", "env": ["X_TOKEN"]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pilot.json"
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                load_pilot(path)


class ReviewedConfigTest(unittest.TestCase):
    """The committed cli-agent-v1 plan: four agents, one prompt, the same limits."""

    ROOT = Path(__file__).resolve().parent.parent / "pilot"
    LABELS = ["claude-code/claude-fable-5-1", "claude-code/claude-fable-5-2", "claude-code/claude-opus-5-5",
              "codex/gpt-6.1-sol", "codex/gpt-6-astra"]

    def test_every_agent_runs_the_same_experiment(self) -> None:
        config = load_pilot(self.ROOT / "cli-agent-v1.json")
        self.assertEqual([agent.label for agent in config.agents], self.LABELS)
        self.assertEqual((len(config.problems), config.repetitions, config.order), (8, 3, "round"))
        self.assertEqual(config.planned_runs, 120)
        self.assertEqual((config.sandbox, config.image), ("docker", "adpbench-agent-cli"))
        prompts = set()
        for agent in config.agents:
            cli, model = agent.label.split("/")
            self.assertEqual(agent.timeout_s, 7200)
            self.assertIn(f"--model {model} ", agent.cmd)
            prompts.add(agent.cmd.split("'")[1])
            self.assertTrue(agent.infra_patterns)
            if cli == "claude-code":
                self.assertTrue(agent.cmd.startswith("claude -p "))
                self.assertIn("--effort high", agent.cmd)
                self.assertIn("--disallowedTools WebSearch WebFetch", agent.cmd)
                self.assertEqual((agent.env, agent.mounts), (("CLAUDE_CODE_OAUTH_TOKEN",), ()))
            else:
                self.assertTrue(agent.cmd.startswith("codex exec "))
                self.assertIn("model_reasoning_effort=high", agent.cmd)
                self.assertIn("web_search=disabled", agent.cmd)
                self.assertEqual(agent.env, ())
                self.assertEqual([mount.container for mount in agent.mounts], ["/home/agent/.codex"])
        self.assertEqual(len(prompts), 1)

    def test_the_canary_checks_the_same_agents_on_one_problem(self) -> None:
        full = load_pilot(self.ROOT / "cli-agent-v1.json")
        canary = load_pilot(self.ROOT / "cli-agent-v1-canary.json")
        self.assertEqual((canary.problems, canary.repetitions), (["001_dot_product"], 1))
        self.assertEqual([(a.label, a.cmd, a.env, a.mounts, a.infra_patterns) for a in canary.agents],
                         [(a.label, a.cmd, a.env, a.mounts, a.infra_patterns) for a in full.agents])
        self.assertTrue(all(agent.timeout_s == 1800 for agent in canary.agents))


class CellDestinationTest(unittest.TestCase):
    def write(self, path: Path, record: dict) -> None:
        path.mkdir(parents=True)
        (path / "record.json").write_text(json.dumps(record))

    def test_new_failed_and_final_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertEqual(cell_destination(base, 1), base / "rep1")
            self.write(base / "rep1", {"infra_failure": "usage limit"})
            self.assertEqual(cell_destination(base, 1), base / "rep1-t2")
            self.write(base / "rep1-t2", {"infra_failure": ""})
            self.assertIsNone(cell_destination(base, 1))
            # rep10 is not a try of rep1.
            self.write(base / "rep10", {"infra_failure": "x"})
            self.assertIsNone(cell_destination(base, 1))


@requires_tools
class ResumeIntegrationTest(unittest.TestCase):
    def test_usage_limit_pauses_then_resume_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "limit-reset"
            solution = PROBLEM_DIR / "solutions" / "parallel.v"
            config = {
                "name": "limited",
                "problems": [str(PROBLEM_DIR)],
                "repetitions": 2,
                "order": "round",
                "sandbox": {"mode": "none"},
                "agents": [
                    {
                        "label": "scripted/limited",
                        "cmd": (
                            f"if [ -f '{marker}' ]; then cp '{solution}' dut.v; "
                            "else echo 'Error: usage limit reached'; fi"
                        ),
                        "timeout_s": 300,
                        "infra_patterns": [r"usage limit reached"],
                    }
                ],
            }
            path = Path(tmp) / "pilot.json"
            path.write_text(json.dumps(config))
            messages: list[str] = []
            pilot_dir = run_pilot(load_pilot(path), runs_root=Path(tmp) / "runs", echo=messages.append)

            cell = pilot_dir / "scripted-limited" / "001_dot_product"
            first = json.loads((cell / "rep1" / "record.json").read_text())
            self.assertEqual(first["infra_failure"], "Error: usage limit reached")
            self.assertFalse((cell / "rep2").exists(), "a paused agent must not spend more cells")
            self.assertTrue(any("--resume" in message for message in messages))
            before = summarize(load_runs(pilot_dir))["totals"]
            self.assertEqual(before["failures"]["infrastructure"], 1)
            self.assertEqual(before["failures"]["wrong_rtl"], 0)

            marker.write_text("")
            run_pilot(load_pilot(path), echo=lambda *_: None, resume=pilot_dir)

            self.assertTrue((cell / "rep1" / "record.json").is_file(), "the voided try is kept")
            self.assertTrue((cell / "rep1-t2" / "record.json").is_file())
            self.assertTrue((cell / "rep2" / "record.json").is_file())
            self.assertEqual(len(list(pilot_dir.glob("resume_*.json"))), 1)
            report = json.loads((pilot_dir / "report.json").read_text())
            bucket = report["labels"]["scripted/limited"]
            self.assertEqual(bucket["attempts"], 2)
            self.assertEqual(bucket["correct"], 2)

    def test_resume_refuses_a_different_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pilot_dir = Path(tmp)
            (pilot_dir / "plan.json").write_text(json.dumps({"name": "other"}))
            config = PilotConfig(name="mine", problems=[], agents=[PilotAgent("a", "true")])
            with self.assertRaises(ValueError):
                run_pilot(config, echo=lambda *_: None, resume=pilot_dir)


if __name__ == "__main__":
    unittest.main()
