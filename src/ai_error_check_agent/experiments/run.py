"""Run: python -m ai_error_check_agent.experiments.run --backend mock|vllm|gemini."""

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import random
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .backends import BackendFailure, GeminiBackend, MockBackend, VLLMBackend
from .design import (
    ARMS,
    EXPLANATION_FIELDS,
    LENGTH_TARGETS,
    PROMPT_VERSION,
    PUBLIC_PILOT,
    ROOT,
    Decision,
    ExperimentConfig,
    digest,
    input_for,
    load_cases,
    measure_lengths,
    normalize_decision_output,
    prepare_case,
    prompt_for,
    validate_decision,
    validate_evaluation_split,
)


def save(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Windows preview/indexing can briefly lock the old checkpoint. Retry only
    # the atomic file replacement, never a model request or an entire run.
    for attempt in range(5):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.025 * (2**attempt))


def environment():
    versions = {}
    for package in ("pydantic", "httpx", "rdflib", "pyshacl", "torch", "vllm", "transformers"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": versions,
    }


def schedule(cases, repeats, seed):
    rng = random.Random(seed)
    items = []
    for repeat in range(repeats):
        order = list(cases)
        rng.shuffle(order)
        for case in order:
            # Seeded counterbalancing; each case/repeat has all four arms once.
            arms = list(ARMS)
            rng.shuffle(arms)
            items.extend((case, repeat, arm) for arm in arms)
    return items


