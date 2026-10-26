"""Research safeguards. All model/backend/container execution is faked or forbidden."""

import asyncio
import copy
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_controls_common_v4 as common
import run_review_controls_v4 as runner
from evaluate_review_controls_v4 import score
from review_controls_prepare_v4 import evidence

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.experiments.backends import BackendFailure
from ai_error_check_agent.experiments.study_v4 import save
from ai_error_check_agent.experiments.study_v4_runtime import mock_answer
from ai_error_check_agent.graph_compact import FALLBACK_CODES


@pytest.fixture
def prepared():
    root = common.DEFAULT_ROOT
    cases = common.read(root / "dataset.json")["cases"]
    refs = common.read(root / "references.json")["cases"]
    case = cases[0]
    data, packet, _ = common.packet_files(root, case["id"], "R_auto")
    hints = common.read(root / "packets" / case["id"] / "candidate-hints.json")
    return root, cases, refs, case, data, packet, hints


def bound(packet):
    c, p = packet["candidate"], packet["packaging_candidate"]
    return {
        "decision": "accept",
        "plan": "add_docker_copy",
        "target": c["target"],
        "code_path": c["code_path"],
        "code_line": c["code_line"],
        "evidence_ids": c["evidence_ids"],
        "source_evidence_ids": c["source_evidence_ids"],
        "packaging": {"repo_path": p["repo_path"], "insert_after_line": p["insert_after_line"]},
        "summary": "제공된 자료에서 파일 읽기와 복사 누락 후보를 확인했습니다.",
        "uncertainty": "실제 적용 조건과 파일 공급 계약을 확인해야 합니다.",
        "counter_evidence_ids": [],
    }


class FakeBackend:
    def __init__(self, answers=None, error=None):
        self.answers = list(answers or [])
        self.error = error
        self.requests = []

    def generate(self, prompt, payload, seed, *, schema):
        self.requests.append((prompt, copy.deepcopy(payload), copy.deepcopy(schema), seed))
        if self.error:
            raise BackendFailure(self.error)
        answer = self.answers.pop(0) if self.answers else mock_answer(schema)
        return {
            "text": answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False),
            "input_tokens": 10,
            "output_tokens": 5,
            "thinking_tokens": None,
            "finish_reason": "stop",
            "model_version": "fake-test-only",
            "provider_calls": 1,
        }


def session(tmp_path, backend):
    config, b, _ = common.configuration(common.DEFAULT_CONFIG)
    b = b.model_copy(update={"request_interval_seconds": 0})
    return runner.ReviewSession(tmp_path, backend, b, config, live=True)


def slot(case, arm, repeat=0):
    row = {"case_id": case["id"], "arm": arm, "repeat": repeat}
    return {**row, "cell_id": common.digest(row)[:20]}


def test_all_contexts_are_eligible_and_capture_unchanged(prepared):
    root, cases, refs, *_ = prepared
    assert len(cases) == 12 and all(c["eligible"] for c in cases)
    assert Counter(c["context_variant"] for c in cases) == dict.fromkeys(common.CONTEXTS, 3)
    assert common.read(root / "method-before.json") == common.source_manifest()
    base = {
        c["id"]: c
        for c in common.read(common.ROOT / "evaluation/cross-program-v4/dataset.json")["cases"]
    }
    provenance = common.read(root / "context-provenance.json")
    for c, p in zip(cases, provenance, strict=True):
        before = base[p["captured_base_case"]]["payload"]["data"]
        after = c["payload"]["data"]
        for old, new in zip(before["logs"], after["logs"][:-1], strict=True):
            assert {k: v for k, v in old.items() if k != "sourceId"} == {
                k: v for k, v in new.items() if k != "sourceId"
            }
        assert (
            before["deploymentStatus"] == after["deploymentStatus"]
            and before["logRange"] == after["logRange"]
        )
        request, bundle, data, _ranges, hints, archive_hash = evidence(c)
        assert hints and archive_hash == refs[c["id"]]["source_archive_sha256"]
        from ai_error_check_agent.knowledge.compact import build

        graph = build(request, bundle, data["source_evidence"], data["archive_observations"])
        assert graph and graph["report"]["metrics"]["conforms"]
        assert common.project_hints(graph["packet"]) == common.read(
            root / "packets" / c["id"] / "candidate-hints.json"
        )


