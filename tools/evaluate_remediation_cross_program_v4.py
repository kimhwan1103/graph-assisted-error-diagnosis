"""Evaluate the first saved COPY plan against registered cross-program fixtures."""

import argparse
from collections import Counter
from pathlib import Path

from cross_program_common_v4 import (
    REQUIRED,
    ROOT,
    digest,
    fresh,
    hashes,
    load_source,
    read,
    readonly_run_lock,
    separate,
    sha,
    write,
)
from evaluate_remediation_v4 import classify, visible_inputs
from remediation_adapter_v4 import POLICY, PlanError, adapt
from remediation_cross_program_docker_v4 import VERSION, CrossReplay, EvaluationError
from revalidate_cross_program_v4 import assess_cell


def validate_bound(root, bound, frozen):
    spec = read(root / "remediation-spec.json")
    expected = {
        **spec,
        "study_sha256": digest(frozen),
        "freeze_sha256": sha(root / "freeze-manifest.json"),
        "spec_sha256": sha(root / "remediation-spec.json"),
    }
    if bound != expected or bound["engine"] != VERSION:
        raise ValueError("Unregistered or changed case/test manifest")
    for case in frozen["cases"]:
        entries = [s for s in spec["cases"] if s["case_id"] == case["id"]]
        if len(entries) != 1 or entries[0]["case_sha256"] != digest(case):
            raise ValueError("Case registry digest mismatch")


def unresolved(row, reason):
    row.update(
        evaluation_complete=False,
        complete_restoration=None,
        outcome="evaluation_error",
        evaluation_error=reason,
    )


def metrics(rows):
    repair = [r for r in rows if r["case_group"] == "repairable"]
    pending = sum(not r["evaluation_complete"] for r in repair)
    success = sum(r["complete_restoration"] is True for r in repair)
    controls = [r for r in rows if r["case_group"] == "no_change"]
    return {
        "scheduled": len(rows),
        "valid_responses": sum(r["response_valid"] is True for r in rows),
        "repairable_scheduled": len(repair),
        "repairable_observed": len(repair) - pending,
        "repairable_unobserved": pending,
        "applied": sum(r["application_pass"] is True for r in repair),
        "restored": success,
        "restoration_rate": success / len(repair) if repair and not pending else None,
        "confirmed_success_lower_bound": success / len(repair) if repair else None,
        "no_change_scheduled": len(controls),
        "no_change_correct": sum(r["outcome"] == "no_change_correct" for r in controls),
        "unnecessary_change": sum(r["outcome"] == "unnecessary_change" for r in controls),
        "control_response_failures": sum(r["response_valid"] is False for r in controls),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
    }


