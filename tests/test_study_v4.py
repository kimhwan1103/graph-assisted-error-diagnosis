"""Methodological safeguards exercised at the actual API and durable runtime boundaries."""

import asyncio

import pytest
from test_source_analysis import factory

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.experiments.backends import BackendFailure
from ai_error_check_agent.experiments.design import ROOT
from ai_error_check_agent.experiments.study_v4 import (
    DEFAULT_CONFIG,
    archive,
    diagnose_cell,
    load,
    read,
    run,
    save,
)
from ai_error_check_agent.experiments.study_v4_arms import BoundDecision
from ai_error_check_agent.experiments.study_v4_audit import audit
from ai_error_check_agent.experiments.study_v4_review import (
    export_reviews,
    import_reviews,
    paired,
    report,
)
from ai_error_check_agent.experiments.study_v4_runtime import Session, mock_answer
from ai_error_check_agent.source_contracts import DiagnoseAPIResult


def case(name="direct-literal"):
    return next(c for c in load(DEFAULT_CONFIG)[2] if c["id"] == name)


def bound(name="direct-literal"):
    packaging = name == "docker-copy-omission"
    return {
        "decision": "accept",
        "plan": "add_docker_copy" if packaging else "provide_verified_file",
        "target": "/app/data/tasks.json",
        "code_path": "src/tasks.js",
        "code_line": 8 if packaging else 2,
        "evidence_ids": ["EV000001", "EV000003"],
        "source_evidence_ids": ["SC000007", "SC000008"] if packaging else ["SC000002"],
        "packaging": {"repo_path": "data/tasks.json", "insert_after_line": 5}
        if packaging
        else None,
        "summary": "제공 자료에서 필수 파일 읽기 실패를 확인했습니다.",
        "uncertainty": "실제 배포와 소스의 일치 및 파일 공급 계약은 확인해야 합니다.",
        "counter_evidence_ids": [],
    }


@pytest.mark.parametrize("name", ["direct-literal", "docker-copy-omission"])
def test_candidate_free_compact_is_a_real_successful_baseline(name):
    observed = {}
    for arm in ("C", "RC"):
        create, seen, _ = factory([bound(name)])
        result = asyncio.run(diagnose_cell(case(name), arm, create, graph_timeout=5))
        assert result["http_status"] == 200, result["result"].get("error")
        DiagnoseAPIResult.model_validate(result["result"])
        assert len(seen) == 1
        assert result["result"]["source_analysis"]["findings"]
        observed[arm] = (result, seen[0])
    c, rc = observed["C"], observed["RC"]
    assert c[0]["graph_trace"]["events"] == []
    assert c[0]["graph_trace"]["atoms"]["candidates"] == []
    assert "rule_candidates" not in c[1][1]
    assert "rule_candidates" in rc[1][1]
    assert c[1][0] == rc[1][0] and c[1][2] == rc[1][2]
    assert c[1][1] == {k: v for k, v in rc[1][1].items() if k != "rule_candidates"}
    assert c[0]["result"]["analysis"] == rc[0]["result"]["analysis"]


def test_full_and_compact_factorial_keep_identical_evidence():
    seen_by_arm = {}
    from ai_error_check_agent.source_analysis import SourceFindings, stage_schema

    for arm in ("F", "RF", "C", "RC"):
        answers = (
            [bound()]
            if arm in {"C", "RC"}
            else [mock_answer(stage_schema("source_findings", SourceFindings))]
        )
        create, seen, _ = factory(answers)
        result = asyncio.run(diagnose_cell(case(), arm, create, graph_timeout=5))
        assert result["http_status"] == 200
        seen_by_arm[arm] = seen[0][1]
    assert seen_by_arm["F"] == seen_by_arm["C"]
    assert seen_by_arm["RF"] == seen_by_arm["RC"]
    assert seen_by_arm["F"] == {
        k: v for k, v in seen_by_arm["RF"].items() if k != "rule_candidates"
    }
    assert not any(
        k in seen_by_arm["RF"] for k in ("quality_rubric", "expected_route", "graph", "approved_by")
    )