def aggregate(rows, *, is_mock):
    groups = {}
    for subset in ("all", "declared_compact", "declared_controls"):
        groups[subset] = {}
        for arm in ARMS:
            selected = [
                r
                for r in rows
                if r["arm"] == arm.name
                and (
                    subset == "all"
                    or (r["expected_route"] == "compact") == (subset == "declared_compact")
                )
            ]
            ok = [r for r in selected if r["contract_valid"]]

            def mean(field, records=ok):
                values = [r[field] for r in records if r.get(field) is not None]
                return statistics.mean(values) if values else None

            groups[subset][arm.name] = {
                "n": len(selected),
                "contract_valid": len(ok),
                "raw_json_valid_n": None
                if is_mock
                else sum(r.get("raw_json_valid") is True for r in selected),
                "format_normalized_n": None
                if is_mock
                else sum(r.get("format_normalized") is True for r in selected),
                "raw_contract_valid_n": None
                if is_mock
                else sum(r.get("raw_json_valid") is True for r in ok),
                "execution_errors": sum(r["error"] is not None for r in selected),
                "graph_hints_available": sum(r["graph_hints_available"] for r in selected),
                "status_matches": None
                if is_mock
                else sum(r["status_match"] is True for r in selected),
                "status_match_rate": None
                if is_mock or not selected
                else (sum(r["status_match"] is True for r in selected) / len(selected)),
                "median_request_ms": None
                if is_mock or not ok
                else statistics.median(r["request_ms"] for r in ok),
                "mean_input_tokens": None if is_mock else mean("input_tokens"),
                "mean_output_tokens": None if is_mock else mean("output_tokens"),
                "mean_output_characters": mean("output_characters"),
                "mean_end_to_end_ms": None if is_mock else mean("end_to_end_ms"),
                "mean_graph_build_ms": mean("graph_build_ms", selected),
                "length_target_compliance": None if is_mock else mean("length_target_met"),
                "length_valid_n": None
                if is_mock
                else sum(r.get("length_target_met") is True for r in ok),
                "length_field_compliance": {
                    field: None if is_mock else mean(field + "_length_target_met")
                    for field in EXPLANATION_FIELDS
                },
                "mean_explanation_characters": {
                    field: mean(field + "_characters") for field in EXPLANATION_FIELDS
                },
                "unique_bug_count": len({r.get("bug_id", r["case"]) for r in selected}),
                "semantic_accuracy": None,
            }
    contrasts = {}
    comparisons = {
        "graph_effect_long": ("raw_long", "graph_long"),
        "graph_effect_short": ("raw_short", "graph_short"),
        "short_effect_raw": ("raw_long", "raw_short"),
        "short_effect_graph": ("graph_long", "graph_short"),
    }
    pairs = {(r["case"], r["repeat"], r["arm"]): r for r in rows}
    for name, (before, after) in comparisons.items():
        paired = [
            (row, pairs[(row["case"], row["repeat"], after)])
            for row in rows
            if row["arm"] == before and (row["case"], row["repeat"], after) in pairs
        ]
        valid = [(a, b) for a, b in paired if a["contract_valid"] and b["contract_valid"]]
        graph_pairs = sum(b["graph_hints_available"] for _, b in paired)
        identifiable = not name.startswith("graph_effect") or graph_pairs > 0
        contrasts[name] = {
            "direction": "after minus before; negative latency/tokens means reduction",
            "before": before,
            "after": after,
            "paired_n": len(paired),
            "valid_paired_n": len(valid),
            "graph_hint_paired_n": graph_pairs if name.startswith("graph_effect") else None,
            "effect_identifiable": identifiable,
            "mean_request_ms_difference": None
            if is_mock or not valid or not identifiable
            else statistics.mean(b["request_ms"] - a["request_ms"] for a, b in valid),
            "mean_output_token_difference": None
            if is_mock or not identifiable
            else paired_token_difference(valid),
            "mean_end_to_end_ms_difference": None
            if is_mock or not identifiable
            else paired_difference(valid, "end_to_end_ms"),
            "status_match_difference": None
            if is_mock or not paired or not identifiable
            else statistics.mean(
                int(b["status_match"] is True) - int(a["status_match"] is True) for a, b in paired
            ),
        }
    return {
        "run_kind": "mock_dry_run" if is_mock else "live_model_experiment",
        "model_identity": {
            "backend": rows[0].get("backend") if rows else None,
            "requested_model": rows[0].get("model_id") if rows else None,
            "returned_model_versions": sorted(
                {r["model_version"] for r in rows if isinstance(r.get("model_version"), str)}
            ),
        },
        "groups": groups,
        "paired_contrasts": contrasts,
        "alert_evaluation": alert_evaluation(rows, is_mock=is_mock),
        "source_bug_evaluation": source_bug_evaluation(rows, is_mock=is_mock),
        "semantic_review": "not applicable to mock" if is_mock else "pending independent review",
        "limits": [
            "Development cases and small public pilots are not held-out incident accuracy.",
            "Status agreement and valid references do not establish root-cause correctness.",
            "Same output schema in all arms; this tests verbosity, not production template expansion.",
            "HTTP request time includes server queue/transport/generation; excludes model startup, evidence and graph preparation.",
            "Hosted API timing is not local GPU speed. Client pacing wait is recorded separately and excluded from request/end-to-end metrics.",
            "End-to-end time = cached shared preparation cost + measured arm preparation/request/validation; excludes startup, checkpoint I/O, external Kaggle allocation queue. It is reconstructed per-call pipeline cost, not whole-run wall time.",
            "Graph comparisons test added graph-derived hints, not graph serialization superiority over identical hints in plain text.",
            "Token/time comparisons use successful paired responses; report failures separately.",
            "Length compliance is measured on valid decisions, not used to discard or retry off-target responses. Prompt instructions do not guarantee length compliance.",
            "Repeated calls on the same case are not independent incidents; no significance claim.",
            "Graph effects are unidentifiable when graph hints are absent; their contrast metrics are null.",
        ],
    }


