"""Export arm-label-blinded human review packets and join reviewed quality to cost."""

import argparse
import hashlib
import json
import random
from pathlib import Path

from .design import ARMS, digest
from .reproduce import build_dataset, sha
from .run import save

RATINGS = ("diagnosis_correct", "evidence_grounded", "unsupported_causal_claim")
RUBRIC = {
    "diagnosis_correct": "0/0.5/1: wrong or unjustified / partly correct / correct explanation of the observed test and source. Distinguish a reported frame from the defect; do not demand certainty unsupported by supplied evidence.",
    "evidence_grounded": "0/0.5/1: unsupported / partly supported / all material claims supported by cited original logs and source.",
    "unsupported_causal_claim": "0/1: absent / present. Compare causal or repair claims against patch/test reference and actual model-visible evidence.",
    "limits": "Patch is a reviewer reference, not an automatic causal label. Confirm a causal statement only if test and patch support it. This scores observed-test diagnosis, not general root-cause accuracy. Length remains visible even though arm labels are removed.",
}


def calls_from(run):
    manifest = json.loads((run / "manifest.json").read_text("utf-8"))
    calls = [json.loads(path.read_text("utf-8")) for path in sorted((run / "calls").glob("*.json"))]
    if (
        manifest["backend"] not in {"vllm", "gemini"}
        or manifest["state"] != "completed"
        or not calls
        or any(call["is_mock"] for call in calls)
        or len(calls) != manifest["completed_rows"]
    ):
        raise ValueError(
            "Review requires a completed live run; mock/partial results cannot be scored"
        )
    return manifest, calls


def immutable(item):
    return {
        key: value for key, value in item.items() if key not in {"ratings", "reviewer", "notes"}
    }


def export_review(run, reproduction, output):
    manifest, calls = calls_from(run)
    if any(c["row"].get("task_kind") != "reproduced_test_observation" for c in calls):
        raise ValueError(
            "Formal review needs reproduced evaluation pairs; development remains exploratory"
        )
    output.mkdir(parents=True, exist_ok=False)
    # Revalidate receipts and original stdout, not just a boolean in a run manifest.
    dataset = build_dataset(reproduction, output / "verified-dataset.json")
    cases = {case["id"]: case for case in dataset["cases"]}
    if set(manifest["case_ids"]) != set(cases):
        raise ValueError("Run does not match reproduced cases")
    manifest_hash = sha(run / "manifest.json")
    if not any(
        entry["sha256"] == sha(output / "verified-dataset.json") for entry in manifest["datasets"]
    ):
        raise ValueError("Run dataset differs from verified reproduction")
    random.Random(42).shuffle(calls)
    items, mapping = [], {}
    for call in calls:
        row = call["row"]
        case = cases[row["case"]]
        identifier = hashlib.sha256((manifest_hash + digest(row)).encode()).hexdigest()[:16]
        item = {
            "review_id": identifier,
            "response": call.get("response", {}).get("text"),
            "contract_valid": row["contract_valid"],
            "evidence": {
                key: value for key, value in call["model_input"].items() if key != "graph_hints"
            },
            "reference": {
                "observed_test_label": case["gold"]["label"],
                "receipt_sha256": case["provenance"]["receipt_sha256"],
                "production_patch": (reproduction / case["bug_id"] / "reference.patch").read_text(
                    "utf-8"
                ),
                "review_scope": case["provenance"]["scope"],
            },
            "ratings": {field: None for field in RATINGS},
            "reviewer": "",
            "notes": "",
        }
        items.append(item)
        mapping[identifier] = {
            "case": row["case"],
            "repeat": row["repeat"],
            "arm": row["arm"],
            "response_sha256": digest(item["response"]),
            "immutable_sha256": digest(immutable(item)),
        }
    save(output / "review.json", {"rubric": RUBRIC, "items": items})
    save(output / "key.json", {"run_manifest_sha256": manifest_hash, "mapping": mapping})
    return len(items)


