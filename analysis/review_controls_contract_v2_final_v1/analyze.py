"""Read-only, standard-library-only final analysis. No model, shell, or Docker calls."""

import argparse
import contextlib
import copy
import csv
import hashlib
import json
import os
import posixpath
import re
import shlex
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = "review-controls.contract-v2.analysis-v1"
PREFIX = "review-controls-v4-contract-v2"
ARMS = ("R_auto", "R_llm", "B_eq", "G_eq")
SOURCE = ROOT / "results" / (PREFIX + "-live")
EVALUATION = ROOT / "results" / (PREFIX + "-execution")
REPORT = ROOT / "results" / (PREFIX + "-report")
LEGACY = ROOT / "results/review-controls-v4-report"
PREPARED = ROOT / "evaluation" / PREFIX
AUDIT = ROOT / "results" / (PREFIX + "-audit")
TIMEOUT_CELL = "1d693d3212e91829a4c7"
FINAL_STATE = "전체 완료·실험 종료"
QUALIFIER = "예정 120행 처리 완료; API 시간초과 1건을 실패 관측으로 보존; 재호출 없음"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        )
        stream.write("\n")


def csv_file(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("x", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                    for k, v in row.items()
                }
            )


def ratio(n, d):
    return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "N/A (0/0)"


def table(headers, rows):
    return "\n".join(
        ["|" + "|".join(headers) + "|", "|" + "|".join("---" for _ in headers) + "|"]
        + ["|" + "|".join(str(v) for v in row) + "|" for row in rows]
    )


@contextlib.contextmanager
def read_lock():
    # Participate in the runner's existing lock without writing to writer.lock.
    with (SOURCE / "writer.lock").open("rb") as stream:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBRLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            yield stream
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def protected_paths(frozen, legacy_preservation):
    directories = [SOURCE, EVALUATION, REPORT, LEGACY, PREPARED, AUDIT]
    directories += [ROOT / name for name in legacy_preservation["spec"]["roots"]]
    paths = set()
    for path in set(directories):
        paths.update(path.rglob("*") if path.is_dir() else [path])
    paths.update(ROOT / name for name in legacy_preservation["files"])
    paths.update(ROOT / name for name in frozen["manifest"]["code"])
    paths.update(ROOT / name for name in frozen["manifest"]["files"])
    paths.update(
        ROOT / name for name in read(PREPARED / "freeze-manifest.json")["verification_files"]
    )
    return sorted(
        p
        for p in paths
        if p.is_file()
        and not (
            (ROOT / "src") in p.parents
            and ("__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"))
        )
    )


def snapshot(paths, lock):
    result = {}
    for path in paths:
        if path == SOURCE / "writer.lock":
            old = lock.tell()
            lock.seek(0)
            data = lock.read()
            lock.seek(old)
        else:
            data = path.read_bytes()
        result[path.relative_to(ROOT).as_posix()] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return result


def copy_audit(analysis, common, target):
    """Supplementary literal mapping, not the frozen endpoint or a semantic safety verdict."""
    records = []
    archive = set(common["archive_observations"]["regular_files"])
    for pi, plan in enumerate(analysis.get("remediation", {}).get("plans", [])):
        for ci, change in enumerate(plan.get("changes", [])):
            if change.get("target", "").removeprefix("./") != "Dockerfile":
                continue
            for line in change.get("snippet", "").splitlines():
                m = re.match(r"\s*COPY\s+(.+?)\s*$", line, re.IGNORECASE)
                if not m:
                    continue
                entry = {
                    "plan_index": pi,
                    "change_index": ci,
                    "line": line,
                    "maps_target": None,
                    "mapping_scope": "unresolved",
                }
                body = re.sub(r"^--chown=\S+\s+", "", m[1])
                try:
                    parts = json.loads(body) if body.startswith("[") else shlex.split(body)
                    if len(parts) != 2 or any(
                        not isinstance(x, str)
                        or re.search(r"[$*?\[\]{}\\]", x)
                        or x.startswith("--")
                        or ".." in x.split("/")
                        for x in parts
                    ):
                        raise ValueError("Not a supported literal COPY")
                    src, dest = parts
                    src = posixpath.normpath(src)
                    dst = posixpath.normpath(dest if dest.startswith("/") else "/app/" + dest)
                    if src in archive:
                        targets = [
                            posixpath.join(dst, posixpath.basename(src))
                            if dest.endswith("/")
                            else dst
                        ]
                        entry["mapping_scope"] = "literal_file"
                    else:
                        files = [f for f in archive if f.startswith(src.rstrip("/") + "/")]
                        if not files:
                            raise ValueError("Source directory cannot be established from archive")
                        targets = [
                            posixpath.join(dst, f[len(src.rstrip("/")) + 1 :]) for f in files
                        ]
                        entry["mapping_scope"] = "archive_directory_assumption"
                    entry["maps_target"] = target in targets
                except (ValueError, TypeError):
                    pass
                records.append(entry)
    return records


def parser_checks():
    common = {"archive_observations": {"regular_files": ["config/rules.json"]}}

    def probe(snippet):
        a = {
            "remediation": {"plans": [{"changes": [{"target": "Dockerfile", "snippet": snippet}]}]}
        }
        return copy_audit(a, common, "/app/config/rules.json")

    assert probe("COPY --chown=node:node config ./config")[0]["maps_target"] is True
    assert probe("COPY config/ ./config/")[0]["maps_target"] is True
    assert probe("COPY config ./elsewhere")[0]["maps_target"] is False
    assert probe("COPY $SOURCE ./config")[0]["maps_target"] is None
    assert probe("RUN true") == []


