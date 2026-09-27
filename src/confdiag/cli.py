"""Command-line entry point.

  confdiag list
  confdiag collect --fault F02 --variant a --runs 5
  confdiag collect --all --runs 5
  confdiag diagnose data/raw/F02-a/F02-a-20261101-101500
  confdiag evaluate
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .catalog import REPO_ROOT, load_catalog


def cmd_list(_args) -> None:
    for key, fv in load_catalog().items():
        print(f"{key:6} {fv.name}")
        print(f"       ground truth: {fv.config_path}")


def cmd_collect(args) -> None:
    from .collect import run_experiments

    catalog = load_catalog()
    if args.all:
        chosen = [fv for fv in catalog.values() if args.variant in (None, fv.variant)]
    else:
        if not args.fault:
            raise SystemExit("give --fault F0x (and optionally --variant) or --all")
        chosen = [fv for fv in catalog.values()
                  if fv.fault == args.fault and args.variant in (None, fv.variant)]
    if not chosen:
        raise SystemExit("no matching fault/variant in faults/faults.yaml")
    print(f"will run {len(chosen)} scenario(s) x {args.runs} run(s): {[fv.key for fv in chosen]}")
    run_experiments(chosen, args.runs, args.duration, args.warmup, Path(args.data_dir),
                    shuffle=not args.no_shuffle, seed=args.seed)


def cmd_diagnose(args) -> None:
    from .parse import load_run
    from .rules import diagnose

    run = load_run(Path(args.run_dir))
    d = diagnose(run)
    print(f"Diagnosis      : {d.fault}")
    print(f"Suspected field: {d.config_path}")
    print(f"Rule           : {d.rule}")
    print(f"Why            : {d.explanation}")
    if d.evidence:
        print("Evidence:")
        for e in d.evidence:
            print(f"  - {e}")
    truth = run.meta.get("label")
    if truth:
        ok = "correct" if truth == d.fault else "WRONG"
        print(f"\nGround truth   : {truth} {run.meta.get('config_path')} -> {ok}")


def cmd_evaluate(args) -> None:
    from .evaluate import evaluate

    evaluate(Path(args.data_dir), Path(args.out_dir), folds=args.folds, seed=args.seed)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="confdiag", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the fault catalogue").set_defaults(func=cmd_list)

    c = sub.add_parser("collect", help="run fault-injection experiments on the cluster")
    c.add_argument("--fault", help="e.g. F02")
    c.add_argument("--variant", help="a or b (default: all variants)")
    c.add_argument("--all", action="store_true", help="every fault in the catalogue")
    c.add_argument("--runs", type=int, default=1)
    c.add_argument("--duration", type=int, default=120, help="seconds to observe after injection")
    c.add_argument("--warmup", type=int, default=20, help="seconds of healthy traffic before injection")
    c.add_argument("--data-dir", default=str(REPO_ROOT / "data" / "raw"))
    c.add_argument("--no-shuffle", action="store_true")
    c.add_argument("--seed", type=int, default=0)
    c.set_defaults(func=cmd_collect)

    d = sub.add_parser("diagnose", help="diagnose one collected run")
    d.add_argument("run_dir")
    d.set_defaults(func=cmd_diagnose)

    e = sub.add_parser("evaluate", help="evaluate both diagnosers on all collected runs")
    e.add_argument("--data-dir", default=str(REPO_ROOT / "data" / "raw"))
    e.add_argument("--out-dir", default=str(REPO_ROOT / "results"))
    e.add_argument("--folds", type=int, default=5)
    e.add_argument("--seed", type=int, default=0)
    e.set_defaults(func=cmd_evaluate)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
