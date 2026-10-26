"""Expand explicit auxiliary judgments and audit a saved offline v4 assessment.

This script does not assign semantic scores or contact a provider. An evaluator
must first author reviews/group-judgments.json after inspecting the packets.
"""

import argparse
import copy
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from revalidate_study_v4 import CRITERIA, ROOT, digest, hashes, normalize, read, write


def normalization_checks():
    value = {
        "limitations": ["existing", {"text": "  exact\ntext  "}, {"text": "x", "extra": 1},
                        {"text": 4}, {"other": "keep"}, None],
        "evidence_ids": ["SC000001"],
        "analysis_status": "diagnosed",
        "nested": {"limitations": [{"text": "keep nested"}]},
    }
    before = copy.deepcopy(value)
    result, edits = normalize(value)
    assert value == before, "Normalization mutated its input"
    assert result["limitations"][1] == "  exact\ntext  "
    assert result["limitations"][:1] + result["limitations"][2:] == before["limitations"][:1] + before["limitations"][2:]
    assert edits == [{"path": "/limitations/1", "before": {"text": "  exact\ntext  "}, "after": "  exact\ntext  "}]
    for key in ("evidence_ids", "analysis_status", "nested"):
        assert result[key] == before[key]
    assert normalize(result) == (result, [])
    for outside_scope in ([], None, {"limitations": {"text": "keep"}}, {"x": 1}):
        assert normalize(outside_scope) == (outside_scope, [])
    return "passed: exact one-key string objects only; unchanged content and IDs; no input mutation; idempotence; non-target shapes unchanged"


