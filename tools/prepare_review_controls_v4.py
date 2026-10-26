"""Prepare/freeze only; Docker preflight runs only when this command is invoked by the user."""

import argparse
import json
from pathlib import Path

from review_controls_common_v4 import (
    DEFAULT_CONFIG,
    DEFAULT_ROOT,
    ROOT,
    fresh,
    now,
    prepared_manifest,
    read,
    sha,
    verify_prepared,
    write,
)
from review_controls_prepare_v4 import build, preflight


def freeze(root, config, preflight_path, mock, tests):
    fresh(root / "freeze-manifest.json")
    pre = read(preflight_path / "summary.json")
    mock_report = read(mock / "summary.json")
    test_report = read(tests)
    for manifest in (
        pre["manifest"],
        read(mock / "frozen.json")["manifest"],
        test_report["manifest"],
    ):
        verify_prepared(root, config, manifest)
    if not pre["passed"] or not pre["docker_executed"]:
        raise ValueError("Docker preflight must pass before live freeze")
    if (
        mock_report["mode"] != "mock"
        or mock_report["pending"]
        or mock_report["excluded"]
        or mock_report["completed"] != mock_report["scheduled"]
        or mock_report["new_model_calls"] != 0
    ):
        raise ValueError("Complete mock required")
    if test_report["exit_code"] != 0:
        raise ValueError("Tests must pass")
    from run_review_controls_v4 import completed_rows

    _, pending, excluded = completed_rows(mock, read(mock / "frozen.json"))
    if pending or excluded:
        raise ValueError("Mock terminal rows must all be verifiable")
    record = {
        "at": now(),
        "manifest": prepared_manifest(root, config),
        "preflight_passed": True,
        "mock_passed": True,
        "tests_passed": True,
        "actual_human_approval": False,
        "routing_audit": [
            {k: v for k, v in r.items() if k not in ("graph_report", "candidate_ranges")}
            for r in read(preflight_path / "routing-audit.json")
        ],
        "verification_files": {
            p.resolve().relative_to(ROOT).as_posix(): sha(p)
            for p in (
                *preflight_path.rglob("*.json"),
                mock / "summary.json",
                mock / "frozen.json",
                tests,
            )
        },
    }
    write(root / "freeze-manifest.json", record)
    return {"frozen": str(root / "freeze-manifest.json"), "live_executed": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("build", "preflight", "freeze"))
    p.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--output", type=Path, default=ROOT / "results/review-controls-v4-preflight")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--mock", type=Path, default=ROOT / "results/review-controls-v4-mock")
    p.add_argument("--tests", type=Path, default=ROOT / "results/review-controls-v4-tests.json")
    a = p.parse_args()
    if a.command == "build":
        result = build(a.root.resolve(), a.config.resolve())
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
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
