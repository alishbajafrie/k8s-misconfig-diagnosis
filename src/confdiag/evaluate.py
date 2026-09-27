"""Evaluation: rules on all runs, k-fold CV on variant a, train a / test b."""
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


def _scores(y_true, y_pred) -> dict:
    return {
        "n": len(y_true),
        "accuracy": round(accuracy_score(y_true, y_pred), 4),
        "macro_f1": round(f1_score(y_true, y_pred, average="macro", zero_division=0), 4),
    }


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
    results["rules"] = _scores(labels, rule_pred)
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
            for tr, te in skf.split(idx_a, y_a):
                model = Diagnoser(name, seed=seed).fit([feats[i] for i in idx_a[tr]], list(y_a[tr]))
                pred[te] = model.predict([feats[i] for i in idx_a[te]])
            results["cv"][name] = {**_scores(list(y_a), list(pred)), "folds": k}
            for j, i in enumerate(idx_a):
                per_run[i][f"cv_{name}"] = pred[j]
            if name == "logs+k8s":
                plot_confusion(list(y_a), list(pred), f"Learned (logs+k8s), {k}-fold CV, variant a",
                               out_dir / "confusion_cv_logs+k8s.png")
    else:
        results["cv"]["skipped"] = f"need >=2 runs per class in variant a (min is {min_class})"

    # 3. held-out variants -------------------------------------------------
    idx_b = [i for i, v in enumerate(variants) if v == "b"]
    results["holdout"] = {}
    if idx_b and len(set(y_a)) >= 2:
        y_b = [labels[i] for i in idx_b]
        for name in ABLATIONS:
            model = Diagnoser(name, seed=seed).fit([feats[i] for i in idx_a], list(y_a))
            pred = model.predict([feats[i] for i in idx_b])
            results["holdout"][name] = _scores(y_b, pred)
            for i, p in zip(idx_b, pred):
                per_run[i][f"holdout_{name}"] = p
            if name == "logs+k8s":
                (out_dir / "top_features.json").write_text(json.dumps(model.top_features(), indent=2))
        results["holdout"]["rules"] = _scores(y_b, [rule_pred[i] for i in idx_b])
        loc_b = [i for i in idx_b if runs[i].meta.get("config_path")]
        results["holdout"]["rules"]["localisation_accuracy"] = round(
            sum(diags[i].config_path == runs[i].meta["config_path"] for i in loc_b) / len(loc_b), 4
        ) if loc_b else None
    else:
        results["holdout"]["skipped"] = "no variant-b runs collected yet"

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


def summary_md(res: dict) -> str:
    lines = [f"# Results ({res['runs']} runs)", "",
             "| Experiment | Method | n | Accuracy | Macro-F1 | Localisation |",
             "|---|---|---|---|---|---|"]
    r = res["rules"]
    lines.append(f"| All runs | Rules | {r['n']} | {r['accuracy']} | {r['macro_f1']} | {r['localisation_accuracy']} |")
    for name, s in res.get("cv", {}).items():
        if isinstance(s, dict):
            lines.append(f"| CV variant a ({s['folds']}-fold) | Learned: {name} | {s['n']} | {s['accuracy']} | {s['macro_f1']} | - |")
    for name, s in res.get("holdout", {}).items():
        if isinstance(s, dict):
            loc = s.get("localisation_accuracy", "-")
            method = "Rules" if name == "rules" else f"Learned: {name}"
            lines.append(f"| Held-out variant b | {method} | {s['n']} | {s['accuracy']} | {s['macro_f1']} | {loc} |")
    for sec in ("cv", "holdout"):
        if "skipped" in res.get(sec, {}):
            lines.append(f"\n_{sec} skipped: {res[sec]['skipped']}_")
    return "\n".join(lines) + "\n"
