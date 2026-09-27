import json

import pytest

from confdiag.catalog import load_catalog
from confdiag.evaluate import evaluate
from confdiag.features import extract, normalise
from confdiag.parse import load_run
from confdiag.rules import diagnose

from sim import apply_command, build_run, load_manifests, resources_valid

CATALOG = load_catalog()


@pytest.mark.parametrize("key", sorted(CATALOG))
def test_fault_commands_apply_to_manifests(key):
    docs = load_manifests()
    for cmd in CATALOG[key].commands:
        apply_command(docs, cmd)
    assert resources_valid(docs) == []


def test_ground_truth_paths_exist():
    for key, fv in CATALOG.items():
        if fv.fault == "F00":
            assert fv.config_path is None
        else:
            assert fv.config_path and (":" in fv.config_path or fv.config_path.startswith("networkpolicy/")), key


@pytest.mark.parametrize("key", sorted(CATALOG))
def test_rules_on_simulated_runs(tmp_path, key):
    run = load_run(build_run(tmp_path, key))
    d = diagnose(run)
    assert d.fault == CATALOG[key].fault, (key, d)
    assert d.config_path == CATALOG[key].config_path, (key, d)


def test_normalise_masks_variable_parts():
    a = normalise('connection to server at "postgres" (10.43.12.7), port 5432 failed: timeout expired')
    b = normalise('connection to server at "postgres" (10.43.99.1), port 5432 failed: timeout expired')
    assert a == b
    assert "<ip>" in a and "<svc>" in a
    assert normalise("pod orders-5d8f7c9b4-x2k9p restarted") == "pod <pod> restarted"


def test_features_are_service_agnostic(tmp_path):
    fa = extract(load_run(build_run(tmp_path, "F04-a")))
    fb = extract(load_run(build_run(tmp_path, "F04-b")))
    assert fa["k8s_state"].get("terminated_OOMKilled") == fb["k8s_state"].get("terminated_OOMKilled") == 1
    assert set(fa["k8s_state"]) >= {"restarts_total", "pods_not_ready"}


def test_evaluate_end_to_end(tmp_path):
    data = tmp_path / "raw"
    for key in CATALOG:
        for seed in range(3):
            build_run(data, key, seed=seed)
    res = evaluate(data, tmp_path / "results", folds=3)
    assert res["rules"]["accuracy"] == 1.0
    assert set(res["cv"]) == {"logs_only", "k8s_only", "logs+k8s"}
    assert "rules" in res["holdout"]
    for f in ("metrics.json", "per_run.csv", "summary.md", "confusion_rules.png"):
        assert (tmp_path / "results" / f).exists()
    json.loads((tmp_path / "results" / "metrics.json").read_text())
