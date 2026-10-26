"""Read-only legacy response audit and complete protected-file preservation checks."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import review_controls_common_v4 as legacy
from evaluate_remediation_v4 import locked_bytes, readonly_run_lock
from report_review_controls_v4 import write_csv
from review_controls_contract_v2 import (
    DEFAULT_AUDIT,
    LEGACY_ROOT,
    PREFIX,
    ROOT,
    VERSION,
    code_manifest,
    diagnostics,
    fresh,
    now,
    read,
    sha,
    write,
)
from run_review_controls_v4 import completed_rows


def protection_spec():
    roots = ["src", "evaluation/review-controls-v4"]
    roots += [
        p.relative_to(ROOT).as_posix()
        for parent in ("evaluation", "results")
        for p in (ROOT / parent).iterdir()
        if not p.name.startswith(PREFIX)
    ]
    freeze = legacy.verify_freeze(LEGACY_ROOT, legacy.DEFAULT_CONFIG)
    files = (
        set(freeze["manifest"]["code"])
        | set(freeze["manifest"]["files"])
        | set(freeze["verification_files"])
    )
    files.add("evaluation/review-controls-v4/freeze-manifest.json")
    return {
        "roots": sorted(set(roots)),
        "files": sorted(files),
        "ignore": "Python caches under src only; all result files are protected",
    }


def snapshot(spec, lock_handle=None):
    paths = set()
    for name in spec["roots"]:
        p = ROOT / name
        paths.update(p.rglob("*") if p.is_dir() else [p])
    paths.update(ROOT / n for n in spec["files"])
    return {
        p.relative_to(ROOT).as_posix(): {
            "size": p.stat().st_size,
            "sha256": hashlib.sha256(locked_bytes(lock_handle)).hexdigest()
            if lock_handle and p.resolve() == Path(lock_handle.name).resolve()
            else sha(p),
        }
        for p in sorted(paths)
        if p.is_file()
        and not (
            (ROOT / "src") in p.parents
            and ("__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"))
        )
    }


def verify_preserved(manifest, output=None, *, lock_handle=None):
    before = read(manifest)
    actual = snapshot(before["spec"], lock_handle)
    expected = before["files"]
    result = {
        "at": now(),
        "preserved": actual == expected,
        "checked_files": len(expected),
        "added": sorted(set(actual) - set(expected)),
        "removed": sorted(set(expected) - set(actual)),
        "changed": sorted(k for k in set(actual) & set(expected) if actual[k] != expected[k]),
        "manifest": str(manifest),
        "manifest_sha256": sha(manifest),
        "new_model_calls": 0,
        "docker_calls": 0,
    }
    if output:
        fresh(output)
        write(output, result)
    if not result["preserved"]:
        raise ValueError(
            "Protected legacy files changed: " + json.dumps(result, ensure_ascii=False)
        )
    return result


def audit(legacy_root, source, output):
    fresh(output)
    if legacy_root != LEGACY_ROOT:
        raise ValueError("This repair protocol audits the original frozen review-controls root")
    output.mkdir(parents=True)
    with readonly_run_lock(source) as lock:
        spec = protection_spec()
        write(
            output / "legacy-preservation.json",
            {"at": now(), "spec": spec, "files": snapshot(spec, lock)},
        )
        frozen = read(source / "frozen.json")
        legacy.verify_prepared(legacy_root, legacy.DEFAULT_CONFIG, frozen["manifest"])
        rows, pending, excluded = completed_rows(source, frozen)
        if len(rows) != 120 or pending or excluded:
            raise ValueError("Expected the completed 120-row parent experiment")
        findings = []
        for row in rows:
            common, compact, _ = legacy.packet_files(legacy_root, row["case_id"], row["arm"])
            for stage, text in (
                ("primary", row["primary_text"]),
                ("fallback", row["fallback_text"]),
            ):
                if text is None:
                    continue
                record = diagnostics(
                    text, row["arm"], common, compact, fallback=stage == "fallback"
                )
                path = row["raw_response_path"] if stage == "primary" else row["fallback_call_id"]
                record.update(
                    legacy_cell_id=row["cell_id"],
                    case_id=row["case_id"],
                    arm=row["arm"],
                    stage=stage,
                    source_response_path=str(source / path) if path else None,
                    source_response_sha256=sha(source / path) if path else None,
                    original_saved_error=row["primary"]["server_transition_code"]
                    if stage == "primary"
                    else row["final_original"]["error"],
                )
                findings.append(record)
        primary = [r for r in findings if r["stage"] == "primary" and r["arm"] in ("B_eq", "G_eq")]
        fallback = [r for r in findings if r["stage"] == "fallback"]
        counts = Counter((r["first_failure"] or {}).get("error_code", "none") for r in fallback)
        expected = len(primary) == 72 and all(
            r["stages"]["source_single_file"] is False
            and r["original_server_code"] == "INVALID_COMPACT_RESULT"
            for r in primary
        )
        expected = expected and dict(counts) == {
            "SCHEMA_ENUM": 51,
            "SCHEMA_MINITEMS": 18,
            "INVALID_SOURCE_LOCATION": 6,
        }
        result = {
            "version": VERSION,
            "audit_code": code_manifest(),
            "diagnostic_only": True,
            "passed": expected,
            "legacy_rows": len(rows),
            "mixed_source_primary": len(primary),
            "fallback_responses": len(fallback),
            "first_failure_counts": dict(counts),
            "all_error_count": sum(len(r["errors"]) for r in findings),
            "counts_note": "First-failure counts count responses; all-error totals may count several errors in one response.",
            "new_model_calls": 0,
            "docker_calls": 0,
            "rows": findings,
        }
        write(output / "legacy-failure-audit.json", result)
        write_csv(output / "legacy-failure-audit.csv", findings)
        (output / "legacy-failure-audit.md").write_text(
            "# 기존 원응답 계약 감사\n\n원응답을 변경하지 않은 오프라인 진단이다.\n\n"
            + f"- 기존 완료: {len(rows)}행\n- E2 혼합 소스 근거 거절: {len(primary)}건\n- 후속 첫 오류 분류: {dict(counts)}\n- 기대 분류 일치: {expected}\n"
            + "- 첫 오류는 응답 수이며 전체 오류 건수는 중복될 수 있다. 실제 API·Docker 호출은 없다.\n",
            encoding="utf-8",
        )
        verify_preserved(
            output / "legacy-preservation.json",
            output / "audit-preservation-after.json",
            lock_handle=lock,
        )
    if not expected:
        raise ValueError("Legacy audit differs; inspect saved diagnostics before proceeding")
    return {k: v for k, v in result.items() if k != "rows"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("audit", "verify-preserved"))
    p.add_argument("--legacy-root", type=Path, default=LEGACY_ROOT)
    p.add_argument("--source-run", type=Path, default=ROOT / "results/review-controls-v4-live")
    p.add_argument("--output", type=Path, default=DEFAULT_AUDIT)
    p.add_argument("--manifest", type=Path)
    a = p.parse_args()
    result = (
        audit(a.legacy_root.resolve(), a.source_run.resolve(), a.output.resolve())
        if a.command == "audit"
        else verify_preserved(
            (a.manifest or DEFAULT_AUDIT / "legacy-preservation.json").resolve(), a.output.resolve()
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
