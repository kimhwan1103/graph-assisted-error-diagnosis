"""Offline, fixed-route sensitivity analysis of saved v4 responses; no provider calls."""

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jsonschema import Draft202012Validator

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.experiments.design import digest
from ai_error_check_agent.experiments.study_v4_arms import bound_expand
from ai_error_check_agent.graph_compact import expand
from ai_error_check_agent.source_analysis import parse_stage
from ai_error_check_agent.source_contracts import SourceFindings, SourceRequest
from ai_error_check_agent.validation import strict_json

POLICY = {
    "version": "limitations-text-unwrapping.v1",
    "operation": "Only top-level limitations list items with exactly one key 'text' and a string value become that exact string.",
    "unchanged": ["claims", "evidence IDs", "source lines", "diagnosis state", "missing information", "plans", "validator", "recorded call route"],
    "scope": "Post-hoc validation of recorded responses; not a new live run or an estimate of changed routing, tokens, or latency.",
}
CRITERIA = ["correct_diagnosis", "grounded_claims", "appropriate_uncertainty", "action_applicability", "safe_verification_and_rollback"]


def read(path):
    return strict_json(Path(path).read_text(encoding="utf-8-sig"))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def hashes(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def decode(text):
    stripped = text.strip()
    if stripped.startswith("```json\n") and stripped.endswith("\n```"):
        text = stripped[8:-4]  # Exactly the recorded production normalization.
    return strict_json(text)


def normalize(value):
    value = copy.deepcopy(value)
    edits = []
    if isinstance(value, dict) and isinstance(value.get("limitations"), list):
        for index, item in enumerate(value["limitations"]):
            if isinstance(item, dict) and set(item) == {"text"} and isinstance(item["text"], str):
                value["limitations"][index] = item["text"]
                edits.append({"path": f"/limitations/{index}", "before": item, "after": item["text"]})
    return value, edits


def validate(value, call, public):
    data = call["input"]
    log_ids = {x["id"] for x in public["evidence"]}
    packet_ids = {occ["id"] for x in data["logs"] for occ in x.get("occurrences", [x])}
    assert log_ids == packet_ids, "Recorded log evidence changed"
    bundle = SimpleNamespace(context=data["context"], evidence_ids=log_ids,
                             lines=[SimpleNamespace(**x) for x in public["evidence"]])
    source = data.get("source_evidence", public["source_analysis"]["evidence"])
    assert source == public["source_analysis"]["evidence"], "Recorded source evidence changed"
    issues = [{"path": list(e.absolute_path), "validator": e.validator, "message": e.message}
              for e in Draft202012Validator(call["schema"]).iter_errors(value)]
    props = call["schema"]["properties"]
    text = json.dumps(value, ensure_ascii=False)
    try:
        if "decision" in props:
            text = (expand if "candidate_id" in props else bound_expand)(text, data)
            name, model = "source_findings", SourceFindings
        elif "source_findings" in props:
            name, model = "source_findings", SourceFindings
        elif "source_request" in props:
            name, model = "source_request", SourceRequest
        else:
            raise AssertionError("Unsupported recorded stage")
        parsed = parse_stage(text, bundle, name, model, source_lines=source)
        extra = parsed.pop(name)
        return {"valid": True, "error": None, "schema_errors": issues,
                "analysis": parsed, "source_findings": extra.get("findings", []), "stage": name}
    except DiagnosisError as exc:
        return {"valid": False, "error": exc.as_dict(), "schema_errors": issues,
                "analysis": None, "source_findings": [], "stage": None}


def run(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("Output must be a separate sibling directory")
    if destination.exists():
        raise ValueError("Output already exists; preserve it and choose a new directory")
    before = hashes(source)
    frozen = read(source / "frozen.json")
    for relative, expected in frozen["source_manifest"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, relative
    packet = read(source / "reviews/primary-template.json")
    private_map = read(source / "reviews/private-map.json")
    inputs = {private_map[item["review_id"]]["cell_id"]: item for item in packet["items"]}
    assert packet["study_sha256"] == digest(frozen)
    rows, review_items = [], []
    for slot in frozen["schedule"]:
        directory = source / "cells" / slot["cell_id"]
        original = read(directory / "result.json")
        assert read(directory / "state.json")["result_sha256"] == digest(original)
        public = original["result"]
        call_paths = sorted((directory / "calls").glob("*.json"))
        last = read(call_paths[-1])
        assert last["status"] == "completed" and last["raw"]["finish_reason"] == "stop"
        value = decode(last["raw"]["text"])
        normalized, edits = normalize(value)
        baseline = validate(value, last, public)
        after = validate(normalized, last, public)
        old_success = public["job_status"] == "succeeded"
        assert baseline["valid"] == old_success, (slot, "baseline status mismatch")
        if old_success:
            assert baseline["analysis"] == public["analysis"], (slot, "baseline analysis mismatch")
            assert baseline["source_findings"] == public["source_analysis"]["findings"]
        else:
            assert baseline["error"]["code"] == public["error"]["code"], (slot, baseline["error"])
        row = {**slot, "review_id": inputs[slot["cell_id"]]["review_id"],
               "original_result_sha256": digest(original),
               "last_call": call_paths[-1].relative_to(source).as_posix(),
               "last_call_sha256": hashlib.sha256(call_paths[-1].read_bytes()).hexdigest(),
               "original_success": old_success, "normalized_success": after["valid"],
               "changed": bool(edits), "changes": edits,
               "baseline": baseline, "normalized": after,
               "normalized_model_response": normalized}
        rows.append(row)
        item = copy.deepcopy(inputs[slot["cell_id"]])
        item["response"] = {"job_status": "succeeded" if after["valid"] else "failed",
                            "analysis": after["analysis"], "evidence": public["evidence"],
                            "error": after["error"]}
        item["source_findings"] = after["source_findings"]
        item["scores"] = dict.fromkeys(CRITERIA)
        item["rationale"] = ""
        # Include observations actually visible to this model. The original exporter
        # omitted archive observations; do not penalize claims supported by these facts.
        item["model_visible_context"] = {k: last["input"][k] for k in
            ("context", "input_limitations", "source_limitations", "archive_observations", "limitations")
            if k in last["input"]}
        item["response_sha256"] = digest({k: item[k] for k in
            ("response", "source_evidence", "source_findings", "model_visible_context")})
        review_items.append(item)
    assert before == hashes(source), "Original run changed during revalidation"
    assessment_hash = digest({"policy": POLICY, "source_manifest": before,
                              "rows": [{k:r[k] for k in ("cell_id", "original_result_sha256", "last_call_sha256", "normalized")} for r in rows]})
    summary = {
        "version": "study-v4-offline-normalization.v1", "policy": POLICY,
        "study_sha256": digest(frozen), "assessment_sha256": assessment_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "original_directory": str(source), "source_files_unchanged": True,
        "baseline_reproduced_cells": len(rows), "total_cells": len(rows),
        "original_valid": sum(r["original_success"] for r in rows),
        "normalized_valid": sum(r["normalized_success"] for r in rows),
        "newly_valid": sum(r["normalized_success"] and not r["original_success"] for r in rows),
        "changed_cells": sum(r["changed"] for r in rows),
        "remaining_failures": dict(Counter(r["normalized"]["error"]["code"] for r in rows if not r["normalized_success"])),
        "by_arm": {arm:{"total":sum(r["arm"]==arm for r in rows),
                         "before":sum(r["arm"]==arm and r["original_success"] for r in rows),
                         "after":sum(r["arm"]==arm and r["normalized_success"] for r in rows)}
                   for arm in frozen["config"]["arms"]},
        "new_api_calls": 0, "new_token_usage": 0,
        "timing_note": "No changed-system live latency or token saving is estimated; recorded routes and outputs are fixed.",
        "human_ratings": 0, "llm_ratings": 0, "paper_ready": False,
    }
    write(destination / "revalidation.json", summary)
    write(destination / "original-files.sha256.json", before)
    for row in rows:
        write(destination / "cells" / f"{row['cell_id']}.json", row)
    review_header = {"version":"study-v4-posthoc-review-packet.v1", "study_sha256":digest(frozen),
                     "assessment_sha256":assessment_hash, "normalization_policy":POLICY,
                     "note":"Post-hoc fixed-route normalized responses; LLM auxiliary review only; not human ratings."}
    write(destination / "reviews" / "all-items.json", {**review_header, "items":review_items})
    valid_items = [x for x in review_items if x["response"]["job_status"] == "succeeded"]
    for offset in range(0, len(valid_items), 3):
        batch_id = f"batch-{offset//3+1:03}"
        write(destination / "reviews" / "batches" / f"{batch_id}.json",
              {**review_header, "batch_id":batch_id, "items":valid_items[offset:offset+3]})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.source, args.output)
