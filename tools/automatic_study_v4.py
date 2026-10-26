"""Deterministic metrics from saved v4 experiments; no human or LLM scoring.

Reads frozen reference values, final response fields and recorded graph/call
traces. It never infers natural-language correctness from formatting or keywords.
"""

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path

from revalidate_study_v4 import ROOT, digest, hashes, read, write

from ai_error_check_agent.experiments.study_v4_graph import score as graph_score

DIMENSIONS = ("facts", "relations", "candidates")
PROTOCOL = {
    "version": "study-v4-automatic-metrics.v1",
    "reference": "Frozen authored fixtures; provisional references, not independently approved gold.",
    "status_match": "Valid final analysis_status belongs to reference.acceptable_statuses. This is label agreement, not diagnosis correctness.",
    "anchor_hit": "For references accepting only diagnosed and having one SourceRead path/line, at least one finding spans the expected line AND cites that exact source line. Whole-response hit; not top-1, root-cause, or repair accuracy.",
    "citation_integrity": "Unique cited EV IDs resolve to supplied evidence; cited SC IDs and finding spans agree with supplied source lines. Does not measure whether prose follows from evidence.",
    "graph": "Micro TP/FP/FN over frozen canonical atoms. Report both all scheduled graph-arm cells and source-stage visited cells. Plain Python and no-graph arms are N/A.",
    "f1": "2*TP/(2*TP+FP+FN); zero denominator -> null. Precision/recall also null when their denominator is zero.",
    "failure": "Contract failure contributes 0 to end-to-end label/anchor checks and is excluded only from explicitly conditional denominators.",
    "efficiency": "Actual original live call tokens and wall/queue measurements only. Normalized results have no new measured latency or token cost.",
    "fault_audit": "Separate offline component experiment. Report SHACL rejection and candidate emission separately, including clean-input candidate availability.",
    "not_measured": [
        "natural-language diagnosis correctness",
        "claim entailment",
        "uncertainty adequacy",
        "repair applicability",
        "executed repair success",
        "human usable_final",
    ],
}


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def assessment_inputs(folder):
    result = {"cells/" + k: v for k, v in hashes(folder / "cells").items()}
    for name in ("revalidation.json", "original-files.sha256.json"):
        result[name] = hashlib.sha256((folder / name).read_bytes()).hexdigest()
    return result


def stats(values):
    known = sorted(x for x in values if x is not None)
    return {
        "observed": len(known),
        "total": len(values),
        "sum": sum(known) if len(known) == len(values) and known else None,
        "mean": statistics.mean(known) if known else None,
        "median": statistics.median(known) if known else None,
        "p95_nearest_rank": known[math.ceil(len(known) * 0.95) - 1] if known else None,
    }


def micro(scores):
    known = [s for s in scores if s is not None]
    if not known:
        return None
    tp, fp, fn = (sum(s[k] for s in known) for k in ("tp", "fp", "fn"))
    return {
        "scored_cells": len(known),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
    }


def anchor(reference):
    if reference.get("acceptable_statuses") != ["diagnosed"]:
        return None
    facts = (reference.get("graph") or {}).get("facts", [])
    paths = {a[2] for a in facts if a[:2] == ["SourceRead", "path"]}
    lines = {a[2] for a in facts if a[:2] == ["SourceRead", "line"]}
    if len(paths) != 1 or len(lines) != 1:
        return None
    return {"path": next(iter(paths)), "line": int(next(iter(lines)))}


def anchor_hit(expected, findings, source_evidence):
    if expected is None:
        return None
    by_id = {s["id"]: s for s in source_evidence}
    for finding in findings:
        if finding["path"] != expected["path"] or not (
            finding["start_line"] <= expected["line"] <= finding["end_line"]
        ):
            continue
        if any(
            by_id.get(s, {}).get("path") == expected["path"]
            and by_id[s]["line"] == expected["line"]
            for s in finding["source_evidence_ids"]
        ):
            return True
    return False


