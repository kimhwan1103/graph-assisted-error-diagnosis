"""Offline judgments and optional positive-only Docker replay. No model backend calls."""

import argparse
import copy
import json
import posixpath
import re
from pathlib import Path

from evaluate_remediation_v4 import readonly_run_lock, separate
from remediation_adapter_v4 import POLICY, PlanError, adapt, parse_copy
from remediation_cross_program_docker_v4 import CrossReplay, EvaluationError
from review_controls_common_v4 import (
    DEFAULT_ROOT,
    VERSION,
    digest,
    fresh,
    hashes,
    read,
    sha,
    verify_freeze,
    verify_prepared,
    write,
)
from run_review_controls_v4 import completed_rows, first_plan


def rate(n, d):
    return {"numerator": n, "denominator": d, "rate": n / d if d else None}


def binding_checks(primary, ref, arm):
    d = primary.get("parsed_decision")
    keys = ("target", "source_path", "source_line", "repository_path", "insert_after_line", "plan")
    if not d or d["decision"] != "accept":
        return dict.fromkeys(keys)
    if arm in ("R_auto", "R_llm"):
        # E1 server-bound facts were checked before freeze, not model-selected locations.
        return {
            **dict.fromkeys(keys, primary["effective_decision"] == "accept"),
            "plan": d["plan"] == ref["allowed_plan_if_accepted"],
        }
    p = d.get("packaging") or {}
    return {
        "target": d["target"] == ref["allowed_target"],
        "source_path": any(x["path"] == d["code_path"] for x in ref["allowed_source_locations"]),
        "source_line": {"path": d["code_path"], "line": d["code_line"]}
        in ref["allowed_source_locations"],
        "repository_path": p.get("repo_path") == ref["allowed_repo_path"],
        "insert_after_line": p.get("insert_after_line") in ref["allowed_insert_after_lines"],
        "plan": d["plan"] == ref["allowed_plan_if_accepted"],
    }


def copy_target_check(analysis, ref):
    """Literal structured COPY detection; prose-only / unresolved destinations stay unknown."""
    if analysis is None:
        return None
    ambiguous = False
    for plan in (analysis.get("remediation") or {}).get("plans", []):
        for change in plan.get("changes", []):
            if change.get("target", "").removeprefix("./") != "Dockerfile":
                continue
            for line in change.get("snippet", "").splitlines():
                if not re.match(r"\s*COPY\s", line, re.IGNORECASE):
                    continue
                try:
                    op = parse_copy(line)
                    dest = op["destination"]
                    destination = posixpath.normpath(
                        dest if dest.startswith("/") else "/app/" + dest
                    )
                    if destination == ref["allowed_target"] or (
                        dest.endswith("/") and ref["allowed_target"].startswith(destination + "/")
                    ):
                        return True
                except PlanError:
                    # Literal target remains detectable even if source has a placeholder.
                    if ref["allowed_target"] in line or ("./" + ref["allowed_repo_path"]) in line:
                        return True
                    ambiguous = True
    return None if ambiguous else False


def score(row, ref):
    p = row["primary"]
    d = p.get("parsed_decision")
    bindings = binding_checks(p, ref, row["arm"])
    counter = p["counter_evidence_ids"]
    groups = ref["counter_evidence_groups"]
    allowed = {x for group in groups for x in group}
    accepted = p["effective_decision"] == "accept"
    valid_defer = bool(
        d
        and d["decision"] == d["plan"] == "defer"
        and not p["assessment_error"]
        and p["server_transition_code"] == "COMPACT_DEFERRED"
    )
    expected = ref["expected_review_decision"]
    state = (
        "effective_accept"
        if accepted
        else "explicit_defer"
        if valid_defer
        else "provider_error"
        if row["primary_error"]
        else "blocked_inconsistent"
        if p["decision_consistent"] is False
        else "invalid_or_blocked"
    )
    return {
        "expected_decision": expected,
        "positive": expected == "accept",
        "primary_state": state,
        "explicit_accept": p["raw_declared_decision"] == "accept",
        "explicit_defer": valid_defer,
        "effective_accept": accepted,
        "inappropriate_accept": expected == "defer" and accepted,
        "appropriate_accept": expected == "accept"
        and accepted
        and all(v is True for v in bindings.values()),
        "excessive_defer": expected == "accept" and valid_defer,
        "primary_failure": state
        in ("provider_error", "invalid_or_blocked", "blocked_inconsistent"),
        "binding_checks": bindings,
        "bindings_scope": "server_bound" if row["arm"].startswith("R_") else "model_selected",
        "counter_precision": rate(sum(x in allowed for x in counter), len(counter)),
        "counter_group_coverage": rate(
            sum(bool(set(g) & set(counter)) for g in groups), len(groups)
        ),
        "counter_ids_exist": not any(x in p["invalid_reference_ids"] for x in counter),
        "declared_copy_plan": bool(d and d["plan"] == "add_docker_copy"),
        "final_inapplicable_copy_plan": {
            v: (
                copy_target_check(row["final_" + v]["analysis"], ref)
                if expected == "defer"
                else False
            )
            for v in ("original", "normalized")
        },
    }


