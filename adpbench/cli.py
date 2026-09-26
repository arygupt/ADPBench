"""Command line interface. Run `adpbench <command> --help` for each command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import sim, synth
from .agent import DEFAULT_IMAGE, run_agent
from .environment import build_environment, snapshot
from .evaluate import evaluate, evaluate_multi, record_baseline
from .hashing import sha256_file
from .pilot import load_pilot, run_pilot
from .problem import discover_problems, find_problem, load_problem, repo_root
from .report import load_runs, markdown, summarize
from .seeds import DEV_SEEDS, EVAL_SEEDS
from .site import export_sanity, export_site


def cmd_list(args: argparse.Namespace) -> int:
    for path in discover_problems():
        problem = load_problem(path)
        relative = path.relative_to(repo_root())
        params = " ".join(f"{name}={value}" for name, value in problem.params.items())
        marker = "baseline recorded" if problem.baseline_metrics.is_file() else "no baseline"
        print(f"{relative}\n    {params}\n    {marker} ({problem.transactions} transactions)")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    problem = load_problem(find_problem(args.problem))
    result = record_baseline(problem, [problem.baseline_rtl])
    if result.correct:
        print(f"{problem.name}: baseline recorded")
        print(f"  cells={result.cells} cycles={result.cycles} adp={result.adp:.0f}")
        print(f"  frozen -> {problem.baseline_metrics.relative_to(repo_root())}")
        return 0

    print(f"{problem.name}: baseline FAILED")
    print(f"  {result.metadata.get('correctness')}")
    if "synthesis_log" in result.metadata:
        print(result.metadata["synthesis_log"])
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    problem = load_problem(find_problem(args.problem))
    rtl = Path(args.file).resolve() if args.file else problem.baseline_rtl
    if not rtl.is_file():
        raise SystemExit(f"no such RTL file: {rtl}")

    result = evaluate(problem, [rtl], seed=args.seed, source=rtl.name)

    if args.json:
        print(result.to_json())
        return 0 if result.correct or result.synthesizable else 1

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

    if not result.correct:
        for key in ("synthesis_log", "sim_log", "protocol_log"):
            if key in result.metadata:
                print(f"\n--- {key} ---")
                print(result.metadata[key])
    return 0 if result.correct or result.synthesizable else 1


def cmd_env(args: argparse.Namespace) -> int:
    problem = load_problem(find_problem(args.problem))
    if args.dest:
        dest = Path(args.dest).resolve()
    else:
        dest = repo_root() / "runs" / problem.name / "env"
    build_environment(problem, dest, force=args.force, sandbox=args.sandbox)
    print(dest)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Dev-seed feedback, run by an agent's check.sh."""
    problem = load_problem(find_problem(args.problem))
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

    print(f"simulation ok - exact match on {len(result.metadata['cases'])} cases")
    print(f"cycles     {result.cycles}")
    if result.ratio > 0:
        print(f"score      {result.ratio:.2f}x baseline")
    else:
        print("score      no baseline recorded")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    problem = load_problem(find_problem(args.problem))
    dest = Path(args.dest).resolve() if args.dest else None
    record = run_agent(
        problem,
        agent_cmd=args.cmd,
        dest=dest,
        timeout_s=args.timeout,
        label=args.label,
        attempt=args.attempt,
        sandbox=args.sandbox,
        image=args.image,
        network=args.network,
    )

    timed_out = " (TIMED OUT)" if record.timed_out else ""
    print(f"problem    {problem.name}")
    print(f"label      {record.label}")
    print(f"agent      {record.agent_cmd}")
    print(f"sandbox    {record.sandbox}")
    print(f"duration   {record.duration_s}s{timed_out}")
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
        elif "error" in record.result:
            print(f"result     ERROR - {record.result['error']}")
        else:
            print(f"result     FAIL - {metadata.get('correctness', 'incorrect')}")
            print(f"           {record.result['cells']} cells")

    if dest:
        print(f"run        {dest}")
    return 0 if record.score() > 0 else 1


