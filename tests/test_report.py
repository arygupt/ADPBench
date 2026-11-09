"""Aggregation must count failures, not silently average them away.

Review item 7: a correctness-gated geometric mean alone can rank a system
that solved one problem at 10x above one that solved all four at 2x.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adpbench.pricing import PRICES, run_cost
from adpbench.report import RunSummary, load_runs, markdown, summarize


def _run(
    *,
    problem: str = "001_dot_product",
    label: str = "model-a",
    attempt: int = 1,
    correct: bool = False,
    ratio: float = -1.0,
    stage: str = "",
    error: str = "",
    timed_out: bool = False,
) -> RunSummary:
    return RunSummary(
        path=f"/tmp/{problem}/{label}/{attempt}",
        problem=problem,
        label=label,
        attempt=attempt,
        correct=correct,
        ratio=ratio,
        stage=stage,
        error=error,
        timed_out=timed_out,
    )


class SummarizeTest(unittest.TestCase):
    def test_one_brilliant_solve_does_not_outrank_coverage(self) -> None:
        runs = [
            _run(label="spiky", problem="a", correct=True, ratio=10.0, stage="ok"),
            _run(label="spiky", problem="b", stage="incorrect"),
            _run(label="spiky", problem="c", stage="incorrect"),
            _run(label="spiky", problem="d", stage="incorrect"),
            _run(label="solid", problem="a", correct=True, ratio=2.0, stage="ok"),
            _run(label="solid", problem="b", correct=True, ratio=2.0, stage="ok"),
            _run(label="solid", problem="c", correct=True, ratio=2.0, stage="ok"),
            _run(label="solid", problem="d", correct=True, ratio=2.0, stage="ok"),
        ]
        report = summarize(runs)
        spiky = report["labels"]["spiky"]
        solid = report["labels"]["solid"]
        self.assertEqual(spiky["geomean_ratio_successful"], 10.0)
        self.assertEqual(spiky["correctness_rate"], 0.25)
        self.assertEqual(solid["beat_baseline_rate"], 1.0)
        self.assertGreater(solid["beat_baseline_rate"], spiky["beat_baseline_rate"])

    def test_infrastructure_failures_are_split_from_wrong_rtl(self) -> None:
        runs = [
            _run(correct=True, ratio=1.5, stage="ok"),
            _run(stage="incorrect"),
            _run(error="RuntimeError: simulator missing"),
            _run(timed_out=True),
            _run(stage="no result"),
        ]
        bucket = summarize(runs)["labels"]["model-a"]
        self.assertEqual(bucket["failures"]["wrong_rtl"], 1)
        self.assertEqual(bucket["failures"]["infrastructure"], 3)
        self.assertEqual(bucket["correctness_rate"], 0.2)

    def test_attempts_and_repetitions_are_disclosed(self) -> None:
        runs = [
            _run(attempt=1, correct=True, ratio=1.2, stage="ok"),
            _run(attempt=2, correct=True, ratio=3.0, stage="ok"),
        ]
        bucket = summarize(runs)["labels"]["model-a"]
        self.assertEqual(bucket["attempts"], 2)
        self.assertEqual(bucket["repetitions"], 2)
        self.assertAlmostEqual(bucket["geomean_ratio_successful"], (1.2 * 3.0) ** 0.5, places=3)

    def test_markdown_mentions_the_primary_metric(self) -> None:
        text = markdown(summarize([_run(correct=True, ratio=2.0, stage="ok")]))
        self.assertIn("beat baseline", text)
        self.assertIn("correctness", text)


class LoadRunsTest(unittest.TestCase):
    def test_nested_fabricated_records_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "pilot_x" / "m" / "001_dot_product" / "rep1"
            real.mkdir(parents=True)
            (real / "record.json").write_text(
                json.dumps(
                    {
                        "problem": "001_dot_product",
                        "label": "m",
                        "result": {"correct": False, "metadata": {}},
                    }
                )
            )
            fake = real / "nested" / "deeper"
            fake.mkdir(parents=True)
            (fake / "record.json").write_text(
                json.dumps(
                    {
                        "problem": "001_dot_product",
                        "label": "m",
                        "result": {"correct": True, "ratio": 999.0, "metadata": {}},
                    }
                )
            )
            runs = load_runs(root)
            self.assertEqual(len(runs), 1)
            self.assertFalse(runs[0].correct)

    def test_records_are_read_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "model-a" / "001_dot_product" / "rep1"
            run_dir.mkdir(parents=True)
            (run_dir / "record.json").write_text(
                json.dumps(
                    {
                        "problem": "001_dot_product",
                        "agent_cmd": "opencode run",
                        "label": "model-a",
                        "attempt": 1,
                        "timed_out": False,
                        "result": {
                            "correct": True,
                            "ratio": 2.5,
                            "metadata": {"stage": "ok"},
                        },
                    }
                )
            )
            runs = load_runs(tmp)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].label, "model-a")
            self.assertTrue(runs[0].correct)


FABLE = "anthropic/claude-fable-5-2 [agent-assisted-v2]"


def _priced_record(root: Path, problem: str, *, correct: bool, ratio: float, tokens: dict | None,
                   label: str = FABLE, incomplete: bool = False) -> None:
    run_dir = root / "anthropic-claude-fable-5-2" / problem / "rep1"
    if label != FABLE:
        run_dir = root / "other" / problem / "rep1"
    run_dir.mkdir(parents=True)
    generation = {"incomplete_usage": incomplete}
    if tokens is not None:
        generation["billable_tokens"] = tokens
    record = {
        "problem": problem,
        "label": label,
        "outcome": "correct" if correct else "incorrect",
        "execution_health": "completed",
        "result": {"correct": correct, "ratio": ratio, "metadata": {"stage": "ok"}},
        "manifest": {"generation": generation},
    }
    (run_dir / "record.json").write_text(json.dumps(record))


class CostTest(unittest.TestCase):
    """Dollars per baseline-beating design: every attempt is paid for, only wins count."""

    def test_cost_is_tokens_at_list_price_by_class(self) -> None:
        tokens = {"input": 1_000_000, "cache_read": 2_000_000, "cache_write": 100_000, "output": 500_000}
        # 8 + 2 x 0.20 + 0.1 x 10 + 0.5 x 40
        self.assertAlmostEqual(run_cost(FABLE, {"billable_tokens": tokens}), 29.4)
        self.assertEqual(run_cost(FABLE, {"billable_tokens": {name: 0 for name in tokens}}), 0.0)
        # No receipt, a malformed receipt, or a subscription model: unknown, never zero.
        self.assertIsNone(run_cost(FABLE, {}))
        self.assertIsNone(run_cost(FABLE, {"billable_tokens": {**tokens, "output": -1}}))
        self.assertIsNone(run_cost(FABLE, {"billable_tokens": {**tokens, "output": 1.5}}))
        self.assertIsNone(run_cost("opencode-go/kimi-k3 [agent-assisted-v2]", {"billable_tokens": tokens}))
        for key, price in PRICES.items():
            self.assertEqual(set(price), {"input", "cache_read", "cache_write", "output"}, key)
            self.assertLess(price["cache_read"], price["input"], key)

    def test_failed_attempts_count_toward_the_cost_of_each_win(self) -> None:
        million = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 100_000}  # $4.00 each
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _priced_record(root, "005_argmax", correct=True, ratio=2.0, tokens=million)
            _priced_record(root, "006_topk", correct=True, ratio=0.9, tokens=million)
            _priced_record(root, "007_histogram", correct=False, ratio=-1.0, tokens=million)
            report = summarize(load_runs(root))
        bucket = report["labels"][FABLE]
        self.assertEqual((bucket["attempts"], bucket["beating_baseline"]), (3, 1))
        self.assertEqual((bucket["cost_usd"], bucket["usd_per_baseline_beating"], bucket["cost_incomplete"]),
                         (12.0, 12.0, False))
        text = markdown(report)
        self.assertIn("$ per baseline-beating design", text)
        self.assertIn("| $12.00 | $12.00 |", text)

    def test_unknown_spend_is_marked_as_a_lower_bound_and_no_win_has_no_ratio(self) -> None:
        tokens = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 100_000}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _priced_record(root, "005_argmax", correct=False, ratio=-1.0, tokens=tokens, incomplete=True)
            _priced_record(root, "006_topk", correct=True, ratio=3.0, tokens=None, label="other [single-shot]")
            report = summarize(load_runs(root))
        fable = report["labels"][FABLE]
        self.assertEqual((fable["cost_usd"], fable["usd_per_baseline_beating"], fable["cost_incomplete"]),
                         (4.0, None, True))
        self.assertNotIn("cost_usd", report["labels"]["other [single-shot]"])
        self.assertTrue(report["totals"]["cost_incomplete"])  # one run has no price at all
        rows = markdown(report).splitlines()
        self.assertTrue(any(FABLE in row and row.endswith("| ≥$4.00 | — |") for row in rows), rows)
        self.assertTrue(any("other [single-shot]" in row and row.endswith("| — | — |") for row in rows), rows)

    def test_reports_without_priced_runs_are_unchanged(self) -> None:
        report = summarize([_run(correct=True, ratio=2.0, stage="ok")])
        self.assertNotIn("cost_usd", report["totals"])
        self.assertNotIn("cost", markdown(report).replace("correct", ""))


if __name__ == "__main__":
    unittest.main()
