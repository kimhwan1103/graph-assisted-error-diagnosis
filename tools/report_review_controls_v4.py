"""Separate E1/E2 descriptive results with scheduled denominators and failure traces."""

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from evaluate_remediation_v4 import readonly_run_lock, separate
from evaluate_review_controls_v4 import rate, score
from review_controls_common_v4 import (
    ARMS,
    ROOT,
    VERSION,
    digest,
    fresh,
    hashes,
    original_routes,
    read,
    verify_prepared,
    write,
)
from run_review_controls_v4 import call_records, completed_rows


def aggregate(rows, raw_by_id, arm, ledger=None):
    selected = [r for r in rows if r["arm"] == arm]
    positive = [r for r in selected if r["expected_decision"] == "accept"]
    negative = [r for r in selected if r["expected_decision"] == "defer"]

    def count(rr, name):
        return sum(bool((r["score"] or {}).get(name)) for r in rr)

    costs = [raw_by_id[r["cell_id"]] for r in selected if r["cell_id"] in raw_by_id]
    missing_rows = len(selected) - len(costs)
    result = {
        "arm": arm,
        "scheduled": len(selected),
        "positive_scheduled": len(positive),
        "negative_scheduled": len(negative),
        "source_unavailable": missing_rows,
        "primary_states": dict(
            Counter((r["score"] or {}).get("primary_state", "source_unavailable") for r in selected)
        ),
        "explicit_accept": rate(count(selected, "explicit_accept"), len(selected)),
        "effective_accept": rate(count(selected, "effective_accept"), len(selected)),
        "appropriate_accept": rate(count(positive, "appropriate_accept"), len(positive)),
        "inappropriate_accept": rate(count(negative, "inappropriate_accept"), len(negative)),
        "negative_explicit_defer": rate(count(negative, "explicit_defer"), len(negative)),
        "excessive_defer": rate(count(positive, "excessive_defer"), len(positive)),
        "primary_failure": rate(count(selected, "primary_failure"), len(selected)),
        "schema_valid": rate(sum(r["primary"]["schema_valid"] for r in costs), len(selected)),
        "all_binding_match": rate(
            sum(
                bool((r["score"] or {}).get("binding_checks"))
                and all(v is True for v in r["score"]["binding_checks"].values())
                for r in selected
            ),
            len(selected),
        ),
        "counter_precision": rate(
            sum(
                (r["score"] or {}).get("counter_precision", {}).get("numerator", 0)
                for r in selected
            ),
            sum(
                (r["score"] or {}).get("counter_precision", {}).get("denominator", 0)
                for r in selected
            ),
        ),
        "counter_group_coverage": rate(
            sum(
                (r["score"] or {}).get("counter_group_coverage", {}).get("numerator", 0)
                for r in negative
            ),
            len(negative),
        ),
        "counter_group_coverage_scope": "one predefined counterfact group per negative case; missing responses counted as zero coverage",
        "primary_calls": sum(
            c["dispatched"] and c["stage"] == "primary" for r in costs for c in r["calls"]
        ),
        "fallback_calls": sum(
            c["dispatched"] and c["stage"] == "fallback" for r in costs for c in r["calls"]
        ),
        "returned_versions": sorted({str(v) for r in costs for v in r["returned_model_versions"]}),
        "review_ms_known": sum(
            r["stage_times"]["primary_call_wall_ms"]
            - r["stage_times"]["primary_pacing_wait_ms"]
            + r["stage_times"]["parse_assemble_validate_ms"]
            for r in costs
        ),
        "fallback_ms_known": sum(r["stage_times"]["fallback_ms"] for r in costs),
        "pacing_wait_ms_known": sum(r["pacing_wait_ms"] for r in costs),
        "wall_ms_known": sum(r["wall_ms"] for r in costs),
        "timing_rows_observed": len(costs),
    }
    for key in ("input_tokens", "output_tokens"):
        missing = sum(r[key + "_missing_calls"] for r in costs)
        known = sum(r[key + "_known"] for r in costs)
        result[key] = {
            "known_sum": known,
            "missing_calls": missing,
            "unavailable_rows": missing_rows,
            "total": known if not missing and not missing_rows else None,
        }
    if ledger is not None:
        selected_ids = {r["cell_id"] for r in selected}
        calls = [
            record
            for path, record in ledger
            if path.parent.parent.name in selected_ids and record.get("dispatched")
        ]
        result["primary_calls"] = sum(c["stage"] == "primary" for c in calls)
        result["fallback_calls"] = sum(c["stage"] == "fallback" for c in calls)
        result["network_ms_known_all_calls"] = sum(c.get("network_ms") or 0 for c in calls)
        result["pacing_wait_ms_known"] = sum(c.get("queue_ms") or 0 for c in calls)
        result["calls_from_unavailable_rows"] = sum(
            path.parent.parent.name not in raw_by_id and c.get("dispatched", False)
            for path, c in ledger
            if path.parent.parent.name in selected_ids
        )
        result["returned_versions"] = sorted(
            {str(c.get("raw", {}).get("model_version")) for c in calls}
        )
        for key in ("input_tokens", "output_tokens"):
            values = [c.get("raw", {}).get(key) for c in calls]
            missing = sum(v is None for v in values)
            known = sum(v for v in values if v is not None)
            result[key] = {
                "known_sum": known,
                "missing_calls": missing,
                "unavailable_rows": missing_rows,
                "total": known if not missing and not missing_rows else None,
            }
    for version in ("original", "normalized"):
        eligible = [r["execution"][version] for r in selected if r["execution_eligible"]]
        successes = sum(x["restored"] is True for x in eligible)
        unresolved = sum(x["restored"] is None for x in eligible)
        actual = [x for x in eligible if x["executed"]]
        ncopy = [
            (r["score"] or {}).get("final_inapplicable_copy_plan", {}).get(version)
            for r in negative
        ]
        result[version] = {
            "execution_eligible": len(eligible),
            "applicable_plan": sum(x["applicable_plan"] for x in eligible),
            "applied": sum(x["applied"] for x in eligible),
            "executed": len(actual),
            "new_docker_replays": sum(not x.get("execution_cache_hit") for x in actual),
            "restored_scheduled": rate(successes, len(eligible)),
            "restored_applied": rate(sum(x["restored"] is True for x in actual), len(actual)),
            "unresolved": unresolved,
            "restoration_rate_complete": successes / len(eligible)
            if eligible and not unresolved
            else None,
            "execution_outcomes": dict(Counter(x["execution_status"] for x in eligible)),
            "final_inapplicable_copy": rate(sum(v is True for v in ncopy), len(negative)),
            "final_inapplicable_copy_unavailable": sum(v is None for v in ncopy),
        }
    return result