def run(directory):
    directory = directory.resolve()
    packet = read(directory / "reviews/all-items.json")
    inspection = read(directory / "reviews/inspection-groups.json")
    authored = read(directory / "reviews/group-judgments.json")
    validation = read(directory / "revalidation.json")
    assert authored["criterion_order"] == CRITERIA
    assert authored["role"] == "llm_auxiliary"
    original = Path(validation["original_directory"])
    assert hashes(original) == read(directory / "original-files.sha256.json")
    frozen = read(original / "frozen.json")
    assert digest(frozen) == packet["study_sha256"] == validation["study_sha256"]
    assert packet["assessment_sha256"] == validation["assessment_sha256"]
    for relative, expected in frozen["source_manifest"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, relative
    items = {x["review_id"]: x for x in packet["items"]}
    assert len(items) == len(packet["items"])
    for item in items.values():
        assert item["response_sha256"] == digest({key: item[key] for key in
            ("response", "source_evidence", "source_findings", "model_visible_context")})
    judgments = {x["group_id"]: x for x in authored["groups"]}
    assert len(judgments) == len(authored["groups"]) == len(inspection)
    assert set(judgments) == {x["group_id"] for x in inspection}
    assessed, results = set(), []
    equality_keys = ("case_id", "logs", "source_files", "reference", "response", "source_evidence", "source_findings", "model_visible_context")
    for group in inspection:
        judgment = judgments[group["group_id"]]
        assert len(judgment["scores"]) == len(judgment["reasons"]) == len(CRITERIA)
        assert all(type(x) is bool for x in judgment["scores"])
        assert all(isinstance(x, str) and x for x in judgment["reasons"])
        assert group["context_variants"] == [group["item"]["model_visible_context"]]
        for review_id in group["review_ids"]:
            assert review_id not in assessed
            assessed.add(review_id)
            item = items[review_id]
            assert item["response"]["job_status"] == "succeeded"
            for key in equality_keys:
                assert item[key] == group["item"][key], (review_id, key)
            known_ids = {e["id"] for e in item["response"]["evidence"] + item["source_evidence"]}
            cited = set(re.findall(r"\b(?:EV|SC)\d{6}\b", " ".join(judgment["reasons"])))
            assert cited <= known_ids, (group["group_id"], cited - known_ids)
            refs = [
                {"location": "response.analysis.summary, observations, hypotheses", "supports": "진단, 근거 및 확신 수준의 평가 대상 원문"},
                {"location": "response.evidence, source_evidence, source_findings", "supports": "실제 로그·소스 인용과 대응 위치"},
                {"location": "response.analysis.next_checks, missing_information, limitations, remediation", "supports": "추가 확인·미확인 조건·수정·검증·롤백의 유무와 내용"},
                {"location": "logs, source_files, model_visible_context, reference.quality_rubric", "supports": "원본 사례와 모델에게 실제 제공된 관찰 범위; reference는 잠정 참조"},
            ]
            results.append({"review_id": review_id, "case_id": item["case_id"],
                "response_sha256": item["response_sha256"], "group_id": group["group_id"],
                "review_status": "rated", "scores": dict(zip(CRITERIA, judgment["scores"])),
                "criterion_reasons": dict(zip(CRITERIA, judgment["reasons"])),
                "evidence_refs": refs, "rationale": judgment["rationale"],
                "issues": judgment["issues"], "needs_human_review": False,
                "llm_usable_final": int(all(judgment["scores"]))})
    for review_id, item in items.items():
        if review_id in assessed:
            continue
        assert item["response"]["job_status"] == "failed", review_id
        error = item["response"]["error"]
        reason = f"정규화 후에도 최종 응답 검증 실패({error['code']}). 유효한 최종 응답이 없어 해당 내용 기준을 채점하지 않았다."
        results.append({"review_id": review_id, "case_id": item["case_id"],
            "response_sha256": item["response_sha256"], "group_id": None,
            "review_status": "generation_failed", "scores": dict.fromkeys(CRITERIA),
            "criterion_reasons": dict.fromkeys(CRITERIA, reason),
            "evidence_refs": [{"location": "response.error", "supports": error["message"]}],
            "rationale": reason, "issues": [], "needs_human_review": False, "llm_usable_final": 0})
    order = {x["review_id"]: n for n, x in enumerate(packet["items"])}
    results.sort(key=lambda x: order[x["review_id"]])
    assert len(results) == len(items)
    assert {x["review_id"] for x in results} == set(items)
    for result in results:
        assert set(result["scores"]) == set(result["criterion_reasons"]) == set(CRITERIA)
        if result["review_status"] == "rated":
            assert result["llm_usable_final"] == int(all(result["scores"].values()))
        else:
            assert all(x is None for x in result["scores"].values()) and result["llm_usable_final"] == 0
    reviewed_at = datetime.now(timezone.utc).isoformat()
    review = {"version": "study-v4-llm-review.v1", "guide_version": "study-v4-llm-review-guide.v1",
        "study_sha256": packet["study_sha256"], "assessment_sha256": packet["assessment_sha256"],
        "batch_id": None, "role": "llm_auxiliary", "reviewer_type": "llm",
        "reviewer_id": "llm:OpenAI/Codex-assistant-model-unspecified", "reviewed_at": reviewed_at,
        "grader": {"provider": "OpenAI", "model_id": None, "model_version": None, "temperature": None, "seed": None},
        "evaluation_disclosure": {
            "independent_human": False, "blinded": False,
            "same_assistant_prepared_normalization": True, "previous_review_seen": True,
            "note": "The assistant saw method labels and an earlier supplied review. Judgments were made against saved responses and actual input context, including previously omitted archive observations. This is a new non-independent auxiliary assessment, not a blinded replication or human review.",
            "duplicate_handling": "Identical case, logs, full sources, reference, response, source evidence/findings and model-visible context share an authored judgment; every review ID is retained.",
            "repair_execution": False, "new_study_provider_calls": 0,
            "grading_chat_token_usage": None,
            "failure_label": "generation_failed means final response contract failure here; the original model calls completed. It does not mean 35 API/network errors.",
        }, "items": results}
    rows = [read(p) for p in sorted((directory / "cells").glob("*.json"))]
    assert len(rows) == len(items)
    assert {r["review_id"] for r in rows} == set(items)
    row_map = {r["review_id"]: r for r in rows}
    rated = [x for x in results if x["review_status"] == "rated"]
    summary = {"version": "study-v4-normalized-auxiliary-summary.v1", "reviewed_at": reviewed_at,
        "study_sha256": packet["study_sha256"], "assessment_sha256": packet["assessment_sha256"],
        "total_cells": len(results), "original_valid": validation["original_valid"],
        "normalized_valid": validation["normalized_valid"], "newly_valid": validation["newly_valid"],
        "remaining_validation_failures": validation["remaining_failures"],
        "rated": len(rated), "needs_human_review": 0,
        "all_five_pass": sum(x["llm_usable_final"] for x in rated),
        "rated_not_all_five_pass": sum(not x["llm_usable_final"] for x in rated),
        "criterion_passes": {k: sum(x["scores"][k] for x in rated) for k in CRITERIA},
        "unique_judgment_groups": len(inspection), "human_ratings_added": 0,
        "new_study_provider_calls": 0, "grading_chat_token_usage": None,
        "same_project_authored_cases": len({x["case_id"] for x in results}),
        "by_arm": {}, "by_case": {}, "paper_primary_endpoint": False}
    for arm, counts in validation["by_arm"].items():
        subset = [x for x in results if row_map[x["review_id"]]["arm"] == arm]
        summary["by_arm"][arm] = {**counts, "all_five_pass": sum(x["llm_usable_final"] for x in subset)}
    for case_id in sorted({x["case_id"] for x in results}):
        subset = [x for x in results if x["case_id"] == case_id]
        summary["by_case"][case_id] = {"total": len(subset), "rated": sum(x["review_status"] == "rated" for x in subset),
            "all_five_pass": sum(x["llm_usable_final"] for x in subset)}
    verification = {"version": "study-v4-normalized-review-verification.v1", "verified_at": reviewed_at,
        "normalization_boundaries": normalization_checks(), "original_results_byte_unchanged": True,
        "frozen_source_hashes_unchanged": True, "original_baseline_reproduced": validation["baseline_reproduced_cells"],
        "all_review_ids_exactly_once": True, "all_response_hashes_match": True,
        "group_inputs_exactly_equal": True, "explicit_cited_ids_exist": True, "score_logic_valid": True,
        "semantic_judgments_externally_validated": False,
        "file_sha256": {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in (
            ROOT / "tools/revalidate_study_v4.py", ROOT / "tools/summarize_normalized_review_v4.py",
            ROOT / "docs/PAPER_STUDY_V4_LLM_REVIEW_GUIDE.md", directory / "reviews/group-judgments.json")}}
    assert hashes(original) == read(directory / "original-files.sha256.json")
    write(directory / "reviews/codex-llm-review.json", review)
    write(directory / "summary.json", summary)
    write(directory / "verification.json", verification)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assessment", type=Path, required=True)
    run(parser.parse_args().assessment)