def citation_integrity(analysis, findings, evidence, source_evidence, source_files):
    cited = set()

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"evidence_ids", "counter_evidence_ids"}:
                    cited.update(child)
                else:
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(analysis)
    walk(findings)
    log_ids = {e["id"] for e in evidence}
    sources = {s["id"]: s for s in source_evidence}
    file_lines = {s["path"]: s["content"].splitlines() for s in source_files}
    bindings = {
        (f["path"], f["start_line"], f["end_line"], s)
        for f in findings
        for s in f["source_evidence_ids"]
    }
    valid = 0
    for path, start, end, sid in bindings:
        s = sources.get(sid)
        lines = file_lines.get(path, [])
        if (
            s
            and s["path"] == path
            and start <= s["line"] <= end
            and (1 <= s["line"] <= len(lines) and s["text"] == lines[s["line"] - 1])
        ):
            valid += 1
    return {
        "log_ids": ratio(len(cited & log_ids), len(cited)),
        "source_bindings": ratio(valid, len(bindings)),
    }


def final_checks(public, findings, source_evidence, case, reference):
    valid = public["job_status"] == "succeeded" and public.get("analysis") is not None
    analysis = public.get("analysis") if valid else None
    expected = anchor(reference)
    return {
        "valid_contract": valid,
        "expected_statuses": reference["acceptable_statuses"],
        "observed_status": analysis["analysis_status"] if valid else None,
        "status_match": bool(
            valid and analysis["analysis_status"] in reference["acceptable_statuses"]
        ),
        "expected_anchor": expected,
        "anchor_hit": anchor_hit(expected, findings, source_evidence)
        if valid
        else (False if expected else None),
        "citations": citation_integrity(
            analysis, findings, public["evidence"], source_evidence, case["source_files"]
        )
        if valid
        else None,
    }


def summarize_final(rows, key):
    available = [r[key] for r in rows if r[key] is not None]
    valid = [r for r in available if r["valid_contract"]]
    eligible = [r for r in available if r["expected_anchor"] is not None]
    valid_eligible = [r for r in eligible if r["valid_contract"]]
    return {
        "valid_contracts": ratio(len(valid), len(available)),
        "status_match_all": ratio(sum(r["status_match"] for r in available), len(available)),
        "status_match_given_valid": ratio(sum(r["status_match"] for r in valid), len(valid)),
        "anchor_hit_all_eligible": ratio(sum(r["anchor_hit"] for r in eligible), len(eligible)),
        "anchor_hit_given_valid": ratio(
            sum(r["anchor_hit"] for r in valid_eligible), len(valid_eligible)
        ),
        "citations": {
            k: ratio(
                sum(r["citations"][k]["numerator"] for r in valid),
                sum(r["citations"][k]["denominator"] for r in valid),
            )
            for k in ("log_ids", "source_bindings")
        },
    }


def summarize(rows):
    graph_rows = [r for r in rows if r["graph_scores"] is not None]
    visited = [r for r in graph_rows if r["graph_status"] != "not_visited"]
    return {
        "cells": len(rows),
        "original": summarize_final(rows, "original"),
        "normalized": summarize_final(rows, "normalized")
        if any(r["normalized"] for r in rows)
        else None,
        "graph": {
            "applicable_cells": len(graph_rows),
            "source_stage_visited": len(visited),
            "statuses": dict(Counter(r["graph_status"] for r in graph_rows)),
            "all_cells": {k: micro([r["graph_scores"][k] for r in graph_rows]) for k in DIMENSIONS},
            "visited_cells": {
                k: micro([r["graph_scores"][k] for r in visited]) for k in DIMENSIONS
            },
        },
        "efficiency_original_run": {
            k: stats([r["efficiency"][k] for r in rows])
            for k in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "generation_calls",
                "wall_ms",
                "queue_ms",
                "pipeline_ms_excluding_pacing",
            )
        },
    }


def fault_summary(audit):
    result = {}
    for arm in sorted({r["arm"] for r in audit["faults"]}):
        rows = [r for r in audit["faults"] if r["arm"] == arm]
        clean = [r for r in audit["records"] if r["arm"] == arm]
        details = [
            {
                "fault": r["fault"],
                "candidate_emitted": r["candidate_generated"],
                "shacl_rejected": any(
                    e["stage"] == "shacl" and e.get("conforms") is False
                    for e in r["trace"]["events"]
                ),
                "graph_status": (r["report"] or {}).get("status"),
                "api_valid": r["api_valid"],
            }
            for r in rows
        ]
        enabled_checks = [
            r
            for r in clean
            if any(e["stage"] == "shacl" and e["enabled"] for e in r["trace"]["events"])
        ]
        result[arm] = {
            "faults": details,
            "fault_candidate_emission": ratio(
                sum(r["candidate_emitted"] for r in details), len(rows)
            ),
            "fault_shacl_rejection": ratio(sum(r["shacl_rejected"] for r in details), len(rows)),
            "clean_candidate_emission": ratio(
                sum(bool(r["trace"]["atoms"]["candidates"]) for r in clean), len(clean)
            ),
            "clean_shacl_rejection": ratio(
                sum(
                    any(
                        e["stage"] == "shacl" and e.get("conforms") is False
                        for e in r["trace"]["events"]
                    )
                    for r in enabled_checks
                ),
                len(enabled_checks),
            ),
        }
    return {
        "real_model": False,
        "by_arm": result,
        "procedural_equivalence_pass": audit["procedural_equivalence_pass"],
        "plain_python_candidate_equivalence_pass": audit["plain_python_candidate_equivalence_pass"],
        "worker_lifecycle_pass": audit["worker_lifecycle_pass"],
        "audit_timeout_seconds": audit["audit_graph_timeout_seconds"],
        "live_timeout_seconds": audit["production_graph_timeout_seconds"],
        "note": "Rule-disabled arms emitting no candidates is not evidence of useful detection. Interpret with clean candidate availability; cross-scope/target-mismatch can be blocked by rule joins rather than SHACL.",
    }