def fraction(value):
    return f"{value['numerator']}/{value['denominator']}" if value["denominator"] else "N/A"


def case_comparisons(rows):
    """One deterministic value vs repeat means, never three independent auto observations."""
    out = []
    metrics = ("appropriate_accept", "inappropriate_accept", "excessive_defer", "primary_failure")
    for cid in sorted({r["case_id"] for r in rows}):
        selected = [r for r in rows if r["case_id"] == cid]
        entry = {
            k: selected[0][k]
            for k in ("case_id", "program_id", "context_variant", "expected_decision")
        }
        entry["unit"] = "one base-case context; repeated calls are not independent incidents"
        for arm in ARMS:
            subset = [r for r in selected if r["arm"] == arm]
            entry[arm] = {
                "scheduled": len(subset),
                "observed": sum(r["score"] is not None for r in subset),
                **{
                    metric: sum(bool(r["score"][metric]) for r in subset) / len(subset)
                    if subset and all(r["score"] is not None for r in subset)
                    else None
                    for metric in metrics
                },
            }
        for name, first, second in (
            ("E1_R_llm_minus_R_auto", "R_llm", "R_auto"),
            ("E2_G_eq_minus_B_eq", "G_eq", "B_eq"),
        ):
            entry[name] = {
                metric: entry[first][metric] - entry[second][metric]
                if entry[first][metric] is not None and entry[second][metric] is not None
                else None
                for metric in metrics
            }
        out.append(entry)
    return out


