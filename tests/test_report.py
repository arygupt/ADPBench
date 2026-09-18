"""Aggregation must count failures, not silently average them away.

Review item 7: a correctness-gated geometric mean alone can rank a system
that solved one problem at 10x above one that solved all four at 2x.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
