"""Clone unchanged evidence, validate prerequisites, and freeze contract-v2 for user execution."""

import argparse
import json
import shutil
from pathlib import Path

from audit_review_controls_v4_contract_v2 import verify_preserved
from review_controls_contract_v2 import (
    BOUND_CONTRACT,
    DEFAULT_AUDIT,
    DEFAULT_CONFIG,
    DEFAULT_ROOT,
    FALLBACK_CONTRACT,
    LEGACY_ROOT,
    PREFIX,
    ROOT,
    VERSION,
    bound_schema,
    code_manifest,
    configuration,
    copied_files,
    fresh,
    legacy,
    model_request,
    now,
    packet_files,
    prepared_manifest,
    read,
    request_parity,
    schedule,
    sha,
    verify_equivalence,
    verify_prepared,
    write,
)
from review_controls_preflight_contract_v2 import preflight


def clone(source, root, config_path, audit):
    fresh(root)
    config, _, configured = configuration(config_path)
    if root != configured or source != LEGACY_ROOT:
        raise ValueError("Wrong clone source/configured destination")
    legacy.verify_freeze(source, legacy.DEFAULT_CONFIG)
    if not read(audit / "legacy-failure-audit.json")["passed"]:
        raise ValueError("Parent response audit must pass")
    verify_preserved(audit / "legacy-preservation.json")
    root.mkdir(parents=True)
    copied = {}
    for p in copied_files(source):
        if not p.is_file():
            continue
        name = p.relative_to(source).as_posix()
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
        if sha(p) != sha(target):
            raise ValueError("Clone byte mismatch")
        copied[name] = sha(p)
    write(
        root / "data-equivalence.json",
        {"source_root": str(source), "files": copied, "all_equal": True},
    )
    old = read(source / "protocol.json")
    protocol = {
        **old,
        "version": VERSION,
        "protocol_version": VERSION,
        "dataset_version": "review-controls.v1",
        "role": "development",
        "parent_root": str(source),
        "parent_protocol_sha256": sha(source / "protocol.json"),
        "parent_freeze_sha256": sha(source / "freeze-manifest.json"),
        "created_at": now(),
        "post_result_development_revision": True,
        "human_approval": False,
        "change_scope": "E2 prompt/schema descriptions, shared fallback prompt, diagnostic-only reporting and phased orchestration; acceptance criteria unchanged",
        "bound_contract_appendix": BOUND_CONTRACT,
        "fallback_contract_appendix": FALLBACK_CONTRACT,
        "e2_transmitted_schema": bound_schema(),
        "config": config,
        "schedule": schedule(read(root / "dataset.json")["cases"]),
        "initial_rows": 21,
        "initial_e2_rows": 6,
        "stop_rule": "Stop remaining when all six initial E2 primary responses are contract-blocked; explicit defer is not contract failure.",
        "final_planned_experiment": True,
    }
    write(root / "protocol.json", protocol)
    write(root / "request-parity.json", request_parity(root, config))
    common, compact, hints = packet_files(root, protocol["schedule"][0]["case_id"], "R_llm")
    hints = read(root / "packets" / protocol["schedule"][0]["case_id"] / "candidate-hints.json")
    write(
        root / "request-contracts.json",
        {
            "primary": {
                a: {"prompt": r[0], "schema": r[2]}
                for a in ("R_llm", "B_eq", "G_eq")
                for r in [model_request(a, common, compact, hints)]
            },
            "fallback": {
                "prompt": model_request("B_eq", common, fallback=True)[0],
                "schema": model_request("B_eq", common, fallback=True)[2],
            },
        },
    )
    verify_equivalence(root)
    return {
        "root": str(root),
        "copied_files": len(copied),
        "contexts": 12,
        "all_bytes_equal": True,
        "model_calls": 0,
        "docker_calls": 0,
    }


def freeze(root, config, preflight_path, mock, tests, audit):
    from run_review_controls_v4_contract_v2 import completed_rows, summarize

    fresh(root / "freeze-manifest.json")
    pre, test_record = read(preflight_path / "summary.json"), read(tests)
    mock_frozen = read(mock / "frozen.json")
    coverage = read(mock / "mock_trace_coverage.json")
    audit_record = read(audit / "legacy-failure-audit.json")
    for manifest in (pre["manifest"], mock_frozen["manifest"], test_record["manifest"]):
        verify_prepared(root, config, manifest)
    if not pre["passed"] or not pre["docker_executed"] or len(pre["docker"]) != 3:
        raise ValueError("All three Docker preflight groups required")
    if test_record["exit_code"] != 0 or test_record["counts"]["tests"] < 1:
        raise ValueError("Passing tests required")
    if sha(Path(test_record["junit_path"])) != test_record["junit_sha256"]:
        raise ValueError("JUnit changed")
    report = summarize(mock, mock_frozen)
    completed, pending, excluded = completed_rows(mock, mock_frozen)
    if (
        mock_frozen["mode"] != "mock"
        or len(completed) != 120
        or pending
        or excluded
        or report["new_model_calls"]
        or not coverage["passed"]
        or not all(coverage["checks"].values())
    ):
        raise ValueError(
            "Complete mock and explicit accept/fallback/error/resume path coverage required"
        )
    if not audit_record["passed"] or audit_record["audit_code"] != code_manifest():
        raise ValueError("Audit must match final code")
    verify_preserved(audit / "legacy-preservation.json")
    request_parity(root, configuration(config)[0])
    proofs = [
        *preflight_path.rglob("*.json"),
        *mock.rglob("*.json"),
        tests,
        Path(test_record["junit_path"]),
        audit / "legacy-preservation.json",
        audit / "legacy-failure-audit.json",
    ]
    record = {
        "at": now(),
        "protocol_version": VERSION,
        "manifest": prepared_manifest(root, config),
        "preflight_passed": True,
        "mock_passed": True,
        "tests_passed": True,
        "audit_passed": True,
        "actual_human_approval": False,
        "preservation_manifest": str(audit / "legacy-preservation.json"),
        "routing_audit": [
            {k: v for k, v in r.items() if k not in ("graph_report", "candidate_ranges")}
            for r in read(preflight_path / "routing-audit.json")
        ],
        "verification_files": {p.resolve().relative_to(ROOT).as_posix(): sha(p) for p in proofs},
        "protocol_sha256": sha(root / "protocol.json"),
        "audit": str(audit),
    }
    write(root / "freeze-manifest.json", record)
    return {"frozen": str(root / "freeze-manifest.json"), "live_executed": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("clone", "preflight", "freeze"))
    p.add_argument("--source-root", type=Path, default=LEGACY_ROOT)
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    p.add_argument("--output", type=Path, default=ROOT / "results" / (PREFIX + "-preflight"))
    p.add_argument("--mock", type=Path, default=ROOT / "results" / (PREFIX + "-mock"))
    p.add_argument("--tests", type=Path, default=ROOT / "results" / (PREFIX + "-tests.json"))
    p.add_argument("--offline", action="store_true")
    a = p.parse_args()
    if a.command == "clone":
        result = clone(
            a.source_root.resolve(), a.root.resolve(), a.config.resolve(), a.audit.resolve()
        )
    elif a.command == "preflight":
        result = preflight(
            a.root.resolve(), a.output.resolve(), a.config.resolve(), offline=a.offline
        )
    else:
        result = freeze(
            a.root.resolve(),
            a.config.resolve(),
            a.output.resolve(),
            a.mock.resolve(),
            a.tests.resolve(),
            a.audit.resolve(),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if a.command == "preflight" and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
