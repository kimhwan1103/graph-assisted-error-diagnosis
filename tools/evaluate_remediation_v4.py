"""Offline remediation assessment of immutable v4 responses. No model backend imports."""

import argparse
import contextlib
import copy
import hashlib
import json
import os
import platform
import re
import shutil
from collections import Counter
from pathlib import Path

from remediation_adapter_v4 import POLICY, PlanError, adapt
from remediation_adapter_v4 import VERSION as ADAPTER_VERSION
from remediation_docker_v4 import REQUIRED_CHECKS, DockerReplay, file_hash

ROOT = Path(__file__).resolve().parents[1]
VERSION = "study-v4-remediation-evaluation.v1"
MANIFEST_VERSION = "study-v4-remediation-cases.v1"
PROTOCOL = {
    "version": VERSION,
    "primary_metric": "complete_restoration / all scheduled repairable cells",
    "claim_scope": "restoration within supported plan expressions and the declared replay environment",
    "selection": "first plan of the single saved terminal result; no replacement or success-based selection",
    "adapter": POLICY,
    "required_checks": REQUIRED_CHECKS,
    "attempts": 1,
    "cache": "none; each executed response starts in a fresh fixture/container",
    "retry": "no automatic retry; separate output required for identical-response infrastructure retry",
    "missing": "null is unexecuted, false is observed failure; infrastructure errors stay unresolved",
    "normalization": "original and normalized are separate assessments; no new runtime/token estimate",
    "scope_exclusion": "case manifest fixed before this evaluator runs, never selected by model outcome",
    "no_model_calls": True,
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def locked_bytes(handle):
    handle.seek(0)
    content = handle.read()
    handle.seek(0)
    return content


def hashes(folder, lock_handle=None):
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(
            locked_bytes(lock_handle)
            if lock_handle and p == folder / "writer.lock"
            else p.read_bytes()
        ).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Invalid case/cell identifier")
    return value


def separate(output, source):
    if output == source or output in source.parents or source in output.parents:
        raise ValueError("Output must not equal, contain, or be contained in an input path")


@contextlib.contextmanager
def readonly_run_lock(source):
    """Participate in the existing writer lock WITHOUT writing a byte to it."""
    path = source / "writer.lock"
    if not path.is_file() or path.stat().st_size < 1:
        raise ValueError("A completed v4 run with its existing writer.lock is required")
    with path.open("rb") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBRLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Source run has an active writer; use a completed snapshot") from exc
        try:
            yield handle
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_completed(source):
    frozen = read(source / "frozen.json")
    if frozen.get("version") != "iris-study.v4" or not frozen.get("live"):
        raise ValueError("Agent performance assessment requires a saved live v4 run")
    seen = set()
    for slot in frozen["schedule"]:
        cid = safe_id(slot["cell_id"])
        safe_id(slot["case_id"])
        safe_id(slot["arm"])
        if cid in seen:
            raise ValueError("Duplicate scheduled cell")
        seen.add(cid)
        folder = source / "cells" / cid
        if not (folder / "state.json").exists() or not (folder / "result.json").exists():
            raise ValueError("Source run is incomplete; missing cells cannot be scored as failures")
        state, result = read(folder / "state.json"), read(folder / "result.json")
        if state.get("status") != "completed" or state.get("result_sha256") != digest(result):
            raise ValueError("Source run is incomplete or terminal result hash differs")
        if any(result.get(k) != slot[k] for k in ("case_id", "cell_id", "arm", "repeat")):
            raise ValueError("Result does not match its scheduled identity")
        for path in (folder / "calls").glob("*.json"):
            if read(path).get("status") not in ("completed", "failed"):
                raise ValueError("A source call is still in progress")
    for name, expected in frozen["source_manifest"].items():
        path = (ROOT / name).resolve()
        if ROOT not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Frozen source version mismatch: {name}")
    return frozen


def development_manifest(frozen):
    """Post-hoc development classification; not a held-out/pre-registered benchmark."""
    groups = {
        "docker-copy-omission": "repairable",
        "recovered-same-stream": "no_change",
        "expected-test-error": "no_change",
        "source-target-mismatch": "insufficient_context",
    }
    entries, excluded = [], []
    for case in frozen["cases"]:
        case_id = case["id"]
        if case_id not in groups:
            reason = (
                "Known source/log execution inconsistency"
                if case_id == "direct-literal"
                else "Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair"
            )
            excluded.append({"case_id": case_id, "reason": reason})
            continue
        files = {s["path"]: s["content"] for s in case["source_files"]}
        group = groups[case_id]
        entry = {
            "case_id": case_id,
            "incident_id": case_id,
            "cluster_id": case["cluster_id"],
            "case_group": group,
            "development_exposure": case["exposure"],
            "case_sha256": digest(case),
            "source_files_sha256": digest(case["source_files"]),
            "payload_sha256": digest(case["payload"]),
            "file_sha256": {k: file_hash(v) for k, v in files.items()},
            "provided_resources": {
                "source_archive": "frozen case source_files; agent routing decides model-visible subset",
                "external_resources": [],
                "new_or_invented_data": False,
            },
            "allowed_states": [
                "insufficient_evidence"
                if group == "insufficient_context"
                else "no_failure_evidence"
                if group == "no_change"
                else "diagnosed"
            ],
            "technical_preflight": "pending_execution"
            if group == "repairable"
            else "not_applicable_to_response_only_control",
        }
        if group == "repairable":
            entry["adapter_policy"] = copy.deepcopy(POLICY)
            entry["execution"] = {
                "engine": "iris-copy-replay.v1",
                "base_image": "node:22-alpine",
                "isolation": "fresh, no network, no mounts, read-only root",
                "conditions": [
                    "same supplied source/build context",
                    "no external mounts or initialization",
                    "supplied tasks.json is approved static fixture seed data",
                ],
            }
            entry["tests"] = {
                "version": "iris-copy-tests.v1",
                "target_path": "/app/data/tasks.json",
                "health_app": "iris-demo-web",
                "title": "작은 완료",
                "expected_tasks": json.loads(files["data/tasks.json"]),
                "task_comparison": "exact all non-id fields; ignore generated UUID and list order",
                "regression": "loadConfig default/custom/invalid ports; passes on broken image",
                "protected_files": {
                    k: file_hash(v)
                    for k, v in files.items()
                    if k.startswith(("src/", "public/", "data/")) or k == "package.json"
                },
                "required_checks": REQUIRED_CHECKS,
            }
        entries.append(entry)
    return {
        "version": MANIFEST_VERSION,
        "study_sha256": digest(frozen),
        "classification_timing": "post_hoc_development_before_replay_not_before_generation",
        "normalization_policy": "limitations-text-unwrapping.v1",
        "notes": "Available source archives are shared; actual model input subsets may differ and are recorded per cell. No claims of pre-registration or independent held-out incidents.",
        "cases": entries,
        "excluded_cases": excluded,
    }


def validate_manifest(manifest, frozen):
    if manifest.get("version") != MANIFEST_VERSION or manifest.get("study_sha256") != digest(
        frozen
    ):
        raise ValueError("Case manifest does not match the saved study/version")
    cases = {c["id"]: c for c in frozen["cases"]}
    ids = [c["case_id"] for c in manifest["cases"]]
    excluded = [c["case_id"] for c in manifest["excluded_cases"]]
    if len(set(ids + excluded)) != len(ids + excluded) or set(ids + excluded) != set(cases):
        raise ValueError(
            "Every study case must be included once or have an explicit exclusion reason"
        )
    if any(not c.get("reason") for c in manifest["excluded_cases"]):
        raise ValueError("Exclusion reason required")
    for spec in manifest["cases"]:
        case = cases[spec["case_id"]]
        for k in ("case_id", "incident_id", "cluster_id"):
            safe_id(spec[k])
        if spec["cluster_id"] != case["cluster_id"] or spec["case_sha256"] != digest(case):
            raise ValueError("Case identity/hash mismatch")
        if spec["source_files_sha256"] != digest(case["source_files"]) or spec[
            "payload_sha256"
        ] != digest(case["payload"]):
            raise ValueError("Source or offered input hash mismatch")
        if spec["file_sha256"] != {
            f["path"]: file_hash(f["content"]) for f in case["source_files"]
        }:
            raise ValueError("Fixture file hashes differ")
        if spec["case_group"] not in {"repairable", "no_change", "insufficient_context"}:
            raise ValueError("Unknown case group")
        if spec["case_group"] == "repairable":
            if (
                spec.get("adapter_policy") != POLICY
                or spec.get("tests", {}).get("required_checks") != REQUIRED_CHECKS
            ):
                raise ValueError("Unsupported adapter or missing mandatory checks")
            if (
                spec.get("execution", {}).get("engine") != "iris-copy-replay.v1"
                or spec["execution"]["base_image"] != "node:22-alpine"
            ):
                raise ValueError("Unsupported replay engine/base")
            known = next(
                c
                for c in read(ROOT / "evaluation/packaging.v1.json")["cases"]
                if c["id"] == "docker-copy-omission"
            )
            if digest(case["source_files"]) != digest(known["source_files"]):
                raise ValueError(
                    "v1 replay supports only the pinned COPY fixture; register another engine for new programs"
                )
            expected = next(
                c
                for c in development_manifest(frozen)["cases"]
                if c["case_id"] == "docker-copy-omission"
            )
            if spec["tests"] != expected["tests"]:
                raise ValueError("Fixed checker oracle mismatch")


def visible_inputs(folder):
    records = []
    for path in sorted((folder / "calls").glob("*.json")):
        call = read(path)
        data = call.get("input") or {}
        paths = {s["path"] for s in data.get("source_evidence", []) if "path" in s}
        for item in data.get("deployment_code", []):
            if isinstance(item, dict) and "path" in item:
                paths.add(item["path"])
        if isinstance(data.get("code"), dict) and "path" in data["code"]:
            paths.add(data["code"]["path"])
        records.append(
            {
                "call": path.name,
                "input_sha256": digest(data),
                "source_paths": sorted(paths),
                "archive_observations_provided": bool(data.get("archive_observations")),
                "packaging_candidate_provided": bool(data.get("packaging_candidate")),
            }
        )
    return records


def response_for(slot, source, variant, normalized):
    folder = source / "cells" / slot["cell_id"]
    original = read(folder / "result.json")
    public = original["result"]
    valid = public.get("job_status") == "succeeded" and public.get("analysis") is not None
    analysis, error = public.get("analysis"), public.get("error")
    assessed = public
    if variant == "normalized":
        n = read(normalized / "cells" / (slot["cell_id"] + ".json"))
        if n["original_result_sha256"] != digest(original) or any(n[k] != slot[k] for k in slot):
            raise ValueError("Normalized cell identity/original hash mismatch")
        call_path = (source / n["last_call"]).resolve()
        if (folder / "calls").resolve() not in call_path.parents:
            raise ValueError("Normalized call path is outside the selected cell")
        call = read(call_path)
        if hashlib.sha256(call_path.read_bytes()).hexdigest() != n["last_call_sha256"]:
            raise ValueError("Normalized last-call digest mismatch")
        from revalidate_study_v4 import decode, normalize, validate

        value, _ = normalize(decode(call["raw"]["text"]))
        if value != n["normalized_model_response"]:
            raise ValueError("Normalized response differs from the frozen text-only policy")
        assessed = validate(value, call, public)
        if assessed != n["normalized"]:
            raise ValueError("Normalized validation cannot be reproduced using the frozen source")
        valid, analysis, error = assessed["valid"], assessed["analysis"], assessed["error"]
    return {
        "response_valid": valid,
        "analysis": analysis,
        "response_error": error,
        "source_response_sha256": digest(public),
        "assessed_response_sha256": digest(assessed),
        "model_visible_inputs": visible_inputs(folder),
    }


def classify(response, spec):
    valid, analysis = response["response_valid"], response["analysis"]
    if not valid:
        code = (response["response_error"] or {}).get("code", "")
        return (
            "contract_failed" if code.startswith(("INVALID", "UNSAFE")) else "generation_failed"
        ), None
    remediation = analysis.get("remediation") or {}
    plans = remediation.get("plans")
    status = analysis.get("analysis_status")
    expected = {
        "diagnosed": "proposed",
        "no_failure_evidence": "not_needed",
        "insufficient_evidence": "needs_more_evidence",
    }
    if (
        not isinstance(plans, list)
        or remediation.get("status") != expected.get(status)
        or bool(plans) != (status == "diagnosed")
    ):
        return "contract_failed", None
    group = spec["case_group"]
    if group == "no_change":
        if plans:
            return "unnecessary_change", plans[0]
        return (
            "no_change_correct"
            if status in spec["allowed_states"] and remediation["status"] == "not_needed"
            else "inappropriate_response"
        ), None
    if group == "insufficient_context":
        missing = analysis.get("missing_information")
        complete = (
            isinstance(missing, list)
            and bool(missing)
            and all(
                isinstance(m, dict)
                and all(
                    isinstance(m.get(k), str) and m[k].strip() for k in ("requested_data", "reason")
                )
                for m in missing
            )
        )
        correct = (
            status == "insufficient_evidence"
            and status in spec["allowed_states"]
            and not plans
            and complete
        )
        return ("structural_deferral_correct" if correct else "inappropriate_response"), plans[
            0
        ] if plans else None
    if not plans:
        return (
            "deferred" if status == "insufficient_evidence" else "inappropriate_no_change"
        ), None
    return "plan_selected", plans[0]


def execution_outcome(row):
    if row.get("evaluation_error"):
        return "evaluation_error"
    if all(row.get(k) is True for k in REQUIRED_CHECKS):
        return "restored"
    for key, outcome in [
        ("application_pass", "apply_failed"),
        ("build_pass", "build_failed"),
        ("startup_pass", "startup_failed"),
        ("target_failure_resolved", "target_failed"),
        ("health_pass", "health_failed"),
        ("functional_pass", "functional_failed"),
        ("regression_pass", "regression_failed"),
        ("data_preservation_pass", "preservation_failed"),
    ]:
        if row.get(key) is False:
            return outcome
    return "evaluation_error"


def ratio(n, d, *, final=True, lower_bound=True):
    return {
        "numerator": n,
        "denominator": d,
        "rate": n / d if d and final else None,
        "confirmed_success_lower_bound": n / d if d and not final and lower_bound else None,
    }


def summarize(rows):
    result = {}
    for arm in sorted({r["arm"] for r in rows}):
        selected = [r for r in rows if r["arm"] == arm]
        groups = {}
        for name in ("repairable", "no_change", "insufficient_context"):
            group = [r for r in selected if r["case_group"] == name]
            n, pending = len(group), sum(not r["evaluation_complete"] for r in group)
            valid = sum(r["response_valid"] for r in group)
            value = {
                "scheduled": n,
                "completed": n - pending,
                "unresolved": pending,
                "outcomes": dict(Counter(r["outcome"] for r in group)),
                "response_outcomes": dict(Counter(r.get("response_outcome") for r in group)),
                "adapter_outcomes": dict(
                    Counter(r["adapter_outcome"] for r in group if r.get("adapter_outcome"))
                ),
                "response_valid_rate": ratio(valid, n),
                "response_failures": n - valid,
            }
            if name == "repairable":
                applied = sum(r["application_pass"] is True for r in group)
                restored = sum(r["complete_restoration"] is True for r in group)
                checked = [
                    r
                    for r in group
                    if r["application_pass"] is True and r["regression_pass"] is not None
                ]
                value.update(
                    application_rate=ratio(applied, n, final=not pending),
                    complete_restoration_rate=ratio(restored, n, final=not pending),
                    restoration_given_applied=ratio(
                        restored, applied, final=not pending, lower_bound=False
                    ),
                    regression_rate=ratio(
                        sum(r["regression_pass"] is False for r in checked), len(checked)
                    ),
                    applied_cells=applied,
                    regression_completed=len(checked),
                    regression_uncompleted=applied - len(checked),
                )
            elif name == "no_change":
                unnecessary = sum(r["response_valid"] and r["plan_present"] for r in group)
                value.update(
                    unnecessary_change_rate=ratio(unnecessary, n),
                    unnecessary_change_given_valid=ratio(unnecessary, valid),
                    appropriate_no_change_rate=ratio(
                        sum(r["outcome"] == "no_change_correct" for r in group), n
                    ),
                    response_failure_rate=ratio(n - valid, n),
                )
            else:
                value.update(
                    structural_deferral_rate=ratio(
                        sum(r["outcome"] == "structural_deferral_correct" for r in group), n
                    ),
                    response_failure_rate=ratio(n - valid, n),
                )
            groups[name] = value
        result[arm] = groups
    pairs = Counter()
    keys = {(r["case_id"], r["repeat"]) for r in rows if r["case_group"] == "repairable"}
    for key in keys:
        pair = {
            r["arm"]: r
            for r in rows
            if (r["case_id"], r["repeat"]) == key and r["arm"] in {"adaptive", "graph_compact"}
        }
        if len(pair) != 2:
            pairs["missing_comparator"] += 1
        elif not all(r["evaluation_complete"] for r in pair.values()):
            pairs["unresolved"] += 1
        else:
            a, g = (pair[k]["complete_restoration"] is True for k in ("adaptive", "graph_compact"))
            pairs[
                "both_restored"
                if a and g
                else "adaptive_only"
                if a
                else "graph_compact_only"
                if g
                else "neither_restored"
            ] += 1
    return {
        "status": "incomplete" if any(not r["evaluation_complete"] for r in rows) else "complete",
        "cells": len(rows),
        "cases": len({r["case_id"] for r in rows}),
        "incidents": len({r["incident_id"] for r in rows}),
        "lineage_clusters": len({r["cluster_id"] for r in rows}),
        "repeats": sorted({r["repeat"] for r in rows}),
        "by_arm": result,
        "paired_repairable": {
            k: pairs[k]
            for k in (
                "both_restored",
                "adaptive_only",
                "graph_compact_only",
                "neither_restored",
                "unresolved",
                "missing_comparator",
            )
        },
    }


def rate_text(value):
    n, d = value["numerator"], value["denominator"]
    if not d:
        return "N/A (0/0)"
    if value["rate"] is not None:
        return f"{n}/{d} ({100 * value['rate']:.1f}%)"
    lower = value["confirmed_success_lower_bound"]
    return f"미판정; 확인된 하한 {n}/{d} ({100 * lower:.1f}%)" if lower is not None else "미판정"


def report_text(summary, manifest, variant, execute):
    lines = [
        "# v4 해결안 자동 실행 평가",
        "",
        f"상태: **{summary['status']}** · 응답: **{variant}** · 실행 요청: **{execute}**",
        "",
        "평가 범위는 지원된 COPY 계획 표현과 고정 재현 환경이다. 구조 검증 통과를 복구 성공으로 세지 않는다.",
        f"사례 {summary['cases']}개, incident {summary['incidents']}개, 계보 {summary['lineage_clusters']}개, 셀 {summary['cells']}개. 분류 시점: `{manifest['classification_timing']}`.",
        "",
        "## 수정 가능한 사례",
        "",
        "| 조건 | 예정 | 완료 | 미판정 | 응답 유효 | 적용률 | 완전 복구율 | 적용 후 복구율 | 회귀 / 검사 완료 | 회귀 미검사 적용 셀 |",
        "|---|---:|---:|---:|---|---|---|---|---|---:|",
    ]
    for arm, groups in summary["by_arm"].items():
        g = groups["repairable"]
        lines.append(
            f"| {arm} | {g['scheduled']} | {g['completed']} | {g['unresolved']} | {rate_text(g['response_valid_rate'])} | {rate_text(g['application_rate'])} | {rate_text(g['complete_restoration_rate'])} | {rate_text(g['restoration_given_applied'])} | {rate_text(g['regression_rate'])} | {g['regression_uncompleted']} |"
        )
    lines += [
        "",
        "## 무변경 및 정보 부족",
        "",
        "| 조건 | 무변경 셀 | 불필요한 제안 / 전체 | 불필요한 제안 / 유효 | 적절한 무변경 | 무변경 응답 실패 | 정보 부족 셀 | 구조적 보류 | 정보 부족 응답 실패 |",
        "|---|---:|---|---|---|---:|---:|---|---:|",
    ]
    for arm, groups in summary["by_arm"].items():
        n, i = groups["no_change"], groups["insufficient_context"]
        lines.append(
            f"| {arm} | {n['scheduled']} | {rate_text(n['unnecessary_change_rate'])} | {rate_text(n['unnecessary_change_given_valid'])} | {rate_text(n['appropriate_no_change_rate'])} | {n['response_failures']} | {i['scheduled']} | {rate_text(i['structural_deferral_rate'])} | {i['response_failures']} |"
        )
    lines += [
        "",
        "## 대응 비교와 실행 범위",
        "",
        "```json",
        json.dumps(summary["paired_repairable"], ensure_ascii=False, indent=2),
        "```",
        "",
        f"실제 에이전트 계획 재현 시도: {summary['agent_execution_attempts']}회. 캐시 공유: 0회. 신규 모델 호출: 0회.",
        "사전 참조 수정과 평가기 회귀 테스트는 에이전트 복구 성공에 포함하지 않는다. 같은 장애의 여러 조건은 독립 사례 수를 늘리지 않는다.",
        "dry-run과 인프라 오류의 미실행 검사는 null이다. 미판정 셀이 있으면 완전 복구율을 확정하지 않으며, 예정 분모에 대한 확인된 성공만 하한으로 표시한다.",
        "원본과 정규화는 별도 평가다. 이 실행 시간은 모델의 기존 응답 시간이 아니며, 정규화 후 시스템의 새 토큰·지연 측정도 아니다.",
        "실제 모델 입력의 소스 범위가 조건별로 다를 수 있다. 셀별 model_visible_inputs와 fixture 조건을 확인해야 한다. 조건부 적용의 자연어 타당성은 자동 채점하지 않는다.",
        "",
        "## 셀별 판정",
        "",
        "| 셀 | 조건 | 사례 | 판정 | 이유 |",
        "|---|---|---|---|---|",
    ]
    for row in summary["cell_index"]:
        reason = str(row["failure_reason"] or "").replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {row['cell_id']} | {row['arm']} | {row['case_id']} | {row['outcome']} | {reason} |"
        )
    lines += ["", "## 제외 사례", ""] + [
        f"- {c['case_id']}: {c['reason']}" for c in manifest["excluded_cases"]
    ]
    return "\n".join(lines) + "\n"