def test_no_reference_or_condition_labels_in_model_packets(prepared):
    root, cases, refs, *_ = prepared
    for case in cases:
        data, compact, _ = common.packet_files(root, case["id"], "R_auto")
        text = common.canonical([data, compact])
        assert all(
            word not in text
            for word in (
                *common.CONTEXTS,
                "expected_decision",
                "expected_review_decision",
                "execution_eligible",
                "R_llm",
                "R_auto",
                "B_eq",
                "G_eq",
                "__evaluation",
            )
        )
        ids = {x["id"] for x in data["logs"]}
        assert set(refs[case["id"]]["context_evidence_ids"]) <= ids
        assert set(refs[case["id"]]["context_evidence_ids"]) <= {
            o["id"] for r in compact["logs"] for o in r["occurrences"]
        }
        assert {d["path"] for d in compact["deployment_code"]} == {"Dockerfile", ".dockerignore"}


def test_e1_identical_assembler_and_no_model_calls(prepared, tmp_path):
    root, _, _, case, data, packet, _ = prepared
    text = common.canonical(common.automatic_decision(packet))
    a = common.assess_primary(text, "R_auto", data, packet)
    b = common.assess_primary(text, "R_llm", data, packet)
    assert a == b and a["effective_decision"] == "accept"
    fake = FakeBackend(error="MUST_NOT_CALL")
    row = asyncio.run(
        runner.run_cell(slot(case, "R_auto"), root, tmp_path, session(tmp_path, fake))
    )
    assert (
        not fake.requests and row["input_tokens"] == row["output_tokens"] == 0 and not row["calls"]
    )


def test_e2_exact_input_prompt_schema_and_output(prepared):
    _, _, _, _, data, packet, hints = prepared
    b = common.model_request("B_eq", data)
    g = common.model_request("G_eq", data, hints=hints)
    assert (
        b[0] == g[0]
        and b[2] == g[2]
        and b[1] == {k: v for k, v in g[1].items() if k != "rule_candidates"}
    )
    text = common.canonical(bound(packet))
    bb = common.assess_primary(text, "B_eq", data)
    gg = common.assess_primary(text, "G_eq", data)
    assert bb == gg and bb["effective_decision"] == "accept"
    for arm in ("R_llm", "B_eq", "G_eq"):
        assert common.model_request(arm, data, fallback=True) == common.model_request(
            "B_eq", data, fallback=True
        )


@pytest.mark.parametrize(
    "key", ["snippet", "anchor", "insert_after_line", "expected_decision", "recommended_plan"]
)
def test_forbidden_candidate_fields_rejected(prepared, key):
    hints = copy.deepcopy(prepared[-1])
    hints["packaging_candidate"][key] = "forbidden"
    with pytest.raises(ValueError, match="forbidden"):
        common.validate_hints(hints)


def test_b_eq_runtime_never_builds_or_reads_candidates(prepared, tmp_path, monkeypatch):
    root, _, _, case, _, packet, _ = prepared

    def forbidden(*args, **kwargs):
        raise AssertionError("Graph/candidate access during B_eq")

    monkeypatch.setattr("ai_error_check_agent.knowledge.compact.build", forbidden)
    monkeypatch.setattr("ai_error_check_agent.diagnosis_routing.candidate_ranges", forbidden)
    monkeypatch.setattr("ai_error_check_agent.knowledge.compact.candidate_ranges", forbidden)
    monkeypatch.setattr("ai_error_check_agent.experiments.study_v4_arms.build", forbidden)
    oldread = common.read

    def guarded(path):
        assert Path(path).name not in ("compact.json", "candidate-hints.json", "references.json")
        return oldread(path)

    monkeypatch.setattr(common, "read", guarded)
    fake = FakeBackend([bound(packet)])
    row = asyncio.run(runner.run_cell(slot(case, "B_eq"), root, tmp_path, session(tmp_path, fake)))
    assert row["primary"]["effective_decision"] == "accept" and len(fake.requests) == 1
    assert "rule_candidates" not in fake.requests[0][1]