@pytest.mark.parametrize("name", ["direct-literal", "docker-copy-omission"])
def test_procedural_baseline_supplies_same_candidates_without_rdf(name, monkeypatch):
    create, seen, _ = factory([bound(name)])
    first = asyncio.run(diagnose_cell(case(name), "RC", create, graph_timeout=5))
    original = seen[0][1]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Procedural baseline must not construct an RDF graph")

    monkeypatch.setattr("ai_error_check_agent.experiments.study_v4_arms.build", forbidden)
    create, seen, _ = factory([bound(name)])
    second = asyncio.run(diagnose_cell(case(name), "procedural_RC", create, graph_timeout=5))
    assert original == seen[0][1]
    assert second["graph_trace"]["events"] == []
    assert first["result"]["analysis"] == second["result"]["analysis"]
    assert second["http_status"] == 200


def test_bad_binding_falls_back_and_counts_both_calls():
    answer = bound()
    answer["source_evidence_ids"] = ["SC999999"]
    from ai_error_check_agent.source_analysis import SourceFindings, stage_schema

    create, seen, _ = factory(
        [answer, mock_answer(stage_schema("source_findings", SourceFindings))]
    )
    result = asyncio.run(diagnose_cell(case(), "C", create, graph_timeout=5))
    assert result["http_status"] == 200
    assert len(seen) == result["result"]["execution"]["provider_call_count"] == 2
    assert result["graph_report"]["decision_status"] == "INVALID_COMPACT_RESULT"


def test_archive_and_schedule_are_reproducible():
    assert archive(case()) == archive(case())
    config, backend, cases, _ = load(DEFAULT_CONFIG)
    from ai_error_check_agent.experiments.study_v4 import schedule

    assert schedule(cases, config, backend) == schedule(cases, config, backend)
    assert len(schedule(cases, config, backend)) == 110