def unresolved(status, reason=None):
    return {
        "execution_status": status,
        "executed": False,
        "applied": False,
        "restored": None,
        "evaluation_error": reason,
        "check_results": None,
        "patch_path": None,
        "patch_sha256": None,
    }


def evaluate(source, root, output, *, execute=False):
    fresh(output)
    separate(output, source)
    separate(output, root)
    frozen = read(source / "frozen.json")
    if frozen["mode"] != "live":
        raise ValueError("Mock results are not model performance evidence")
    if Path(frozen["root"]).resolve() != root:
        raise ValueError("Wrong prepared study")
    verify_prepared(root, Path(frozen["config_path"]), frozen["manifest"])
    if digest(verify_freeze(root, Path(frozen["config_path"]))) != frozen["freeze_sha256"]:
        raise ValueError("Live freeze changed")
    cases = {c["id"]: c for c in read(root / "dataset.json")["cases"]}
    refs = read(root / "references.json")["cases"]
    protocol = read(root / "protocol.json")
    with readonly_run_lock(source) as lock:
        before = hashes(source, lock)
        completed, pending, excluded = completed_rows(source, frozen)
        by_id = {r["cell_id"]: r for r in completed}
        output.mkdir(parents=True)
        engine = CrossReplay(output)
        rows, infra = [], None
        try:
            if execute:
                try:
                    engine.initialize("node:22-alpine")
                    if engine.base_image != protocol["base_image"]:
                        raise EvaluationError("Pinned image mismatch")
                except EvaluationError as exc:
                    infra = str(exc)
            for slot in frozen["schedule"]:
                ref = refs[slot["case_id"]]
                case = cases[slot["case_id"]]
                raw = by_id.get(slot["cell_id"])
                row = {
                    **slot,
                    "program_id": ref["program_id"],
                    "base_incident_id": ref["base_incident_id"],
                    "context_variant": ref["context_variant"],
                    "expected_decision": ref["expected_review_decision"],
                    "execution_eligible": ref["execution_eligible"],
                    "source_available": raw is not None,
                    "source_result_path": str(source / "cells" / slot["cell_id"] / "result.json"),
                    "score": score(raw, ref) if raw else None,
                    "diagnosis": raw,
                    "execution": {},
                    "raw_result_sha256": digest(raw) if raw else None,
                }
                for variant in ("original", "normalized"):
                    folder = output / "cells" / slot["cell_id"] / variant
                    final = raw["final_" + variant] if raw else None
                    plan = first_plan(final["analysis"]) if final and final["valid"] else None
                    result = {
                        **unresolved("not_execution_eligible"),
                        "final_valid": final["valid"] if final else None,
                        "applicable_plan": False,
                        "selected_plan": plan,
                        "adapter_error": None,
                    }
                    if not raw:
                        result.update(unresolved("source_unavailable"))
                    elif not ref["execution_eligible"]:
                        pass
                    elif any(
                        (raw.get(k) or {})
                        .get("code", "")
                        .startswith(
                            (
                                "MODEL_HTTP_",
                                "MODEL_TIMEOUT",
                                "MODEL_CONNECTION",
                                "STUDY_CALL_BUDGET",
                            )
                        )
                        for k in ("primary_error", "fallback_error")
                    ):
                        result.update(
                            unresolved(
                                "provider_unavailable",
                                str(raw.get("primary_error") or raw.get("fallback_error")),
                            )
                        )
                    elif not final["valid"]:
                        result.update(execution_status="invalid_final", restored=False)
                    elif not plan:
                        result.update(execution_status="no_plan", restored=False)
                    else:
                        try:
                            patch = adapt(plan, case["source_files"], POLICY)
                            write(folder / "adapter.json", patch)
                            (folder / "patch.diff").write_text(
                                patch["patch_diff"], encoding="utf-8"
                            )
                            result.update(
                                applicable_plan=True,
                                patch_path=str(folder / "patch.diff"),
                                patch_sha256=patch["normalized_patch_sha256"],
                                adapter_supplied_fields=patch["adapter_supplied_fields"],
                            )
                            if not execute or infra:
                                result.update(
                                    execution_status="not_executed"
                                    if not execute
                                    else "infrastructure_error",
                                    evaluation_error=infra,
                                )
                            elif (
                                variant == "normalized"
                                and row["execution"]["original"].get("executed")
                                and row["execution"]["original"]["patch_sha256"]
                                == patch["normalized_patch_sha256"]
                            ):
                                previous = copy.deepcopy(row["execution"]["original"])
                                result.update(
                                    previous,
                                    execution_cache_hit=True,
                                    execution_reused_from=str(
                                        output
                                        / "cells"
                                        / slot["cell_id"]
                                        / "original/execution.json"
                                    ),
                                    execution_reused_sha256=sha(
                                        output
                                        / "cells"
                                        / slot["cell_id"]
                                        / "original/execution.json"
                                    ),
                                )
                            else:
                                checks = engine.replay(
                                    case["source_files"],
                                    patch["dockerfile"],
                                    ref["execution_spec"]["tests"],
                                    folder / "context",
                                    digest([slot["cell_id"], variant])[:16],
                                )
                                write(folder / "execution.json", checks)
                                required = [
                                    "application_pass",
                                    "build_pass",
                                    "execution_pass",
                                    "target_failure_resolved",
                                    "functional_pass",
                                    "regression_pass",
                                    "data_preservation_pass",
                                ]
                                if ref["execution_spec"]["tests"]["lifecycle"] == "http":
                                    required.append("health_pass")
                                result.update(
                                    executed=checks["executed"],
                                    applied=checks["application_pass"] is True,
                                    check_results={k: checks.get(k) for k in required},
                                    evaluation_error=checks["evaluation_error"],
                                    baseline_failure_reproduced=True,
                                )
                                result["restored"] = (
                                    None
                                    if checks["evaluation_error"]
                                    else all(checks.get(k) is True for k in required)
                                )
                                result["execution_status"] = (
                                    "infrastructure_error"
                                    if checks["evaluation_error"]
                                    else "restored"
                                    if result["restored"]
                                    else "restoration_failed"
                                )
                        except PlanError as exc:
                            result.update(
                                execution_status=exc.outcome, adapter_error=str(exc), restored=False
                            )
                    row["execution"][variant] = result
                write(output / "cells" / slot["cell_id"] / "result.json", row)
                rows.append(row)
                print(
                    f"[{len(rows)}/{len(frozen['schedule'])}] {slot['cell_id']}: {row['execution']['original']['execution_status']}",
                    flush=True,
                )
        finally:
            write(
                output / "execution.json",
                {"commands": engine.commands, "cleanup": engine.cleanup(), "new_model_calls": 0},
            )
        if hashes(source, lock) != before:
            raise ValueError("Source results changed")
        write(
            output / "summary.json",
            {
                "version": VERSION,
                "study_sha256": digest(frozen),
                "source_unchanged": True,
                "executed": execute,
                "new_model_calls": 0,
                "pending": len(pending),
                "excluded": len(excluded),
                "independent_execution_eligible_rows": sum(r["execution_eligible"] for r in rows),
                "execution_policy": "Original primary, limited normalization secondary; same-row identical patch replay reused with path/hash, never counted as another independent execution",
                "rows": rows,
            },
        )
    return {"rows": len(rows), "output": str(output), "new_model_calls": 0}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-run", type=Path, required=True)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    a = p.parse_args()
    print(
        json.dumps(
            evaluate(
                a.source_run.resolve(), a.root.resolve(), a.output.resolve(), execute=a.execute
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