@pytest.mark.parametrize("arm", ["R_llm", "B_eq", "G_eq"])
@pytest.mark.parametrize("mode", ["defer", "invalid_ev", "conflicting_accept", "invalid_json"])
def test_decision_errors_are_not_correct_deferrals(prepared, arm, mode):
    _, _, refs, case, data, packet, _ = prepared
    d = common.automatic_decision(packet) if arm == "R_llm" else bound(packet)
    ev = refs[case["id"]]["context_evidence_ids"][0]
    if mode == "defer":
        d.update(decision="defer", plan="defer", counter_evidence_ids=[ev])
    elif mode == "invalid_ev":
        d.update(decision="defer", plan="defer", counter_evidence_ids=["EV999999"])
    elif mode == "conflicting_accept":
        d["counter_evidence_ids"] = [ev]
    text = "{" if mode == "invalid_json" else common.canonical(d)
    r = common.assess_primary(text, arm, data, packet)
    if mode == "defer":
        assert r["effective_decision"] == "defer" and not r["assessment_error"]
    elif mode == "invalid_ev":
        assert (
            r["server_transition_code"] == "COMPACT_DEFERRED"
            and "invalid_reference" in r["assessment_error"]
        )
    elif mode == "conflicting_accept":
        assert (
            r["decision_consistent"] is False
            and r["effective_decision"] == "blocked_inconsistent_or_invalid"
        )
    else:
        assert not r["schema_valid"] and r["effective_decision"] == "error"


def test_bad_source_binding_and_single_uncontaminated_fallback(prepared, tmp_path):
    root, _, _, case, data, packet, _ = prepared
    bad = bound(packet)
    bad["source_evidence_ids"] = ["SC999999"]
    fake = FakeBackend([bad])
    row = asyncio.run(runner.run_cell(slot(case, "B_eq"), root, tmp_path, session(tmp_path, fake)))
    assert len(fake.requests) == 2 and row["fallback_reason"] in FALLBACK_CODES
    assert row["final_original"]["valid"] and row["fallback_call_id"]
    assert fake.requests[1][:3] == common.model_request("R_llm", data, fallback=True)
    assert "rule_candidates" not in fake.requests[1][1] and "decision" not in fake.requests[1][1]


@pytest.mark.parametrize(
    "error", ["MODEL_HTTP_401", "MODEL_HTTP_429", "MODEL_TIMEOUT", "MODEL_CONNECTION_OR_JSON_ERROR"]
)
def test_provider_failure_is_durable_and_never_fallback(prepared, tmp_path, error):
    root, _, _, case, *_ = prepared
    fake = FakeBackend(error=error)
    row = asyncio.run(runner.run_cell(slot(case, "B_eq"), root, tmp_path, session(tmp_path, fake)))
    assert (
        row["primary_error"]["code"] == error
        and len(fake.requests) == 1
        and row["fallback_call_id"] is None
    )
    saved = common.read(tmp_path / row["raw_response_path"])
    assert saved["status"] == "failed" and saved["dispatched"] is True


def test_inflight_checkpoint_and_stage_budget(prepared, tmp_path):
    root, _, _, case, _, packet, _ = prepared
    fake = FakeBackend([bound(packet)])
    generate = fake.generate

    def assert_checkpoint(*args, **kwargs):
        calls = runner.call_records(tmp_path)
        assert calls[-1][1]["status"] == "in_flight" and calls[-1][1]["dispatched"] is True
        return generate(*args, **kwargs)

    fake.generate = assert_checkpoint
    s = session(tmp_path, fake)
    asyncio.run(runner.run_cell(slot(case, "B_eq"), root, tmp_path, s))
    s.review_config = {**s.review_config, "primary_call_cap": 1}
    with pytest.raises(DiagnosisError, match="예산"):
        s.assert_budget("primary")
    s.backend_config = s.backend_config.model_copy(update={"max_calls": 1})
    with pytest.raises(DiagnosisError):
        s.assert_budget("fallback")


