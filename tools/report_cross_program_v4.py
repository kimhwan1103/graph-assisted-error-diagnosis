"""Scheduled-cell, program/stratum/repeat report; no surrogate semantic ratings."""

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path

from automatic_study_v4 import anchor_hit, citation_integrity
from cross_program_common_v4 import (
    ROOT,
    digest,
    fresh,
    hashes,
    load_source,
    read,
    readonly_run_lock,
    separate,
    sha,
    source_cell,
    write,
)
from revalidate_cross_program_v4 import assess_cell


def stats(values):
    present = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return {
        "observed": len(present),
        "scheduled": len(values),
        "mean": statistics.mean(present) if present else None,
        "median": statistics.median(present) if present else None,
        "sum_known": sum(present) if present else None,
        "total": sum(present) if len(present) == len(values) and present else None,
    }


def routes(original, calls, arm):
    report = (original or {}).get("graph_report")
    if arm == "adaptive":
        return "adaptive", None, False, False
    compact_calls = [c for c in calls if "decision" in c.get("schema", {}).get("properties", {})]
    if not report:
        return "early_general", "no_graph_attempt_recorded", False, False
    status = report.get("status")
    decision = report.get("decision_status")
    if decision == "accepted":
        route = "compact_accepted"
    elif compact_calls:
        route = "general_after_llm_review"
    else:
        route = "general_after_graph_attempt"
    return (
        route,
        decision or report.get("reason") or status,
        True,
        bool(report.get("packaging_candidate")),
    )


def diagnosis_row(source, slot, case, reference, assessment, variant):
    state, original = source_cell(source, slot)
    public = (original or {}).get("result") or {}
    folder = source / "cells" / slot["cell_id"]
    calls = [read(p) for p in sorted((folder / "calls").glob("*.json"))]
    dispatched = [c for c in calls if c.get("dispatched")]
    assessed = assessment["normalized" if variant == "normalized" else "original"]
    valid = assessed["valid"]
    analysis = assessed["analysis"] or {}
    findings = assessed["source_findings"]
    expected = reference["expected_anchor"]
    evidence = (public.get("source_analysis") or {}).get("evidence", [])
    a_hit = anchor_hit(expected, findings, evidence) if valid else False
    target_mention = valid and expected["target"] in json.dumps(analysis, ensure_ascii=False)
    citations = (
        citation_integrity(
            analysis, findings, public.get("evidence", []), evidence, case["source_files"]
        )
        if valid
        else None
    )
    route, reason, visited, candidate = routes(original, calls, slot["arm"])
    wall = (original or {}).get("wall_ms")
    queue = [c.get("queue_ms") for c in dispatched]
    pipeline = wall - sum(queue) if wall is not None and all(v is not None for v in queue) else None
    plans = (analysis.get("remediation") or {}).get("plans") or []
    nochange = (
        valid
        and analysis.get("analysis_status") == "no_failure_evidence"
        and (analysis.get("remediation") or {}).get("status") == "not_needed"
        and not plans
    )
    row = {
        **slot,
        "program": case["cluster_id"],
        "pair_id": case["pair_id"],
        "variant": case["variant"],
        "stratum": {
            "supported_copy_omission": "A",
            "optional_fallback": "B",
            "unsupported_alias": "C",
        }[case["variant"]],
        "assessment_variant": variant,
        "source_state": state["status"],
        "response_valid": valid,
        "error": (assessed.get("error") or {}).get("code"),
        "analysis_status": analysis.get("analysis_status"),
        "target_mentioned": target_mention,
        "source_location_match": a_hit,
        "target_and_source_match": target_mention and a_hit,
        "citations": citations,
        "normal_state_recognized": nochange,
        "unnecessary_change": bool(valid and plans),
        "route": route,
        "transition_reason": reason,
        "graph_visited": visited,
        "packaging_candidate": candidate,
        "graph_scores": (original or {}).get("graph_scores"),
        "graph_completed_before_drain": (original or {}).get("graph_completed_before_drain"),
        "calls": len(dispatched),
        "failed_calls": sum(c.get("status") == "failed" for c in dispatched),
        "wall_ms": wall,
        "pacing_ms": sum(queue) if all(v is not None for v in queue) else None,
        "pipeline_ms": pipeline,
        "normalization_status": assessment["normalization_status"],
        "normalization_reason": assessment["normalization_reason"],
        "original_result_sha256": assessment["original_result_sha256"],
        "last_call_sha256": assessment["last_call_sha256"],
        "source_result_path": str(folder / "result.json"),
        "call_files": [
            {"path": str(p), "sha256": sha(p)} for p in sorted((folder / "calls").glob("*.json"))
        ],
    }
    for key in ("input_tokens", "output_tokens", "thinking_tokens"):
        values = [(c.get("raw") or {}).get(key) for c in dispatched]
        row[key] = sum(values) if values and all(v is not None for v in values) else None
        row[key + "_known"] = (
            sum(v for v in values if v is not None) if any(v is not None for v in values) else None
        )
        row[key + "_missing_calls"] = sum(v is None for v in values)
    return row