def test_transient_windows_save_lock_retries_only_local_replace(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "record.json"
    save(path, {"before": True})
    original = Path.replace
    calls = []

    def held_once(source, target):
        calls.append(source)
        if len(calls) == 1:
            raise PermissionError("temporary Windows file lock")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", held_once)
    save(path, {"after": True})
    assert read(path) == {"after": True} and len(calls) == 2


def tiny_config(tmp_path):
    config = read(DEFAULT_CONFIG)
    data = {"version": "study-v4-dataset.v1", "cases": [case()]}
    save(tmp_path / "dataset.json", data)
    config["dataset"] = str(tmp_path / "dataset.json")
    config["references"] = str(ROOT / config["references"])
    config["arms"] = ["adaptive", "C"]
    save(tmp_path / "config.json", config)
    return tmp_path / "config.json"


def test_freeze_resume_reviews_and_mock_metric_separation(tmp_path):
    config = tiny_config(tmp_path)
    output = tmp_path / "out"
    asyncio.run(run(config, output))
    before = {p: p.read_bytes() for p in (output / "cells").rglob("*.json")}
    asyncio.run(run(config, output))
    assert before == {p: p.read_bytes() for p in before}
    r = report(output)
    assert r["completed_cells"] == 2
    assert all(
        row["success"]
        and row["usable_final"] is None
        and row["output_tokens"] is None
        and row["wall_ms"] is None
        for row in r["rows"]
    )
    export_reviews(output)
    review = read(output / "reviews/primary-template.json")
    review.update(reviewer_id="test-reviewer", reviewed_at="2026-10-11")
    save(tmp_path / "review.json", review)
    with pytest.raises(ValueError, match="five boolean"):
        import_reviews(output, tmp_path / "review.json")
    for item in review["items"]:
        item["scores"] = dict.fromkeys(item["scores"], True)
        item["rationale"] = "Test-only review, never a real study rating."
    save(tmp_path / "review.json", review)
    import_reviews(output, tmp_path / "review.json")
    assert all(row["usable_final"] is None for row in report(output)["rows"])
    review["role"] = "overlap"
    save(tmp_path / "review.json", review)
    with pytest.raises(ValueError, match="different person"):
        import_reviews(output, tmp_path / "review.json")
    changed = read(config)
    changed["backend"]["seed"] = 43
    save(config, changed)
    with pytest.raises(ValueError, match="Frozen"):
        asyncio.run(run(config, output))


def test_ambiguous_started_cell_is_never_resent(tmp_path):
    config = tiny_config(tmp_path)
    output = tmp_path / "out"
    asyncio.run(run(config, output))
    state_path = next((output / "cells").glob("*/state.json"))
    state = read(state_path)
    state["status"] = "started"
    save(state_path, state)
    with pytest.raises(ValueError, match="No automatic resend"):
        asyncio.run(run(config, output))


def test_provider_failure_is_durably_recorded_once(tmp_path):
    _, config, _, _ = load(DEFAULT_CONFIG)
    config = config.model_copy(update={"request_interval_seconds": 0})

    class FailedBackend:
        def generate(self, *_args, **_kwargs):
            raise BackendFailure("MODEL_HTTP_429")

    session = Session(tmp_path, FailedBackend(), config, save, read, live=True)
    runtime = session.factory(tmp_path / "cells/one", 42)()
    with pytest.raises(DiagnosisError, match="모델 API"):
        asyncio.run(runtime.run("prompt", {}, BoundDecision.model_json_schema()))
    records = list((tmp_path / "cells/one/calls").glob("*.json"))
    assert len(records) == 1
    record = read(records[0])
    assert (
        record["dispatched"]
        and record["status"] == "failed"
        and record["error"] == "MODEL_HTTP_429"
    )


def test_single_lineage_does_not_get_fake_confidence_interval():
    rows = [
        {"case_id": str(i), "repeat": 0, "cluster_id": "one", "arm": arm, "output_tokens": value}
        for i in range(20)
        for arm, value in (("adaptive", 100), ("graph_compact", 50))
    ]
    result = paired(rows, "adaptive", "graph_compact", "output_tokens")
    assert result["clusters"] == 1 and result["paired_cells"] == 20
    assert result["mean_cluster_delta"] == -50 and result["cluster_bootstrap_95pct"] is None


def test_factorial_interaction_uses_all_four_matched_cells():
    from ai_error_check_agent.experiments.study_v4_statistics import factorial

    rows = [
        {
            "case_id": "test",
            "repeat": 0,
            "cluster_id": "one",
            "arm": arm,
            "factorial_applied": True,
            "output_tokens": count,
        }
        for arm, count in (("F", 100), ("RF", 90), ("C", 50), ("RC", 30))
    ]
    value = factorial(rows, "output_tokens")
    assert value["contrasts"]["rules_main"]["estimate"] == -15
    assert value["contrasts"]["compact_main"]["estimate"] == -55
    assert value["contrasts"]["interaction"]["estimate"] == -10
    rows[0]["factorial_applied"] = False
    assert factorial(rows, "output_tokens")["complete_factorial_blocks"] == 0


def test_noninferiority_does_not_use_zero_width_bootstrap_as_proof():
    from ai_error_check_agent.experiments.study_v4_statistics import noninferiority

    cases = [{"id": str(i), "cluster_id": str(i)} for i in range(20)]
    rows = [
        {"case_id": c["id"], "repeat": 0, "arm": arm, "usable_final": 1}
        for c in cases
        for arm in ("adaptive", "graph_compact")
    ]
    frozen = {
        "live": True,
        "cases": cases,
        "config": {"role": "evaluation", "statistics": {"protocol_registration": "test-only"}},
    }
    value = noninferiority(rows, frozen)
    assert value["degenerate_cluster_distribution"] and value["noninferiority_supported"] is None


def test_evaluation_requires_unexposed_approved_case_bound_references(tmp_path):
    path = tiny_config(tmp_path)
    config = read(path)
    config["role"] = "evaluation"
    save(path, config)
    with pytest.raises(ValueError, match="held-out"):
        load(path)


def test_shacl_faults_rules_equivalence_and_busy_lifecycle(tmp_path):
    output = tmp_path / "audit"
    result = asyncio.run(audit(DEFAULT_CONFIG, output))
    assert result["procedural_equivalence_pass"]
    assert result["plain_python_candidate_equivalence_pass"]
    assert result["worker_lifecycle_pass"]
    by_key = {(r["fault"], r["arm"]): r for r in result["faults"]}
    assert not by_key[("dangling_evidence", "graph_compact")]["candidate_generated"]
    assert by_key[("dangling_evidence", "no_shacl")]["candidate_generated"]
    assert all(r["api_valid"] for r in result["faults"])
    assert not any(
        r["candidate_generated"] for r in result["faults"] if r["fault"] == "cross_scope"
    )