def test_schedule_denominators_and_pairing(prepared):
    rows = common.schedule(prepared[1])
    assert rows == common.schedule(prepared[1])
    assert Counter(r["arm"] for r in rows) == {"R_auto": 12, "R_llm": 36, "B_eq": 36, "G_eq": 36}
    assert len({r["cell_id"] for r in rows}) == 120
    for i in range(12, len(rows), 3):
        assert len({(r["case_id"], r["repeat"]) for r in rows[i : i + 3]}) == 1
        assert {r["arm"] for r in rows[i : i + 3]} == {"R_llm", "B_eq", "G_eq"}


def test_unknown_is_not_execution_eligible_and_errors_do_not_count_as_deferral(prepared, tmp_path):
    root, cases, refs, _, _, _, _ = prepared
    case = next(c for c in cases if c["context_variant"] == "P_unknown")
    assert (
        refs[case["id"]]["expected_review_decision"] == "accept"
        and refs[case["id"]]["execution_eligible"] is False
    )
    fake = FakeBackend(error="MODEL_HTTP_429")
    row = asyncio.run(runner.run_cell(slot(case, "R_llm"), root, tmp_path, session(tmp_path, fake)))
    assessed = score(row, refs[case["id"]])
    assert (
        assessed["primary_failure"]
        and not assessed["excessive_defer"]
        and not assessed["explicit_defer"]
    )


def test_normalization_does_not_change_decision_or_binding(prepared):
    _, _, _, _, data, packet, _ = prepared
    value = json.loads(common.expand(common.canonical(common.automatic_decision(packet)), packet))
    original = copy.deepcopy(value)
    value["limitations"] = [{"text": x} for x in value["limitations"]]
    assert not common.assess_final(json.dumps(value, ensure_ascii=False), data)["valid"]
    norm = common.assess_final(
        json.dumps(value, ensure_ascii=False), data, allow_normalization=True
    )
    assert norm["valid"]
    assert norm["analysis"] == common.validate_full(json.dumps(original, ensure_ascii=False), data)


def test_production_recovery_and_multi_source_guards_still_reject(prepared):
    from dataclasses import replace

    from ai_error_check_agent.diagnosis_routing import candidate_ranges

    _, _, _, case, *_ = prepared
    request, bundle, data, *_ = evidence(case)
    recovered = replace(
        bundle,
        lines=(
            *bundle.lines,
            replace(bundle.lines[-1], text="service recovered and ready", id="EV999990"),
        ),
    )
    mixed = replace(
        bundle,
        lines=(
            *bundle.lines,
            replace(bundle.lines[-1], source_id="another-producer", id="EV999991"),
        ),
    )
    assert candidate_ranges(recovered) == candidate_ranges(mixed) == []
    from ai_error_check_agent.knowledge.compact import build

    assert build(request, recovered, data["source_evidence"], data["archive_observations"]) is None
    assert build(request, mixed, data["source_evidence"], data["archive_observations"]) is None


def test_tampered_manifest_is_rejected(prepared, tmp_path):
    path = tmp_path / "protected.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="mismatch"):
        common.verify_files({"src/ai_error_check_agent/graph_compact.py": "0" * 64})


def test_started_is_not_resent(prepared, tmp_path):
    config, _b, root = common.configuration(common.DEFAULT_CONFIG)
    frozen = {
        "version": common.VERSION,
        "mode": "mock",
        "config_path": str(common.DEFAULT_CONFIG),
        "config": config,
        "root": str(root),
        "manifest": common.prepared_manifest(root, common.DEFAULT_CONFIG),
        "freeze_sha256": None,
        "schedule": common.schedule(prepared[1]),
    }
    save(tmp_path / "frozen.json", frozen)
    s = frozen["schedule"][0]
    save(tmp_path / "cells" / s["cell_id"] / "state.json", {**s, "status": "started"})
    with pytest.raises(ValueError, match="no automatic resend"):
        asyncio.run(runner.run(common.DEFAULT_CONFIG, tmp_path))