def write_csv(path, rows):
    if not rows:
        return
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("x", newline="", encoding="utf-8-sig") as handle:
        out = csv.DictWriter(handle, fieldnames=keys)
        out.writeheader()
        for row in rows:
            out.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for k, v in row.items()
                }
            )


def report(source, evaluation, output):
    fresh(output)
    separate(output, source)
    separate(output, evaluation)
    frozen = read(source / "frozen.json")
    root = Path(frozen["root"])
    if frozen["mode"] != "live":
        raise ValueError("Mock report cannot claim model quality")
    verify_prepared(root, Path(frozen["config_path"]), frozen["manifest"])
    with readonly_run_lock(source) as lock:
        before = hashes(source, lock)
        raw, pending, excluded = completed_rows(source, frozen)
        raw_by_id = {r["cell_id"]: r for r in raw}
        ledger = call_records(source)
        evaluated = read(evaluation / "summary.json")
        if evaluated["study_sha256"] != digest(frozen):
            raise ValueError("Evaluation belongs to a different run")
        rows = evaluated["rows"]
        if [{k: r[k] for k in ("case_id", "arm", "repeat", "cell_id")} for r in rows] != frozen[
            "schedule"
        ]:
            raise ValueError("Scheduled evaluation rows differ")
        refs = read(root / "references.json")["cases"]
        for row in rows:
            actual = raw_by_id.get(row["cell_id"])
            if row["raw_result_sha256"] != (digest(actual) if actual else None):
                raise ValueError("Evaluation source changed")
            if actual and row["score"] != score(actual, refs[row["case_id"]]):
                raise ValueError("Review assessment is not reproducible")
            if row != read(evaluation / "cells" / row["cell_id"] / "result.json"):
                raise ValueError("Evaluation summary mismatch")
        paired = []
        for cid, ref in refs.items():
            selected = [r for r in rows if r["case_id"] == cid]
            if not selected:
                continue
            auto = next(r for r in selected if r["arm"] == "R_auto")
            for repeat in range(3):
                b = next(
                    (
                        raw_by_id[r["cell_id"]]
                        for r in selected
                        if r["arm"] == "B_eq"
                        and r["repeat"] == repeat
                        and r["cell_id"] in raw_by_id
                    ),
                    None,
                )
                g = next(
                    (
                        raw_by_id[r["cell_id"]]
                        for r in selected
                        if r["arm"] == "G_eq"
                        and r["repeat"] == repeat
                        and r["cell_id"] in raw_by_id
                    ),
                    None,
                )
                llm = next(r for r in selected if r["arm"] == "R_llm" and r["repeat"] == repeat)
                request_checks = {
                    "E2_actual_payload_match": None,
                    "E2_generation_config_match": None,
                }
                if b and g and b["calls"] and g["calls"]:
                    bc, gc = (
                        read(source / b["calls"][0]["path"]),
                        read(source / g["calls"][0]["path"]),
                    )
                    request_checks = {
                        "E2_actual_payload_match": bc["input"]
                        == {k: v for k, v in gc["input"].items() if k != "rule_candidates"},
                        "E2_generation_config_match": bc["generation_config_sha256"]
                        == gc["generation_config_sha256"],
                    }
                    if not all(request_checks.values()):
                        raise ValueError("Actual matched-baseline request invariant failed")
                paired.append(
                    {
                        **request_checks,
                        "case_id": cid,
                        "program_id": ref["program_id"],
                        "context_variant": ref["context_variant"],
                        "repeat": repeat,
                        "deterministic_reference_reused": True,
                        "R_auto_cell": auto["cell_id"],
                        "R_auto_effective_accept": (auto["score"] or {}).get("effective_accept"),
                        "R_llm_cell": llm["cell_id"],
                        "R_llm_effective_accept": (llm["score"] or {}).get("effective_accept"),
                        "E2_evidence_hash_match": b["evidence_sha256"] == g["evidence_sha256"]
                        if b and g
                        else None,
                        "E2_prompt_schema_match": (b["prompt_sha256"], b["schema_sha256"])
                        == (g["prompt_sha256"], g["schema_sha256"])
                        if b and g
                        else None,
                    }
                )
        groups = [aggregate(rows, raw_by_id, arm, ledger) for arm in ARMS]
        comparisons = case_comparisons(rows)
        programs = [
            {
                "program_id": program,
                **aggregate(
                    [r for r in rows if r["program_id"] == program], raw_by_id, arm, ledger
                ),
            }
            for program in sorted({r["program_id"] for r in rows})
            for arm in ARMS
        ]
        dataset_rows = []
        routing = original_routes(root)
        for cid, ref in refs.items():
            eligibility = read(root / "packets" / cid / "eligibility.json")
            common = read(root / "packets" / cid / "common.json")
            dataset_rows.append(
                {
                    "case_id": cid,
                    "program_id": ref["program_id"],
                    "base_incident": ref["base_incident_id"],
                    "context": ref["context_variant"],
                    "expected_decision": ref["expected_review_decision"],
                    "provenance": ref["context_provenance"],
                    "normal_route": routing.get(cid, {}).get("normal_route"),
                    "routing_audit": routing.get(cid),
                    "eligible": eligibility["eligible"],
                    "candidate_ranges": eligibility["candidate_ranges"],
                    "candidate_generated": eligibility["checks"]["both_candidates"],
                    "source_bytes": len(
                        json.dumps(common["source_evidence"], ensure_ascii=False).encode()
                    ),
                    "preparation_ms": eligibility["preparation_ms"],
                    "graph_ms": eligibility["graph_ms"],
                }
            )
        summary = {
            "version": VERSION,
            "mode": "development",
            "scheduled": len(rows),
            "pending": len(pending),
            "excluded": len(excluded),
            "by_arm": groups,
            "by_program": programs,
            "case_repeat_means_and_differences": comparisons,
            "E1_and_E2_are_separate": True,
            "human_approvals": 0,
            "base_incidents": 3,
            "data": dataset_rows,
            "source_unchanged": True,
            "analysis_model_calls": 0,
        }
        output.mkdir(parents=True)
        write(output / "summary.json", summary)
        write(output / "cells.json", rows)
        write(output / "paired.json", paired)
        write_csv(output / "cells.csv", rows)
        write_csv(output / "program-summary.csv", programs)
        write_csv(output / "paired.csv", paired)
        write_csv(output / "case-comparisons.csv", comparisons)
        failure_rows = [
            r
            for r in rows
            if not r["score"]
            or any(
                r["score"][k]
                for k in ("primary_failure", "inappropriate_accept", "excessive_defer")
            )
            or (r["execution_eligible"] and r["execution"]["original"]["restored"] is not True)
            or any(
                v is True
                for v in (r["score"] or {}).get("final_inapplicable_copy_plan", {}).values()
            )
            or (
                (r["score"] or {}).get("positive")
                and (r["score"] or {}).get("effective_accept")
                and not (r["score"] or {}).get("appropriate_accept")
            )
        ]
        write(output / "failures.json", failure_rows)
        lines = [
            "# LLM 검토 제거 및 동일 입력·출력 기준선 실험",
            "",
            f"예정 {len(rows)}행, 미완료 {len(pending)}행, 제외 {len(excluded)}행. 기본 장애 3개에 저자 통제 문맥 4종을 적용했다. development이며 사람 참조 승인 없음.",
            "",
            "## 자료·경로",
            "",
            "|프로그램|문맥|참조 판단|적격|후보|사전 모의 운영 경로|",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"|{r['program_id']}|{r['context']}|{r['expected_decision']}|{r['eligible']}|{r['candidate_generated']}|{r['normal_route']}|"
            for r in dataset_rows
        ]
        for title, arms in (
            ("E1: 문맥 검토 없는 자동 채택 대비 LLM 검토", ("R_auto", "R_llm")),
            ("E2: 원자료·프롬프트·출력 계약이 같은 후보 제공 비교", ("B_eq", "G_eq")),
        ):
            lines += [
                "",
                "## " + title,
                "",
                "|조건|예정 양성/음성|원응답 accept|양성 적절 채택|음성 부적합 채택|음성 명시 보류|양성 과도 보류|주 단계 실패|반대 근거 coverage|",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for g in groups:
                if g["arm"] not in arms:
                    continue
                lines.append(
                    "|"
                    + "|".join(
                        [g["arm"], f"{g['positive_scheduled']}/{g['negative_scheduled']}"]
                        + [
                            fraction(g[k])
                            for k in (
                                "explicit_accept",
                                "appropriate_accept",
                                "inappropriate_accept",
                                "negative_explicit_defer",
                                "excessive_defer",
                                "primary_failure",
                                "counter_group_coverage",
                            )
                        ]
                    )
                    + "|"
                )
            lines.append("")
            for g in groups:
                if g["arm"] in arms:
                    lines.append(
                        f"- {g['arm']}의 상호 배타적인 주 단계 상태: `{json.dumps(g['primary_states'], ensure_ascii=False)}`"
                    )
        lines += [
            "",
            "|E2 조건|스키마 통과|모든 바인딩 일치(예정 전체 분모)|원본 적용 가능|실행 적격|원본 복구|",
            "|---|---|---|---|---|---|",
        ]
        for g in groups:
            if g["arm"] in ("B_eq", "G_eq"):
                lines.append(
                    f"|{g['arm']}|{fraction(g['schema_valid'])}|{fraction(g['all_binding_match'])}|{g['original']['applicable_plan']}|{g['original']['execution_eligible']}|{fraction(g['original']['restored_scheduled'])}|"
                )
        matched = [p for p in paired if p["E2_evidence_hash_match"] is not None]
        lines += [
            "",
            f"E2 대응 입력 확인: 관측 {len(matched)}/{len(paired)}쌍, 근거 해시 일치 {sum(p['E2_evidence_hash_match'] for p in matched)}, 프롬프트·스키마 일치 {sum(p['E2_prompt_schema_match'] for p in matched)}. B_eq는 후보 생성/조회 없이 공통 원자료만 재생한다.",
            "사례별 반복 평균과 대응 차이는 case-comparisons.csv에 제공한다. R_auto 값은 사례당 한 번만 집계하며 응답 미완료가 있으면 해당 대응 차이는 null이다. 올바른 채택 증가와 부적합 채택·과도 보류·실패 증가의 방향을 구분한다.",
            "",
            "## 후속 분석·비용",
            "",
            "|조건|주/후속 호출|입력 토큰 관측 합|출력 토큰 관측 합|usage 누락 호출(입력/출력)|검토 ms 합(대기 제외)|후속 ms 합(대기 포함)|pacing ms 합|wall ms 합|",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for g in groups:
            lines.append(
                f"|{g['arm']}|{g['primary_calls']}/{g['fallback_calls']}|{g['input_tokens']['known_sum']}|{g['output_tokens']['known_sum']}|{g['input_tokens']['missing_calls']}/{g['output_tokens']['missing_calls']}|{g['review_ms_known']:.1f}|{g['fallback_ms_known']:.1f}|{g['pacing_wait_ms_known']:.1f}|{g['wall_ms_known']:.1f}|"
            )
        lines += [
            "",
            "오프라인 준비·그래프 생성 시간은 data 표(JSON)에 사례별 1회만 기록했다. 온라인 시간은 고정 패킷 재생 단계이며 전체 시스템 지연이 아니다. 토큰 누락은 0이 아닌 null로 보존한다. 정규화는 같은 호출의 보조 분석이다.",
            "",
            "## 최종 계획·실행",
            "",
            "|조건|응답|음성 부적합 COPY|음성 최종 미판정|실행 적격|적용 가능|실제 적용|복구/예정|복구/실행|미평가|신규 Docker 실행|",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for g in groups:
            for v in ("original", "normalized"):
                x = g[v]
                lines.append(
                    f"|{g['arm']}|{v}|{fraction(x['final_inapplicable_copy'])}|{x['final_inapplicable_copy_unavailable']}|{x['execution_eligible']}|{x['applicable_plan']}|{x['applied']}|{fraction(x['restored_scheduled'])}|{fraction(x['restored_applied'])}|{x['unresolved']}|{x['new_docker_replays']}|"
                )
        lines += [
            "",
            "실행 적격은 P_supported만이다. 원본과 정규화의 동일 패치는 같은 행의 실행 결과를 해시로 연결해 재사용하며 독립 성공으로 합산하지 않는다. 음성의 COPY 제안은 조건부 제안의 부적합성이지 실제 실행·피해의 관측이 아니다.",
            "",
            "## 프로그램별 결과",
            "",
            "|프로그램|조건|양성 적절 채택|음성 부적합 채택|양성 과도 보류|주 단계 실패|원본 복구/예정|정규화 복구/예정|",
            "|---|---|---|---|---|---|---|---|",
        ]
        for g in programs:
            lines.append(
                f"|{g['program_id']}|{g['arm']}|{fraction(g['appropriate_accept'])}|{fraction(g['inappropriate_accept'])}|{fraction(g['excessive_defer'])}|{fraction(g['primary_failure'])}|{fraction(g['original']['restored_scheduled'])}|{fraction(g['normalized']['restored_scheduled'])}|"
            )
        lines += [
            "",
            "## 전체 실패·보류·부적합 사례",
            "",
        ]
        for r in failure_rows:
            lines.append(
                f"- {r['cell_id']} / {r['program_id']} / {r['context_variant']} / {r['arm']} / r{r['repeat']}: 주 단계 {(r['score'] or {}).get('primary_state', 'source_unavailable')}, 최종 부적합 COPY {(r['score'] or {}).get('final_inapplicable_copy_plan')}, 원본 실행 {r['execution']['original']['execution_status']}; {r['execution']['original'].get('adapter_error') or ''}"
            )
        lines += ["", "## 같은 형식의 문맥 추적", ""]
        for context in ("P_supported", "N_external_supply", "P_unknown"):
            representative = next(
                (
                    r
                    for r in rows
                    if r["context_variant"] == context and r["arm"] == "R_llm" and r["repeat"] == 0
                ),
                None,
            )
            if representative:
                r = representative
                original = raw_by_id.get(r["cell_id"], {})
                trace = {
                    "context": context,
                    "cell_id": r["cell_id"],
                    "expected": r["expected_decision"],
                    "primary": original.get("primary"),
                    "fallback_reason": original.get("fallback_reason"),
                    "final_inapplicable_copy": (r["score"] or {}).get(
                        "final_inapplicable_copy_plan"
                    ),
                    "execution_eligible": r["execution_eligible"],
                }
                lines += ["```json", json.dumps(trace, ensure_ascii=False, indent=2), "```", ""]
        lines += [
            "## 해석 범위",
            "",
            "E1은 문맥 검토 없는 자동 채택과 검토·설명 생성의 묶음 효과를 비교한다. 가능한 모든 결정적 계약 검사기보다 LLM이 우수하거나 필수임을 검증하지 않는다. E2는 같은 원자료·출력 계약에서 후보 필드를 추가 제공한 효과이며 RDF/OWL 자체의 우위를 뜻하지 않는다. 음성 채택 감소와 양성 보류 증가를 함께 보고한다. 개선이 없거나 악화되면 추가 효과 미확인으로 해석한다. 기본 장애 3개, 합성 계약 문맥, 같은 장애의 반복이므로 유의성·비열등성·일반화 신뢰구간을 주장하지 않는다. 원본이 주 분석이며 정규화는 보조 분석이다.",
        ]
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for name, doc in (
            ("reproduction.md", "PAPER_STUDY_V4_REVIEW_CONTROLS_COMMANDS.md"),
            ("deviations.md", "PAPER_STUDY_V4_REVIEW_CONTROLS_IMPLEMENTATION.md"),
        ):
            (output / name).write_text((ROOT / "docs" / doc).read_text("utf-8"), encoding="utf-8")
        if before != hashes(source, lock):
            raise ValueError("Source changed during reporting")
    return {"report": str(output / "report.md"), "scheduled": len(rows)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-run", type=Path, required=True)
    p.add_argument("--evaluation", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(
        json.dumps(
            report(a.source_run.resolve(), a.evaluation.resolve(), a.output.resolve()),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
