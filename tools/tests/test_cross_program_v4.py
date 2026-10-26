"""Invariant and failure-path checks; synthetic data never enters research results."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cross_program_common_v4 import (
    REGISTRY,
    ROOT,
    digest,
    fixtures,
    read,
    reference_dockerfile,
    source_cell,
    variant_files,
    verify_diagnosis,
    write,
)
from evaluate_remediation_cross_program_v4 import metrics, unresolved, validate_bound
from evaluate_remediation_v4 import classify
from prepare_cross_program_v4 import payload
from remediation_adapter_v4 import POLICY, PlanError, adapt
from report_cross_program_v4 import routes, stats
from revalidate_cross_program_v4 import assess_cell
from revalidate_study_v4 import normalize

from ai_error_check_agent.experiments.study_v4 import plan
from ai_error_check_agent.knowledge.node_source import file_read


@pytest.mark.parametrize("program", REGISTRY)
def test_variant_only_changes_declared_read_binding_and_copy(program):
    a = {x["path"]: x["content"] for x in variant_files(program, "A")}
    c = {x["path"]: x["content"] for x in variant_files(program, "C")}
    s = REGISTRY[program]
    assert {p for p in a if a[p] != c[p]} == {s["reader"]}
    assert "readFileSync as readText" in c[s["reader"]]
    b = {x["path"]: x["content"] for x in variant_files(program, "B")}
    assert s["target"] in b and s["optional"] not in b
    assert "ENV OPTIONAL_CONFIG=1" in b["Dockerfile"]
    original = {x["path"]: x["content"] for x in fixtures(program)}
    assert reference_dockerfile(variant_files(program, "A"), s) == original["Dockerfile"]
    for variant, expected in [("A", True), ("C", False)]:
        code = {x["path"]: x["content"] for x in variant_files(program, variant)}[s["reader"]]
        lines = [
            {"text": text, "line": i, "id": f"SC{i:06}"}
            for i, text in enumerate(code.splitlines(), 1)
        ]
        assert bool(file_read(lines, "/app/" + s["reader"], 4, "/app/" + s["target"])) is expected


def test_plan_and_provenance():
    p = plan(ROOT / "config/study.gemma-api.v4.cross-program.json")
    assert (
        p["cases"],
        p["lineage_clusters"],
        p["cells"],
        p["generation_call_upper_bound"],
        p["hard_generation_call_budget"],
    ) == (9, 3, 54, 162, 162)
    assert p["human_reference_approvals"] == 0 and p["role"] == "development"
    verify_diagnosis()
    dataset = read(ROOT / "evaluation/cross-program-v4/dataset.json")
    for case in dataset["cases"]:
        assert set(case) == {
            "id",
            "cluster_id",
            "pair_id",
            "variant",
            "split",
            "exposure",
            "payload",
            "source_files",
        }
        assert not any("__evaluation" in f["path"] for f in case["source_files"])
        capture = read(ROOT / "evaluation/cross-program-v4/captures" / case["id"] / "capture.json")
        regenerated = payload(capture, case["id"], case["cluster_id"], case["source_files"])
        assert regenerated == case["payload"]
        assert len(capture["records"]) == len(case["payload"]["data"]["logs"])
        for record, log in zip(capture["records"], case["payload"]["data"]["logs"], strict=True):
            assert (record["text"], record["stream"], record["sequence"], record["timestamp"]) == (
                log["text"],
                log["stream"],
                log["sequence"],
                log["timestamp"],
            )


def test_preflight_evidence_and_wrong_states():
    root = ROOT / "results/cross-program-v4-preflight"
    result = read(root / "summary.json")
    assert result["status"] == "passed" and len(result["cases"]) == 9
    for row in result["cases"]:
        assert row["baseline_qualified"] and row["checks"]["reference"]
        if "copy-missing" in row["case_id"]:
            assert all(
                not row["checks"][k]
                for k in ["unmodified", "wrong_destination", "empty_target", "wrong_content"]
            )
        for mode in row["checks"]:
            r = read(root / row["case_id"] / mode / "result.json")
            assert r["evaluation_error"] is None
            if not row["case_id"].startswith("http-"):
                assert r["health_pass"] is None
            if mode == "reference":
                assert r["before_hashes"] == r["after_hashes"]
                assert r["functional_pass"] and r["regression_pass"] and r["data_preservation_pass"]


def sample_plan(snippet):
    return {
        "id": "R1",
        "changes": [
            {
                "target": "Dockerfile",
                "target_known": True,
                "instruction": "Add a COPY instruction",
                "snippet": snippet,
                "placeholders": [],
            }
        ],
    }


def test_adapter_has_no_oracle_and_first_plan_is_not_replaced():
    import inspect

    assert list(inspect.signature(adapt).parameters) == ["plan", "source_files", "policy"]
    bad = sample_plan("RUN touch /app/assets/tariffs.json")
    good = sample_plan("COPY assets/tariffs.json ./assets/tariffs.json")
    response = {
        "response_valid": True,
        "response_error": None,
        "analysis": {
            "analysis_status": "diagnosed",
            "remediation": {"status": "proposed", "plans": [bad, good]},
        },
    }
    outcome, selected = classify(response, {"case_group": "repairable"})
    assert outcome == "plan_selected" and selected is bad
    with pytest.raises(PlanError):
        adapt(selected, variant_files("shipping-cli", "A"), POLICY)
    both = sample_plan("COPY assets/tariffs.json ./assets/tariffs.json")
    both["changes"] += bad["changes"]
    with pytest.raises(PlanError):
        adapt(both, variant_files("shipping-cli", "A"), POLICY)


def test_wrong_copy_is_not_corrected():
    r = adapt(
        sample_plan("COPY assets/tariffs.json ./wrong.json"),
        variant_files("shipping-cli", "A"),
        POLICY,
    )
    assert r["operations"][0]["destination"] == "wrong.json"
    assert 'COPY ["assets/tariffs.json", "wrong.json"]' in r["dockerfile"]


def test_metric_denominator_and_unknown():
    row = {
        "case_group": "repairable",
        "evaluation_complete": True,
        "complete_restoration": True,
        "application_pass": True,
        "response_valid": True,
        "outcome": "restored",
    }
    pending = copy.deepcopy(row)
    unresolved(pending, "docker unavailable")
    pending["application_pass"] = None
    failed = {
        **row,
        "complete_restoration": False,
        "application_pass": None,
        "outcome": "contract_failed",
        "response_valid": False,
    }
    m = metrics([row, pending, failed])
    assert m["repairable_scheduled"] == 3 and m["restored"] == 1 and m["repairable_unobserved"] == 1
    assert m["restoration_rate"] is None and m["confirmed_success_lower_bound"] == 1 / 3
    assert stats([10, None])["total"] is None and stats([10, None])["mean"] == 10


def test_normalization_is_exact_and_immutable():
    value = {
        "limitations": [{"text": "unchanged"}, {"text": "not eligible", "extra": 1}],
        "remediation": {"plans": [sample_plan("COPY missing ./missing")]},
    }
    baseline = copy.deepcopy(value)
    normalized, edits = normalize(value)
    assert value == baseline and normalized["limitations"] == [
        "unchanged",
        {"text": "not eligible", "extra": 1},
    ]
    assert normalized["remediation"] == value["remediation"] and len(edits) == 1


def synthetic_cell(tmp_path, finish="stop", call_status="completed"):
    # Copy a MOCK response only to pytest's temporary folder; never rewrite real results.
    mock = ROOT / "results/cross-program-v4-mock"
    slot = read(mock / "frozen.json")["schedule"][0]
    result = read(mock / "cells" / slot["cell_id"] / "result.json")
    folder = tmp_path / "cells" / slot["cell_id"]
    write(folder / "result.json", result)
    write(folder / "state.json", {**slot, "status": "completed", "result_sha256": digest(result)})
    call = read(max((mock / "cells" / slot["cell_id"] / "calls").glob("*.json")))
    call["status"] = call_status
    call["raw"]["finish_reason"] = finish
    write(folder / "calls/00.json", call)
    return slot, folder


@pytest.mark.parametrize(
    ("finish", "status", "reason"),
    [
        ("length", "completed", "last_call_not_completed_stop"),
        ("stop", "failed", "last_call_not_completed_stop"),
    ],
)
def test_ineligible_normalization_retains_scheduled_cell(tmp_path, finish, status, reason):
    slot, _ = synthetic_cell(tmp_path, finish, status)
    row = assess_cell(tmp_path, slot)
    assert row["cell_id"] == slot["cell_id"] and row["normalization_reason"] == reason
    assert row["normalized"] == row["original"]


def test_partial_and_tampered_source(tmp_path):
    slot = {"case_id": "new-case", "cell_id": "aa", "arm": "adaptive", "repeat": 0}
    row = assess_cell(tmp_path, slot)
    assert row["source_state"] == "not_started" and row["normalization_status"] == "unavailable"
    slot, folder = synthetic_cell(tmp_path)
    result = read(folder / "result.json")
    result["repeat"] = 9
    (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        source_cell(tmp_path, slot)


def test_non_json_and_supported_normalization(tmp_path):
    slot, folder = synthetic_cell(tmp_path)
    row = assess_cell(tmp_path, slot)
    assert row["normalization_status"] == "available" and row["normalized"]["valid"]
    call = read(folder / "calls/00.json")
    call["raw"]["text"] = "not JSON"
    (folder / "calls/00.json").write_text(json.dumps(call), encoding="utf-8")
    assert assess_cell(tmp_path, slot)["normalization_reason"] == "non_json_response"


def test_route_separates_early_and_review_fallback():
    assert routes({}, [], "graph_compact")[0] == "early_general"
    r = {
        "graph_report": {
            "status": "ok",
            "decision_status": "COMPACT_DEFERRED",
            "packaging_candidate": {"id": "P1"},
        }
    }
    call = {"schema": {"properties": {"decision": {}}}}
    assert routes(r, [call], "graph_compact")[0] == "general_after_llm_review"
    assert (
        routes({"graph_report": {"status": "timed_out"}}, [], "graph_compact")[0]
        == "general_after_graph_attempt"
    )


def test_bound_manifest_cannot_replace_oracle(tmp_path):
    spec = {"engine": "node-copy-replay.v2", "cases": []}
    write(tmp_path / "remediation-spec.json", spec)
    write(tmp_path / "freeze-manifest.json", {})
    frozen = {"cases": []}
    from cross_program_common_v4 import sha

    bound = {
        **spec,
        "study_sha256": digest(frozen),
        "freeze_sha256": sha(tmp_path / "freeze-manifest.json"),
        "spec_sha256": sha(tmp_path / "remediation-spec.json"),
    }
    validate_bound(tmp_path, bound, frozen)
    bound["cases"] = [{"tests": {"always_pass": True}}]
    with pytest.raises(ValueError, match="manifest"):
        validate_bound(tmp_path, bound, frozen)


def test_synthetic_end_to_end_keeps_54_cells_and_both_versions(tmp_path, monkeypatch):
    """All fake live flags stay inside pytest temp; no provider or Docker is invoked."""
    import shutil

    import evaluate_remediation_cross_program_v4 as evaluator
    import report_cross_program_v4 as reporter
    import revalidate_cross_program_v4 as normalizer
    from cross_program_common_v4 import sha
    from evaluate_remediation_v4 import hashes

    source = tmp_path / "synthetic-source"
    shutil.copytree(ROOT / "results/cross-program-v4-mock", source)
    frozen = read(source / "frozen.json")
    frozen["live"] = True
    (source / "frozen.json").write_text(json.dumps(frozen), encoding="utf-8")
    for path in source.glob("cells/*/calls/*.json"):
        call = read(path)
        call["raw"]["finish_reason"] = "stop"
        path.write_text(json.dumps(call), encoding="utf-8")
    before = hashes(source)
    root = tmp_path / "registry"
    write(root / "freeze-manifest.json", {"synthetic_test_only": True})
    spec = read(ROOT / "evaluation/cross-program-v4/remediation-spec.json")
    write(root / "remediation-spec.json", spec)
    write(root / "protocol.json", read(ROOT / "evaluation/cross-program-v4/protocol.json"))
    bound = {
        **spec,
        "study_sha256": digest(frozen),
        "freeze_sha256": sha(root / "freeze-manifest.json"),
        "spec_sha256": sha(root / "remediation-spec.json"),
    }
    write(root / "bound.json", bound)
    for module in (normalizer, evaluator, reporter):
        monkeypatch.setattr(module, "load_source", lambda source, root: frozen)
    norm = tmp_path / "normalized"
    normalizer.run(source, norm, root)
    assert len(list((norm / "cells").glob("*.json"))) == 54
    outputs = []
    for variant in ("original", "normalized"):
        out = tmp_path / ("remediation-" + variant)
        evaluator.run(
            source,
            variant,
            norm if variant == "normalized" else None,
            root / "bound.json",
            out,
            False,
            root,
        )
        assert len(read(out / "summary.json")["rows"]) == 54
        outputs.append(out)
    output = tmp_path / "report"
    reporter.run(source, norm, *outputs, output, root)
    rows = read(output / "cells.json")
    assert len(rows) == 108
    assert {r["assessment_variant"] for r in rows} == {"original", "normalized"}
    assert hashes(source) == before
    assert all(
        r["remediation"]["complete_restoration"] is None for r in rows if r["stratum"] in ("A", "C")
    )
