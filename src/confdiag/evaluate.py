"""Evaluation: rules on all runs, k-fold CV on variant a, and transfer
between services: train a / test b and (v0.2) train b / test a.

v0.2 also reports top-3 accuracy and MRR for the learned model, and a 95%
bootstrap confidence interval for every accuracy."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score  # noqa: E402
from sklearn.model_selection import StratifiedKFold  # noqa: E402

from .features import ABLATIONS, extract  # noqa: E402
from .ml import Diagnoser  # noqa: E402
from .parse import Run, find_runs, load_run  # noqa: E402
from .rules import diagnose  # noqa: E402


N_BOOT = 1000


def _acc_ci(y_true, y_pred, seed: int = 0) -> list[float]:
    """95% percentile bootstrap interval for accuracy (resampling runs)."""
    hits = np.array([t == p for t, p in zip(y_true, y_pred)], dtype=float)
    if len(hits) == 0:
        return [0.0, 0.0]
    rng = np.random.default_rng(seed)
    means = rng.choice(hits, size=(N_BOOT, len(hits)), replace=True).mean(axis=1)
    return [round(float(np.percentile(means, 2.5)), 4), round(float(np.percentile(means, 97.5)), 4)]


def _scores(y_true, y_pred, ranked=None, seed: int = 0) -> dict:
    out = {
        "n": len(y_true),
        "accuracy": round(accuracy_score(y_true, y_pred), 4),
        "accuracy_ci95": _acc_ci(y_true, y_pred, seed),
        "macro_f1": round(f1_score(y_true, y_pred, average="macro", zero_division=0), 4),
    }
    if ranked is not None:
        top3 = [t in r[:3] for t, r in zip(y_true, ranked)]
        rr = [1.0 / (r.index(t) + 1) if t in r else 0.0 for t, r in zip(y_true, ranked)]
        out["top3"] = round(float(np.mean(top3)), 4)
        out["mrr"] = round(float(np.mean(rr)), 4)
    return out


def plot_confusion(y_true, y_pred, title: str, out: Path) -> None:
    labels = sorted(set(y_true) | set(y_pred))
    cm = confusion_matrix(y_true, y_pred, labels=labels)
    fig, ax = plt.subplots(figsize=(0.55 * len(labels) + 2.5, 0.55 * len(labels) + 2))
    ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(labels)), labels, rotation=45, ha="right")
    ax.set_yticks(range(len(labels)), labels)
    ax.set_xlabel("predicted")
    ax.set_ylabel("true")
    ax.set_title(title, fontsize=10)
    for i in range(len(labels)):
        for j in range(len(labels)):
            if cm[i, j]:
                ax.text(j, i, cm[i, j], ha="center", va="center",
                        color="white" if cm[i, j] > cm.max() / 2 else "black", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def evaluate(data_dir: Path, out_dir: Path, folds: int = 5, seed: int = 0) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    runs: list[Run] = [load_run(p) for p in find_runs(data_dir)]
    if not runs:
        raise SystemExit(f"no runs found under {data_dir}")
    feats = [extract(r) for r in runs]
    labels = [r.label for r in runs]
    variants = [r.variant for r in runs]
    print(f"loaded {len(runs)} runs: {dict(sorted(Counter(f'{l}-{v}' for l, v in zip(labels, variants)).items()))}")

    results: dict = {"runs": len(runs)}
    per_run = [{"run": r.meta.get("run_id", r.path.name), "label": r.label, "variant": r.variant,
                "true_config_path": r.meta.get("config_path")} for r in runs]

    # 1. rules -----------------------------------------------------------
    diags = [diagnose(r) for r in runs]
    rule_pred = [d.fault for d in diags]
    fault_runs = [i for i, r in enumerate(runs) if r.meta.get("config_path")]
    loc_correct = sum(diags[i].config_path == runs[i].meta["config_path"] for i in fault_runs)
    results["rules"] = _scores(labels, rule_pred, seed=seed)
    results["rules"]["localisation_accuracy"] = (
        round(loc_correct / len(fault_runs), 4) if fault_runs else None)
    for row, d in zip(per_run, diags):
        row.update(rule_pred=d.fault, rule_config_path=d.config_path, rule=d.rule)
    plot_confusion(labels, rule_pred, "Rule-based diagnoser (all runs)", out_dir / "confusion_rules.png")

    # 2. cross-validation on variant a -------------------------------------
    idx_a = np.array([i for i, v in enumerate(variants) if v == "a"])
    y_a = np.array(labels)[idx_a]
    min_class = min(Counter(y_a).values()) if len(idx_a) else 0
    k = min(folds, min_class)
    results["cv"] = {}
    if k >= 2:
        skf = StratifiedKFold(n_splits=k, shuffle=True, random_state=seed)
        for name in ABLATIONS:
            pred = np.empty(len(idx_a), dtype=object)
            ranked: list = [None] * len(idx_a)
            for tr, te in skf.split(idx_a, y_a):
                model = Diagnoser(name, seed=seed).fit([feats[i] for i in idx_a[tr]], list(y_a[tr]))
                for j, r in zip(te, model.rank([feats[i] for i in idx_a[te]])):
                    ranked[j] = r
                    pred[j] = r[0]
            results["cv"][name] = {**_scores(list(y_a), list(pred), ranked, seed), "folds": k}
            for j, i in enumerate(idx_a):
                per_run[i][f"cv_{name}"] = pred[j]
            if name == "logs+k8s":
                plot_confusion(list(y_a), list(pred), f"Learned (logs+k8s), {k}-fold CV, variant a",
                               out_dir / "confusion_cv_logs+k8s.png")
    else:
        results["cv"]["skipped"] = f"need >=2 runs per class in variant a (min is {min_class})"

    # 3. transfer between services ----------------------------------------
    # a -> b: train on every variant-a run, test on every variant-b run.
    # b -> a (v0.2): train on every variant-b run plus the F00 baseline (it
    # has no b variant), test on the variant-a runs of faults that have a b
    # variant. F00 is in both training sets and never in a test set.
    idx_b = [i for i, v in enumerate(variants) if v == "b"]
    b_faults = {labels[i] for i in idx_b}
    directions = {
        "holdout": (list(idx_a), idx_b),
        "holdout_ba": ([i for i in range(len(runs)) if variants[i] == "b" or labels[i] == "F00"],
                       [i for i in idx_a if labels[i] in b_faults]),
    }
    for key, (train, test) in directions.items():
        results[key] = {}
        if not test or len({labels[i] for i in train}) < 2:
            results[key]["skipped"] = "not enough variant-b runs"
            continue
        y_test = [labels[i] for i in test]
        for name in ABLATIONS:
            model = Diagnoser(name, seed=seed).fit([feats[i] for i in train], [labels[i] for i in train])
            ranked = model.rank([feats[i] for i in test])
            pred = [r[0] for r in ranked]
            results[key][name] = _scores(y_test, pred, ranked, seed)
            for i, p in zip(test, pred):
                per_run[i][f"{key}_{name}"] = p
            if key == "holdout" and name == "logs+k8s":
                (out_dir / "top_features.json").write_text(json.dumps(model.top_features(), indent=2))
        results[key]["rules"] = _scores(y_test, [rule_pred[i] for i in test], seed=seed)
        loc = [i for i in test if runs[i].meta.get("config_path")]
        results[key]["rules"]["localisation_accuracy"] = round(
            sum(diags[i].config_path == runs[i].meta["config_path"] for i in loc) / len(loc), 4
        ) if loc else None

    # write outputs ----------------------------------------------------------
    (out_dir / "metrics.json").write_text(json.dumps(results, indent=2))
    keys = sorted({k for row in per_run for k in row})
    with open(out_dir / "per_run.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(per_run)
    (out_dir / "summary.md").write_text(summary_md(results))
    print((out_dir / "summary.md").read_text())
    return results


def _fmt_row(exp: str, method: str, s: dict, loc="-") -> str:
    lo, hi = s.get("accuracy_ci95", ["-", "-"])
    return (f"| {exp} | {method} | {s['n']} | {s['accuracy']} [{lo}, {hi}] | {s['macro_f1']} "
            f"| {s.get('top3', '-')} | {s.get('mrr', '-')} | {loc} |")


def summary_md(res: dict) -> str:
    lines = [f"# Results ({res['runs']} runs)", "",
             "| Experiment | Method | n | Accuracy [95% CI] | Macro-F1 | Top-3 | MRR | Localisation |",
             "|---|---|---|---|---|---|---|---|"]
    r = res["rules"]
    lines.append(_fmt_row("All runs", "Rules", r, r["localisation_accuracy"]))
    for name, s in res.get("cv", {}).items():
        if isinstance(s, dict):
            lines.append(_fmt_row(f"CV variant a ({s['folds']}-fold)", f"Learned: {name}", s))
    for key, exp in (("holdout", "Train a, test b"), ("holdout_ba", "Train b, test a")):
        for name, s in res.get(key, {}).items():
            if isinstance(s, dict):
                method = "Rules" if name == "rules" else f"Learned: {name}"
                lines.append(_fmt_row(exp, method, s, s.get("localisation_accuracy", "-")))
    for sec in ("cv", "holdout", "holdout_ba"):
        if "skipped" in res.get(sec, {}):
            lines.append(f"\n_{sec} skipped: {res[sec]['skipped']}_")
    lines.append("\nRules give one answer, so they have no top-3 or MRR. "
                 "CI = 95% bootstrap interval over runs (1000 resamples).")
    return "\n".join(lines) + "\n"
