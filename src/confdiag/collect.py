"""Fault injection runs.

Output: data/raw/<fault>-<variant>/<run_id>/ with meta.json, object state
(*.json) and logs/<pod>__<container>[__previous].log
"""
from __future__ import annotations

import json
import random
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .catalog import REPO_ROOT, FaultVariant

NS = "demo"
KINDS = ["pods", "services", "endpoints", "deployments", "networkpolicies", "events"]


def sh(cmd: str, timeout: int = 300, check: bool = True) -> str:
    res = subprocess.run(
        cmd, shell=True, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"command failed ({res.returncode}): {cmd}\n{res.stderr.strip()}")
    return res.stdout


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def check_context() -> None:
    # collect deletes the demo namespace, so only run against a local k3d cluster
    ctx = sh("kubectl config current-context", check=False).strip()
    if not ctx.startswith("k3d-"):
        raise SystemExit(f"current kubectl context is '{ctx}', expected a k3d cluster "
                         f"(run: kubectl config use-context k3d-confdiag)")


def reset_baseline(warmup: int) -> None:
    print("  resetting namespace ...", flush=True)
    sh(f"kubectl delete namespace {NS} --ignore-not-found --wait=true", timeout=300)
    # start the data stores first so orders does not crash-loop on startup
    sh("kubectl apply -f k8s/base/00-namespace.yaml -f k8s/base/10-postgres.yaml -f k8s/base/20-redis.yaml")
    sh(f"kubectl -n {NS} wait --for=condition=available deployment/postgres deployment/redis --timeout=240s", timeout=300)
    sh("kubectl apply -f k8s/base/")
    sh(f"kubectl -n {NS} wait --for=condition=available deployment --all --timeout=240s", timeout=300)
    print(f"  baseline healthy, warming up {warmup}s ...", flush=True)
    time.sleep(warmup)


def snapshot(outdir: Path, since: datetime) -> None:
    (outdir / "logs").mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        out = sh(f"kubectl -n {NS} get {kind} -o json", check=False) or '{"items": []}'
        (outdir / f"{kind}.json").write_text(out)

    pods = json.loads((outdir / "pods.json").read_text())["items"]
    since_s = iso(since)
    for pod in pods:
        pname = pod["metadata"]["name"]
        for cs in pod.get("status", {}).get("containerStatuses", []) or []:
            cname = cs["name"]
            cur = sh(f"kubectl -n {NS} logs {pname} -c {cname} --since-time={since_s}", check=False)
            (outdir / "logs" / f"{pname}__{cname}.log").write_text(cur)
            if cs.get("restartCount", 0) > 0:
                prev = sh(
                    f"kubectl -n {NS} logs {pname} -c {cname} --previous --since-time={since_s}",
                    check=False,
                )
                (outdir / "logs" / f"{pname}__{cname}__previous.log").write_text(prev)


def run_once(fv: FaultVariant, duration: int, warmup: int, data_dir: Path) -> Path:
    run_id = f"{fv.key}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    outdir = data_dir / fv.key / run_id
    print(f"[{run_id}] {fv.name}", flush=True)

    reset_baseline(warmup)
    # margin for clock skew between WSL and the cluster
    inject_time = utcnow() - timedelta(seconds=2)
    for cmd in fv.commands:
        print(f"  inject: {cmd}", flush=True)
        sh(cmd)
    print(f"  observing {duration}s ...", flush=True)
    time.sleep(duration)
    end_time = utcnow()

    snapshot(outdir, inject_time)
    server_version = sh("kubectl version -o json", check=False)
    meta = {
        "run_id": run_id,
        "fault": fv.fault,
        "variant": fv.variant,
        "label": fv.fault,
        "fault_name": fv.name,
        "config_path": fv.config_path,
        "commands": list(fv.commands),
        "inject_time": iso(inject_time),
        "end_time": iso(end_time),
        "duration_s": duration,
        "warmup_s": warmup,
        "kubectl_version": json.loads(server_version) if server_version else None,
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"  saved {outdir.relative_to(REPO_ROOT) if outdir.is_relative_to(REPO_ROOT) else outdir}", flush=True)
    return outdir


def run_experiments(
    variants: list[FaultVariant], runs: int, duration: int, warmup: int,
    data_dir: Path, shuffle: bool = True, seed: int = 0,
) -> None:
    check_context()
    rng = random.Random(seed)
    total = runs * len(variants)
    done = 0
    for r in range(runs):
        order = list(variants)
        if shuffle:
            rng.shuffle(order)
        for fv in order:
            done += 1
            print(f"\n=== run {done}/{total} (round {r + 1}/{runs}) ===")
            run_once(fv, duration, warmup, data_dir)