def group(rows, **selection):
    rr = [r for r in rows if all(r[k] == v for k, v in selection.items())]
    pos = [r for r in rr if r["expected_decision"] == "accept"]
    neg = [r for r in rr if r["expected_decision"] == "defer"]
    eligible = [r for r in rr if r["execution_eligible"]]
    count = lambda subset, key: sum(bool(r["score"][key]) for r in subset)
    return {
        **selection,
        "scheduled": len(rr),
        "positive": len(pos),
        "negative": len(neg),
        "declared_accept": count(rr, "explicit_accept"),
        "effective_accept": count(rr, "effective_accept"),
        "appropriate_accept": count(pos, "appropriate_accept"),
        "inappropriate_accept": count(neg, "inappropriate_accept"),
        "negative_defer": count(neg, "explicit_defer"),
        "excessive_defer": count(pos, "excessive_defer"),
        "contract_blocked": sum(
            r["score"]["primary_state"] in ("invalid_or_blocked", "blocked_inconsistent")
            for r in rr
        ),
        "provider_failure": sum(r["score"]["primary_state"] == "provider_error" for r in rr),
        "final_valid": sum(r["diagnosis"]["final_original"]["valid"] for r in rr),
        "normalized_valid": sum(r["diagnosis"]["final_normalized"]["valid"] for r in rr),
        "fallback_calls": sum(
            c["stage"] == "fallback" and c["dispatched"]
            for r in rr
            for c in r["diagnosis"]["calls"]
        ),
        "eligible": len(eligible),
        "executed": sum(r["execution"]["original"]["executed"] for r in eligible),
        "restored": sum(r["execution"]["original"]["restored"] is True for r in eligible),
        "invalid_final_not_executed": sum(
            r["execution"]["original"]["execution_status"] == "invalid_final" for r in eligible
        ),
        "provider_not_executed": sum(
            r["execution"]["original"]["execution_status"] == "provider_unavailable"
            for r in eligible
        ),
    }


def derive(rows, summary, references):
    arms = [group(rows, arm=a) for a in ARMS]
    programs = [
        group(rows, program_id=p, arm=a)
        for p in sorted({r["program_id"] for r in rows})
        for a in ARMS
    ]
    contexts = [
        group(rows, program_id=p, context_variant=c, arm=a)
        for p in sorted({r["program_id"] for r in rows})
        for c in ("P_supported", "P_unknown", "N_external_supply", "N_snapshot_conflict")
        for a in ARMS
    ]
    bindings = []
    for a in ("B_eq", "G_eq"):
        rr = [r for r in rows if r["arm"] == a]
        for key in rr[0]["score"]["binding_checks"]:
            values = [r["score"]["binding_checks"][key] for r in rr]
            bindings.append(
                {
                    "arm": a,
                    "field": key,
                    "scheduled": len(rr),
                    "match": sum(v is True for v in values),
                    "mismatch": sum(v is False for v in values),
                    "unavailable": sum(v is None for v in values),
                }
            )
    costs = []
    for g in summary["by_arm"]:
        costs.append(
            {
                "arm": g["arm"],
                "primary_calls": g["primary_calls"],
                "fallback_calls": g["fallback_calls"],
                "total_calls": g["primary_calls"] + g["fallback_calls"],
                "input_tokens_observed": g["input_tokens"]["known_sum"],
                "output_tokens_observed": g["output_tokens"]["known_sum"],
                "usage_missing_calls": g["input_tokens"]["missing_calls"],
                "network_seconds_including_timeout": g["network_ms_known_all_calls"] / 1000,
                "pacing_seconds": g["pacing_wait_ms_known"] / 1000,
                "summed_cell_wall_seconds": g["wall_ms_known"] / 1000,
            }
        )
    failures, copies = [], []
    for row in rows:
        d = row["diagnosis"]
        common = read(PREPARED / "packets" / row["case_id"] / "common.json")
        ref = references[row["case_id"]]
        if row["score"]["primary_failure"] or row["score"]["explicit_defer"]:
            p = d["primary"].get("parsed_decision") or {}
            observed = (p.get("packaging") or {}).get("insert_after_line")
            copy_lines = [
                s
                for s in common["source_evidence"]
                if s["path"] == "Dockerfile" and s["text"].startswith("COPY ")
            ]
            last = max(copy_lines, key=lambda s: s["line"])
            failures.append(
                {k: row[k] for k in ("cell_id", "program_id", "context_variant", "arm", "repeat")}
                | {
                    "primary_state": row["score"]["primary_state"],
                    "server_code": d["primary"]["server_transition_code"],
                    "provider_error": d["primary_error"],
                    "insert_after_line": observed,
                    "allowed_insert_after_lines": ref["allowed_insert_after_lines"],
                    "last_copy_source_id": last["id"],
                    "last_copy_actual_line": last["line"],
                    "equals_sc_numeric_suffix": observed == int(re.search(r"\d+$", last["id"])[0])
                    if observed is not None
                    else None,
                    "fallback_valid": d["final_original"]["valid"] if d["fallback_text"] else None,
                    "final_error": d["final_original"]["error"],
                    "primary_errors": (d["primary_diagnostics"] or {}).get("errors", []),
                    "fallback_errors": (d["fallback_diagnostics"] or {}).get("errors", []),
                }
            )
        if row["expected_decision"] == "defer":
            final = d["final_original"]
            records = (
                copy_audit(final["analysis"], common, ref["allowed_target"])
                if final["valid"]
                else []
            )
            copies.append(
                {k: row[k] for k in ("cell_id", "program_id", "context_variant", "arm", "repeat")}
                | {
                    "frozen_target_copy_detected": row["score"]["final_inapplicable_copy_plan"][
                        "original"
                    ],
                    "final_valid": final["valid"],
                    "first_plan_copy_present": any(x["plan_index"] == 0 for x in records),
                    "any_plan_copy_present": bool(records),
                    "archive_target_mapping_first_plan": any(
                        x["plan_index"] == 0 and x["maps_target"] is True for x in records
                    ),
                    "archive_target_mapping_any_plan": any(
                        x["maps_target"] is True for x in records
                    ),
                    "copy_lines": records,
                    "semantic_safety_judgment": None,
                }
            )
    copy_groups = [
        {
            "arm": a,
            "negative": len(rr := [r for r in copies if r["arm"] == a]),
            **{
                k: sum(r[k] is True for r in rr)
                for k in (
                    "frozen_target_copy_detected",
                    "first_plan_copy_present",
                    "any_plan_copy_present",
                    "archive_target_mapping_first_plan",
                    "archive_target_mapping_any_plan",
                )
            },
        }
        for a in ARMS
    ]
    return {
        "arms": arms,
        "programs": programs,
        "contexts": contexts,
        "bindings": bindings,
        "costs": costs,
        "failures": failures,
        "copy_audit": copies,
        "copy_groups": copy_groups,
    }


