"""Fixed normalization, preserving every scheduled cell including unavailable outputs."""

import argparse
import copy
from pathlib import Path

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
from revalidate_study_v4 import POLICY, decode, normalize, validate


def assess_cell(source, slot):
    state, result = source_cell(source, slot)
    public = (result or {}).get("result") or {}
    analysis = public.get("analysis")
    original = {
        "valid": state["status"] == "completed"
        and public.get("job_status") == "succeeded"
        and analysis is not None,
        "analysis": analysis,
        "error": public.get("error"),
        "source_findings": (public.get("source_analysis") or {}).get("findings", []),
    }
    row = {
        **slot,
        "source_state": state["status"],
        "original_result_sha256": digest(result) if result is not None else None,
        "original": original,
        "normalized": copy.deepcopy(original),
        "normalization_status": "unavailable",
        "normalization_reason": None,
        "changes": [],
        "changed": False,
        "last_call": None,
        "last_call_sha256": None,
        "normalized_model_response": None,
    }
    if state["status"] != "completed":
        row["normalization_reason"] = "source_" + state["status"]
        return row
    paths = sorted((source / "cells" / slot["cell_id"] / "calls").glob("*.json"))
    if not paths:
        row["normalization_reason"] = "no_saved_call"
        return row
    path = paths[-1]
    call = read(path)
    row.update(last_call=path.relative_to(source).as_posix(), last_call_sha256=sha(path))
    if call.get("status") != "completed" or (call.get("raw") or {}).get("finish_reason") != "stop":
        row["normalization_reason"] = "last_call_not_completed_stop"
        return row
    try:
        raw = decode(call["raw"]["text"])
    except (ValueError, TypeError):
        row["normalization_reason"] = "non_json_response"
        return row
    if not any(
        k in call.get("schema", {}).get("properties", {})
        for k in ("decision", "source_findings", "source_request")
    ):
        row["normalization_reason"] = "unsupported_final_stage"
        return row
    try:
        baseline = validate(raw, call, public)
        normalized, edits = normalize(raw)
        after = validate(normalized, call, public)
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        row["normalization_reason"] = "validator_unavailable:" + type(exc).__name__ + ":" + str(exc)
        return row
    # A replay discrepancy is an evaluation issue, never a reason to invent a repaired response.
    if baseline["valid"] != original["valid"] or (
        original["valid"] and baseline["analysis"] != original["analysis"]
    ):
        row["normalization_reason"] = "baseline_replay_mismatch"
        return row
    row.update(
        normalization_status="available",
        normalization_reason=None,
        normalized=after,
        normalized_model_response=normalized,
        changed=bool(edits),
        changes=edits,
    )
    return row


def run(source, output, root):
    fresh(output)
    separate(output, source)
    with readonly_run_lock(source) as lock:
        frozen = load_source(source, root)
        before = hashes(source, lock)
        rows = []
        for slot in frozen["schedule"]:
            row = assess_cell(source, slot)
            rows.append(row)
            write(output / "cells" / (slot["cell_id"] + ".json"), row)
        if before != hashes(source, lock):
            raise ValueError("Source changed during assessment")
        write(
            output / "revalidation.json",
            {
                "version": "cross-program-normalization.v1",
                "policy_version": POLICY["version"],
                "policy_operation": POLICY["operation"],
                "policy_timing": "fixed before live generation",
                "study_sha256": digest(frozen),
                "total_scheduled": len(rows),
                "available": sum(r["normalization_status"] == "available" for r in rows),
                "original_valid": sum(r["original"]["valid"] for r in rows),
                "normalized_valid": sum(r["normalized"]["valid"] for r in rows),
                "changed": sum(r["changed"] for r in rows),
                "new_model_calls": 0,
                "human_ratings": 0,
                "unavailable_policy": "unchanged saved original assessment retained, eligibility and reason explicit; incomplete stays unobserved",
                "source_unchanged": True,
                "source_files_sha256": before,
            },
        )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--root", type=Path, default=ROOT / "evaluation/cross-program-v4")
    a = p.parse_args()
    run(a.source.resolve(), a.output.resolve(), a.root.resolve())


if __name__ == "__main__":
    main()