def run(source, variant, normalized, bound_path, output, execute, root):
    fresh(output)
    separate(output, source)
    if variant == "normalized" and normalized is None:
        raise ValueError("Normalized source required")
    with readonly_run_lock(source) as lock:
        frozen = load_source(source, root)
        bound = read(bound_path)
        validate_bound(root, bound, frozen)
        before = hashes(source, lock)
        norm_before = hashes(normalized) if normalized else None
        if normalized and read(normalized / "revalidation.json")["study_sha256"] != digest(frozen):
            raise ValueError("Normalization study mismatch")
        cases = {c["id"]: c for c in frozen["cases"]}
        specs = {s["case_id"]: s for s in bound["cases"]}
        pre = read(ROOT / read(root / "protocol.json")["preflight_output"] / "summary.json")
        qualified = {r["case_id"]: r["passed"] for r in pre["cases"]}
        output.mkdir(parents=True)
        replay = CrossReplay(output)
        infrastructure = None
        rows = []
        try:
            if execute:
                try:
                    replay.initialize("node:22-alpine")
                    if replay.base_image != bound["base_image"]:
                        raise EvaluationError("Base image differs from freeze")
                except EvaluationError as exc:
                    infrastructure = str(exc)
            for slot in frozen["schedule"]:
                case = cases[slot["case_id"]]
                spec = specs[slot["case_id"]]
                assessment = assess_cell(source, slot)
                if normalized and assessment != read(
                    normalized / "cells" / (slot["cell_id"] + ".json")
                ):
                    raise ValueError("Normalized row does not reproduce from saved call")
                chosen = assessment["normalized" if variant == "normalized" else "original"]
                folder = output / "cells" / slot["cell_id"]
                row = {
                    **slot,
                    "program": spec["program"],
                    "pair_id": case["pair_id"],
                    "stratum": spec["stratum"],
                    "variant": case["variant"],
                    "case_group": spec["case_group"],
                    "assessment_variant": variant,
                    "source_state": assessment["source_state"],
                    "original_result_sha256": assessment["original_result_sha256"],
                    "last_call_sha256": assessment["last_call_sha256"],
                    "case_sha256": digest(case),
                    "test_manifest_sha256": digest(spec),
                    "assessment_sha256": digest(chosen),
                    "response_valid": chosen["valid"],
                    "response_error": chosen["error"],
                    "normalization_status": assessment["normalization_status"],
                    "model_visible_inputs": visible_inputs(source / "cells" / slot["cell_id"]),
                    **dict.fromkeys(REQUIRED),
                    "health_pass": None,
                    "complete_restoration": False,
                    "evaluation_complete": True,
                    "evaluation_error": None,
                    "executed": False,
                    "selected_plan": None,
                    "adapter_supplied_fields": [],
                }
                response = {
                    "response_valid": chosen["valid"],
                    "analysis": chosen["analysis"],
                    "response_error": chosen["error"],
                }
                outcome, plan = classify(response, spec)
                row.update(response_outcome=outcome, outcome=outcome, selected_plan=plan)
                code = (chosen["error"] or {}).get("code", "")
                if assessment["source_state"] != "completed" or code.startswith(
                    ("MODEL_HTTP_", "MODEL_TIMEOUT", "MODEL_CONNECTION", "STUDY_CALL_BUDGET")
                ):
                    row["response_valid"] = None
                    unresolved(row, "source_unavailable:" + assessment["source_state"] + ":" + code)
                elif spec["case_group"] == "repairable":
                    if not execute or infrastructure or not qualified.get(case["id"]):
                        unresolved(row, infrastructure or "dry_run_or_unqualified_baseline")
                    else:
                        row["baseline_failure_reproduced"] = True
                        if plan:
                            try:
                                # No expected patch, tests, or oracle passed to the adapter.
                                adapted = adapt(plan, case["source_files"], POLICY)
                                write(folder / "adapter.json", adapted)
                                row["adapter_supplied_fields"] = adapted["adapter_supplied_fields"]
                                row["normalized_patch_sha256"] = adapted["normalized_patch_sha256"]
                                (folder / "patch.diff").write_text(
                                    adapted["patch_diff"], encoding="utf-8"
                                )
                                result = replay.replay(
                                    case["source_files"],
                                    adapted["dockerfile"],
                                    spec["tests"],
                                    folder / "context",
                                    slot["cell_id"],
                                )
                                write(folder / "execution.json", result)
                                row.update(
                                    {
                                        k: v
                                        for k, v in result.items()
                                        if k not in ("commands", "baseline_failure_reproduced")
                                    }
                                )
                                if result["evaluation_error"]:
                                    unresolved(row, result["evaluation_error"])
                                else:
                                    required = REQUIRED + (
                                        ["health_pass"]
                                        if spec["tests"]["lifecycle"] == "http"
                                        else []
                                    )
                                    row["complete_restoration"] = all(
                                        row.get(k) is True for k in required
                                    )
                                    row["outcome"] = (
                                        "restored"
                                        if row["complete_restoration"]
                                        else next(
                                            (
                                                k.removesuffix("_pass") + "_failed"
                                                for k in required
                                                if row.get(k) is False
                                            ),
                                            "evaluation_error",
                                        )
                                    )
                                    if row["outcome"] == "evaluation_error":
                                        unresolved(row, "checker_missing_required_verdict")
                            except PlanError as exc:
                                row.update(outcome=exc.outcome, adapter_error=str(exc))
                write(folder / "result.json", row)
                rows.append(row)
                print(
                    f"[{len(rows)}/54] {slot['case_id']} {slot['arm']} r{slot['repeat']}: {row['outcome']}",
                    flush=True,
                )
        finally:
            cleanup = replay.cleanup()
            write(
                output / "execution.json",
                {
                    "commands": replay.commands,
                    "cleanup": cleanup,
                    "base_image": replay.base_image,
                    "new_model_calls": 0,
                },
            )
        if before != hashes(source, lock) or (normalized and norm_before != hashes(normalized)):
            raise ValueError("Inputs changed")
        summary = {
            "version": "cross-program-remediation.v1",
            "assessment_variant": variant,
            "status": "complete"
            if len(rows) == 54 and all(r["evaluation_complete"] for r in rows)
            else "incomplete",
            "study_sha256": digest(frozen),
            "bound_manifest_sha256": sha(bound_path),
            "freeze_sha256": sha(root / "freeze-manifest.json"),
            "agent_replays": sum(r["executed"] for r in rows),
            "new_model_calls": 0,
            "source_unchanged": True,
            "by_arm": {
                arm: metrics([r for r in rows if r["arm"] == arm])
                for arm in frozen["config"]["arms"]
            },
            "rows": rows,
        }
        write(output / "summary.json", summary)
        write(
            output / "preservation.json",
            {
                "source_unchanged": True,
                "source_files_sha256": before,
                "normalized_files_sha256": norm_before,
            },
        )
        lines = [
            "# Cross-program remediation",
            f"Status: {summary['status']} / {variant}",
            "",
            "|Program|Stratum|Arm|Restored|Unobserved|",
            "|---|---|---|---:|---:|",
        ]
        for program in sorted({r["program"] for r in rows}):
            for stratum in ("A", "C"):
                for arm in frozen["config"]["arms"]:
                    m = metrics(
                        [
                            r
                            for r in rows
                            if (r["program"], r["stratum"], r["arm"]) == (program, stratum, arm)
                        ]
                    )
                    lines.append(
                        f"|{program}|{stratum}|{arm}|{m['restored']}/{m['repairable_scheduled']}|{m['repairable_unobserved']}|"
                    )
        (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-run", type=Path, required=True)
    p.add_argument("--assessment-variant", choices=["original", "normalized"], required=True)
    p.add_argument("--normalized-run", type=Path)
    p.add_argument("--case-manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--root", type=Path, default=ROOT / "evaluation/cross-program-v4")
    a = p.parse_args()
    run(
        a.source_run.resolve(),
        a.assessment_variant,
        a.normalized_run.resolve() if a.normalized_run else None,
        a.case_manifest.resolve(),
        a.output.resolve(),
        a.execute,
        a.root.resolve(),
    )


if __name__ == "__main__":
    main()
