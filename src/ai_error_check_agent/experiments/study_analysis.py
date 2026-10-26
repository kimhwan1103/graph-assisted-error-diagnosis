"""Deterministic summaries and two-reviewer, arm-name-blinded evaluation packets."""

import json
import math
import random
import statistics
from collections import defaultdict

from .design import digest
from .reproduce import sha
from .run import save
from .study_inputs import ARMS

RUBRIC = {
    "observation_correct": "0/0.5/1: incorrect / partial / correct selected-test observation. Whole-process failure is separate.",
    "mechanism_correct": "Failure only, 0/1: complete mechanism explains the causal code condition/data flow, consistent with patch/test AND supported by supplied evidence. Symptoms alone score 0. Null for passing controls.",
    "grounded": "0/0.5/1: unsupported / mixed / all material claims grounded in the supplied evidence.",
    "unsupported_causal_claim": "0/1: absent / present. Conditional checks are not established claims.",
}


def read(path):
    return json.loads(path.read_text("utf-8"))


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def immutable(item):
    return {k: v for k, v in item.items() if k not in {"ratings", "notes"}}


def bootstrap_clusters(differences, seed, samples):
    """Each value is a bug mean over repeats; repeated calls are not independent units."""
    if len(differences) < 2:
        return None
    values = [mean(v) for _, v in sorted(differences.items())]
    rng = random.Random(seed)
    boot = sorted(mean(rng.choices(values, k=len(values))) for _ in range(samples))
    return {
        "bug_count": len(values),
        "mean": mean(values),
        "ci95_percentile": [boot[int(0.025 * (samples - 1))], boot[int(0.975 * (samples - 1))]],
        "method": "paired bug-cluster bootstrap; exploratory interval; no power guarantee",
    }


def review_packets(output, protocol, rows, inputs):
    refs = {r["bug_id"]: r for r in protocol["reviewer_references"]["items"]}
    key, template = {}, []
    from .study import resolve

    reproduction = resolve(protocol["fingerprint"]["config"]["reproduction"])
    for row in rows:
        if row.get("is_mock") or not row.get("response"):
            continue
        slot = row["slot"]
        identifier = digest([sha(output / "protocol.json"), slot["id"], row["response"]["text"]])[
            :20
        ]
        item = {
            "review_id": identifier,
            "response": row["response"]["text"],
            "contract_valid": row["contract_valid"],
            "evidence": inputs[slot["case"]]["prepared"]["base"],
            "reference": {
                "selected_test_label": slot["label"],
                "root_reference": refs.get(slot["bug_id"]),
                "published_patch": (reproduction / slot["bug_id"] / "reference.patch").read_text(
                    "utf-8"
                ),
            },
            "ratings": {k: None for k in RUBRIC},
            "notes": "",
        }
        key[identifier] = {
            "slot_id": slot["id"],
            "arm": slot["arm"],
            "immutable_sha256": digest(immutable(item)),
        }
        template.append(item)
    for letter, seed in (("A", 4201), ("B", 4202)):
        path = output / f"review-{letter}.json"
        old = (
            read(path)
            if path.exists()
            else {"reviewer_id": "", "reviewer_type": "unassigned", "items": []}
        )
        previous = {i["review_id"]: i for i in old["items"]}
        if len(previous) != len(old["items"]) or not previous.keys() <= key.keys():
            raise ValueError("Unknown or duplicate review item")
        items = []
        for item in template:
            prior = previous.get(item["review_id"])
            if prior:
                if digest(immutable(prior)) != digest(immutable(item)):
                    raise ValueError("Immutable review evidence/response changed")
                item = prior
            items.append(item)
        random.Random(seed).shuffle(items)
        save(
            path,
            {
                "reviewer_id": old["reviewer_id"],
                "reviewer_type": old["reviewer_type"],
                "instructions": "Set reviewer_id and reviewer_type=human for an independent human reviewer. Edit only ratings/notes. Give A and B to different reviewers, not review-key.json. Length remains visible; full blinding is impossible. Patch is a reference, not automatic truth.",
                "rubric": RUBRIC,
                "items": items,
            },
        )
    save(
        output / "review-key.json",
        {
            "private_mapping_do_not_give_to_reviewers": key,
            "protocol_sha256": sha(output / "protocol.json"),
        },
    )
    return key