def test_real_216_cap_and_partial_call_costs(prepared, tmp_path):
    import report_review_controls_v4 as reporter

    config, b, _ = common.configuration(common.DEFAULT_CONFIG)
    s = runner.ReviewSession(tmp_path, FakeBackend(), b, config, live=True)
    cid = "a" * 20
    for i in range(216):
        save(
            tmp_path / "cells" / cid / "calls" / f"{i:03}.json",
            {
                "dispatched": True,
                "status": "in_flight",
                "stage": "primary" if i < 108 else "fallback",
            },
        )
    with pytest.raises(DiagnosisError):
        s.assert_budget("primary")
    with pytest.raises(DiagnosisError):
        s.assert_budget("fallback")
    row = {
        "cell_id": cid,
        "arm": "B_eq",
        "expected_decision": "defer",
        "execution_eligible": False,
        "score": None,
        "execution": {"original": {}, "normalized": {}},
    }
    result = reporter.aggregate([row], {}, "B_eq", runner.call_records(tmp_path))
    assert result["primary_calls"] == result["fallback_calls"] == 108
    assert result["calls_from_unavailable_rows"] == 216
    assert (
        result["input_tokens"]["missing_calls"] == 216 and result["input_tokens"]["total"] is None
    )
    assert result["negative_explicit_defer"]["numerator"] == 0


def test_fake_docker_preflight_checks_all_five_modes(prepared, tmp_path, monkeypatch):
    import remediation_cross_program_docker_v4 as docker
    import review_controls_prepare_v4 as prep

    root = prepared[0]
    calls = []

    class FakeReplay:
        def __init__(self, *_):
            self.commands = []
            self.base_image = common.read(root / "protocol.json")["base_image"]

        def initialize(self, *_):
            pass

        def cleanup(self):
            return []

        def replay(self, files, dockerfile, spec, folder, label):
            mode = folder.parent.name
            calls.append(mode)
            return {
                **dict.fromkeys(
                    (
                        "build_pass",
                        "execution_pass",
                        "functional_pass",
                        "regression_pass",
                        "data_preservation_pass",
                        "target_failure_resolved",
                        "health_pass",
                    ),
                    mode == "reference",
                ),
                "evaluation_error": None,
            }

    async def fake_routes(*_):
        return [
            {
                "case_id": c["id"],
                "review_reached": True,
                "candidate_generated": True,
                "candidate_ranges": [],
            }
            for c in prepared[1]
        ]

    monkeypatch.setattr(docker, "CrossReplay", FakeReplay)
    monkeypatch.setattr(prep, "route_audit", fake_routes)
    result = prep.preflight(root, tmp_path / "only-fake-containers", common.DEFAULT_CONFIG)
    assert result["passed"] and Counter(calls) == dict.fromkeys(
        ("unmodified", "reference", "wrong_destination", "empty_target", "wrong_content"), 3
    )


def test_defer_with_wrong_candidate_is_assessment_error(prepared):
    _, _, _, _, data, packet, _ = prepared
    d = common.automatic_decision(packet)
    d.update(decision="defer", plan="defer", candidate_id="C999")
    result = common.assess_primary(common.canonical(d), "R_llm", data, packet)
    assert result["server_transition_code"] == "COMPACT_DEFERRED"
    assert "invalid_candidate_reference" in result["assessment_error"]


