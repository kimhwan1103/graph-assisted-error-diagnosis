"""Offline only: real network model and real Docker calls are forbidden in these tests."""

import asyncio
import copy
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_controls_contract_v2 as c
import run_review_controls_v4_contract_v2 as runner
from audit_review_controls_v4_contract_v2 import audit, verify_preserved
from evaluate_review_controls_v4_contract_v2 import score
from prepare_review_controls_v4_contract_v2 import clone, freeze
from review_controls_mock_contract_v2 import bound_fixture, full_fixture

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.experiments.study_v4 import save


@pytest.fixture(autouse=True)
def forbid_real_services(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real API/Docker forbidden in contract unit tests")

    monkeypatch.setattr(
        "ai_error_check_agent.experiments.backends.GeminiBackend.generate", forbidden
    )
    monkeypatch.setattr(
        "ai_error_check_agent.experiments.backends.GeminiBackend.preflight", forbidden
    )
    monkeypatch.setattr("remediation_cross_program_docker_v4.CrossReplay.initialize", forbidden)
    monkeypatch.setattr("remediation_cross_program_docker_v4.CrossReplay.replay", forbidden)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp("contract-v2-synthetic")
    config = copy.deepcopy(c.read(c.DEFAULT_CONFIG))
    root = directory / "new-cloned-packets"
    config["root"] = root.relative_to(c.ROOT).as_posix()
    config_path = directory / "config.json"
    c.write(config_path, config)
    audit_path = directory / "synthetic-offline-audit"
    audit(c.LEGACY_ROOT, c.ROOT / "results/review-controls-v4-live", audit_path)
    clone(c.LEGACY_ROOT, root, config_path, audit_path)
    return root, config_path, audit_path


def cases(root, context="P_supported"):
    return [x for x in c.read(root / "dataset.json")["cases"] if x["context_variant"] == context]


class FakeBackend:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def generate(self, prompt, data, seed, *, schema):
        self.requests.append((prompt, copy.deepcopy(data), copy.deepcopy(schema)))
        return {
            "text": c.canonical(self.answers.pop(0)),
            "input_tokens": 1,
            "output_tokens": 1,
            "thinking_tokens": None,
            "finish_reason": "stop",
            "model_version": "synthetic_contract_test",
            "provider_calls": 1,
        }


def fake_session(config_path, path, backend):
    config, b, _ = c.configuration(config_path)
    return runner.ReviewSession(
        path, backend, b.model_copy(update={"request_interval_seconds": 0}), config, live=True
    )


def slot(case, arm, repeat=0):
    s = {"case_id": case["id"], "arm": arm, "repeat": repeat}
    return {**s, "cell_id": c.digest(s)[:20]}


def test_clone_preserves_original_bytes_and_approvals(prepared):
    root, _, audit_path = prepared
    c.verify_equivalence(root)
    assert len(cases(root)) == 3
    assert c.read(root / "references.json") == c.read(c.LEGACY_ROOT / "references.json")
    assert c.read(root / "protocol.json")["post_result_development_revision"]
    assert verify_preserved(audit_path / "legacy-preservation.json")["preserved"]
    assert not (root / "freeze-manifest.json").exists()


def test_schema_descriptions_only_and_requests_match(prepared):
    root, config, _ = prepared
    old = c.legacy.BoundDecision.model_json_schema()
    before = copy.deepcopy(old)
    new = c.bound_schema()
    assert c.without_descriptions(new) == c.without_descriptions(old)
    assert old == before and new != old
    rows = c.request_parity(root, c.read(config))
    assert len(rows) == 36
    for path in (c.BOUND_CONTRACT, c.FALLBACK_CONTRACT):
        assert not any(
            s in path
            for s in ("EV000", "SC000", "P_supported", "N_external", "shipping", "settlement")
        )


@pytest.mark.parametrize("program", range(3))
@pytest.mark.parametrize("arm", ["B_eq", "G_eq"])
def test_actual_accept_assembly_and_full_validation_all_programs(prepared, program, arm):
    root = prepared[0]
    case = cases(root)[program]
    common, _, _ = c.packet_files(root, case["id"], "B_eq")
    result = c.assess_primary(c.canonical(bound_fixture(common)), arm, common)
    assert result["effective_decision"] == "accept"
    trace = c.diagnostics(c.canonical(bound_fixture(common)), arm, common)
    assert not trace["errors"] and trace["stages"]["full_response_source_locations"] is True


@pytest.mark.parametrize(
    "mode,stage",
    [
        ("mixed", "source_single_file"),
        ("no_sc", "reference_existence"),
        ("no_ev", "reference_existence"),
        ("line", "source_contains_code_line"),
        ("sc_index_as_line", "packaging_binding_contract"),
    ],
)
def test_primary_contract_errors_remain_rejected(prepared, mode, stage):
    root = prepared[0]
    common, _, _ = c.packet_files(root, cases(root)[0]["id"], "B_eq")
    d = bound_fixture(common)
    if mode == "mixed":
        d["source_evidence_ids"].append(
            next(s["id"] for s in common["source_evidence"] if s["path"] == "Dockerfile")
        )
    elif mode == "no_sc":
        d["source_evidence_ids"] = ["SC999999"]
    elif mode == "no_ev":
        d["evidence_ids"] = ["EV999999"]
    elif mode == "line":
        d["code_line"] = 999
    else:
        copy_row = max(
            (
                s
                for s in common["source_evidence"]
                if s["path"] == "Dockerfile" and s["text"].startswith("COPY ")
            ),
            key=lambda s: s["line"],
        )
        d["packaging"]["insert_after_line"] = int(copy_row["id"][2:])
    text = c.canonical(d)
    assert c.assess_primary(text, "B_eq", common)["effective_decision"] == "error"
    assert c.diagnostics(text, "B_eq", common)["stages"][stage] is False


def test_semantic_wrong_location_separate_from_structural_acceptance(prepared):
    root = prepared[0]
    case = cases(root)[0]
    common, _, _ = c.packet_files(root, case["id"], "B_eq")
    d = bound_fixture(common)
    selected = next(s for s in common["source_evidence"] if s["path"] == "Dockerfile")
    d.update(
        code_path="Dockerfile", code_line=selected["line"], source_evidence_ids=[selected["id"]]
    )
    p = c.assess_primary(c.canonical(d), "B_eq", common)
    ref = c.read(root / "references.json")["cases"][case["id"]]
    row = {
        "primary": p,
        "arm": "B_eq",
        "primary_error": None,
        "final_original": {"analysis": p["analysis"]},
        "final_normalized": {"analysis": p["analysis"]},
    }
    assessment = score(row, ref)
    assert c.diagnostics(c.canonical(d), "B_eq", common)["stages"]["source_single_file"]
    assert not assessment["binding_checks"]["source_path"]


@pytest.mark.parametrize("program", range(3))
def test_full_contract_valid_multiple_files_and_invalid_variants(prepared, program):
    root = prepared[0]
    common, _, _ = c.packet_files(root, cases(root)[program]["id"], "B_eq")
    good = full_fixture(common)
    assert len({f["path"] for f in good["source_findings"]["findings"]}) >= 2
    assert c.assess_final(c.canonical(good), common)["valid"]
    for mode in ("language", "empty_ev", "wrong_range", "unknown_reference"):
        bad = copy.deepcopy(good)
        if mode == "language":
            bad["remediation"]["plans"][0]["changes"][0]["language"] = "dockerfile"
        elif mode == "empty_ev":
            bad["observations"][0]["evidence_ids"] = []
        elif mode == "wrong_range":
            bad["source_findings"]["findings"][0]["end_line"] += 1
        else:
            bad["observations"][0]["evidence_ids"] = ["EV999999"]
        assert not c.assess_final(c.canonical(bad), common, allow_normalization=True)["valid"]
        diag = c.diagnostics(c.canonical(bad), "B_eq", common, fallback=True)
        assert diag["errors"] and diag["first_failure"]["json_path"]


@pytest.mark.parametrize(
    "mode", ["valid_counter", "empty_counter", "invalid_counter", "inconsistent"]
)
def test_defer_and_counterevidence_are_separate(prepared, mode):
    root = prepared[0]
    case = cases(root, "N_external_supply")[0]
    common, compact, _ = c.packet_files(root, case["id"], "R_llm")
    d = c.automatic_decision(compact)
    ref = c.read(root / "references.json")["cases"][case["id"]]
    d.update(
        decision="defer", plan="defer", counter_evidence_ids=ref["counter_evidence_groups"][0][:1]
    )
    if mode == "empty_counter":
        d["counter_evidence_ids"] = []
    if mode == "invalid_counter":
        d["counter_evidence_ids"] = ["EV999999"]
    if mode == "inconsistent":
        d["decision"] = "accept"
    p = c.assess_primary(c.canonical(d), "R_llm", common, compact)
    row = {
        "primary": p,
        "arm": "R_llm",
        "primary_error": None,
        "final_original": {"analysis": p["analysis"]},
        "final_normalized": {"analysis": p["analysis"]},
    }
    result = score(row, ref)
    assert result["explicit_defer"] == (mode in ("valid_counter", "empty_counter"))
    if mode == "empty_counter":
        assert result["counter_group_coverage"]["numerator"] == 0


def test_auto_no_backend_and_b_eq_no_candidate_or_reference_access(prepared, tmp_path, monkeypatch):
    root, config, _ = prepared
    case = cases(root)[0]
    common, _, _ = c.packet_files(root, case["id"], "B_eq")
    refs_read = c.legacy.read

    def guarded(path):
        assert Path(path).name not in ("references.json", "compact.json", "candidate-hints.json")
        return refs_read(path)

    def forbidden(*_, **__):
        raise AssertionError("Graph or candidate access")

    monkeypatch.setattr("ai_error_check_agent.knowledge.compact.build", forbidden)
    monkeypatch.setattr("ai_error_check_agent.diagnosis_routing.candidate_ranges", forbidden)
    with monkeypatch.context() as m:
        m.setattr(c.legacy, "read", guarded)
        bad = bound_fixture(common)
        bad["source_evidence_ids"] = ["SC999999"]
        backend = FakeBackend([bad, full_fixture(common)])
        result = asyncio.run(
            runner.run_cell(
                slot(case, "B_eq"),
                root,
                tmp_path / "b",
                fake_session(config, tmp_path / "b", backend),
            )
        )
        assert len(backend.requests) == 2 and result["final_original"]["valid"]
        assert backend.requests[1] == c.model_request("B_eq", common, fallback=True)
        assert "rule_candidates" not in backend.requests[0][1]
    backend = FakeBackend([])
    result = asyncio.run(
        runner.run_cell(
            slot(case, "R_auto"),
            root,
            tmp_path / "auto",
            fake_session(config, tmp_path / "auto", backend),
        )
    )
    assert result["final_original"]["valid"] and not backend.requests


def test_legacy_rejection_counts_and_single_field_diagnostic_copy(prepared):
    root, _, audit_path = prepared
    result = c.read(audit_path / "legacy-failure-audit.json")
    assert result["passed"] and result["mixed_source_primary"] == 72
    assert result["first_failure_counts"] == {
        "SCHEMA_ENUM": 51,
        "SCHEMA_MINITEMS": 18,
        "INVALID_SOURCE_LOCATION": 6,
    }
    row = c.read(c.ROOT / "results/review-controls-v4-live/cells/a5ae7ceefc4afbd8bb9b/result.json")
    common, _, _ = c.packet_files(root, row["case_id"], "B_eq")
    before = json.loads(row["primary_text"])
    after = copy.deepcopy(before)
    allowed = {s["id"] for s in common["source_evidence"] if s["path"] == after["code_path"]}
    after["source_evidence_ids"] = [x for x in after["source_evidence_ids"] if x in allowed]
    assert c.assess_primary(c.canonical(before), "G_eq", common)["effective_decision"] == "error"
    assert c.assess_primary(c.canonical(after), "G_eq", common)["effective_decision"] == "accept"
    assert [k for k in before if before[k] != after[k]] == ["source_evidence_ids"]


def test_schedule_initial_gate_and_frozen_prepared_file_set(prepared):
    root, config, _ = prepared
    cc = c.read(root / "dataset.json")["cases"]
    scheduled = c.schedule(cc)
    assert len(scheduled) == len({s["cell_id"] for s in scheduled}) == 120
    assert all(s["arm"] == "R_auto" for s in scheduled[:12])
    supported = {x["id"] for x in cases(root)}
    assert all(s["case_id"] in supported and s["repeat"] == 0 for s in scheduled[12:21])
    assert Counter(s["arm"] for s in scheduled[12:21]) == {"R_llm": 3, "B_eq": 3, "G_eq": 3}
    manifest = c.prepared_manifest(root, config)
    assert c.code_manifest() == manifest["code"]
    wrong = copy.deepcopy(manifest)
    wrong["files"]["imaginary-file"] = "invalid"
    with pytest.raises(ValueError):
        c.verify_prepared(root, config, wrong)


def test_malformed_fallback_diagnostics_do_not_interrupt_recording(prepared):
    root = prepared[0]
    common, _, _ = c.packet_files(root, cases(root)[0]["id"], "B_eq")
    for malformed in (
        {"source_findings": "invalid", "observations": None},
        {"source_findings": {"findings": None}, "observations": [{"evidence_ids": [{}]}]},
        {"source_findings": {"findings": [{"source_evidence_ids": [{}]}]}},
    ):
        result = c.diagnostics(c.canonical(malformed), "B_eq", common, fallback=True)
        assert result["stages"]["full_response_schema"] is False
        assert result["first_failure"]


def test_mock_all_paths_and_216_budget(prepared, tmp_path):
    _root, config, _ = prepared
    output = tmp_path / "synthetic-mock"
    result = asyncio.run(runner.run(config, output, live=False))
    assert result["completed"] == 120 and result["new_model_calls"] == 0
    trace = c.read(output / "mock_trace_coverage.json")
    assert trace["passed"] and all(trace["checks"].values())
    frozen = c.read(output / "frozen.json")
    before = c.hashes(output)
    again = asyncio.run(runner.run(config, output, live=False))
    # The existing exclusive writer lock records acquisition; saved experiment bytes do not change.
    after = c.hashes(output)
    assert again == result
    assert {k: v for k, v in after.items() if k != "writer.lock"} == {
        k: v for k, v in before.items() if k != "writer.lock"
    }
    config_data, b, _ = c.configuration(config)
    budget = tmp_path / "fake-budget"
    s = runner.ReviewSession(budget, FakeBackend([]), b, config_data, live=True)
    for i in range(216):
        save(
            budget / "cells" / ("a" * 20) / "calls" / f"{i:03}.json",
            {
                "dispatched": True,
                "status": "in_flight",
                "stage": "primary" if i < 108 else "fallback",
            },
        )
    for stage in ("primary", "fallback"):
        with pytest.raises(DiagnosisError):
            s.assert_budget(stage)
    # Existing in-flight states block resending before any backend setup.
    cell = output / "cells" / frozen["schedule"][0]["cell_id"]
    state = c.read(cell / "state.json")
    state["status"] = "started"
    save(cell / "state.json", state)
    with pytest.raises(ValueError, match="no automatic resend"):
        asyncio.run(runner.run(config, output, live=False))


def test_fake_preflight_freeze_initial_stop_evaluate_and_report(prepared, tmp_path, monkeypatch):
    import evaluate_review_controls_v4_contract_v2 as evaluator
    import remediation_cross_program_docker_v4 as docker
    import report_review_controls_v4_contract_v2 as reporter
    import review_controls_preflight_contract_v2 as prep

    root, config, audit_path = prepared
    calls = []

    class FakeReplay:
        def __init__(self, *_):
            self.commands = []
            self.base_image = c.read(root / "protocol.json")["base_image"]

        def initialize(self, *_):
            pass

        def cleanup(self):
            return []

        def replay(self, files, dockerfile, tests, folder, label):
            mode = folder.parent.name
            calls.append(mode)
            passed = (
                mode == "reference"
                if mode
                in ("reference", "unmodified", "wrong_destination", "empty_target", "wrong_content")
                else True
            )
            return {
                **dict.fromkeys(
                    (
                        "application_pass",
                        "build_pass",
                        "execution_pass",
                        "functional_pass",
                        "regression_pass",
                        "data_preservation_pass",
                        "target_failure_resolved",
                        "health_pass",
                    ),
                    passed,
                ),
                "executed": True,
                "evaluation_error": None,
            }

    monkeypatch.setattr(docker, "CrossReplay", FakeReplay)
    monkeypatch.setattr(evaluator, "CrossReplay", FakeReplay)
    pre = tmp_path / "fake-preflight"
    assert prep.preflight(root, pre, config)["passed"]
    assert Counter(calls) == dict.fromkeys(
        ("reference", "unmodified", "wrong_destination", "empty_target", "wrong_content"), 3
    )
    mock = tmp_path / "synthetic-mock"
    asyncio.run(runner.run(config, mock, live=False))
    junit = tmp_path / "synthetic-test-only.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>'
    )
    tests = tmp_path / "synthetic-test-only.json"
    c.write(
        tests,
        {
            "exit_code": 0,
            "counts": {"tests": 1},
            "manifest": c.prepared_manifest(root, config),
            "junit_path": str(junit),
            "junit_sha256": c.sha(junit),
        },
    )
    freeze(root, config, pre, mock, tests, audit_path)
    c.verify_freeze(root, config)
    # Copy mock only to isolated test output; never manufacture production live evidence.
    import shutil

    source = tmp_path / "synthetic-live-format-only"
    shutil.copytree(mock, source)
    f = c.read(source / "frozen.json")
    f.update(
        mode="live",
        run_id=c.digest({"version": c.VERSION, "output": str(source.resolve())})[:24],
        freeze_sha256=c.digest(c.read(root / "freeze-manifest.json")),
    )
    save(source / "frozen.json", f)
    initial_ids = {s["cell_id"] for s in f["schedule"][12:21] if s["arm"] in ("B_eq", "G_eq")}
    for cid in initial_ids:
        path = source / "cells" / cid
        row = c.read(path / "result.json")
        row["primary"]["effective_decision"] = "error"
        row["primary"]["server_transition_code"] = "INVALID_COMPACT_RESULT"
        save(path / "result.json", row)
        state = c.read(path / "state.json")
        state["result_sha256"] = c.digest(row)
        save(path / "state.json", state)
    with pytest.raises(ValueError, match="Initial gate"):
        asyncio.run(runner.run(config, source, live=True, phase="remaining"))
    # Restore the saved mock bytes to inspect a valid end-to-end report format.
    for cid in initial_ids:
        for name in ("result.json", "state.json"):
            shutil.copyfile(mock / "cells" / cid / name, source / "cells" / cid / name)
    evaluation = tmp_path / "fake-execution"
    evaluator.evaluate(source, root, evaluation, execute=True)
    output = tmp_path / "synthetic-report-only"
    reporter.report(
        source, evaluation, output, c.ROOT / "results/review-controls-v4-report", audit_path
    )
    assert c.read(output / "summary.json")["scheduled"] == 120
    assert (output / "paper-claims-handoff.md").is_file()
    assert c.read(output / "preservation-after.json")["preserved"]
