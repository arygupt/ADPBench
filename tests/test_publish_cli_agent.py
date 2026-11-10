import json
import tempfile
import unittest
from pathlib import Path

from scripts.publish_cli_agent import collect, register


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class PublishCliAgentTest(unittest.TestCase):
    def pilot(self, root: Path, done: dict[str, list[str]]) -> Path:
        pilot = root / "pilot"
        agents = [{"label": label, "cmd": "codex exec -c model_reasoning_effort=high"} for label in done]
        write(pilot / "plan.json", {"problems": ["001_dot_product", "002_gemv"], "repetitions": 1, "agents": agents})
        for label, reps in done.items():
            for rep in reps:
                problem, attempt = rep.split(":")
                infra = attempt.endswith("!")
                write(pilot / label.replace("/", "-") / problem / attempt.rstrip("!") / "record.json",
                      {"label": label, "infra_failure": "usage limit" if infra else None})
        return pilot

    def test_only_models_with_every_cell_scored_are_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            pilot = self.pilot(Path(tmp), {
                "codex/gpt-6.1-sol": ["001_dot_product:rep1", "002_gemv:rep1!", "002_gemv:rep1-t2"],
                "claude-code/claude-fable-5-2": ["001_dot_product:rep1", "002_gemv:rep1!"],
            })
            plan, finished, unfinished = collect([pilot])
        self.assertEqual(list(finished), ["codex/gpt-6.1-sol"])
        self.assertEqual(unfinished, {"claude-code/claude-fable-5-2": 1})
        self.assertEqual([agent["label"] for agent in plan["agents"]], ["codex/gpt-6.1-sol"])

    def test_register_appends_once_and_keeps_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "evaluations.json"
            write(catalog, {"schema_version": 1, "default": "go", "evaluations": [
                {"id": "go", "label": "Go", "path": "data/go/leaderboard.json", "protocol": "agent-assisted-v2"}]})
            register(catalog)
            register(catalog)
            data = json.loads(catalog.read_text())
        self.assertEqual(data["default"], "go")
        self.assertEqual([item["id"] for item in data["evaluations"]], ["go", "cli-agent-v1"])


if __name__ == "__main__":
    unittest.main()