def cmd_report(args: argparse.Namespace) -> int:
    runs_root = Path(args.runs).resolve() if args.runs else repo_root() / "runs"
    report = summarize(load_runs(runs_root))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(markdown(report), end="")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-score a frozen run and check that the recorded numbers reproduce."""
    run_dir = Path(args.run).resolve()
    record_path = run_dir / "record.json"
    if not record_path.is_file():
        raise SystemExit(f"no record.json under {run_dir}")
    record = json.loads(record_path.read_text())

    problem = load_problem(find_problem(args.problem or record["problem"]))
    submission = _find_frozen_submission(run_dir)
    if submission is None:
        raise SystemExit(f"no frozen submission under {run_dir}")

    result = evaluate_multi(problem, [submission], seeds=EVAL_SEEDS, source=str(submission), tag="replay")
    recorded = record.get("result") or {}
    print(f"problem    {problem.name}")
    print(f"submission {submission}")
    print(
        f"replayed   correct={result.correct} cells={result.cells} cycles={result.cycles} "
        f"ratio={result.ratio:.4f}"
    )
    if recorded.get("correct") is not None:
        recorded_ratio = float(recorded.get("ratio", -1))
        print(
            f"recorded   correct={recorded.get('correct')} cells={recorded.get('cells')} "
            f"cycles={recorded.get('cycles')} ratio={recorded_ratio:.4f}"
        )

    matches = (
        bool(recorded.get("correct")) == result.correct
        and recorded.get("cells") == result.cells
        and recorded.get("cycles") == result.cycles
    )

    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        recorded_sha = json.loads(manifest_path.read_text()).get("submission_sha256", "")
        if recorded_sha and recorded_sha != sha256_file(submission):
            print("replay     MISMATCH - frozen submission hash differs from the manifest")
            return 1

    if recorded.get("ratio") is not None and result.ratio >= 0:
        if abs(float(recorded["ratio"]) - result.ratio) > 1e-6:
            matches = False
            print("replay     MISMATCH - ratio differs (baseline or case set changed)")

    print(f"replay     {'MATCH' if matches else 'MISMATCH'}")
    return 0 if matches else 1


def _find_frozen_submission(run_dir: Path) -> Path | None:
    """The scored copy of dut.v for a run, checking current and older layouts."""
    candidates = (
        run_dir.parent / f"{run_dir.name}_frozen" / "dut.v",
        run_dir / "clean" / "dut.v",
        run_dir / "dut.v",
    )
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def cmd_pilot(args: argparse.Namespace) -> int:
    config = load_pilot(args.config)
    if args.sandbox:
        config.sandbox = args.sandbox
    if args.repetitions is not None:
        config.repetitions = args.repetitions
    run_pilot(
        config,
        runs_root=Path(args.runs).resolve() if args.runs else None,
        jobs=args.jobs,
        publish=Path(args.publish) if args.publish else None,
    )
    return 0


def cmd_site_export(args: argparse.Namespace) -> int:
    out = export_site(args.pilot, args.out, sanity_file=args.sanity)
    print(f"exported -> {out}")
    return 0


def cmd_site_sanity(args: argparse.Namespace) -> int:
    out = export_sanity(args.out)
    print(f"sanity metrics -> {out}")
    return 0


def cmd_seeds(args: argparse.Namespace) -> int:
    payload = {
        "tool_versions": {"yosys": synth.tool_version(), "iverilog": sim.tool_version()},
        "problems": {},
    }
    for path in discover_problems():
        problem = load_problem(path)
        payload["problems"][problem.name] = {
            "seeds": list(EVAL_SEEDS),
            "directed": problem.directed_cases,
            "transactions": problem.transactions,
            "input_lens": problem.input_lens,
        }
    text = json.dumps(payload, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text)
        print(args.out)
    else:
        print(text, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adpbench", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("list", help="list problems")
    p.set_defaults(func=cmd_list)

    p = commands.add_parser("baseline", help="score baseline.v and freeze it as the denominator")
    p.add_argument("problem")
    p.set_defaults(func=cmd_baseline)

    p = commands.add_parser("run", help="evaluate a Verilog submission")
    p.add_argument("problem")
    p.add_argument("--file", "-f", help="Verilog file (default: the problem's baseline.v)")
    p.add_argument("--seed", default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)

    p = commands.add_parser("env", help="build an agent task directory")
    p.add_argument("problem")
    p.add_argument("--dest")
    p.add_argument("--force", action="store_true")
    p.add_argument("--sandbox", choices=("none", "docker"), default="none")
    p.set_defaults(func=cmd_env)

    p = commands.add_parser("check", help="dev-seed feedback for an agent (synth + sim + score)")
    p.add_argument("--problem", required=True)
    p.add_argument("--file", "-f", default="dut.v")
    p.add_argument("--no-snapshot", action="store_true")
    p.set_defaults(func=cmd_check)

    p = commands.add_parser("agent", help="run an agent CLI against a problem, then audit and score")
    p.add_argument("problem")
    p.add_argument("--cmd", required=True, help="shell command for the agent CLI")
    p.add_argument("--timeout", type=int, default=1800, help="seconds")
    p.add_argument("--dest")
    p.add_argument("--label", default="", help="evaluated system name for reports")
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--sandbox", choices=("none", "docker"), default="none")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--network", default="bridge")
    p.set_defaults(func=cmd_agent)

    p = commands.add_parser("report", help="aggregate run records into a scoreboard")
    p.add_argument("--runs", help="runs directory (default: runs/)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = commands.add_parser("replay", help="re-score a frozen run and check the number")
    p.add_argument("run", help="a run directory containing record.json")
    p.add_argument("--problem", help="override the problem recorded in the run")
    p.set_defaults(func=cmd_replay)

    p = commands.add_parser("pilot", help="run a model x problem x repetition matrix")
    p.add_argument("--config", required=True, help="pilot config JSON")
    p.add_argument("--runs", help="runs directory (default: runs/)")
    p.add_argument("--sandbox", choices=("none", "docker"))
    p.add_argument("--repetitions", type=int)
    p.add_argument("--jobs", type=int, default=1, help="parallel run cells")
    p.add_argument("--publish", help="copy plan and report files here")
    p.set_defaults(func=cmd_pilot)

    p = commands.add_parser("seeds", help="publish the evaluation seed manifest")
    p.add_argument("--out")
    p.set_defaults(func=cmd_seeds)

    site = commands.add_parser("site", help="export data for the static leaderboard site")
    site_commands = site.add_subparsers(dest="site_command", required=True)
    p = site_commands.add_parser("export", help="export a frozen pilot directory")
    p.add_argument("--pilot", required=True, help="pilot run directory")
    p.add_argument("--out", required=True, help="site data directory")
    p.add_argument("--sanity", help="sanity metrics file (adpbench site sanity)")
    p.set_defaults(func=cmd_site_export)
    p = site_commands.add_parser("sanity", help="evaluate every sanity solution into a metrics file")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_site_sanity)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
