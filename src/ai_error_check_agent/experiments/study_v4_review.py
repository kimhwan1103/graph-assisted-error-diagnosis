"""Blinded review of FINAL public responses; no surrogate automatic quality scores."""

import random
import statistics
from collections import defaultdict

from .design import digest
from .study import exclusive
from .study_v4_statistics import agreement as interrater_agreement
from .study_v4_statistics import factorial, noninferiority

CRITERIA = [
    "correct_diagnosis",
    "grounded_claims",
    "appropriate_uncertainty",
    "action_applicability",
    "safe_verification_and_rollback",
]
COMPARISONS = [
    ("adaptive", "graph_compact"),
    ("F", "RF"),
    ("C", "RC"),
    ("F", "C"),
    ("RF", "RC"),
    ("RC", "procedural_RC"),
    ("graph_compact", "no_shacl"),
    ("graph_compact", "no_rules"),
]


def cells(output):
    from .study_v4 import read

    return [
        read(path)
        for path in sorted((output / "cells").glob("*/result.json"))
        if (path.parent / "state.json").exists()
        and read(path.parent / "state.json")["status"] == "completed"
    ]


def export_reviews(output):
    from .study_v4 import read, save

    with exclusive(output):
        frozen = read(output / "frozen.json")
        results = cells(output)
        rng = random.Random(frozen["config"]["backend"]["seed"])
        rng.shuffle(results)
        source = {c["id"]: c for c in frozen["cases"]}
        packages, mapping = [], {}
        for result in results:
            opaque = digest([digest(frozen), result["cell_id"], "review-v4"])[:24]
            mapping[opaque] = {"cell_id": result["cell_id"], "result_sha256": digest(result)}
            public = result["result"]
            case = source[result["case_id"]]
            packages.append(
                {
                    "review_id": opaque,
                    "case_id": case["id"],
                    "logs": case["payload"],
                    "source_files": case["source_files"],
                    "reference": frozen["references"]["cases"][case["id"]],
                    "response": {
                        k: public.get(k) for k in ("job_status", "analysis", "evidence", "error")
                    },
                    "source_evidence": (public.get("source_analysis") or {}).get("evidence", []),
                    "source_findings": (public.get("source_analysis") or {}).get("findings", []),
                    "scores": {k: None for k in CRITERIA},
                    "rationale": "",
                }
            )
        save(output / "reviews/private-map.json", mapping)
        template = {
            "study_sha256": digest(frozen),
            "reviewer_id": "",
            "reviewed_at": "",
            "role": "primary",
            "note": "각 기준에 true/false와 근거를 기록. 전체 최종 응답을 채점. 모의 응답은 연구 정확도로 집계하지 않음.",
            "items": packages,
        }
        for filename, document in (
            ("primary-template.json", template),
            (
                "overlap-template.json",
                {
                    **template,
                    "role": "overlap",
                    "items": packages[: max(1, (len(packages) + 3) // 4)],
                },
            ),
        ):
            path = output / "reviews" / filename
            if path.exists() and read(path) != document:
                raise ValueError(
                    "Existing review template differs; preserve reviewer edits and use a copy for submission"
                )
            save(path, document)
    print(f"Review templates: {output / 'reviews'}")


def import_reviews(output, path):
    from .study_v4 import read, save

    with exclusive(output):
        submission = read(path)
        frozen = read(output / "frozen.json")
        mapping = read(output / "reviews/private-map.json")
        if submission.get("study_sha256") != digest(frozen) or submission.get("role") not in {
            "primary",
            "overlap",
        }:
            raise ValueError("Review belongs to a different study or role")
        reviewer = submission.get("reviewer_id", "").strip()
        if not reviewer or not submission.get("reviewed_at"):
            raise ValueError("Reviewer identity and review date are required")
        accepted_path = output / "reviews/accepted.json"
        accepted = read(accepted_path) if accepted_path.exists() else []
        existing_role = {row["role"] for row in accepted if row["reviewer_id"] == reviewer}
        if existing_role and submission["role"] not in existing_role:
            raise ValueError("Overlap review must come from a different person")
        normalized = []
        seen = set()
        for item in submission["items"]:
            rid = item["review_id"]
            if rid in seen or rid not in mapping:
                raise ValueError("Unknown or duplicate review ID")
            seen.add(rid)
            link = mapping[rid]
            result = read(output / "cells" / link["cell_id"] / "result.json")
            if digest(result) != link["result_sha256"]:
                raise ValueError("Final response changed after review export")
            if (
                set(item["scores"]) != set(CRITERIA)
                or any(type(v) is not bool for v in item["scores"].values())
                or not item.get("rationale", "").strip()
            ):
                raise ValueError(
                    "Every submitted item requires all five boolean ratings and rationale"
                )
            normalized.append(
                {
                    "cell_id": link["cell_id"],
                    "reviewer_id": reviewer,
                    "role": submission["role"],
                    "reviewed_at": submission["reviewed_at"],
                    "scores": item["scores"],
                    "rationale": item["rationale"],
                }
            )
        if not normalized:
            raise ValueError("Empty review submission")
        for row in normalized:
            conflicts = [
                r for r in accepted if r["cell_id"] == row["cell_id"] and r["role"] == row["role"]
            ]
            if conflicts and conflicts != [row]:
                raise ValueError(
                    "Existing accepted rating differs; do not silently overwrite or adjudicate"
                )
            if not conflicts:
                accepted.append(row)
        save(accepted_path, accepted)
    report(output)


def paired(rows, before, after, metric):
    groups = defaultdict(list)
    by_pair = defaultdict(dict)
    for row in rows:
        by_pair[(row["case_id"], row["repeat"])][row["arm"]] = row
    for arms in by_pair.values():
        a, b = arms.get(before), arms.get(after)
        if not a or not b or a.get(metric) is None or b.get(metric) is None:
            continue
        if before in {"F", "RF", "C", "RC", "procedural_RC"} and not (
            a["factorial_applied"] and b["factorial_applied"]
        ):
            continue
        groups[a["cluster_id"]].append(b[metric] - a[metric])
    means = [statistics.mean(v) for v in groups.values()]
    interval = None
    if len(means) >= 2:
        rng = random.Random(4204)
        boot = sorted(statistics.mean(rng.choices(means, k=len(means))) for _ in range(2000))
        interval = [boot[49], boot[1949]]
    return {
        "before": before,
        "after": after,
        "metric": metric,
        "paired_cells": sum(map(len, groups.values())),
        "clusters": len(means),
        "mean_cluster_delta": statistics.mean(means) if means else None,
        "cluster_bootstrap_95pct": interval,
        "method": "after-minus-before; equal-lineage-weighted; descriptive, not a confirmatory NI test",
    }


def report(output):
    from .study_v4 import read, save

    frozen = read(output / "frozen.json")
    live = frozen["live"]
    accepted_path = output / "reviews/accepted.json"
    ratings = read(accepted_path) if accepted_path.exists() else []
    primary = {r["cell_id"]: r for r in ratings if r["role"] == "primary"}
    case_map = {c["id"]: c for c in frozen["cases"]}
    rows = []
    for cell in cells(output):
        public = cell["result"]
        calls = [
            read(p) for p in sorted((output / "cells" / cell["cell_id"] / "calls").glob("*.json"))
        ]

        def total(key, calls=calls):
            values = [r.get("raw", {}).get(key) for r in calls]
            return sum(values) if live and values and all(type(v) is int for v in values) else None

        successful = cell["http_status"] == 200 and public.get("job_status") == "succeeded"
        review = primary.get(cell["cell_id"])
        compact_attempted = any("decision" in c["schema"].get("properties", {}) for c in calls)
        model_fallback = compact_attempted and len(calls) > 1
        full_by_design = cell["arm"] in {"adaptive", "F", "RF"}
        graph_expected = cell["arm"] not in {"adaptive", "F", "C"}
        graph_status = (cell["graph_report"] or {}).get("status", "not_visited")
        rows.append(
            {
                **{
                    k: cell[k] for k in ("cell_id", "case_id", "arm", "repeat", "factorial_applied")
                },
                "cluster_id": case_map[cell["case_id"]]["cluster_id"],
                "success": successful,
                "human_reviewed": review is not None,
                "usable_final": (
                    int(successful and all(review["scores"].values()))
                    if review
                    else (0 if not successful else None)
                )
                if live
                else None,
                "input_tokens": total("input_tokens"),
                "output_tokens": total("output_tokens"),
                "thinking_tokens": total("thinking_tokens"),
                "provider_total_tokens": total("total_tokens"),
                "wall_ms": cell["wall_ms"] if live else None,
                "model_network_ms": sum(c.get("network_ms") or 0 for c in calls) if live else None,
                "queue_ms": sum(c.get("queue_ms") or 0 for c in calls) if live else None,
                "pipeline_ms_excluding_pacing": max(
                    0, cell["wall_ms"] - sum(c.get("queue_ms") or 0 for c in calls)
                )
                if live
                else None,
                "generation_calls": sum(bool(c.get("dispatched")) for c in calls),
                "compact_attempted": compact_attempted,
                "model_fallback": model_fallback,
                "graph_unavailable": graph_expected and graph_status != "ok",
                "fallback": not full_by_design and (not compact_attempted or model_fallback),
                "final_response_bytes": len(
                    __import__("json").dumps(public.get("analysis"), ensure_ascii=False).encode()
                ),
                "graph_status": graph_status,
                "graph_scores": cell["graph_scores"],
                "error": public.get("error"),
            }
        )
    summary = {}
    for arm in frozen["config"]["arms"]:
        selected = [r for r in rows if r["arm"] == arm]
        scored = [r["usable_final"] for r in selected if r["usable_final"] is not None]
        summary[arm] = {
            "completed": len(selected),
            "successful_contracts": sum(r["success"] for r in selected),
            "factorial_eligible": sum(r["factorial_applied"] for r in selected),
            "fallbacks": sum(r["fallback"] for r in selected),
            "model_fallbacks": sum(r["model_fallback"] for r in selected),
            "graph_unavailable_cells": sum(r["graph_unavailable"] for r in selected),
            "quality_scored": len(scored),
            "usable_final_rate_among_scored": statistics.mean(scored) if scored else None,
            "usable_final_rate_all": statistics.mean(scored)
            if scored and len(scored) == len(selected)
            else None,
        }
    overlap = [r for r in ratings if r["role"] == "overlap" and r["cell_id"] in primary]
    agreement = {
        key: (
            statistics.mean(
                r["scores"][key] == primary[r["cell_id"]]["scores"][key] for r in overlap
            )
            if overlap
            else None
        )
        for key in CRITERIA
    }
    document = {
        "version": "study-v4-report.v1",
        "real_model": live,
        "role": frozen["config"]["role"],
        "planned_cells": len(frozen["schedule"]),
        "completed_cells": len(rows),
        "lineage_clusters": len({c["cluster_id"] for c in frozen["cases"]}),
        "paper_ready": False,
        "total_provider_calls_all_states": sum(
            bool(read(p).get("dispatched")) for p in (output / "cells").glob("*/calls/*.json")
        ),
        "unfinished_or_excluded_cells": [
            read(p)
            for p in (output / "cells").glob("*/state.json")
            if read(p)["status"] != "completed"
        ],
        "interpretation": "Development and incomplete review results cannot establish general accuracy or non-inferiority. Mock outputs have no model quality/token/latency estimates.",
        "summary": summary,
        "factorial": [
            factorial(rows, metric)
            for metric in ("usable_final", "output_tokens", "pipeline_ms_excluding_pacing")
        ],
        "noninferiority": noninferiority(rows, frozen),
        "paired": [
            paired(rows, a, b, m)
            for a, b in COMPARISONS
            for m in (
                "usable_final",
                "output_tokens",
                "model_network_ms",
                "pipeline_ms_excluding_pacing",
            )
            if a in summary and b in summary
        ],
        "review": {
            "primary_ratings": len(primary),
            "overlap_ratings": len(overlap),
            "criterion_agreement": agreement,
            "interrater": interrater_agreement(overlap, primary, CRITERIA),
            "disagreements": [
                r["cell_id"] for r in overlap if r["scores"] != primary[r["cell_id"]]["scores"]
            ],
            "policy": "One primary expert for all outputs; optional independent overlap; disagreements remain visible.",
        },
        "rows": rows,
    }
    save(output / "report.json", document)
    lines = [
        "# v4 experiment report",
        "",
        f"Mode: {'live' if live else 'MOCK (no model quality/tokens/latency measurements)'}",
        f"Completed cells: {len(rows)}/{len(frozen['schedule'])}; lineage clusters: {document['lineage_clusters']}",
        f"Primary ratings: {len(primary)}; overlap ratings: {len(overlap)}",
        "",
        "| Arm | Completed | Valid contract | Fallback | Human quality scored |",
        "|---|---:|---:|---:|---:|",
    ]
    for arm, s in summary.items():
        lines.append(
            f"| {arm} | {s['completed']} | {s['successful_contracts']} | {s['fallbacks']} | {s['quality_scored']} |"
        )
    lines.extend(
        [
            "",
            "Valid contract does not imply a correct diagnosis. See report.json for paired differences and graph fact/reference/candidate scores.",
            "The bundled cases share one project lineage. Confidence intervals are suppressed for fewer than two clusters.",
            "F/RF/C/RC mechanism contrasts include only cells in which the common source-stage intervention was applied. Unsupported cases remain in whole-pipeline accounting.",
        ]
    )
    (output / "report.md").write_text("\n".join(lines) + "\n", "utf-8")
    print(f"Report: {output / 'report.md'}")
    return document