def source_bug_evaluation(rows, *, is_mock):
    public = [
        row
        for row in rows
        if row.get("task_kind") in {"bug_regression_diagnosis", "reproduced_test_observation"}
    ]
    if not public:
        return None
    return {
        "scope": "Selected-test diagnosis and source grounding; not whole-system causal accuracy or agent-generated repair success",
        "independently_reproduced": all(
            row.get("task_kind") == "reproduced_test_observation" for row in public
        ),
        "semantic_accuracy": None,
        "mock_metrics_unmeasured": is_mock,
        "graph_effect_identifiable": any(row["graph_hints_available"] for row in public),
        "by_arm": {
            arm.name: {
                "n": len(selected := [row for row in public if row["arm"] == arm.name]),
                "source_input_available": sum(
                    row.get("source_evidence_lines", 0) > 0 for row in selected
                ),
                "graph_hints_available": sum(row["graph_hints_available"] for row in selected),
                "source_citation_coverage": None
                if is_mock or not selected
                else sum(row.get("source_citation_provided") is True for row in selected)
                / len(selected),
                "passing_test_false_positive_rate": None
                if is_mock
                or not (normal := [r for r in selected if r.get("gold_label") == "test_pass"])
                else sum(r["contract_valid"] and r["status"] == "diagnosed" for r in normal)
                / len(normal),
                "passing_test_abstentions_or_errors": None
                if is_mock
                else sum(
                    not r["contract_valid"] or r["status"] == "insufficient_evidence"
                    for r in selected
                    if r.get("gold_label") == "test_pass"
                ),
            }
            for arm in ARMS
        },
        "limit": "Citation coverage is not defect accuracy. Passing-test controls measure false positives only for that test; unrelated suite failures remain in reproduction receipts.",
    }


def alert_evaluation(rows, *, is_mock):
    public = [row for row in rows if row.get("task_kind") == "log_alert_detection"]
    if not public:
        return None
    groups = {}
    for arm in ARMS:
        selected = [row for row in public if row["arm"] == arm.name]
        confusion = {
            label: {prediction: 0 for prediction in ("alert", "non_alert", "abstain_or_error")}
            for label in ("alert", "non_alert")
        }
        if not is_mock:
            for row in selected:
                prediction = (
                    {"diagnosed": "alert", "no_failure_evidence": "non_alert"}.get(row["status"])
                    if row["contract_valid"]
                    else None
                )
                confusion[row["gold_label"]][prediction or "abstain_or_error"] += 1
        tp, tn = confusion["alert"]["alert"], confusion["non_alert"]["non_alert"]
        fp = confusion["non_alert"]["alert"]
        positives = sum(confusion["alert"].values())
        negatives = sum(confusion["non_alert"].values())
        decided = tp + tn + fp + confusion["alert"]["non_alert"]

        def ratio(numerator, denominator):
            return None if is_mock or not denominator else numerator / denominator

        groups[arm.name] = {
            "n": len(selected),
            "confusion": None if is_mock else confusion,
            "accuracy_abstentions_incorrect": ratio(tp + tn, len(selected)),
            "decision_coverage": ratio(decided, len(selected)),
            "selective_accuracy": ratio(tp + tn, decided),
            "alert_precision": ratio(tp, tp + fp),
            "alert_recall_abstentions_missed": ratio(tp, positives),
            "alert_f1_abstentions_missed": ratio(2 * tp, positives + tp + fp),
            "non_alert_false_positive_rate": ratio(fp, negatives),
            "abstentions_or_errors": None if is_mock else len(selected) - decided,
            "graph_hints_available": sum(row["graph_hints_available"] for row in selected),
        }
    return {
        "scope": "line-level BGL alert labels only; not deployment root-cause correctness",
        "by_arm": groups,
        "mock_metrics_unmeasured": is_mock,
        "graph_effect_identifiable": any(row["graph_hints_available"] for row in public),
    }


def paired_token_difference(valid):
    diffs = [
        b["output_tokens"] - a["output_tokens"]
        for a, b in valid
        if a["output_tokens"] is not None and b["output_tokens"] is not None
    ]
    return statistics.mean(diffs) if diffs else None