def group(rows):
    repair = [r for r in rows if r["stratum"] in ("A", "C")]
    normal = [r for r in rows if r["stratum"] == "B"]
    return {
        "scheduled": len(rows),
        "completed": sum(r["source_state"] == "completed" for r in rows),
        "valid": sum(r["response_valid"] for r in rows),
        "repairable_scheduled": len(repair),
        "target_and_source_match": sum(r["target_and_source_match"] for r in repair),
        "no_change_scheduled": len(normal),
        "normal_state_recognized": sum(r["normal_state_recognized"] for r in normal),
        "unnecessary_change": sum(r["unnecessary_change"] for r in normal),
        "no_change_response_failure": sum(
            not r["response_valid"] and r["source_state"] == "completed" for r in normal
        ),
        "routes": dict(Counter(r["route"] for r in rows)),
        "errors": dict(Counter(r["error"] for r in rows if r["error"])),
        "transition_reasons": dict(
            Counter(r["transition_reason"] for r in rows if r["transition_reason"])
        ),
        "calls": sum(r["calls"] for r in rows),
        "failed_calls": sum(r["failed_calls"] for r in rows),
        "input_tokens": stats([r["input_tokens"] for r in rows]),
        "output_tokens": stats([r["output_tokens"] for r in rows]),
        "pipeline_ms": stats([r["pipeline_ms"] for r in rows]),
        "wall_ms": stats([r["wall_ms"] for r in rows]),
        "input_tokens_known": sum(r["input_tokens_known"] or 0 for r in rows)
        if any(r["input_tokens_known"] is not None for r in rows)
        else None,
        "output_tokens_known": sum(r["output_tokens_known"] or 0 for r in rows)
        if any(r["output_tokens_known"] is not None for r in rows)
        else None,
        "input_usage_missing_calls": sum(r["input_tokens_missing_calls"] for r in rows),
        "output_usage_missing_calls": sum(r["output_tokens_missing_calls"] for r in rows),
        "graph_visited": sum(r["graph_visited"] for r in rows),
        "packaging_candidates": sum(r["packaging_candidate"] for r in rows),
    }


def csv_write(path, rows):
    if not rows:
        raise ValueError("Empty report")
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("x", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, keys)
        w.writeheader()
        for row in rows:
            w.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for k, v in row.items()
                }
            )


def tex(rows, columns):
    def esc(x):
        return (
            str(x)
            .replace("\\", r"\textbackslash{}")
            .replace("_", r"\_")
            .replace("%", r"\%")
            .replace("&", r"\&")
        )

    return (
        "\\begin{tabular}{"
        + "l" * len(columns)
        + "}\n"
        + " & ".join(esc(c) for c in columns)
        + " \\\\\n\\hline\n"
        + "\n".join(" & ".join(esc(r.get(c, "")) for c in columns) + r" \\" for r in rows)
        + "\n\\end{tabular}\n"
    )