def verify_inputs(rows, summary, evaluation, frozen, before):
    freeze = read(PREPARED / "freeze-manifest.json")
    assert digest(freeze) == frozen["freeze_sha256"]
    for mapping in (
        frozen["manifest"]["code"],
        frozen["manifest"]["files"],
        freeze["verification_files"],
    ):
        for name, expected in mapping.items():
            assert sha(ROOT / name) == expected, name
    assert freeze["manifest"] == frozen["manifest"]
    assert len(rows) == len(frozen["schedule"]) == 120
    assert len({r["cell_id"] for r in rows}) == 120
    assert not summary["pending"] and not summary["excluded"]
    assert evaluation["executed"] and evaluation["source_unchanged"]
    assert not read(EVALUATION / "execution.json")["cleanup"]
    evaluated = {r["cell_id"]: r for r in evaluation["rows"]}
    slots = {s["cell_id"]: s for s in frozen["schedule"]}
    calls = []
    for row in rows:
        cid = row["cell_id"]
        raw_path = SOURCE / "cells" / cid / "result.json"
        raw, state = read(raw_path), read(raw_path.parent / "state.json")
        assert state["status"] == "completed" and digest(raw) == state["result_sha256"]
        assert all(raw[k] == v for k, v in slots[cid].items())
        assert (
            raw["run_id"] == frozen["run_id"]
            and raw["protocol_version"] == frozen["protocol_version"]
        )
        assert raw == row["diagnosis"] and row == evaluated[cid]
        assert digest(raw) == row["raw_result_sha256"]
        for call in raw["calls"]:
            path = SOURCE / call["path"]
            assert sha(path) == call["sha256"]
            record = read(path)
            assert record["dispatched"] and record["status"] in ("completed", "failed")
            calls.append(record)
        for v in ("original", "normalized"):
            x = row["execution"][v]
            if x.get("execution_cache_hit"):
                assert sha(x["execution_reused_from"]) == x["execution_reused_sha256"]
    assert len(calls) == summary["call_budget"]["new_model_calls"] == 128
    assert Counter(c["stage"] for c in calls) == {"primary": 108, "fallback": 20}
    provider = [r for r in rows if r["score"]["primary_state"] == "provider_error"]
    assert len(provider) == 1 and provider[0]["cell_id"] == TIMEOUT_CELL
    assert provider[0]["diagnosis"]["primary_error"]["code"] == "MODEL_TIMEOUT"
    assert Counter(c["status"] for c in calls) == {"completed": 127, "failed": 1}
    outcomes = Counter(r["execution"]["original"]["execution_status"] for r in rows)
    assert outcomes == {
        "restored": 27,
        "not_execution_eligible": 90,
        "invalid_final": 2,
        "provider_unavailable": 1,
    }
    paired = read(REPORT / "paired.json")
    assert len(paired) == 36
    for p in paired:
        assert all(
            p[k] is True
            for k in (
                "E2_actual_payload_match",
                "E2_generation_config_match",
                "E2_evidence_hash_match",
                "E2_prompt_schema_match",
            )
        )
    for name, expected in read(AUDIT / "legacy-preservation.json")["files"].items():
        assert before[name] == expected, name
    return {
        "passed": True,
        "scheduled_cells_verified": 120,
        "request_records_verified": 128,
        "paired_requests_verified": 36,
        "frozen_file_hashes_verified": True,
        "original_and_evaluation_rows_identical": True,
        "legacy_preservation_verified": True,
        "docker_cleanup_failures": 0,
        "api_calls_in_this_analysis": 0,
        "docker_runs_in_this_analysis": 0,
    }


def verify_aggregates(data, summary):
    for g, old in zip(data["arms"], summary["by_arm"], strict=True):
        assert g["arm"] == old["arm"]
        for key, original in (
            ("appropriate_accept", "appropriate_accept"),
            ("inappropriate_accept", "inappropriate_accept"),
            ("negative_defer", "negative_explicit_defer"),
            ("final_valid", "final_original_valid"),
        ):
            assert g[key] == old[original]["numerator"]
        assert g["contract_blocked"] + g["provider_failure"] == old["primary_failure"]["numerator"]
        assert g["restored"] == old["original"]["restored_scheduled"]["numerator"]
        assert g["normalized_valid"] == g["final_valid"]
    assert sum(g["final_valid"] for g in data["arms"]) == 117
    assert sum(g["restored"] for g in data["arms"]) == 27
    assert all(
        x["equals_sc_numeric_suffix"]
        for x in data["failures"]
        if x["primary_state"] == "invalid_or_blocked"
    )


