"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import DEFAULT_IMAGE, sha256_file
from .evaluate import DEV_SEEDS, EVAL_SEEDS, evaluate, evaluate_multi, record_baseline
from .problem import discover_problems, load_problem, repo_root


def _resolve(spec: str) -> Path:
    candidate = Path(spec)
    if (candidate / "dut.py").is_file():
        return candidate
    matches = [p for p in discover_problems() if p.name == spec or str(p).endswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"no problem matching '{spec}'")
    raise SystemExit(
        "ambiguous problem '{}':\n  {}".format(spec, "\n  ".join(str(m) for m in matches))
    )


def cmd_list(args: argparse.Namespace) -> int:
    for path in discover_problems():
        problem = load_problem(path)
        rel = path.relative_to(repo_root())
        params = " ".join(f"{k}={v}" for k, v in problem.params.items())
        marker = "baseline recorded" if problem.baseline_metrics.is_file() else "no baseline"
        print(f"{rel}\n    {params}\n    {marker} ({problem.transactions} transactions)")
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
        for key in ("synthesis_log", "sim_log", "protocol_log"):
            if key in result.metadata:
                print(f"\n--- {key} ---")
                print(result.metadata[key])
    return 0 if result.correct or result.synthesizable else 1


def cmd_env(args: argparse.Namespace) -> int:
    from .agent import build_environment

    problem = load_problem(_resolve(args.problem))
    dest = Path(args.dest).resolve() if args.dest else repo_root() / "runs" / problem.name / "env"
    build_environment(problem, dest, force=args.force, sandbox=args.sandbox)
    print(dest)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    from .agent import snapshot

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

    print(f"simulation ok - exact match on {len(result.metadata['cases'])} cases")
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
        label=args.label,
        attempt=args.attempt,
        sandbox=args.sandbox,
        image=args.image,
        network=args.network,
    )

    print(f"problem    {problem.name}")
    print(f"label      {record.label}")
    print(f"agent      {record.agent_cmd}")
    print(f"sandbox    {record.sandbox}")
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
        elif "error" in record.result:
            print(f"result     ERROR - {record.result['error']}")
        else:
            print(f"result     FAIL - {metadata.get('correctness', 'incorrect')}")
            print(f"           {record.result['cells']} cells")

    if dest:
        print(f"run        {dest}")
    return 0 if record.score() > 0 else 1