def run(
    source,
    output,
    case_manifest,
    *,
    variant="original",
    normalized=None,
    arms=None,
    execute=False,
    replay_class=DockerReplay,
):
    source, output, case_manifest = source.resolve(), output.resolve(), case_manifest.resolve()
    normalized = normalized.resolve() if normalized else None
    if variant not in {"original", "normalized"} or (variant == "normalized") != (
        normalized is not None
    ):
        raise ValueError(
            "Use --normalized-run only and always with --assessment-variant normalized"
        )
    for path in [source, case_manifest] + ([normalized] if normalized else []):
        separate(output, path)
    if output.exists():
        raise ValueError("Output already exists; use a new directory")
    with readonly_run_lock(source) as lock_handle:
        before = hashes(source, lock_handle)
        frozen = load_completed(source)
        manifest = read(case_manifest)
        validate_manifest(manifest, frozen)
        norm_before = hashes(normalized) if normalized else None
        if normalized:
            meta = read(normalized / "revalidation.json")
            if (
                meta["study_sha256"] != digest(frozen)
                or meta["policy"].get("version") != manifest["normalization_policy"]
            ):
                raise ValueError("Normalization study/policy mismatch")
            if read(normalized / "original-files.sha256.json") != before:
                raise ValueError("Normalization is bound to a different source snapshot")
        selected_arms = arms or ["adaptive", "graph_compact"]
        if (
            not selected_arms
            or len(set(selected_arms)) != len(selected_arms)
            or set(selected_arms) - set(frozen["config"]["arms"])
        ):
            raise ValueError("Unknown or duplicate comparison arm")
        specs = {c["case_id"]: c for c in manifest["cases"]}
        fixtures = {c["id"]: c for c in frozen["cases"]}
        scheduled = [
            s for s in frozen["schedule"] if s["arm"] in selected_arms and s["case_id"] in specs
        ]
        output.mkdir(parents=True, exist_ok=False)
        shutil.copytree(
            source,
            output / "snapshot/source",
            ignore=lambda directory, names: ["writer.lock"] if Path(directory) == source else [],
        )
        (output / "snapshot/source/writer.lock").write_bytes(locked_bytes(lock_handle))
        if normalized:
            (output / "snapshot/normalized").mkdir()
            for name in ("revalidation.json", "original-files.sha256.json"):
                shutil.copy2(normalized / name, output / "snapshot/normalized" / name)
            shutil.copytree(normalized / "cells", output / "snapshot/normalized/cells")
        for name in frozen["source_manifest"]:
            target = output / "snapshot/code" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        write(output / "protocol.json", PROTOCOL)
        write(output / "case-manifest.json", manifest)
        evaluator_files = [
            Path(__file__),
            Path(__file__).with_name("remediation_adapter_v4.py"),
            Path(__file__).with_name("remediation_docker_v4.py"),
            Path(__file__).with_name("remediation_checks_v4.cjs"),
            Path(__file__).with_name("revalidate_study_v4.py"),
        ]
        for path in evaluator_files:
            target = output / "snapshot/evaluator" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        record = {
            "version": VERSION,
            "evaluation_environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "assessment_variant": variant,
            "normalization_policy": manifest["normalization_policy"] if normalized else None,
            "source_run": str(source),
            "study_sha256": digest(frozen),
            "source_files_sha256": before,
            "normalized_files_sha256": norm_before,
            "case_manifest_sha256": digest(manifest),
            "frozen_source_sha256": frozen["source_manifest"],
            "scheduled": scheduled,
            "evaluator_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in evaluator_files
            },
            "source_completion": "all scheduled source cells completed; read lock held",
            "execute": execute,
        }
        write(output / "manifest.json", record)
        replay = replay_class(output)
        preflights = {}
        rows = []
        try:
            for spec in specs.values():
                if spec["case_group"] == "repairable":
                    pre = (
                        replay.preflight(spec, fixtures[spec["case_id"]])
                        if execute
                        else {
                            "status": "not_executed",
                            "baseline_failure_reproduced": None,
                            "error": "dry-run; technical qualification not executed",
                        }
                    )
                    preflights[spec["case_id"]] = pre
                    write(output / "preflight" / spec["case_id"] / "result.json", pre)
            # Assessment reads the completed copy, not changing original files.
            snapshot = output / "snapshot/source"
            norm_snapshot = output / "snapshot/normalized" if normalized else None
            for slot in scheduled:
                spec, fixture = specs[slot["case_id"]], fixtures[slot["case_id"]]
                response = response_for(slot, snapshot, variant, norm_snapshot)
                outcome, plan = classify(response, spec)
                row = {
                    **slot,
                    "incident_id": spec["incident_id"],
                    "cluster_id": spec["cluster_id"],
                    "case_group": spec["case_group"],
                    "assessment_variant": variant,
                    "case_manifest_sha256": digest(manifest),
                    "test_manifest_sha256": digest(spec.get("tests")),
                    "adapter_version": ADAPTER_VERSION,
                    "normalization_policy": record["normalization_policy"],
                    **{k: v for k, v in response.items() if k != "analysis"},
                    "response_outcome": outcome,
                    "selected_plan_id": plan.get("id") if plan else None,
                    "selected_plan": plan,
                    "plan_present": bool(plan),
                    "adapter_supported": None,
                    "adapter_outcome": None,
                    "adapter_failure_reason": None,
                    "adapter_supplied_fields": [],
                    "normalized_patch_sha256": None,
                    **{k: None for k in REQUIRED_CHECKS},
                    "complete_restoration": None,
                    "outcome": outcome,
                    "failure_reason": None,
                    "failure_reasons": [],
                    "execution_id": None,
                    "evaluation_complete": True,
                }
                if outcome == "contract_failed":
                    row["response_valid"] = False
                folder = output / "cells" / slot["cell_id"]
                folder.mkdir(parents=True)
                execution = {
                    "executed": False,
                    "commands": [],
                    "reason": "response-only or unsupported plan",
                }
                if spec["case_group"] == "repairable":
                    adapted = None
                    if plan:
                        try:
                            adapted = adapt(plan, fixture["source_files"], spec["adapter_policy"])
                            row.update(
                                adapter_supported=True,
                                adapter_outcome="supported_plan",
                                adapter_supplied_fields=adapted["adapter_supplied_fields"],
                                normalized_patch_sha256=adapted["normalized_patch_sha256"],
                            )
                            write(
                                folder / "adapter.json",
                                {
                                    k: v
                                    for k, v in adapted.items()
                                    if k not in {"patch_diff", "dockerfile"}
                                },
                            )
                        except PlanError as exc:
                            row.update(
                                outcome=exc.outcome,
                                adapter_outcome=exc.outcome,
                                adapter_failure_reason=str(exc),
                                failure_reason=str(exc),
                                adapter_supported=False,
                            )
                    pre = preflights[slot["case_id"]]
                    row["baseline_failure_reproduced"] = pre.get("baseline_failure_reproduced")
                    if pre["status"] != "passed":
                        row["evaluation_complete"] = False
                        if execute:
                            row["outcome"] = "evaluation_error"
                        elif adapted:
                            row["outcome"] = "not_executed"
                        row["failure_reason"] = pre.get("error", "Technical preflight pending")
                    elif adapted:
                        execution = replay.evaluate(row, spec, fixture, adapted, folder)
                        row.update(
                            {k: v for k, v in execution.items() if k not in {"commands", "details"}}
                        )
                        row["outcome"] = execution_outcome(row)
                        row["evaluation_complete"] = row["outcome"] != "evaluation_error"
                        row["failure_reason"] = row.get("evaluation_error")
                    if row["evaluation_complete"]:
                        row["complete_restoration"] = row["outcome"] == "restored"
                    row["failure_reasons"] = [k for k in REQUIRED_CHECKS if row[k] is False]
                    if row["adapter_failure_reason"]:
                        row["failure_reasons"].append("adapter: " + row["adapter_failure_reason"])
                if not row["failure_reason"] and row["outcome"] not in {
                    "restored",
                    "no_change_correct",
                    "structural_deferral_correct",
                }:
                    row["failure_reason"] = (response["response_error"] or {}).get("code") or row[
                        "outcome"
                    ]
                write(folder / "execution.json", execution)
                write(folder / "result.json", row)
                rows.append(row)
        finally:
            cleanup = replay.cleanup() if execute else []
            write(
                output / "execution.json",
                {
                    "commands": replay.commands,
                    "base_image": replay.base_image,
                    "cleanup_errors": cleanup,
                    "agent_execution_attempts": replay.agent_attempts,
                },
            )
            unchanged = hashes(source, lock_handle) == before and (
                not normalized or hashes(normalized) == norm_before
            )
            code_unchanged = all(
                hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
                for name, expected in frozen["source_manifest"].items()
            )
            write(
                output / "preservation.json",
                {
                    "source_unchanged": unchanged,
                    "new_model_calls": 0,
                    "frozen_source_verified": code_unchanged,
                },
            )
            if not unchanged or not code_unchanged:
                raise ValueError("Input changed during evaluation; output must not be used")
        summary = summarize(rows)
        summary.update(
            assessment_variant=variant,
            agent_execution_attempts=replay.agent_attempts,
            linked_cached_responses=0,
            new_model_calls=0,
            preflight=preflights,
            cell_index=[
                {k: r[k] for k in ("cell_id", "case_id", "arm", "outcome", "failure_reason")}
                for r in rows
            ],
        )
        write(output / "summary.json", summary)
        (output / "report.md").write_text(
            report_text(summary, manifest, variant, execute), encoding="utf-8"
        )
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument(
        "--assessment-variant", choices=["original", "normalized"], default="original"
    )
    parser.add_argument("--normalized-run", type=Path)
    parser.add_argument("--case-manifest", type=Path)
    parser.add_argument("--write-development-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--arms", nargs="+", default=["adaptive", "graph_compact"])
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run isolated Docker preflight and stored plans; never invokes a model",
    )
    args = parser.parse_args()
    if args.write_development_manifest:
        path = args.write_development_manifest.resolve()
        separate(path, args.source_run.resolve())
        with readonly_run_lock(args.source_run.resolve()):
            frozen = load_completed(args.source_run.resolve())
            write(path, development_manifest(frozen))
        print(f"Development manifest saved: {path}")
        return
    if not args.case_manifest or not args.output:
        parser.error("--case-manifest and --output are required for assessment")
    result = run(
        args.source_run,
        args.output,
        args.case_manifest,
        variant=args.assessment_variant,
        normalized=args.normalized_run,
        arms=args.arms,
        execute=args.execute,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "cells": result["cells"],
                "agent_execution_attempts": result["agent_execution_attempts"],
                "new_model_calls": 0,
                "report": str(args.output / "report.md"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