def documents(output, data, amended, original, legacy_rows, checks):
    a = {g["arm"]: g for g in data["arms"]}
    main_table = table(
        [
            "조건",
            "예정",
            "양성 적절 채택",
            "음성 부적합 채택",
            "음성 명시 보류",
            "계약 차단",
            "API 실패",
            "최종 유효",
        ],
        [
            [
                g["arm"],
                g["scheduled"],
                ratio(g["appropriate_accept"], g["positive"]),
                ratio(g["inappropriate_accept"], g["negative"]),
                ratio(g["negative_defer"], g["negative"]),
                g["contract_blocked"],
                g["provider_failure"],
                ratio(g["final_valid"], g["scheduled"]),
            ]
            for g in data["arms"]
        ],
    )
    execution_table = table(
        [
            "조건",
            "실행 적격",
            "실제 실행",
            "복구/예정",
            "복구/실행",
            "검증 실패로 미실행",
            "API 실패로 미실행",
        ],
        [
            [
                g["arm"],
                g["eligible"],
                g["executed"],
                ratio(g["restored"], g["eligible"]),
                ratio(g["restored"], g["executed"]),
                g["invalid_final_not_executed"],
                g["provider_not_executed"],
            ]
            for g in data["arms"]
        ],
    )
    program_table = table(
        [
            "프로그램",
            "조건",
            "양성 적절 채택",
            "음성 부적합 채택",
            "음성 보류",
            "계약/API 실패",
            "최종 유효",
            "복구/예정",
        ],
        [
            [
                g["program_id"],
                g["arm"],
                ratio(g["appropriate_accept"], g["positive"]),
                ratio(g["inappropriate_accept"], g["negative"]),
                ratio(g["negative_defer"], g["negative"]),
                f"{g['contract_blocked']}/{g['provider_failure']}",
                ratio(g["final_valid"], g["scheduled"]),
                ratio(g["restored"], g["eligible"]),
            ]
            for g in data["programs"]
        ],
    )
    copy_table = table(
        [
            "조건",
            "동결된 표적 COPY 지표",
            "첫 계획에 COPY 존재",
            "어느 계획이든 COPY 존재",
            "아카이브 경로 매핑상 표적 포함(어느 계획)",
        ],
        [
            [
                g["arm"],
                ratio(g["frozen_target_copy_detected"], g["negative"]),
                ratio(g["first_plan_copy_present"], g["negative"]),
                ratio(g["any_plan_copy_present"], g["negative"]),
                ratio(g["archive_target_mapping_any_plan"], g["negative"]),
            ]
            for g in data["copy_groups"]
        ],
    )
    cost_table = table(
        [
            "조건",
            "주/후속 호출",
            "관측 입력 토큰",
            "관측 출력 토큰",
            "usage 누락",
            "API 대기·처리 합계(s)",
            "pacing 합계(s)",
        ],
        [
            [
                g["arm"],
                f"{g['primary_calls']}/{g['fallback_calls']}",
                g["input_tokens_observed"],
                g["output_tokens_observed"],
                g["usage_missing_calls"],
                f"{g['network_seconds_including_timeout']:.2f}",
                f"{g['pacing_seconds']:.2f}",
            ]
            for g in data["costs"]
        ],
    )
    binding_table = table(
        ["조건", "필드", "일치/예정", "불일치", "응답 없음"],
        [
            [
                g["arm"],
                g["field"],
                ratio(g["match"], g["scheduled"]),
                g["mismatch"],
                g["unavailable"],
            ]
            for g in data["bindings"]
        ],
    )
    old_groups = {g["arm"]: g for g in original["by_arm"]}
    # The original argument here is the untouched prior contract-v2 summary, not legacy v1.
    legacy_valid = {
        arm: sum(r["diagnosis"]["final_original"]["valid"] for r in legacy_rows if r["arm"] == arm)
        for arm in ARMS
    }
    old_summary = read(LEGACY / "summary.json")
    legacy_table = table(
        ["조건", "이전 주 실패 → 이번", "이전 최종 유효 → 이번", "이전 복구 → 이번"],
        [
            [
                arm,
                f"{next(g for g in old_summary['by_arm'] if g['arm'] == arm)['primary_failure']['numerator']}/{a[arm]['scheduled']} → {a[arm]['contract_blocked'] + a[arm]['provider_failure']}/{a[arm]['scheduled']}",
                f"{legacy_valid[arm]}/{a[arm]['scheduled']} → {a[arm]['final_valid']}/{a[arm]['scheduled']}",
                f"{next(g for g in old_summary['by_arm'] if g['arm'] == arm)['original']['restored_scheduled']['numerator']}/{a[arm]['eligible']} → {a[arm]['restored']}/{a[arm]['eligible']}",
            ]
            for arm in ARMS
        ],
    )
    text = f"""# 최종 결과 분석 — contract-v2 / analysis-v1

상태: **{FINAL_STATE}**. {QUALIFIER}.

분석 버전: `{VERSION}`. 원실험 run_id: `{amended["run_id"]}`. 이번 분석의 모델 호출·Docker 실행은 각각 0회다. 원본 보고서·응답·실행 기록·동결 코드의 바이트를 수정하지 않았다. `source-manifest.json`, `preservation-after.json`, `verification.json`으로 보존과 집계를 검증한다.

## 1. 완료 상태의 정정과 분석 분모

원보고서의 `기술적 복구 필요`는 실행 적격 행에 `provider_unavailable`가 하나라도 있으면 발생하는 표시였다. 31번째 셀 `{TIMEOUT_CELL}`은 template-http / P_supported / G_eq / 반복 2에서 180.156초 후 MODEL_TIMEOUT으로 끝났다. 해당 호출은 이미 전송됐고 실패 관측으로 보존되었다. 이후 나머지 일정이 처리되어 예정 120행, 완료 120행, 미완료 0행, 제외 0행이다. 동결된 조건에서 새 응답을 얻으려는 재호출은 하지 않았다.

이번 별도 분석은 **일정 완료 여부**와 **개별 결과의 가용성**을 분리한다. 해당 1건의 진단·잠재적 복구 결과는 여전히 알 수 없고, 성공으로 전환하거나 분모에서 제거하지 않는다. `summary.json`의 기존 점수·비율은 그대로 유지하고 종료 상태 및 설명 메타데이터만 변경했다. 이 정정은 API 장애의 해결이나 모든 응답의 성공을 의미하지 않는다. 새로운 전체 실험 없이 현재 자료로 이번 계획 실험을 종료한다.

- 실제 생성 시도: 128회 = 주 호출 108회 + 후속 호출 20회. 시간초과도 1회로 포함한다. 총 상한 216회 내에서 끝났다.
- 최종 원본 유효 응답: 117/120 (97.5%). 정규화도 117/120으로 같다. 자동 12행을 제외한 LLM 조건은 105/108 (97.2%)이다. 형식·근거 검증 통과율이며 진단 정확도가 아니다.
- 실제 복구: 실행 적격 30행 중 27행 (90.0%). 실제 실행한 27행은 27행 모두 복구했다. 나머지는 최종 검증 실패 2행, API 실패 1행이며, 실제 Docker 복구 실패로 섞어 세지 않는다.
- 원본/정규화 동일 패치의 27개 재사용 결과는 독립 복구 성공으로 더하지 않는다. 정규화의 신규 Docker 실행은 0회다.

## 2. 실험 범위와 조건

세 프로그램(shipping-cli, template-http, settlement-batch)의 기본 장애 3개에 저자 통제 문맥 네 종류를 적용한 12개 문맥이다. P_supported와 P_unknown은 참조상 채택, N_external_supply와 N_snapshot_conflict는 참조상 보류다. P_unknown은 가정이 제한된 조건부 계획 채택을 평가하며 실제 복구 성공을 평가하지 않는다. 원자료는 실제 수집 부분과 저자가 추가한 통제 문맥이 결합되어 있고, 실제 사람의 참조 승인 기록은 없다. 따라서 development 개념 검증으로 기술한다.

|비교|조건|구성|행 수|
|---|---|---|---|
|E1|R_auto|규칙 후보를 LLM 검토 없이 자동 채택|12 (문맥당 1회)|
|E1|R_llm|동일 후보를 LLM이 문맥과 함께 검토|36 (문맥당 3회)|
|E2|B_eq|공통 원자료와 출력 계약, 규칙 후보 추가 제공 없음|36|
|E2|G_eq|B_eq와 같은 조건에 규칙 후보 필드를 추가 제공|36|

E1/E2는 별도 비교다. E2의 36개 대응 쌍은 실제 입력에서 후보 필드를 제외한 원자료, 프롬프트·스키마, 생성 설정이 모두 일치한다. 반복 3회는 같은 사례의 모델 출력 반복이며 독립 장애 3개를 늘리지 않는다. R_auto를 36개 관측으로 복제하지 않는다. 유의성 검정·독립 표본 신뢰구간은 제시하지 않는다.

생성 모델은 Gemma API의 `gemma-4-31b-it`이며 temperature=0, max_tokens=4096, seed=42·43·44, warmup=0, 요청 간격 60초, timeout=180초를 사용했다. 연구 설정의 고정값이며 seed가 제공자에서 완전한 결정론을 보장한다는 뜻은 아니다. 모델 입력·원응답과 실제 전송 설정은 각 호출 파일에 보존되어 있다.

## 3. 주 단계 판단 및 최종 유효성

{main_table}

양성 적절 채택은 참조상 양성에서 효과적인 accept와 연결값 일치를 모두 만족한 경우다. 음성 부적합 채택은 참조상 음성에서 효과적으로 accept한 경우다. 명시 보류, 계약 차단, API 오류는 서로 다른 상태다. 모든 조건의 양성 과도 보류는 0건이다. 실패로 인해 accept가 줄어든 현상을 보류 효과로 해석하지 않는다.

### E1: 제한된 주 단계 보류 효과

R_auto는 양성 6/6과 음성 6/6을 모두 채택했다. R_llm은 양성 18/18을 채택했고 음성 15/18을 부적합하게 채택했으며 3/18을 명시 보류했다. 음성 부적합 채택률은 100.0%에서 83.3%로 16.7%p 낮아졌다. 세 보류는 **settlement-batch의 N_external_supply 한 문맥의 세 반복**에 집중됐다. 나머지 다섯 음성 문맥은 모든 반복에서 채택했다. 따라서 여러 오류 유형에 걸친 일관된 안전성 개선으로 일반화할 수 없다.

구조화된 counter_evidence_ids는 보류 세 건을 포함해 비어 있어 음성 근거 그룹 coverage는 R_auto 0/6, R_llm 0/18이다. 인용 자체가 없어 precision은 0%가 아니라 N/A(0/0)다. 자유 서술이나 다른 evidence_ids의 로그 언급을 구조화된 반대 근거 인용으로 대체 집계하지 않았다. 보류 이후의 최종 계획에 관한 한계는 6절을 함께 인용해야 한다.

### E2: 후보 추가 제공의 비교 우위 미확인

B_eq는 36행 모두 주 단계 계약을 통과했지만 음성 18/18도 채택했다. G_eq는 효과적인 채택 18/36, 계약 차단 17/36, API 실패 1/36이며 명시 보류는 0건이었다. G_eq의 음성 부적합 채택 9/18은 나머지 9건이 계약 차단된 결과다. **음성의 의미를 판단해 보류한 결과가 아니다.** 양성 적절 채택도 B_eq 18/18에서 G_eq 9/18로 감소했다.

G_eq의 후속 분석 17회 중 15회가 유효한 최종 응답을 생성해 최종 유효성은 33/36까지 회복됐다. B_eq는 후속 호출이 없었고 최종 유효성은 36/36이다. 이번 데이터에서 후보 추가 제공에 따른 정확도·계약 준수·호출 비용의 비교 우위는 확인되지 않았다. 이 비교는 후보 필드 제공의 효과에 관한 것이며 RDF 표현 자체나 SHACL·SPARQL 각각의 우위를 독립적으로 검증하지 않는다.

## 4. 계약 차단과 연결값 분석

{binding_table}

G_eq의 계약 차단 17건은 모두 packaging.insert_after_line의 잘못된 행 번호와 연결됐다. template-http 11건은 실제 마지막 COPY 행 6 대신 11을 반환했고, settlement-batch 6건은 실제 마지막 COPY 행 7 대신 12를 반환했다. 각 값은 해당 마지막 COPY의 SC ID 숫자 부분(SC000011, SC000012)과 일치한다. 따라서 **SC 식별자 숫자와 실제 행 번호의 혼동에 부합하는 패턴**으로 설명할 수 있다. 모델 내부 추론이나 후보 제공이 그 혼동을 일으킨 인과 기전까지 확인된 것은 아니다.

후속 응답에서도 template-http / P_supported 반복 0·1의 두 건은 source_findings의 인용 SC 범위와 선언한 행 범위가 일치하지 않아 INVALID_SOURCE_LOCATION으로 차단됐다. 같은 문맥의 반복 2는 API 시간초과였다. 언어 enum이나 빈 observations.evidence_ids로 인한 기존 후속 실패는 이번 저장 결과에서 재현되지 않았다. 원응답을 수정하거나 행 번호를 자동 보정하지 않았다.

## 5. 실제 복구와 프로그램별 결과

{execution_table}

복구는 P_supported만 평가했다. 참조 복구와 잘못된 복구의 사전 검사를 통과한 동일 이미지·검사기로 첫 최종 계획 전체를 적용했다. P_unknown과 음성 문맥에 Docker 복구율을 부여하지 않았다. G_eq의 6/9는 예정 단위에서 관측된 복구 비율이며 API 실패의 잠재 결과를 아는 값은 아니다. API 실패를 제외한 6/8=75.0%는 참고용 가용 자료 비율이고 주 분석 6/9를 대체하지 않는다. 해당 한 건이 복구됐다고 가정해도 G_eq의 예정 복구는 최대 7/9=77.8%여서 B_eq 9/9를 넘지 않는다. 이는 통계적 추정이나 재실험 결과가 아닌 한 건의 결측 영향 범위다.

{program_table}

G_eq의 shipping-cli 12행은 모두 주 단계 채택됐다. template-http에서는 11건 계약 차단과 1건 시간초과가 발생했고, settlement-batch에서는 P_unknown·N_external_supply의 각 세 반복이 계약 차단됐다. 이런 프로그램·문맥별 편중을 전체 평균과 함께 제시한다.

## 6. 부정 문맥의 최종 COPY 지표와 보조 감사

{copy_table}

동결된 COPY 지표는 명시적인 표적 파일 도착 경로나 끝에 `/`가 붙은 상위 디렉터리를 탐지한다. `COPY --chown=node:node config ./config`처럼 목적지에 끝 슬래시가 없는 디렉터리 전체 복사를 놓칠 수 있다. 실제로 R_llm의 보류 세 건 모두 후속 첫 계획에서 이 COPY를 제안했다. G_eq의 원래 지표가 false인 음성 9건에도 디렉터리 COPY가 첫 번째 또는 다른 계획에 들어 있다.

이 표의 추가 열은 **사후 보조 감사**다. 첫 계획/전체 계획의 Dockerfile snippet에 COPY가 있는지, 제공 아카이브의 파일·디렉터리와 문자 경로를 조합하면 표적이 포함되는지를 별도 계산했다. 허용 가능한 단일 literal COPY만 매핑하고 미지원 문법은 미확정으로 남긴다. `.dockerignore`, 적용 조건, 실제 배포 문맥, 경로를 바꾸는 다른 변경과의 상호작용 및 안전성을 모두 검증하는 새 채점기가 아니다. 추가 Docker 실행이나 사람의 의미 채점은 하지 않았다.

원래 점수 15/18·9/18은 보존하되 **최종 위험 제안의 정확한 발생률이나 안전 성공률로 제시하지 않는다.** 특히 R_llm의 주 단계 보류 효과를 전체 에이전트가 부적합한 복구 제안을 막았다는 주장으로 확장할 수 없다. 후속 분석으로 보류가 유지되는지는 이번 지표로 입증되지 않았다. `tables/supplementary-copy-audit.csv`에 60개 음성 평가 단위의 snippet과 판별 근거를 남겼다.

## 7. 호출 수·토큰·시간

{cost_table}

G_eq는 B_eq보다 총 호출이 53/36=1.47배였고, 관측 입력 토큰은 279,128/134,634=2.07배, 관측 출력 토큰은 37,510/10,239=3.66배였다. G_eq의 시간초과 한 호출은 usage가 없어 해당 총 토큰을 정확한 완전 합계로 취급하지 않는다. 입력 토큰 차이는 후보 필드 추가와 후속 호출 증가가 함께 포함된 값이다. 화폐 비용은 산출하지 않았다.

API 시간에는 180초 시간초과를 포함하며 실제 모델 계산 시간과 동일하지 않다. pacing은 호출 간격 60초 정책에 따른 대기다. 그래프·후보 생성은 오프라인 준비에서 한 번 수행됐고 live는 고정 패킷을 재생했다. 따라서 위 시간을 그래프 구축부터 실행 복구까지의 전체 시스템 지연으로 주장하지 않는다. 프로세스 재개 사이의 사용자 대기 시간도 셀 시간 합계에 포함되지 않는다.

## 8. 이전 계약 버전과의 개발 비교

{legacy_table}

이전 183회(주 108+후속 75)에서 이번 128회(주 108+후속 20)로 생성 호출이 55회 줄었다. 계약 설명 보완 후 유효 응답·조립 가능성이 개선됐다는 개발 관찰이다. 기존 결과를 본 뒤 같은 문맥으로 수정한 사후 비교이므로 독립 holdout, 사전 등록된 확증 실험, 그래프 진단 정확도의 개선으로 해석하지 않는다. 두 버전의 행을 합쳐 표본 수를 늘리지 않는다.

## 9. 논문에서 허용되는 해석

- 제한된 Node.js 파일 읽기 실패·Docker COPY 문맥에서 근거 연결, 검증, 규칙 후보 생성, LLM 검토 및 실행 복구를 연계한 구현의 작동을 보였다. 실행 가능한 양성 계획 27건의 복구를 확인했다.
- E1에서 LLM 검토는 한 부정 문맥의 세 반복에서 명시 보류를 보였지만 다른 음성 문맥에서는 부적합 채택이 유지됐고 최종 계획의 안전성은 입증하지 못했다.
- E2에서 후보 필드 제공의 추가 이점은 확인되지 않았다. G_eq에서 위치 계약 위반과 후속 호출 비용 증가가 관측됐다.
- 계약 설명 보완은 응답 유효성을 개선했지만 유효성은 의미적 정답이나 안전성의 대리 지표가 아니다.

제한: 기본 장애 3개, 저자 통제 문맥, 반복의 비독립성, 단일 모델·고정 설정, 사후 개발 수정, 실제 사람의 참조 승인 부재, API 결측 1건, 제한된 출력 계약·적용기, COPY 지표의 탐지 범위다. 다른 프로젝트·언어·미지원 오류로의 일반화, LLM의 필수성, RDF/OWL의 보편적 우위, 통계적 유의성·동등성은 주장하지 않는다. SHACL 구조 검증을 원인 증명으로, SPARQL 규칙 실행을 일반적인 OWL 추론으로 바꾸어 표현하지 않는다.

## 10. 재현과 원자료

`reproduction.md`는 저장된 자료만 읽는 이번 분석 명령을 제공한다. `summary.json`은 기존 수치가 동일한 상태 정정본, `analysis.json`은 재집계·보조 감사, `status-amendment.json`은 변경 사유와 범위다. 검증한 원본 파일 수는 {checks["protected_files"]}개다. 원래 보고서는 `{REPORT.as_posix()}/report.md`에 그대로 있다. 논문 TeX/Bib와 원실험 코드·프롬프트·스키마·정답·계획·Docker 결과는 수정하지 않았다.
"""
    write(output / "report.md", text)
    paper = f"""# 논문용 결과 요약 — 저장된 contract-v2 결과의 별도 분석

{FINAL_STATE}. {QUALIFIER}. 아래 문장은 결과 초안이며 새 실험을 수행하지 않았다.

세 Node.js 프로그램의 기본 장애 3개에 네 가지 저자 통제 문맥을 적용하여 12개 문맥을 구성하였다. 자동 채택 조건은 문맥당 한 번, 세 LLM 조건은 문맥당 세 번 평가하여 총 120개 평가 단위를 처리하였다. 실제 생성 호출은 주 호출 108회와 후속 호출 20회를 합한 128회였으며, 그중 API 시간초과 1회를 실패 관측으로 보존하였다. 참조는 저자 통제 기준이고 사람의 승인 기록이 없어 본 평가는 development 단계의 소규모 개념 검증으로 한정한다.

모델은 `gemma-4-31b-it`을 사용했고 temperature=0, 최대 출력 4096토큰, 반복 seed=42·43·44, 호출 간격 60초, timeout=180초를 고정하였다. E2의 36개 대응 쌍은 후보 필드를 제외한 입력과 프롬프트·출력 스키마·생성 설정이 일치하였다.

{main_table}

E1에서 자동 채택은 양성 6/6과 음성 6/6을 모두 채택하였다. LLM 검토는 양성 18/18의 적절 채택을 유지하면서 음성 18회 중 3회를 명시적으로 보류하였다. 이에 따라 음성 부적합 채택률은 100.0%에서 83.3%로 16.7%p 감소하였다. 다만 세 보류는 settlement-batch의 외부 공급 문맥 한 개의 반복에서만 나타났고, 구조화된 반대 근거 인용은 없었다. 또한 보류 후 최종 계획에서 디렉터리 COPY가 다시 제안되어, 이 결과는 주 단계의 제한된 보류 효과로 해석해야 하며 전체 에이전트의 안전성 향상으로 확장할 수 없다.

E2에서 후보를 추가 제공하지 않은 B_eq는 주 단계 계약을 36/36 통과했지만 음성도 18/18 채택하였다. 후보를 제공한 G_eq는 효과적 채택 18회, 계약 차단 17회, API 실패 1회였고 명시 보류는 없었다. 계약 차단 17회는 모두 Dockerfile 삽입 행 번호와 관련됐으며 반환 숫자가 마지막 COPY의 SC 식별자 숫자와 일치하는 패턴이 관찰되었다. 후속 분석을 포함한 최종 유효 응답은 B_eq 36/36, G_eq 33/36이었다. 이 조건에서는 후보 추가 제공의 비교 우위를 확인하지 못하였다.

{execution_table}

실행 평가는 P_supported 30행으로 제한하였다. 27행에서 계획을 실제 적용해 복구를 확인했고, 나머지는 최종 검증 실패 2행과 API 시간초과 1행으로 실행하지 못하였다. 따라서 예정 행 기준 복구 비율은 27/30(90.0%), 실행된 계획의 조건부 복구 비율은 27/27(100.0%)이다. 원본과 제한적 정규화의 최종 유효성은 모두 117/120(97.5%)였고 복구 결과도 같았다. 동일 패치의 정규화 평가가 원본 실행을 재사용했으므로 독립 성공으로 합산하지 않았다.

G_eq는 B_eq에 비해 총 호출이 36회에서 53회로 증가하였다. 관측 입력·출력 토큰 합도 각각 2.07배와 3.66배였으며, 시간초과 호출의 usage는 누락값으로 유지하였다. 이는 후보 입력량과 후속 호출이 함께 반영된 비용이고 전체 시스템 지연이나 화폐 비용을 의미하지 않는다.

부정 문맥의 동결된 COPY 탐지 지표는 목적지 끝 슬래시가 없는 디렉터리 복사를 놓치는 한계가 있었다. 사후 보조 감사에서는 모든 조건의 음성 최종 응답에 적어도 한 계획의 COPY 코드가 포함되어 있음을 확인하였다. 이 관찰은 실제 적용·피해를 평가한 결과가 아니며, 기존 지표의 COPY 미검출을 안전 성공으로 해석할 수 없음을 보여준다. 전체 결과는 단일 모델, 3개 기본 장애, 합성 문맥, 반복의 비독립성, 사후 계약 보완 및 사람 참조 승인 부재의 범위에서 기술한다. 통계적 유의성이나 다른 프로젝트·오류 유형에 대한 일반화는 주장하지 않는다.
"""
    write(output / "paper-results.md", paper)
    claims = """# 논문 반영 시 주장 범위

|구성 요소|관측값|허용되는 표현|피해야 하는 표현|
|---|---|---|---|
|실행 가능성|27/30 예정 행에서 복구, 실제 실행 27/27 성공|제한된 P_supported에서 생성 계획의 적용·복구 가능성을 확인|모든 오류 100% 복구, 120개 독립 장애 검증|
|LLM 검토|음성 명시 보류 3/18, 한 문맥의 반복에 집중|특정 외부 공급 문맥에서 주 단계 보류 관찰|LLM이 필수, 부정 복구 제안을 끝까지 방지|
|후보 제공|G_eq 계약 차단 17/36, B_eq 0/36; 호출 53 대 36|이번 조건에서 추가 이점 미확인, 위치 계약과 비용 한계 관찰|그래프/RDF/OWL 자체가 우수하거나 불필요하다는 일반 결론|
|출력 계약 보완|최종 유효 117/120; 이전 실패 감소|같은 개발 문맥에서 응답 유효성 개선|독립 검증에서 진단 정확도 향상 입증|
|근거 사용|구조화 반대 근거 coverage 0; precision N/A|근거 연결 구조 구현 및 실제 인용 한계를 분리 기술|반대 근거를 충분히 활용한 안전 진단 입증|
|COPY 탐지|사후 감사에서 기존 탐지기의 디렉터리 COPY 누락 확인|미검출·불확실·유효 응답·의미적 적절성을 구분|COPY 미검출 50%를 안전 성공률 50%로 표현|

결과 표는 E1/E2, 원본/정규화, 예정/실행 분모를 구분한다. 논의에는 시간초과 1건과 SC 숫자/행 혼동 패턴, 후속 계획에서 보류가 유지되지 않을 가능성을 포함한다. 한계에는 기본 장애 3개, 12개 저자 통제 문맥, 비독립 반복, 단일 모델, 실제 사람 참조 승인 부재와 사후 개발 수정을 기재한다. 초록·결론은 제한된 구현·실행 가능성 및 확인된 한계에 맞춘다. SHACL은 구조 제약 검증, SPARQL은 명시적 규칙 추론으로 표기한다.
"""
    write(output / "paper-claims-handoff.md", claims)
    # Preserve all numerical fields in the amended summary; separate derived metrics live elsewhere.
    assert amended["by_arm"] == original["by_arm"] == list(old_groups.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "results" / (PREFIX + "-analysis-v1"))
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or ROOT not in output.parents:
        raise ValueError("Output must be a NEW directory inside this project")
    for source in (SOURCE, EVALUATION, REPORT, LEGACY, PREPARED, AUDIT):
        if source == output or source in output.parents or output in source.parents:
            raise ValueError("Analysis output must be separate from every source")
    parser_checks()
    with read_lock() as lock:
        frozen = read(SOURCE / "frozen.json")
        old_preservation = read(AUDIT / "legacy-preservation.json")
        before = snapshot(protected_paths(frozen, old_preservation), lock)
        rows, original = read(REPORT / "cells.json"), read(REPORT / "summary.json")
        evaluation = read(EVALUATION / "summary.json")
        checks = verify_inputs(rows, original, evaluation, frozen, before)
        checks["protected_files"] = len(before)
        references = read(PREPARED / "references.json")["cases"]
        data = derive(rows, original, references)
        verify_aggregates(data, original)
        checks["original_aggregate_numerators_reproduced"] = True
        checks["supplementary_copy_parser_checks_passed"] = True
        amended = copy.deepcopy(original)
        amended.update(
            analysis_version=VERSION,
            original_experiment_state=original["experiment_state"],
            experiment_state=FINAL_STATE,
            state_qualifier=QUALIFIER,
            schedule_complete=True,
            observed_provider_failures=1,
            additional_api_calls=0,
            additional_docker_runs=0,
        )
        assert all(amended[k] == v for k, v in original.items() if k != "experiment_state")
        # The fixture-specific assertions above prevent this amendment from masking new failures.
        amendment = {
            "analysis_version": VERSION,
            "old_state": original["experiment_state"],
            "new_state": FINAL_STATE,
            "qualifier": QUALIFIER,
            "basis": "User-authorized separate offline analysis; completed schedule with one immutable provider timeout, not an unresolved retry queue",
            "timeout_cell": TIMEOUT_CELL,
            "timeout_preserved": True,
            "all_original_numerical_fields_unchanged": True,
            "no_provider_retry": True,
            "unknown_timeout_outcome_remains_unknown": True,
            "additional_api_calls": 0,
            "additional_docker_runs": 0,
            "frozen_method_changed": False,
            "frozen_copy_scores_changed": False,
            "supplementary_audit_is_post_hoc": True,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        output.mkdir(parents=True, exist_ok=False)
        write(output / "source-manifest.json", {"root": str(ROOT), "files": before})
        write(output / "summary.json", amended)
        write(output / "status-amendment.json", amendment)
        write(
            output / "analysis.json",
            {
                "version": VERSION,
                "study_run_id": frozen["run_id"],
                "definitions": {
                    "primary_metrics": "unchanged frozen endpoint",
                    "copy_audit": "post-hoc literal COPY presence and archive mapping; no semantic safety verdict",
                    "restoration": "scheduled denominators include invalid final results and the timeout",
                },
                **data,
            },
        )
        for key in (
            "arms",
            "programs",
            "contexts",
            "bindings",
            "costs",
            "failures",
            "copy_audit",
            "copy_groups",
        ):
            name = "supplementary-copy-audit" if key == "copy_audit" else key.replace("_", "-")
            csv_file(output / "tables" / (name + ".csv"), data[key])
        documents(output, data, amended, original, read(LEGACY / "cells.json"), checks)
        write(
            output / "status-amendment.md",
            f"# 종료 상태 정정 기록\n\n기존: {amendment['old_state']}\n\n이번 분석: **{FINAL_STATE}** — {QUALIFIER}.\n\n판정 근거: 완료 120, 미완료 0, 제외 0; API 실패 1건은 이미 전송된 MODEL_TIMEOUT으로 보존; Docker 실행 27건 전부 복구 및 cleanup 오류 없음. 원래 지표, 분모, 응답, 검사 결과를 변경하지 않았다. API 실패의 잠재 응답은 여전히 결측이다. 상태 정정은 추가 호출이나 성공 응답 대체를 요구하지 않는 일정 완료 판정이다. 상세 검증은 verification.json, 원본 보존은 preservation-after.json을 참조한다.\n",
        )
        reproduction = f'''# 저장 결과만 이용하는 재현 명령

새 Gemma 호출, Docker 실행, 원응답 정규화·수정 또는 동결 코드 수정이 없다. 이미 존재하는 출력 폴더는 거부한다. 다른 새 출력 폴더를 지정하면 같은 원자료로 다시 집계할 수 있다.

```cmd
cd /d "{ROOT}"
set "REVIEW_PYTHON=%CD%\\..\\AI_Error_Check_Agent_paper_v3\\.venv\\Scripts\\python.exe"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%REVIEW_PYTHON%" -B analysis\\review_controls_contract_v2_final_v1\\analyze.py --output results/review-controls-v4-contract-v2-analysis-v1-reproduced
```

분석 코드는 Python 표준 라이브러리만 사용한다. 이 명령은 선택적 오프라인 재현이며 현재 결과를 위해 다시 실행할 필요는 없다. 원본 실험 재현 명령은 기존 보고서 reproduction.md에 남아 있지만, 이번 정정에 live/preflight/evaluate --execute 명령은 사용하지 않았다.
'''
        write(output / "reproduction.md", reproduction)
        after = snapshot(protected_paths(frozen, old_preservation), lock)
        assert before == after, "Source file content or file set changed during analysis"
        write(
            output / "preservation-after.json",
            {
                "preserved": True,
                "checked_files": len(before),
                "added": [],
                "removed": [],
                "changed": [],
                "before_manifest_sha256": sha(output / "source-manifest.json"),
                "original_status_still": read(REPORT / "summary.json")["experiment_state"],
                "additional_api_calls": 0,
                "additional_docker_runs": 0,
            },
        )
        write(output / "verification.json", checks)
        write(
            output / "output-manifest.json",
            {
                "analysis_script": str(Path(__file__).resolve()),
                "analysis_script_sha256": sha(__file__),
                "outputs": {
                    p.relative_to(output).as_posix(): sha(p)
                    for p in sorted(output.rglob("*"))
                    if p.is_file()
                },
            },
        )
    print(
        json.dumps(
            {
                "report": str(output / "report.md"),
                "paper_results": str(output / "paper-results.md"),
                "experiment_state": FINAL_STATE,
                "qualifier": QUALIFIER,
                "verification_passed": True,
                "protected_files": len(before),
                "additional_api_calls": 0,
                "additional_docker_runs": 0,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