def cmd_report(args: argparse.Namespace) -> int:
    from .report import load_runs, markdown, summarize

    runs_root = Path(args.runs).resolve() if args.runs else repo_root() / "runs"
    report = summarize(load_runs(runs_root))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(markdown(report), end="")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-score a frozen run and check the number reproduces."""
    import json
    from .evaluate import evaluate_multi

    run_dir = Path(args.run).resolve()
    record_path = run_dir / "record.json"
    if not record_path.is_file():
        raise SystemExit(f"no record.json under {run_dir}")
    record = json.loads(record_path.read_text())

    problem = load_problem(_resolve(args.problem or record["problem"]))
    for candidate in (
        run_dir.parent / f"{run_dir.name}_frozen" / "dut.v",
        run_dir / "clean" / "dut.v",
        run_dir / "dut.v",
    ):
        if candidate.is_file() and not candidate.is_symlink():
            submission = candidate
            break
    else:
        raise SystemExit(f"no frozen submission under {run_dir}")

    result = evaluate_multi(
        problem, [submission], seeds=EVAL_SEEDS, source=str(submission), tag="replay"
    )
    recorded = record.get("result") or {}
    print(f"problem    {problem.name}")
    print(f"submission {submission}")
    print(f"replayed   correct={result.correct} cells={result.cells} cycles={result.cycles} "
          f"ratio={result.ratio:.4f}")
    if recorded.get("correct") is not None:
        print(f"recorded   correct={recorded.get('correct')} cells={recorded.get('cells')} "
              f"cycles={recorded.get('cycles')} ratio={float(recorded.get('ratio', -1)):.4f}")

    matches = (
        bool(recorded.get("correct")) == result.correct
        and recorded.get("cells") == result.cells
        and recorded.get("cycles") == result.cycles
    )
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        recorded_sha = manifest.get("submission_sha256", "")
        if recorded_sha and recorded_sha != sha256_file(submission):
            print("replay     MISMATCH - frozen submission hash differs from the manifest")
            return 1
    if recorded.get("ratio") is not None and result.ratio >= 0:
        if abs(float(recorded["ratio"]) - result.ratio) > 1e-6:
            matches = False
            print("replay     MISMATCH - ratio differs (baseline or case set changed)")
    print(f"replay     {'MATCH' if matches else 'MISMATCH'}")
    return 0 if matches else 1


def cmd_pilot(args: argparse.Namespace) -> int:
    from .pilot import load_pilot, run_pilot

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


def cmd_site(args: argparse.Namespace) -> int:
    from .site import export_sanity, export_site

    if args.site_command == "export":
        out = export_site(args.pilot, args.out, sanity_file=args.sanity)
        print(f"exported -> {out}")
    elif args.site_command == "sanity":
        out = export_sanity(args.out)
        print(f"sanity metrics -> {out}")
    return 0


def cmd_seeds(args: argparse.Namespace) -> int:
    from . import sim, synth

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
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list problems")
    p_list.set_defaults(func=cmd_list)

    p_base = sub.add_parser("baseline", help="score baseline.v and freeze it as the denominator")
    p_base.add_argument("problem")
    p_base.set_defaults(func=cmd_baseline)

    p_run = sub.add_parser("run", help="evaluate a Verilog submission")
    p_run.add_argument("problem")
    p_run.add_argument("--file", "-f", help="Verilog file (default: the problem's baseline.v)")
    p_run.add_argument("--seed", default=0)
    p_run.add_argument("--json", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_env = sub.add_parser("env", help="build an agent task directory")
    p_env.add_argument("problem")
    p_env.add_argument("--dest")
    p_env.add_argument("--force", action="store_true")
    p_env.add_argument("--sandbox", choices=("none", "docker"), default="none")
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
    p_agent.add_argument("--label", default="", help="evaluated system name for reports")
    p_agent.add_argument("--attempt", type=int, default=1)
    p_agent.add_argument("--sandbox", choices=("none", "docker"), default="none")
    p_agent.add_argument("--image", default=DEFAULT_IMAGE)
    p_agent.add_argument("--network", default="bridge")
    p_agent.set_defaults(func=cmd_agent)

    p_report = sub.add_parser("report", help="aggregate run records into a scoreboard")
    p_report.add_argument("--runs", help="runs directory (default: runs/)")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=cmd_report)

    p_replay = sub.add_parser("replay", help="re-score a frozen run and check the number")
    p_replay.add_argument("run", help="a run directory containing record.json")
    p_replay.add_argument("--problem", help="override the problem recorded in the run")
    p_replay.set_defaults(func=cmd_replay)

    p_pilot = sub.add_parser("pilot", help="run a model x problem x repetition matrix")
    p_pilot.add_argument("--config", required=True, help="pilot config JSON")
    p_pilot.add_argument("--runs", help="runs directory (default: runs/)")
    p_pilot.add_argument("--sandbox", choices=("none", "docker"))
    p_pilot.add_argument("--repetitions", type=int)
    p_pilot.add_argument("--jobs", type=int, default=1, help="parallel run cells")
    p_pilot.add_argument("--publish", help="copy plan and report files here")
    p_pilot.set_defaults(func=cmd_pilot)

    p_seeds = sub.add_parser("seeds", help="publish the evaluation seed manifest")
    p_seeds.add_argument("--out")
    p_seeds.set_defaults(func=cmd_seeds)

    p_site = sub.add_parser("site", help="export data for the static leaderboard site")
    site_sub = p_site.add_subparsers(dest="site_command", required=True)
    p_export = site_sub.add_parser("export", help="export a frozen pilot directory")
    p_export.add_argument("--pilot", required=True, help="pilot run directory")
    p_export.add_argument("--out", required=True, help="site data directory")
    p_export.add_argument("--sanity", help="sanity metrics file (adpbench site sanity)")
    p_export.set_defaults(func=cmd_site)
    p_sanity = site_sub.add_parser(
        "sanity", help="evaluate every sanity solution into a metrics file"
    )
    p_sanity.add_argument("--out", required=True)
    p_sanity.set_defaults(func=cmd_site)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