def collect_reviews(output, mapping):
    raters, scores = [], defaultdict(list)
    for letter in ("A", "B"):
        document = read(output / f"review-{letter}.json")
        name, kind = document["reviewer_id"].strip(), document["reviewer_type"]
        if kind not in {"unassigned", "human", "assistant"}:
            raise ValueError("Unknown reviewer type")
        completed = {}
        for item in document["items"]:
            ratings = item["ratings"]
            if set(ratings) != set(RUBRIC):
                raise ValueError("Unexpected rating fields")
            for field, value in ratings.items():
                allowed = (
                    {0, 1}
                    if field in {"mechanism_correct", "unsupported_causal_claim"}
                    else {0, 0.5, 1}
                )
                if value is not None and (type(value) not in (int, float) or value not in allowed):
                    raise ValueError("Invalid rating value")
            failure = item["reference"]["selected_test_label"] == "test_failure"
            if not failure and ratings["mechanism_correct"] is not None:
                raise ValueError("Passing controls must have null mechanism rating")
            required = set(RUBRIC) if failure else set(RUBRIC) - {"mechanism_correct"}
            ready = all(ratings[k] is not None for k in required)
            if ready:
                if not name or kind == "unassigned" or not item["notes"].strip():
                    raise ValueError(
                        "Completed ratings require reviewer identity/type and evidence notes"
                    )
                if not item["contract_valid"] and (
                    ratings["observation_correct"] != 0
                    or (failure and ratings["mechanism_correct"] != 0)
                ):
                    raise ValueError("Invalid outputs cannot receive operational diagnosis credit")
                slot = mapping[item["review_id"]]["slot_id"]
                completed[slot] = ratings
                scores[slot].append(ratings)
        raters.append({"id": name, "type": kind, "completed": completed})
    if raters[0]["id"] and raters[0]["id"] == raters[1]["id"]:
        raise ValueError("Two review files must represent different reviewers")
    paired = [
        (
            raters[0]["completed"][s]["mechanism_correct"],
            raters[1]["completed"][s]["mechanism_correct"],
        )
        for s in raters[0]["completed"].keys() & raters[1]["completed"].keys()
        if raters[0]["completed"][s]["mechanism_correct"] is not None
    ]
    agreement = None
    if paired:
        observed = mean([a == b for a, b in paired])
        pa, pb = mean([a for a, _ in paired]), mean([b for _, b in paired])
        chance = pa * pb + (1 - pa) * (1 - pb)
        agreement = {
            "rated_responses": len(paired),
            "raw_agreement": observed,
            "cohen_kappa": (observed - chance) / (1 - chance) if chance != 1 else None,
            "note": "Responses/repeats are not independent bugs; no agreement CI claimed",
        }
    combined = {
        slot: {k: mean([r[k] for r in ratings]) for k in RUBRIC}
        for slot, ratings in scores.items()
        if len(ratings) == 2
    }
    return combined, {
        "reviewers": [
            {"id": r["id"], "type": r["type"], "completed_responses": len(r["completed"])}
            for r in raters
        ],
        "independent_humans_declared": sum(r["type"] == "human" and bool(r["id"]) for r in raters),
        "agreement": agreement,
        "note": "Independence is reviewer-declared, not automatically verifiable",
    }


