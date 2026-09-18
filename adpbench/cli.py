"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .evaluate import evaluate, record_baseline
from .problem import load_problem, repo_root


def _problems_root() -> Path:
    return repo_root() / "problems"


def _discover() -> list[Path]:
    root = _problems_root()
    if not root.is_dir():
        return []
    return sorted(p.parent for p in root.glob("*/*/dut.py"))


def _resolve(spec: str) -> Path:
    candidate = Path(spec)
    if (candidate / "dut.py").is_file():
        return candidate
    matches = [p for p in _discover() if p.name == spec or str(p).endswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"no problem matching '{spec}'")
    raise SystemExit(
        "ambiguous problem '{}':\n  {}".format(spec, "\n  ".join(str(m) for m in matches))
    )


def cmd_list(args: argparse.Namespace) -> int:
    for path in _discover():
        problem = load_problem(path)
        rel = path.relative_to(repo_root())
        params = " ".join(f"{k}={v}" for k, v in problem.params.items())
        marker = "baseline recorded" if problem.baseline_metrics.is_file() else "no baseline"
        print(f"{rel}\n    {params}\n    {marker}")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    problem = load_problem(_resolve(args.problem))
    result = record_baseline(problem, [problem.baseline_rtl])
    if result.correct:
        print(f"{problem.name}: baseline recorded")
        print(f"  cells={result.cells} cycles={result.cycles} adp={result.adp:.0f}")
        print(f"  frozen -> {problem.baseline_metrics.relative_to(repo_root())}")
    else:
        print(f"{problem.name}: baseline FAILED")
        print(f"  {result.metadata.get('correctness')}")
        if "synthesis_log" in result.metadata:
            print(result.metadata["synthesis_log"])
    return 0 if result.correct else 1


def cmd_run(args: argparse.Namespace) -> int:
    problem = load_problem(_resolve(args.problem))
    rtl = Path(args.file).resolve() if args.file else problem.baseline_rtl
    if not rtl.is_file():
        raise SystemExit(f"no such RTL file: {rtl}")

    result = evaluate(problem, [rtl], seed=args.seed, source=rtl.name)

    if args.json:
        print(result.to_json())
    else:
        print(f"{problem.name} <- {rtl.name}")
        print(f"  synthesizable : {result.synthesizable}")
        print(f"  cells         : {result.cells}")
        print(f"  cycles        : {result.cycles}")
        print(f"  correct       : {result.correct}  ({result.metadata.get('correctness', '')})")
        if result.correct and result.ratio > 0:
            print(f"  adp           : {result.adp:.0f}")
            print(f"  score         : {result.ratio:.2f}x baseline")
        elif result.correct:
            print("  score         : no baseline recorded (run `adpbench baseline`)")

    if not result.correct and not args.json:
        for key in ("synthesis_log", "sim_log"):
            if key in result.metadata:
                print(f"\n--- {key} ---")
                print(result.metadata[key])
    return 0 if result.correct or result.synthesizable else 1


def cmd_env(args: argparse.Namespace) -> int:
    from .agent import build_environment

    problem = load_problem(_resolve(args.problem))
    dest = Path(args.dest).resolve() if args.dest else repo_root() / "runs" / problem.name / "env"
    build_environment(problem, dest, force=args.force)
    print(dest)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .agent import snapshot
    from .evaluate import DEV_SEEDS, evaluate_multi

    problem = load_problem(_resolve(args.problem))
    rtl = Path(args.file).resolve()
    if not rtl.is_file():
        raise SystemExit(f"no such RTL file: {rtl}")

    if not args.no_snapshot:
        saved = snapshot(Path.cwd(), rtl)
        if saved:
            print(f"snapshot   {saved.name}")

    result = evaluate_multi(problem, [rtl], seeds=DEV_SEEDS, source=rtl.name, tag="dev")

    print(f"problem    {problem.name}")
    if not result.synthesizable:
        print("synthesis  FAIL")
        print(result.metadata.get("synthesis_log", "")[-1500:])
        return 1
    print(f"synthesis  ok - {result.cells} cells")

    if not result.correct:
        print(f"simulation FAIL - {result.metadata.get('correctness', 'incorrect')}")
        return 1

    print(f"simulation ok - exact match on {len(DEV_SEEDS)} seeds")
    print(f"cycles     {result.cycles}")
    if result.ratio > 0:
        print(f"score      {result.ratio:.2f}x baseline")
    else:
        print("score      no baseline recorded")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from .agent import run_agent

    problem = load_problem(_resolve(args.problem))
    dest = Path(args.dest).resolve() if args.dest else None
    record = run_agent(
        problem,
        agent_cmd=args.cmd,
        dest=dest,
        timeout_s=args.timeout,
    )

    print(f"problem    {problem.name}")
    print(f"agent      {args.cmd}")
    print(f"duration   {record.duration_s}s" + (" (TIMED OUT)" if record.timed_out else ""))
    print(f"history    {len(record.history)} submission(s) tested")
    print(f"audit      {'ok' if record.audit['ok'] else 'REJECTED'}")
    for violation in record.audit["violations"]:
        where = f"line {violation['line']}: " if violation["line"] else ""
        print(f"           {where}{violation['reason']}")

    if record.result:
        metadata = record.result.get("metadata", {})
        if record.result.get("correct"):
            print(f"score      {record.score():.2f}x baseline")
            print(f"           {record.result['cells']} cells, {record.result['cycles']} cycles")
        else:
            print(f"result     FAIL - {metadata.get('correctness', 'incorrect')}")
            print(f"           {record.result['cells']} cells")

    if dest:
        print(f"run        {dest}")
    return 0 if record.score() > 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adpbench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list problems")
    p_list.set_defaults(func=cmd_list)

    p_base = sub.add_parser("baseline", help="score baseline.v and freeze it as the denominator")
    p_base.add_argument("problem")
    p_base.set_defaults(func=cmd_baseline)

    p_run = sub.add_parser("run", help="evaluate a Verilog submission")
    p_run.add_argument("problem")
    p_run.add_argument("--file", "-f", help="Verilog file (default: the problem's baseline.v)")
    p_run.add_argument("--seed", type=int, default=0)
    p_run.add_argument("--json", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_env = sub.add_parser("env", help="build an agent task directory")
    p_env.add_argument("problem")
    p_env.add_argument("--dest")
    p_env.add_argument("--force", action="store_true")
    p_env.set_defaults(func=cmd_env)

    p_check = sub.add_parser("check", help="dev-seed feedback for an agent (synth + sim + score)")
    p_check.add_argument("--problem", required=True)
    p_check.add_argument("--file", "-f", default="dut.v")
    p_check.add_argument("--no-snapshot", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_agent = sub.add_parser("agent", help="run an agent CLI against a problem, then audit and score")
    p_agent.add_argument("problem")
    p_agent.add_argument("--cmd", required=True, help="shell command for the agent CLI")
    p_agent.add_argument("--timeout", type=int, default=1800, help="seconds")
    p_agent.add_argument("--dest")
    p_agent.set_defaults(func=cmd_agent)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