def score_review(run, packet):
    _, calls = calls_from(run)
    key = json.loads((packet / "key.json").read_text("utf-8"))
    review = json.loads((packet / "review.json").read_text("utf-8"))
    if key["run_manifest_sha256"] != sha(run / "manifest.json"):
        raise ValueError("Review belongs to a different run")
    indexed = {(c["row"]["case"], c["row"]["repeat"], c["row"]["arm"]): c for c in calls}
    ids = [item["review_id"] for item in review["items"]]
    if len(ids) != len(set(ids)) or set(ids) != set(key["mapping"]):
        raise ValueError("Missing or duplicate review items")
    groups = {arm.name: [] for arm in ARMS}
    for item in review["items"]:
        reference = key["mapping"][item["review_id"]]
        call = indexed[(reference["case"], reference["repeat"], reference["arm"])]
        if (
            digest(immutable(item)) != reference["immutable_sha256"]
            or digest(call.get("response", {}).get("text")) != reference["response_sha256"]
        ):
            raise ValueError("Review evidence or response changed")
        ratings = item["ratings"]
        if set(ratings) != set(RATINGS):
            raise ValueError("Unexpected review criteria")
        complete = all(value is not None for value in ratings.values())
        if any(
            value is not None and (type(value) not in (int, float) or value not in {0, 0.5, 1})
            for value in ratings.values()
        ):
            raise ValueError("Ratings must be 0, 0.5, 1 or null")
        if ratings["unsupported_causal_claim"] not in (None, 0, 1):
            raise ValueError("Unsupported causal claim is binary")
        if complete and (not item["reviewer"].strip() or not item["notes"].strip()):
            raise ValueError("Completed ratings need reviewer identity and evidence notes")
        if complete and not call["row"]["contract_valid"] and ratings["diagnosis_correct"] != 0:
            raise ValueError("Invalid response must score zero in all-call diagnosis quality")
        groups[reference["arm"]].append((ratings if complete else None, call["row"]))
    report = {
        "scope": "Human-reviewed selected-test diagnosis, not general causal accuracy",
        "by_arm": {},
    }
    for arm, records in groups.items():
        complete = bool(records) and all(rating is not None for rating, _ in records)
        group = {"n": len(records), "reviewed": sum(rating is not None for rating, _ in records)}
        for criterion in RATINGS:
            group[criterion] = (
                sum(r[criterion] for r, _ in records) / len(records) if complete else None
            )
        for metric in ("request_ms", "end_to_end_ms", "input_tokens", "output_tokens"):
            values = [row[metric] for _, row in records if row.get(metric) is not None]
            group["mean_" + metric] = sum(values) / len(values) if values else None
        report["by_arm"][arm] = group
    # Within-case/repeat differences retain pairing. No significance from repeated calls.
    reviewed = {}
    for arm, records in groups.items():
        for rating, row in records:
            reviewed[(row["case"], row["repeat"], arm)] = rating
    contrasts = {}
    for before, after in (
        ("raw_long", "graph_long"),
        ("raw_short", "graph_short"),
        ("raw_long", "raw_short"),
        ("graph_long", "graph_short"),
    ):
        pairs = [
            (rating, reviewed.get((case, repeat, after)))
            for (case, repeat, arm), rating in reviewed.items()
            if arm == before
        ]
        complete = bool(pairs) and all(a is not None and b is not None for a, b in pairs)
        contrasts[after + "_minus_" + before] = {
            "paired_n": len(pairs),
            "diagnosis_quality_difference": sum(
                b["diagnosis_correct"] - a["diagnosis_correct"] for a, b in pairs
            )
            / len(pairs)
            if complete
            else None,
        }
    report["paired_contrasts"] = contrasts
    save(packet / "reviewed-summary.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "score"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--reproduction", type=Path)
    args = parser.parse_args(argv)
    if args.action == "export" and args.reproduction is None:
        parser.error("export requires --reproduction")
    try:
        result = (
            export_review(args.run, args.reproduction, args.packet)
            if args.action == "export"
            else score_review(args.run, args.packet)
        )
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Review stopped: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