def summarize(output):
    from .study import read_attempt

    protocol, manifest = read(output / "protocol.json"), read(output / "manifest.json")
    if (output / "protocol.sha256").read_text("ascii").strip() != sha(output / "protocol.json"):
        raise ValueError("Frozen protocol was modified")
    config = protocol["fingerprint"]["config"]
    inputs = {v["case"]: v for p in sorted((output / "inputs").glob("*.json")) if (v := read(p))}
    for value in inputs.values():
        if value["content_sha256"] != digest(
            {k: v for k, v in value.items() if k != "content_sha256"}
        ):
            raise ValueError("Input checkpoint was modified")
    attempts = [read_attempt(p) for p in sorted((output / "attempts").glob("*.json"))]
    schedule = {s["id"]: s for s in protocol["schedule"]}
    selected = {}
    for row in attempts:
        if row["slot"] != schedule.get(row["slot"]["id"]):
            raise ValueError("Attempt does not match frozen schedule")
        if row["input_sha256"] != inputs[row["slot"]["case"]]["content_sha256"]:
            raise ValueError("Attempt refers to different evidence")
        previous = selected.get(row["slot"]["id"])
        if previous and previous["state"] == "returned":
            raise ValueError("A returned response must not be replaced by another attempt")
        selected[row["slot"]["id"]] = row
    is_mock = manifest["mode"] == "mock"
    mapping = review_packets(output, protocol, list(selected.values()), inputs)
    scores, review = collect_reviews(output, mapping)
    groups = {}
    refs = {r["bug_id"]: r for r in protocol["reviewer_references"]["items"]}
    for arm in ARMS:
        slots = [s for s in schedule.values() if s["arm"] == arm]
        rows = [selected[s["id"]] for s in slots if s["id"] in selected]
        valid = [r for r in rows if r["contract_valid"]]
        failures = [s for s in slots if s["label"] == "test_failure"]
        passes = [s for s in slots if s["label"] == "test_pass"]
        fully_reviewed = all(s["id"] in scores for s in failures)

        def count_status(items, status):
            return sum(
                selected.get(s["id"], {}).get("decision", {}).get("analysis_status") == status
                and selected[s["id"]]["contract_valid"]
                for s in items
            )

        tp, fp = count_status(failures, "diagnosed"), count_status(passes, "diagnosed")
        quality = (
            mean([scores[s["id"]]["mechanism_correct"] for s in failures])
            if fully_reviewed and failures and not is_mock
            else None
        )
        times = sorted(r["request_seconds"] for r in valid if r["request_seconds"] is not None)
        localization = []
        for slot in failures:
            ref = refs.get(slot["bug_id"])
            if ref is None:
                continue
            decision = selected.get(slot["id"], {}).get("decision", {})
            line = decision.get("predicted_root_line")
            file_hit = decision.get("predicted_root_path") == ref["root_file"]
            localization.append(
                (
                    file_hit,
                    file_hit
                    and type(line) is int
                    and ref["failed_function_span"][0] <= line <= ref["failed_function_span"][1],
                )
            )
        groups[arm] = {
            "planned": len(slots),
            "responses": sum(r["state"] == "returned" for r in rows),
            "valid": len(valid),
            "failed_slots": len(failures),
            "pass_slots": len(passes),
            "failure_detected": tp if not is_mock else None,
            "failure_recall_including_unavailable": tp / len(failures)
            if failures and not is_mock
            else None,
            "pass_false_positives": fp if not is_mock else None,
            "pass_unavailable_or_abstained": sum(
                not selected.get(s["id"], {}).get("contract_valid")
                or selected[s["id"]].get("decision", {}).get("analysis_status")
                == "insufficient_evidence"
                for s in passes
            )
            if not is_mock
            else None,
            "length_compliant": sum(r["length_target_met"] is True for r in valid)
            if not is_mock
            else None,
            "raw_json_responses": sum(not r.get("format_normalized", True) for r in valid)
            if not is_mock
            else None,
            "mean_input_tokens": mean([r["response"].get("input_tokens") for r in valid])
            if not is_mock
            else None,
            "mean_output_tokens": mean([r["response"].get("output_tokens") for r in valid])
            if not is_mock
            else None,
            "mean_total_tokens": mean([r["response"].get("total_tokens") for r in valid])
            if not is_mock
            else None,
            "mean_request_seconds": mean(times) if not is_mock else None,
            "median_request_seconds": statistics.median(times) if times and not is_mock else None,
            "p95_request_seconds_nearest_rank": times[math.ceil(0.95 * len(times)) - 1]
            if times and not is_mock
            else None,
            "failure_two_reviewer_completed": sum(s["id"] in scores for s in failures),
            "failure_mechanism_quality": quality,
            "failure_grounded": mean([scores[s["id"]]["grounded"] for s in failures])
            if fully_reviewed and not is_mock
            else None,
            "failure_unsupported_causal_rate": mean(
                [scores[s["id"]]["unsupported_causal_claim"] for s in failures]
            )
            if fully_reviewed and not is_mock
            else None,
            "root_location_reference_n": len(localization),
            "root_file_top1_match": sum(x[0] for x in localization)
            if localization and not is_mock
            else None,
            "root_function_top1_match": sum(x[1] for x in localization)
            if localization and not is_mock
            else None,
            "root_location_abstained_or_unavailable": sum(
                selected.get(s["id"], {}).get("decision", {}).get("predicted_root_path") is None
                for s in failures
            )
            if not is_mock
            else None,
            "failure_unavailable_or_abstained": sum(
                not selected.get(s["id"], {}).get("contract_valid")
                or selected[s["id"]].get("decision", {}).get("analysis_status")
                == "insufficient_evidence"
                for s in failures
            )
            if not is_mock
            else None,
        }
        failure_attempts = [
            r for r in attempts if r["slot"]["arm"] == arm and r["slot"]["label"] == "test_failure"
        ]
        correct = quality * len(failures) if quality is not None else 0
        usage_complete = all(
            r.get("response", {}).get("total_tokens") is not None for r in failure_attempts
        )
        groups[arm]["tokens_per_correct_mechanism"] = (
            sum(r["response"]["total_tokens"] for r in failure_attempts) / correct
            if correct and usage_complete
            else None
        )
        groups[arm]["request_seconds_per_correct_mechanism_including_retries"] = (
            sum(r["request_seconds"] or 0 for r in failure_attempts) / correct if correct else None
        )
    indexed = {(s["case"], s["repeat"], s["arm"]): s for s in schedule.values()}
    contrasts = {}
    for before, after in [(f"{r}_long", f"{r}_short") for r in ("raw", "text", "graph")] + [
        (f"{a}_{l}", f"{b}_{l}")
        for a, b in (("raw", "graph"), ("raw", "text"), ("text", "graph"))
        for l in ("short", "long")
    ]:
        for applicable_only in (False, True):
            pairs = [
                (s, indexed[s["case"], s["repeat"], after])
                for s in schedule.values()
                if s["arm"] == before
                and (
                    not applicable_only
                    or (s["label"] == "test_failure" and inputs[s["case"]]["prepared"]["edges"])
                )
            ]
            valid = [
                (selected[a["id"]], selected[b["id"]])
                for a, b in pairs
                if selected.get(a["id"], {}).get("contract_valid")
                and selected.get(b["id"], {}).get("contract_valid")
            ]
            failed = [(a, b) for a, b in pairs if a["label"] == "test_failure"]
            differences = defaultdict(list)
            if failed and all(a["id"] in scores and b["id"] in scores for a, b in failed):
                for a, b in failed:
                    differences[a["bug_id"]].append(
                        scores[b["id"]]["mechanism_correct"] - scores[a["id"]]["mechanism_correct"]
                    )
            effect = {
                "planned_pairs": len(pairs),
                "valid_response_pairs": len(valid),
                "failure_pairs": len(failed),
                "quality": bootstrap_clusters(
                    differences, config["backend"]["seed"], config["bootstrap_samples"]
                )
                if differences and not is_mock
                else None,
            }
            for field in ("output_tokens", "total_tokens", "request_seconds"):
                values = [
                    (
                        a.get(field) if field == "request_seconds" else a["response"].get(field),
                        b.get(field) if field == "request_seconds" else b["response"].get(field),
                    )
                    for a, b in valid
                ]
                available = [(a, b) for a, b in values if a is not None and b is not None]
                effect[field] = (
                    {
                        "paired_n": len(available),
                        "mean_difference": mean([b - a for a, b in available]),
                        "aggregate_reduction_fraction": 1
                        - sum(b for a, b in available) / sum(a for a, b in available)
                        if available and sum(a for a, b in available)
                        else None,
                    }
                    if not is_mock
                    else None
                )
            contrasts[
                f"{after}-minus-{before}" + (":applicable_failures" if applicable_only else "")
            ] = effect
    coverage = []
    for case, item in inputs.items():
        slot = next(s for s in schedule.values() if s["case"] == case)
        if slot["label"] != "test_failure":
            continue
        ref = refs.get(slot["bug_id"])
        sources = item["prepared"]["base"]["source_evidence"]
        hit = [
            s
            for s in sources
            if ref
            and s["path"] == ref["root_file"]
            and ref["failed_function_span"][0] <= s["line"] <= ref["failed_function_span"][1]
        ]
        candidate_ids = {e["to"] for e in item["prepared"]["edges"]}
        coverage.append(
            {
                "case": case,
                "graph_available": bool(candidate_ids),
                "defect_function_any_source": bool(hit) if ref else None,
                "defect_function_fraction": len(hit)
                / (ref["failed_function_span"][1] - ref["failed_function_span"][0] + 1)
                if ref
                else None,
                "defect_function_in_graph": any(s["id"] in candidate_ids for s in hit)
                if ref
                else None,
            }
        )
    all_received = len(selected) == len(schedule) and all(
        r["state"] == "returned" for r in selected.values()
    )
    adequate = (
        all_received
        and not is_mock
        and config["role"] == "evaluation"
        and review["independent_humans_declared"] == 2
        and protocol["independent_bugs"] >= config["minimum_independent_bugs"]
    )
    judgments = {}
    for name, comparison in (
        ("short_quality_noninferiority", "graph_short-minus-graph_long"),
        ("graph_quality_improvement", "graph_short-minus-raw_short"),
    ):
        quality = contrasts[comparison]["quality"]
        base = groups["graph_long"]["failure_mechanism_quality"]
        ready = (
            adequate
            and quality is not None
            and (
                name != "short_quality_noninferiority"
                or (base is not None and base >= config["minimum_long_quality"])
            )
        )
        threshold = (
            -config["noninferiority_margin"] if name == "short_quality_noninferiority" else 0
        )
        judgments[name] = (
            "criterion_met"
            if ready and quality["ci95_percentile"][0] > threshold
            else "criterion_not_met"
            if ready
            else "not_established"
        )
    result = {
        "version": protocol["version"],
        "role": config["role"],
        "mode": manifest["mode"],
        "state": manifest["state"],
        "all_responses_received": all_received,
        "planned": len(schedule),
        "returned": sum(r["state"] == "returned" for r in selected.values()),
        "attempts_including_retries": len(attempts),
        "unavailable_slots": [
            s["id"]
            for s in schedule.values()
            if selected.get(s["id"], {}).get("state") != "returned"
        ],
        "errors": [
            {
                "slot": r["slot"]["id"],
                "attempt": r["attempt"],
                "state": r["state"],
                "error": r.get("error"),
            }
            for r in attempts
            if r["state"] != "returned" or r.get("error")
        ],
        "reported_total_tokens": sum(
            r.get("response", {}).get("total_tokens") or 0 for r in attempts
        )
        if not is_mock
        else None,
        "groups": groups,
        "contrasts": contrasts,
        "coverage": coverage,
        "review": review,
        "primary_judgments": judgments,
        "limits": protocol["limits"]
        + [
            "Missing outputs and source coverage are reported; no response selection by quality",
            "P95 nearest rank is unstable with small samples",
            "Matched relations compare representation only; generated lexical relations are not resolved calls",
            "Two primary judgments use lower 2.5% bootstrap bounds; secondary comparisons are descriptive",
            "No repair success, billing amount, GPU memory, power or QAT identity measured",
        ],
    }
    save(output / "summary.json", result)
    lines = [
        "# 연구 실험 v2 결과",
        "",
        f"역할: {config['role']} / 실행: {manifest['mode']} / 응답: {result['returned']}/{result['planned']} / 시도: {len(attempts)}",
        "",
        "모의 실행의 토큰·시간·품질은 미측정입니다. 실패 상태 해석과 결함 원인 정확도를 구분합니다. 통과 대조군은 선정 테스트만 의미하며 별도 프로세스 오류는 inputs의 scope에 있습니다.",
        "",
        "| 조건 | 응답/계획 | 형식 통과 | 길이 충족 | 평균 출력 토큰 | 평균 요청 초 | 원인 품질(두 평가자) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]

    def fmt(v):
        return "미측정" if v is None else f"{v:.3f}"

    for arm, g in groups.items():
        lines.append(
            f"| {arm} | {g['responses']}/{g['planned']} | {g['valid']} | {g['length_compliant']} | {fmt(g['mean_output_tokens'])} | {fmt(g['mean_request_seconds'])} | {fmt(g['failure_mechanism_quality'])} |"
        )
    lines += [
        "",
        "## 주가설 판단",
        "",
        *[f"- {k}: {v}" for k, v in judgments.items()],
        "",
        "not_established는 효과가 없다는 결론이 아닙니다. 개발 데이터·미채점·부족한 독립 표본·미완료 실행이면 가설을 확정하지 않습니다. criterion_met도 사전 정의한 탐색적 기준을 만족한다는 뜻이며 외부 타당성이나 충분한 검정력을 보증하지 않습니다.",
        "",
        "## 사람 채점",
        "",
        "review-A.json과 review-B.json을 서로 다른 검토자에게 전달하세요. reviewer_id/reviewer_type과 ratings/notes만 작성합니다. review-key.json은 전달하지 않습니다. 채점 후 같은 명령의 report를 실행하면 집계·일치도·버그 단위 대응 신뢰구간을 계산합니다. 자동 LLM 심사나 추가 모델 호출은 없습니다.",
        "",
        "## 상세 파일",
        "",
        "summary.json: 누락·오류·토큰·P95·원인 위치·소스/그래프 범위·대응 비교. protocol.json: 실행 전 고정한 설정·코드·자료·프롬프트 해시. inputs/: 실제 제공 근거와 토큰 사전 검사. attempts/: 재시도를 포함한 원본 응답. 비용은 보고된 토큰만 합산하며 오류 호출의 토큰 사용량은 알 수 없습니다.",
    ]
    (output / "report.ko.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Results: {output / 'report.ko.md'}", flush=True)
    return result