def paired_difference(valid, field):
    diffs = [
        b[field] - a[field]
        for a, b in valid
        if a.get(field) is not None and b.get(field) is not None
    ]
    return statistics.mean(diffs) if diffs else None


def write_tables(output, rows, is_mock):
    fields = [
        "backend",
        "model_id",
        "model_version",
        "case",
        "repeat",
        "arm",
        "graph",
        "response_length",
        "expected_route",
        "task_kind",
        "split",
        "bug_id",
        "gold_label",
        "graph_hints_available",
        "source_evidence_lines",
        "source_citation_provided",
        "contract_valid",
        "raw_json_valid",
        "format_normalized",
        "status",
        "status_match",
        "request_ms",
        "request_wait_ms",
        "shared_prepare_ms",
        "graph_build_ms",
        "validation_ms",
        "arm_prepare_ms",
        "end_to_end_ms",
        "summary_characters",
        "uncertainty_characters",
        "next_check_characters",
        "length_target_met",
        "summary_length_target_met",
        "uncertainty_length_target_met",
        "next_check_length_target_met",
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "total_tokens",
        "cached_input_tokens",
        "output_characters",
        "provider_calls",
        "error",
        "validation_error",
        "shared_input_sha256",
    ]
    with (output / "rows.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary = aggregate(rows, is_mock=is_mock)
    save(output / "summary.json", summary)
    label = "MOCK: 실행 흐름만 확인, 정확도·모델 지연·토큰 미측정" if is_mock else "실제 모델 실험"
    text = [
        f"# {label}",
        "",
        f"Backend: {summary['model_identity']['backend']}; model: {summary['model_identity']['requested_model']}",
        "",
        "| 조건 | 완료 | 유효 응답 | 코드 블록 정규화 | 길이 준수 | 상태 일치 | 요청 중앙값(ms) | 평균 출력 토큰 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, group in summary["groups"]["all"].items():
        text.append(
            f"| {name} | {group['n']} | {group['contract_valid']} | "
            f"{group['format_normalized_n']} | "
            f"{group['length_valid_n']} | "
            f"{group['status_matches']} | {group['median_request_ms']} | "
            f"{group['mean_output_tokens']} |"
        )
    text += [
        "",
        "설명 필드별 평균 문자 수 (유효 응답 기준, 길이 위반 응답도 포함)",
        "",
        "| 조건 | summary | uncertainty | next_check | 각 필드 목표 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for arm in ARMS:
        lengths = summary["groups"]["all"][arm.name]["mean_explanation_characters"]
        low, high = LENGTH_TARGETS[arm.response_length]
        text.append(
            f"| {arm.name} | {lengths['summary']} | {lengths['uncertainty']} | "
            f"{lengths['next_check']} | {low}~{high}자 |"
        )
    if summary["alert_evaluation"]:
        text += [
            "",
            "공개 BGL 로그의 경보 탐지 평가 (근본 원인 정확도 아님)",
            "",
            "| 조건 | 전체 정확도(기권 포함) | 판정 비율 | 경보 F1 | 비경보 오탐률 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for name, group in summary["alert_evaluation"]["by_arm"].items():
            text.append(
                f"| {name} | {group['accuracy_abstentions_incorrect']} | {group['decision_coverage']} | {group['alert_f1_abstentions_missed']} | {group['non_alert_false_positive_rate']} |"
            )
        text += [
            "",
            "이 자료에는 소스가 없어 그래프 후보가 없습니다. 그래프 효과 비교는 미측정으로 표시합니다.",
        ]
    if summary["source_bug_evaluation"]:
        text += [
            "",
            f"BugsJS 소스 연결 평가. 선택 테스트의 독립 재현 여부: {summary['source_bug_evaluation']['independently_reproduced']}.",
            "소스 인용 여부·상태 일치는 결함 원인 정확도가 아닙니다. 원인 정확도는 독립 검토 전까지 미측정입니다.",
            f"그래프 효과 측정 가능: {summary['source_bug_evaluation']['graph_effect_identifiable']}. 후보가 없으면 그래프 차이는 미측정입니다.",
        ]
    text += [
        "",
        "None은 미측정입니다. 의미적 진단 정확도는 독립 검토 전까지 미측정입니다.",
        "",
        "자세한 제한과 조건별 비교는 summary.json, 원본 요청·응답은 calls/에 있습니다.",
    ]
    (output / "report.md").write_text("\n".join(text) + "\n", encoding="utf-8")


def wait_for_call_slot(last_started, interval):
    """Pacing is outside inference/pipeline timings and never retries a call."""
    if last_started is None or interval == 0:
        return 0.0
    start = time.perf_counter()
    delay = max(0.0, interval - (start - last_started))
    if delay:
        time.sleep(delay)
    return round((time.perf_counter() - start) * 1000, 3)


def run_experiment(cases, datasets, config, output, *, backend_name="mock", backend=None):
    if not cases:
        raise ValueError("At least one case is required")
    factories = {"mock": MockBackend, "vllm": VLLMBackend, "gemini": GeminiBackend}
    if backend_name not in factories:
        raise ValueError("Unsupported experiment backend")
    validate_evaluation_split(cases)
    planned = len(cases) * len(ARMS) * config.repeats
    if planned + config.warmups > config.max_calls:
        raise ValueError(
            "Planned calls exceed max_calls; reduce cases/repeats or explicitly raise cap"
        )
    backend = backend or factories[backend_name](config)
    try:
        output.mkdir(parents=True, exist_ok=False)
        (output / "calls").mkdir()
    except BaseException:
        backend.close()
        raise
    is_mock = backend.is_mock
    base_file = ROOT / "UPSTREAM_SNAPSHOT.json"
    manifest = {
        "version": "iris-factorial-run.v1",
        "prompt_version": PROMPT_VERSION,
        "length_targets": LENGTH_TARGETS,
        "length_counting": "len(decoded string), Unicode code points including whitespace/punctuation",
        "output_parser": "strict_json_or_single_code_fence.v1; raw response preserved; no content repair/retry",
        "started_at": datetime.now(UTC).isoformat(),
        "state": "running",
        "planned_rows": planned,
        "completed_rows": 0,
        "backend": backend_name,
        "config": config.model_dump(),
        "datasets": datasets,
        "environment": environment(),
        "decision_schema_sha256": digest(Decision.model_json_schema()),
        "prompt_sha256": {
            a.name: digest(prompt_for(a, cases[0].get("task_kind", "deployment_diagnosis")))
            for a in ARMS
        },
        "prompt_sha256_by_task": {
            task: {arm.name: digest(prompt_for(arm, task)) for arm in ARMS}
            for task in sorted({case.get("task_kind", "deployment_diagnosis") for case in cases})
        },
        "case_ids": [case["id"] for case in cases],
        "code_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "src").rglob("*"))
            if p.is_file() and p.suffix in {".py", ".ttl", ".rq", ".json", ".md"}
        },
        "upstream": json.loads(base_file.read_text("utf-8")) if base_file.exists() else None,
        "design": "same raw evidence, source selection, schema, token cap and sampling; "
        "only graph hints and response-length instructions vary",
    }
    rows = []
    save(output / "manifest.json", manifest)
    try:
        manifest["server"] = backend.preflight()
        last_call_started = None
        # Warmup uses unrelated evidence, never a case's gold labels or generated answer.
        warm = {
            "context": {},
            "logs": [{"id": "EV000001", "text": "INFO startup pending"}],
            "source_evidence": [],
            "input_limitations": ["warmup only"],
        }
        for number in range(config.warmups):
            if not is_mock:
                wait_for_call_slot(last_call_started, config.request_interval_seconds)
                last_call_started = time.perf_counter()
            answer = backend.generate(prompt_for(ARMS[1]), warm, config.seed + number)
            save(output / f"warmup-{number}.json", answer)
        prepared = {}
        for case in cases:
            start = time.perf_counter()
            item = prepare_case(case, config)
            prepared[case["id"]] = (item, (time.perf_counter() - start) * 1000)
        for index, (case, repeat, arm) in enumerate(schedule(cases, config.repeats, config.seed)):
            item, prepare_ms = prepared[case["id"]]
            row = {
                "backend": backend_name,
                "model_id": config.model_id,
                "model_version": None,
                "case": case["id"],
                "repeat": repeat,
                "arm": arm.name,
                "graph": arm.graph,
                "response_length": arm.response_length,
                "expected_route": case.get("expected_route", "unknown"),
                "task_kind": case.get("task_kind", "deployment_diagnosis"),
                "split": case.get("split", "development"),
                "bug_id": case.get("bug_id", case["id"]),
                "gold_label": case.get("gold", {}).get("label")
                if case.get("task_kind") in {"log_alert_detection", "reproduced_test_observation"}
                else None,
                "contract_valid": False,
                "raw_json_valid": None,
                "format_normalized": None,
                "status": None,
                "status_match": None if is_mock else False,
                "input_tokens": None,
                "output_tokens": None,
                "output_characters": None,
                "provider_calls": 0,
                "error": None,
                "validation_error": None,
                "request_ms": None,
                "request_wait_ms": None,
                "thinking_tokens": None,
                "total_tokens": None,
                "cached_input_tokens": None,
                "end_to_end_ms": None,
                "length_target_met": None,
                "validation_ms": None,
                "graph_build_ms": 0,
                "graph_hints_available": False,
                "source_evidence_lines": len(item.source_lines),
                "source_citation_provided": None if is_mock else False,
                "shared_prepare_ms": round(prepare_ms, 3),
                "shared_input_sha256": digest(item.model_input),
            }
            call = {
                "prompt": prompt_for(arm, case.get("task_kind", "deployment_diagnosis")),
                "schema": Decision.model_json_schema(),
                "seed": config.seed + repeat,
                "is_mock": is_mock,
            }
            if not is_mock:
                row["request_wait_ms"] = wait_for_call_slot(
                    last_call_started, config.request_interval_seconds
                )
            arm_started = start = time.perf_counter()
            payload, graph_report = input_for(item, arm)
            row["arm_prepare_ms"] = round((time.perf_counter() - start) * 1000, 3)
            if arm.graph:
                row["graph_build_ms"] = row["arm_prepare_ms"]
            row["graph_hints_available"] = "graph_hints" in payload
            call.update(model_input=payload, graph_report=graph_report)
            start = time.perf_counter()
            fatal = False
            calls_before = backend.calls_started
            try:
                # Calls are serial; no unmeasured retries or fallback to another model.
                last_call_started = time.perf_counter()
                response = backend.generate(call["prompt"], payload, call["seed"])
                row["request_ms"] = (
                    None if is_mock else round((time.perf_counter() - start) * 1000, 3)
                )
                row.update(
                    input_tokens=response["input_tokens"],
                    output_tokens=response["output_tokens"],
                    output_characters=len(response["text"]),
                    model_version=response.get("model_version"),
                    thinking_tokens=response.get("thinking_tokens"),
                    total_tokens=response.get("total_tokens"),
                    cached_input_tokens=response.get("cached_input_tokens"),
                )
                call["response"] = response
                if response["finish_reason"] != ("mock" if is_mock else "stop"):
                    row["validation_error"] = "NON_STOP_FINISH"
                    raise ValueError("Response truncated or did not stop normally")
                validation_started = time.perf_counter()
                validation_text, normalized = normalize_decision_output(response["text"])
                row["raw_json_valid"] = None if is_mock else not normalized
                row["format_normalized"] = None if is_mock else normalized
                if normalized:
                    call["validation_text"] = validation_text
                decision = validate_decision(validation_text, item)
                row["validation_ms"] = round((time.perf_counter() - validation_started) * 1000, 3)
                row.update(
                    contract_valid=True,
                    status=decision["analysis_status"],
                    source_citation_provided=None
                    if is_mock
                    else bool(decision["source_evidence_ids"]),
                    status_match=None
                    if is_mock
                    else decision["analysis_status"] in case["acceptable_statuses"],
                )
                call["validated_decision"] = decision
                row.update(measure_lengths(decision, arm.response_length))
                if is_mock:
                    for key in ["length_target_met"] + [
                        field + "_length_target_met" for field in EXPLANATION_FIELDS
                    ]:
                        row[key] = None
            except BackendFailure as exc:
                row["error"] = str(exc)
                fatal = (
                    True  # Preserve partial results; do not consume the quota in a failure loop.
                )
            except ValueError as exc:
                row["error"] = "INVALID_DECISION"
                row["validation_error"] = row["validation_error"] or getattr(
                    exc, "code", "INVALID_DECISION"
                )
                if not is_mock and row["validation_error"] == "INVALID_JSON":
                    row.update(raw_json_valid=False, format_normalized=False)
            row["provider_calls"] = backend.calls_started - calls_before
            if not is_mock and row["request_ms"] is None:
                row["request_ms"] = round((time.perf_counter() - start) * 1000, 3)
            if not is_mock:
                row["end_to_end_ms"] = round(
                    prepare_ms + (time.perf_counter() - arm_started) * 1000, 3
                )
            rows.append(row)
            save(output / "calls" / f"{index:05d}.json", {"row": row, **call})
            manifest.update(completed_rows=len(rows), provider_calls=backend.calls_started)
            save(output / "manifest.json", manifest)
            write_tables(output, rows, is_mock)
            print(
                json.dumps(
                    {
                        k: row[k]
                        for k in (
                            "case",
                            "repeat",
                            "arm",
                            "contract_valid",
                            "length_target_met",
                            "error",
                            "validation_error",
                        )
                    }
                ),
                flush=True,
            )
            if fatal:
                manifest["state"] = "stopped_on_backend_error"
                break
        else:
            manifest["state"] = "completed"
    except BackendFailure as exc:
        manifest["state"] = "stopped_on_backend_error"
        manifest["error"] = str(exc)
        raise
    finally:
        backend.close()
        manifest["provider_calls"] = backend.calls_started
        if manifest["state"] == "running":
            manifest["state"] = "interrupted"
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save(output / "manifest.json", manifest)
        write_tables(output, rows, is_mock)
    return 0 if manifest["state"] == "completed" and all(r["contract_valid"] for r in rows) else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("mock", "vllm", "gemini"), default="mock")
    parser.add_argument("--config", type=Path, help="Defaults to the selected backend's config")
    parser.add_argument("--dataset", type=Path, action="append")
    parser.add_argument("--limit", type=int, default=0, help="0 = all cases")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    config_path = args.config or ROOT / "config" / (
        "experiment.gemma4-api.json" if args.backend == "gemini" else "experiment.gemma4.json"
    )
    config_data = json.loads(config_path.read_text("utf-8"))
    if args.repeats is not None:
        config_data["repeats"] = args.repeats
    config = ExperimentConfig.model_validate(config_data)
    paths = args.dataset or [PUBLIC_PILOT]
    cases, datasets = load_cases(paths)
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    if args.limit:
        cases = cases[: args.limit]
    output = args.output or ROOT / "results" / (
        f"{args.backend}-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    )
    try:
        status = run_experiment(cases, datasets, config, output, backend_name=args.backend)
    except (ValueError, BackendFailure, FileExistsError) as exc:
        parser.exit(2, f"Experiment failed: {exc}\n")
    print(f"Results: {output.resolve()}", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