def fmt(value):
    if value is None:
        return "N/A"
    if isinstance(value, dict) and "rate" in value:
        return f"{value['numerator']}/{value['denominator']}" + (
            f" ({100 * value['rate']:.1f}%)" if value["rate"] is not None else " (N/A)"
        )
    return f"{value:.3f}"


def markdown(document):
    total = document["overall"]
    lines = [
        "# v4 자동 정량 평가",
        "",
        "사람·LLM 채점 없이 저장된 구조화 결과와 고정 참조를 비교했다. 새 실험 API 호출은 0회다.",
        f"전체 {document['cells']}셀, 작성 사례 {document['cases']}개, 프로젝트 계보 {document['lineage_clusters']}개.",
        "",
        "## 최종 응답의 자동 검사",
        "",
        "상태 일치는 diagnosed/no_failure_evidence 등의 라벨 일치다. 자연어 진단 정답률이 아니다. 코드 위치는 참조 읽기 행을 인용했는지 검사한다.",
        "",
        "| 지표 | 원본 | 정규화 후 |",
        "|---|---:|---:|",
    ]
    for key, title in (
        ("valid_contracts", "최종 응답 검증 통과"),
        ("status_match_all", "상태 라벨 일치 / 전체"),
        ("status_match_given_valid", "상태 라벨 일치 / 유효 응답"),
        ("anchor_hit_all_eligible", "코드 위치 적중 / 대상 셀 전체"),
        ("anchor_hit_given_valid", "코드 위치 적중 / 대상 유효 응답"),
    ):
        lines.append(
            f"| {title} | {fmt(total['original'][key])} | {fmt(total['normalized'][key]) if total['normalized'] else 'N/A'} |"
        )
    for key, title in (
        ("log_ids", "인용 EV ID 존재"),
        ("source_bindings", "인용 SC ID·코드 위치·원문 일치"),
    ):
        lines.append(
            f"| {title} / 유효 응답의 인용 | {fmt(total['original']['citations'][key])} | {fmt(total['normalized']['citations'][key]) if total['normalized'] else 'N/A'} |"
        )
    lines += [
        "",
        "검증 실패는 전체 분모에 남긴다. 인용 검사는 ID·위치 연결만 확인하며 설명이 근거로부터 도출되는지는 평가하지 않는다. 코드 위치 평가는 diagnosed만 허용하고 단일 SourceRead 기준 행이 있는 사례에만 적용한다.",
        "",
        "## 조건별 그래프·효율",
        "",
        "그래프 F1은 모든 그래프 대상 셀의 TP/FP/FN을 합친 micro F1이다. 미방문·미지원 셀의 누락도 포함한다. 소스 단계 방문 셀만의 별도 수치와 TP/FP/FN은 report.json에 있다.",
        "",
        "| 조건 | 원본 유효 | 정규화 유효 | 사실 F1 | 관계 F1 | 후보 F1 | 평균 입력 토큰 | 평균 출력 토큰 | 평균 처리 ms* |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm, s in document["by_arm"].items():
        g = s["graph"]["all_cells"]
        e = s["efficiency_original_run"]
        cells = [
            arm,
            fmt(s["original"]["valid_contracts"]),
            fmt(s["normalized"]["valid_contracts"]) if s["normalized"] else "N/A",
        ]
        cells += [fmt(g[k]["f1"]) if g[k] else "N/A" for k in DIMENSIONS]
        cells += [
            fmt(e[k]["mean"])
            for k in ("input_tokens", "output_tokens", "pipeline_ms_excluding_pacing")
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "* 원래 live 실행의 전체 시간에서 기록된 API 페이싱 대기 시간을 뺀 값이다. 정규화 시스템을 다시 실행한 측정값이 아니다. N/A는 분모·계측이 없거나 그래프 미사용 조건이다.",
    ]
    if document["audit"]:
        lines += [
            "",
            "## 별도 오프라인 오류 주입",
            "",
            "| 조건 | 손상 입력에서 후보 생성 | SHACL 거절 / 손상 입력 | 정상 사례 후보 생성 | SHACL 거절 / 검사한 정상 사례 |",
            "|---|---:|---:|---:|---:|",
        ]
        for arm, s in document["audit"]["by_arm"].items():
            lines.append(
                "| "
                + " | ".join(
                    [arm]
                    + [
                        fmt(s[k])
                        for k in (
                            "fault_candidate_emission",
                            "fault_shacl_rejection",
                            "clean_candidate_emission",
                            "clean_shacl_rejection",
                        )
                    ]
                )
                + " |"
            )
        lines += [
            "",
            "4가지 손상은 dangling_evidence, invalid_line, cross_scope, target_mismatch다. 구조 위반의 SHACL 거절과 규칙 조인 불일치로 후보가 없는 경우를 구분한다. 규칙을 끄면 정상 후보도 생성하지 못하므로 손상 후보 0건만으로 우수하다고 판단하지 않는다.",
            f"SPARQL/Python 조인 동등성: {document['audit']['procedural_equivalence_pass']}; RDF 없는 후보 생성과의 동등성: {document['audit']['plain_python_candidate_equivalence_pass']}; 작업자 수명주기 검사: {document['audit']['worker_lifecycle_pass']}.",
        ]
    lines += [
        "",
        "## 해석 범위",
        "",
        "이 결과는 사람이 점수를 주지 않는 자동 평가다. 참조 사례·기대값은 미리 정의되어 있어야 하며 현재 참조는 작성자 잠정 명세다. 정규화와 이번 지표 구성은 응답을 본 뒤 추가한 사후 분석이다.",
        "자연어 설명의 사실성·과도한 확신·수정안의 적합성과 실제 복구 성공은 측정하지 않았다. 기존 usable_final이나 LLM 채점 수치를 읽거나 대체하지 않는다. 이를 평가하려면 별도 검토 또는 정답이 고정된 수정·기능·롤백 실행 테스트가 필요하다.",
        "direct-literal 개발 fixture에는 함수명·export와 로그의 실행 일관성 문제가 알려져 있다. 그 사례의 코드 위치 일치는 실제 실행 성공의 증거가 아니다. 110셀은 서로 독립된 110개 프로젝트가 아니라 같은 계보의 사례와 비교 조건이다.",
        "",
        "## 재현 자료",
        "",
        "report.json에 셀별 결과, 모든 분모, 그래프 TP/FP/FN, 원본 호출 통계, 참조 및 입력 파일 해시와 지표 정의를 저장했다. 원본 실험 폴더와 정규화 결과는 변경하지 않았다.",
        "",
    ]
    return "\n".join(lines)


def run(source, output, assessment=None, audit_path=None):
    source, output = source.resolve(), output.resolve()
    protected = [source] + ([assessment.resolve()] if assessment else [])
    for folder in protected:
        if output == folder or folder in output.parents or output in folder.parents:
            raise ValueError("Output must be separate from input directories")
    before = hashes(source)
    frozen = read(source / "frozen.json")
    for relative, expected in frozen["source_manifest"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Frozen source changed: {relative}")
    study_hash = digest(frozen)
    normalized = {}
    assessment_hashes = assessment_inputs(assessment) if assessment else None
    if assessment:
        metadata = read(assessment / "revalidation.json")
        if (
            metadata["study_sha256"] != study_hash
            or read(assessment / "original-files.sha256.json") != before
        ):
            raise ValueError("Normalized assessment does not match original run")
        normalized = {p.stem: read(p) for p in (assessment / "cells").glob("*.json")}
        if set(normalized) != {s["cell_id"] for s in frozen["schedule"]}:
            raise ValueError("Normalized assessment has missing or extra cells")
    cases = {c["id"]: c for c in frozen["cases"]}
    rows = []
    for slot in frozen["schedule"]:
        folder = source / "cells" / slot["cell_id"]
        state = read(folder / "state.json")
        if state["status"] != "completed":
            raise ValueError(
                "This report requires a complete saved run; unfinished cells must not disappear from denominators"
            )
        cell = read(folder / "result.json")
        if state["result_sha256"] != digest(cell):
            raise ValueError("Original result hash mismatch")
        public = cell["result"]
        source_info = public.get("source_analysis") or {}
        sources = source_info.get("evidence", [])
        case = cases[slot["case_id"]]
        reference = frozen["references"]["cases"][slot["case_id"]]
        raw_checks = final_checks(public, source_info.get("findings", []), sources, case, reference)
        norm_checks = None
        if normalized:
            n = normalized[slot["cell_id"]]
            if n["original_result_sha256"] != digest(cell):
                raise ValueError("Normalized result hash mismatch")
            v = n["normalized"]
            norm_checks = final_checks(
                {
                    "job_status": "succeeded" if v["valid"] else "failed",
                    "analysis": v["analysis"],
                    "evidence": public["evidence"],
                },
                v["source_findings"],
                sources,
                case,
                reference,
            )
        g = None
        if cell["graph_scores_scope"] == "pipeline_extraction":
            g = graph_score(cell["graph_trace"]["atoms"], reference.get("graph"))
            if digest(g) != digest(cell["graph_scores"]):
                raise ValueError("Recomputed graph atoms differ from saved graph scores")
        calls = [read(p) for p in sorted((folder / "calls").glob("*.json"))]

        def tokens(key, calls=calls):
            values = [(c.get("raw") or {}).get(key) for c in calls]
            return (
                sum(values)
                if frozen["live"] and values and all(type(v) is int for v in values)
                else None
            )

        queue_values = [c.get("queue_ms") for c in calls]
        queue = (
            sum(queue_values)
            if frozen["live"]
            and queue_values
            and all(isinstance(q, (int, float)) for q in queue_values)
            else None
        )
        wall = cell["wall_ms"] if frozen["live"] else None
        rows.append(
            {
                **slot,
                "cluster_id": case["cluster_id"],
                "original": raw_checks,
                "normalized": norm_checks,
                "graph_scores": g,
                "graph_status": (cell["graph_report"] or {}).get("status", "not_visited"),
                "efficiency": {
                    k: tokens(k) for k in ("input_tokens", "output_tokens", "total_tokens")
                }
                | {
                    "generation_calls": sum(bool(c.get("dispatched")) for c in calls)
                    if frozen["live"]
                    else None,
                    "wall_ms": wall,
                    "queue_ms": queue,
                    "pipeline_ms_excluding_pacing": max(0, wall - queue)
                    if wall is not None and queue is not None
                    else None,
                },
            }
        )
    audit = read(audit_path) if audit_path else None
    document = {
        "version": PROTOCOL["version"],
        "protocol": PROTOCOL,
        "study_sha256": study_hash,
        "inputs": {
            "original_directory": str(source),
            "original_files_sha256": before,
            "assessment_files_sha256": assessment_hashes,
            "audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest()
            if audit_path
            else None,
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "real_model_run": frozen["live"],
        "new_study_api_calls": 0,
        "human_or_llm_ratings_used": 0,
        "cells": len(rows),
        "cases": len(cases),
        "lineage_clusters": len({c["cluster_id"] for c in cases.values()}),
        "overall": summarize(rows),
        "by_arm": {
            a: summarize([r for r in rows if r["arm"] == a]) for a in frozen["config"]["arms"]
        },
        "audit": fault_summary(audit) if audit else None,
        "rows": rows,
    }
    if hashes(source) != before or (
        assessment and assessment_inputs(assessment) != assessment_hashes
    ):
        raise ValueError("Input files changed during evaluation")
    rendered = markdown(document)
    if output.exists():
        if (
            digest(read(output / "report.json")) != digest(document)
            or (output / "report.md").read_text("utf-8") != rendered
        ):
            raise ValueError("Existing output differs; choose a new output directory")
    else:
        write(output / "report.json", document)
        (output / "report.md").write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "report": str(output / "report.md"),
                "cells": len(rows),
                "original": document["overall"]["original"],
                "normalized": document["overall"]["normalized"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return document


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=ROOT / "results/research-study-v4-development"
    )
    parser.add_argument("--assessment", type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/research-study-v4-auto-evaluation-v2"
    )
    args = parser.parse_args()
    run(args.source, args.output, args.assessment, args.audit)