@pytest.mark.parametrize("execute_fake", [False, True])
def test_offline_evaluation_and_report_pipeline_with_only_fake_backend(
    prepared, tmp_path, monkeypatch, execute_fake
):
    import evaluate_review_controls_v4 as evaluator
    import report_review_controls_v4 as reporter

    root, _, _, case, _, _packet, _ = prepared
    source = tmp_path / "synthetic-only"
    source.mkdir()
    (source / "writer.lock").write_bytes(b"0")
    selected = [slot(case, "R_auto")] + [
        slot(case, a, r) for r in range(3) for a in ("R_llm", "B_eq", "G_eq")
    ]
    fake = FakeBackend()
    s = session(source, fake)
    for item in selected:
        raw = asyncio.run(runner.run_cell(item, root, source, s))
        save(source / "cells" / item["cell_id"] / "result.json", raw)
        save(
            source / "cells" / item["cell_id"] / "state.json",
            {**item, "status": "completed", "result_sha256": common.digest(raw)},
        )
    frozen = {
        "mode": "live",
        "root": str(root),
        "config_path": str(common.DEFAULT_CONFIG),
        "manifest": common.prepared_manifest(root, common.DEFAULT_CONFIG),
        "freeze_sha256": common.digest({}),
        "schedule": selected,
    }
    save(source / "frozen.json", frozen)
    monkeypatch.setattr(evaluator, "verify_freeze", lambda *_: {})

    def forbidden(*args, **kwargs):
        raise AssertionError("Docker execution forbidden in offline test")

    monkeypatch.setattr(evaluator.CrossReplay, "initialize", forbidden)
    monkeypatch.setattr(evaluator.CrossReplay, "replay", forbidden)
    replay_calls = []
    if execute_fake:

        class FakeReplay:
            def __init__(self, *_):
                self.commands = []
                self.base_image = common.read(root / "protocol.json")["base_image"]

            def initialize(self, *_):
                pass

            def cleanup(self):
                return []

            def replay(self, *args):
                replay_calls.append(args)
                return {
                    **dict.fromkeys(
                        (
                            "executed",
                            "application_pass",
                            "build_pass",
                            "execution_pass",
                            "functional_pass",
                            "regression_pass",
                            "data_preservation_pass",
                            "target_failure_resolved",
                            "health_pass",
                        ),
                        True,
                    ),
                    "evaluation_error": None,
                }

        monkeypatch.setattr(evaluator, "CrossReplay", FakeReplay)
    evaluation = tmp_path / "synthetic-evaluation"
    evaluator.evaluate(source, root, evaluation, execute=execute_fake)
    summary = common.read(evaluation / "summary.json")
    assert len(summary["rows"]) == 10 and summary["new_model_calls"] == 0
    out = tmp_path / "synthetic-report"
    reporter.report(source, evaluation, out)
    aggregate = common.read(out / "summary.json")
    assert aggregate["scheduled"] == 10 and len(aggregate["by_arm"]) == 4
    auto = next(x for x in aggregate["by_arm"] if x["arm"] == "R_auto")
    assert auto["positive_scheduled"] == 1 and auto["primary_calls"] == 0
    assert len(aggregate["case_repeat_means_and_differences"]) == 1
    if execute_fake:
        assert len(replay_calls) == 1
        assert auto["original"]["new_docker_replays"] == 1
        assert auto["normalized"]["new_docker_replays"] == 0
        assert auto["original"]["restored_scheduled"]["numerator"] == 1
        row = next(r for r in summary["rows"] if r["arm"] == "R_auto")
        cached = row["execution"]["normalized"]
        assert cached["execution_cache_hit"]
        assert (
            common.sha(Path(cached["execution_reused_from"])) == cached["execution_reused_sha256"]
        )


def test_existing_operational_routes_with_mock_runtime(prepared, tmp_path):
    from review_controls_prepare_v4 import route_audit

    _, backend, _ = common.configuration(common.DEFAULT_CONFIG)
    routing = asyncio.run(route_audit(prepared[0], tmp_path, backend))
    ids = {c["id"] for c in prepared[1]}
    main = [r for r in routing if r["case_id"] in ids]
    guard = [r for r in routing if r["case_id"] not in ids]
    assert len(main) == 12 and len(guard) == 6
    assert all(
        r["review_reached"] and r["candidate_generated"] and r["http_status"] == 200 for r in main
    )
    assert all(
        not r["candidate_ranges"] and not r["candidate_generated"]
        for r in guard
        if r["case_id"] != "expected-test-error"
    )
    test_fixture = next(r for r in guard if r["case_id"] == "expected-test-error")
    assert test_fixture["graph_report"]["candidate"] and not test_fixture["candidate_generated"]
    assert all(
        not common.read(p)["dispatched"]
        for p in tmp_path.glob("routing-audit/cells/*/calls/*.json")
    )