def run(source, normalized, rem_original, rem_normalized, output, root):
    fresh(output)
    separate(output, source)
    with readonly_run_lock(source) as lock:
        frozen = load_source(source, root)
        before = hashes(source, lock)
        cases = {c["id"]: c for c in frozen["cases"]}
        refs = frozen["references"]["cases"]
        remediation = {
            "original": read(rem_original / "summary.json"),
            "normalized": read(rem_normalized / "summary.json"),
        }
        for v, m in remediation.items():
            if (
                m["study_sha256"] != digest(frozen)
                or m["assessment_variant"] != v
                or len(m["rows"]) != 54
            ):
                raise ValueError("Remediation identity mismatch")
        rows = []
        for slot in frozen["schedule"]:
            assessed = assess_cell(source, slot)
            if assessed != read(normalized / "cells" / (slot["cell_id"] + ".json")):
                raise ValueError("Normalized evidence changed")
            for variant in ("original", "normalized"):
                row = diagnosis_row(
                    source, slot, cases[slot["case_id"]], refs[slot["case_id"]], assessed, variant
                )
                matches = [
                    r for r in remediation[variant]["rows"] if r["cell_id"] == slot["cell_id"]
                ]
                if (
                    len(matches) != 1
                    or matches[0]["original_result_sha256"] != assessed["original_result_sha256"]
                ):
                    raise ValueError("Repair row mismatch")
                row["remediation"] = matches[0]
                rows.append(row)
        groups = []
        for variant in ("original", "normalized"):
            for program in sorted({c["cluster_id"] for c in cases.values()}):
                for arm in frozen["config"]["arms"]:
                    selected = [
                        r
                        for r in rows
                        if (r["assessment_variant"], r["program"], r["arm"])
                        == (variant, program, arm)
                    ]
                    groups.append(
                        {
                            "assessment_variant": variant,
                            "program": program,
                            "arm": arm,
                            **group(selected),
                        }
                    )
        summary = {
            "version": "cross-program-report.v1",
            "created_from": "saved real responses and Docker results",
            "mode": "development",
            "human_reference_approvals": 0,
            "study_sha256": digest(frozen),
            "freeze_sha256": sha(root / "freeze-manifest.json"),
            "independent_programs": 3,
            "base_required_copy_incidents": 3,
            "scenarios": 9,
            "scheduled_cells": 54,
            "diagnosis_by_program": groups,
            "overall": {
                v: {
                    a: group([r for r in rows if r["assessment_variant"] == v and r["arm"] == a])
                    for a in frozen["config"]["arms"]
                }
                for v in ("original", "normalized")
            },
            "remediation": {
                v: {k: m[k] for k in ("status", "by_arm", "agent_replays")}
                for v, m in remediation.items()
            },
            "claims": [
                "Same Node ENOENT/COPY family across three newly authored programs",
                "No independent human semantic scores",
                "Three repeats of same incidents are not independent bugs",
                "No superiority/noninferiority significance tests",
                "Input subset differences and adapter assistance retained",
                "Normalization uses same responses and routes; no changed-system latency estimate",
            ],
            "graph_metric_scope": "A all-scheduled/visited counts and fact/candidate atoms; B/C candidate N/A, semantic fact omissions retained; reference relations not scored",
            "timing": "wall includes pacing; pipeline = wall minus recorded queue wait, not a new runtime measurement after normalization",
            "new_analysis_model_calls": 0,
            "source_unchanged": before == hashes(source, lock),
        }
        output.mkdir(parents=True)
        write(output / "summary.json", summary)
        write(output / "cells.json", rows)
        csv_write(output / "cells.csv", rows)
        csv_write(output / "program-summary.csv", groups)
        repair_table = []
        for variant in ("original", "normalized"):
            for program in sorted({c["cluster_id"] for c in cases.values()}):
                for stratum in ("A", "C"):
                    for arm in frozen["config"]["arms"]:
                        selected = [
                            r
                            for r in remediation[variant]["rows"]
                            if (r["program"], r["stratum"], r["arm"]) == (program, stratum, arm)
                        ]
                        repair_table.append(
                            {
                                "version": variant,
                                "program": program,
                                "stratum": stratum,
                                "arm": arm,
                                "scheduled": len(selected),
                                "applied": sum(r["application_pass"] is True for r in selected),
                                "functional": sum(r["functional_pass"] is True for r in selected),
                                "regression": sum(r["regression_pass"] is True for r in selected),
                                "preserved": sum(
                                    r["data_preservation_pass"] is True for r in selected
                                ),
                                "restored": sum(
                                    r["complete_restoration"] is True for r in selected
                                ),
                                "unobserved": sum(not r["evaluation_complete"] for r in selected),
                            }
                        )
        csv_write(output / "remediation-summary.csv", repair_table)
        (output / "diagnosis-table.tex").write_text(
            tex(
                groups,
                [
                    "assessment_variant",
                    "program",
                    "arm",
                    "valid",
                    "scheduled",
                    "target_and_source_match",
                    "normal_state_recognized",
                    "calls",
                ],
            ),
            encoding="utf-8",
        )
        (output / "remediation-table.tex").write_text(
            tex(
                repair_table,
                [
                    "version",
                    "program",
                    "stratum",
                    "arm",
                    "scheduled",
                    "applied",
                    "functional",
                    "regression",
                    "preserved",
                    "restored",
                    "unobserved",
                ],
            ),
            encoding="utf-8",
        )
        lines = [
            "# 독립 프로그램 추가 실험 결과",
            "",
            "저자 제작 Node.js 프로그램 3개, 상황 9개, 조건별 3회 반복으로 예정 평가 단위는 54개다. 실제 사람 참조 승인이 없는 development 평가다. 원본이 주 분석이고 제한적 정규화는 실행 전 고정한 보조 분석이다.",
            "",
            "|응답|프로그램|방식|유효 응답|대상·위치 일치|정상 인식|불필요 수정|호출|입력 토큰(관측 합)|출력 토큰(관측 합)|평균 pipeline ms|",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for g in groups:
            lines.append(
                f"|{g['assessment_variant']}|{g['program']}|{g['arm']}|{g['valid']}/{g['scheduled']}|{g['target_and_source_match']}/{g['repairable_scheduled']}|{g['normal_state_recognized']}/{g['no_change_scheduled']}|{g['unnecessary_change']}/{g['no_change_scheduled']}|{g['calls']}|{g['input_tokens_known']}|{g['output_tokens_known']}|{g['pipeline_ms']['mean']}|"
            )
        lines += [
            "",
            "대상·위치 일치는 응답의 대상 경로 언급과 소스 인용 위치의 구조적 일치이며, 진단 의미 정확성 점수가 아니다. 토큰 누락 호출 수와 관측 수는 JSON/CSV에 함께 저장한다. 정규화 행의 비용은 동일한 기존 실행 비용이며 합산하지 않는다.",
            "",
            "|응답|프로그램|상황|방식|예정|적용|기능|회귀|보존|복구|미평가|",
            "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for r in repair_table:
            lines.append(
                "|"
                + "|".join(
                    str(r[k])
                    for k in (
                        "version",
                        "program",
                        "stratum",
                        "arm",
                        "scheduled",
                        "applied",
                        "functional",
                        "regression",
                        "preserved",
                        "restored",
                        "unobserved",
                    )
                )
                + "|"
            )
        lines += ["", "## 실패 및 전환", ""]
        for g in groups:
            if g["assessment_variant"] == "original":
                lines.append(
                    f"- {g['program']} / {g['arm']}: 경로 {json.dumps(g['routes'], ensure_ascii=False)}, 사유 {json.dumps(g['transition_reasons'], ensure_ascii=False)}, 응답 오류 {json.dumps(g['errors'], ensure_ascii=False)}"
                )
        for v, m in remediation.items():
            for arm, values in m["by_arm"].items():
                lines.append(
                    f"- {v} / {arm} 복구 평가 판정: {json.dumps(values['outcomes'], ensure_ascii=False)}"
                )
        lines += [
            "",
            "## 해석 범위",
            "",
            "실제 복구 성공 수는 지정된 COPY 어댑터와 고정 컨테이너에서 기능·회귀·보존 조건을 모두 통과한 수다. 미평가가 있는 조건의 전체 복구율은 확정하지 않는다. 양쪽이 성공한 사례는 실행 가능성의 근거이며 그래프 방식의 우월성을 뜻하지 않는다.",
            "",
            "새 프로그램은 외부 공개 프로젝트의 자연 발생 장애가 아니다. 기본 장애 3개의 반복과 구문 변형을 독립 장애 54개로 해석하지 않는다. 그래프 방식의 일반 분석 전환도 원래 조건에 포함한다. 자동 근거 검사는 의미적 충분성이나 사람 채점을 대체하지 않는다.",
            "",
            "원시 응답·호출·실제 경로·패치·검사 결과와 해시는 cells.json/csv 및 각 원본 결과 폴더에 연결돼 있다. 자료 준비와 검사 이력은 deviations.md, 검증된 실행 명령은 reproduction.md를 참조한다.",
        ]
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (output / "deviations.md").write_text(
            (ROOT / "docs/PAPER_STUDY_V4_CROSS_PROGRAM_IMPLEMENTATION.md").read_text("utf-8"),
            encoding="utf-8",
        )
        (output / "reproduction.md").write_text(
            (ROOT / "docs/PAPER_STUDY_V4_CROSS_PROGRAM_COMMANDS.md").read_text("utf-8"),
            encoding="utf-8",
        )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for k in (
        "source-run",
        "normalized-run",
        "remediation-original",
        "remediation-normalized",
        "output",
    ):
        p.add_argument("--" + k, type=Path, required=True)
    p.add_argument("--root", type=Path, default=ROOT / "evaluation/cross-program-v4")
    a = p.parse_args()
    run(
        a.source_run.resolve(),
        a.normalized_run.resolve(),
        a.remediation_original.resolve(),
        a.remediation_normalized.resolve(),
        a.output.resolve(),
        a.root.resolve(),
    )


if __name__ == "__main__":
    main()
