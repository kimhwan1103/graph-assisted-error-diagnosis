"""Freeze-guarded entry point; invokes the unchanged production study runner."""

import argparse
import os
import subprocess
import sys

from cross_program_common_v4 import (
    ROOT,
    fresh,
    now,
    sha,
    verify_diagnosis,
    verify_freeze,
    write,
)

TESTS = [
    "tests/test_study_v4.py",
    "tests/test_knowledge.py",
    "tests/test_graph_compact.py",
    "tests/test_diagnosis_routing.py",
    "tools/tests/test_remediation_eval_v4.py",
    "tools/tests/test_automatic_study_v4.py",
    "tools/tests/test_cross_program_v4.py",
]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["test", "live"])
    a = p.parse_args()
    os.chdir(ROOT)
    os.environ["PYTHONPATH"] = str(ROOT / "src")
    os.environ["PYTHONUTF8"] = "1"
    verify_diagnosis()
    if a.command == "test":
        out = fresh(ROOT / "results/cross-program-v4-tests.json")
        args = [
            sys.executable,
            "-m",
            "pytest",
            *TESTS,
            "-q",
            "--basetemp=.cross-program-test-tmp",
            "--junitxml=results/cross-program-v4-tests.xml",
        ]
        result = subprocess.run(args, check=False)
        paths = list((ROOT / "tools").glob("*cross_program*.py")) + [
            ROOT / "tools/tests/test_cross_program_v4.py"
        ]
        write(
            out,
            {
                "exit_code": result.returncode,
                "at": now(),
                "command": args,
                "tool_sha256": {x.relative_to(ROOT).as_posix(): sha(x) for x in paths},
                "junit_sha256": sha(ROOT / "results/cross-program-v4-tests.xml"),
            },
        )
        sys.exit(result.returncode)
    freeze = verify_freeze(ROOT / "evaluation/cross-program-v4")
    from remediation_cross_program_docker_v4 import CrossReplay

    engine = CrossReplay(ROOT / "results/cross-program-v4-live")
    engine.initialize("node:22-alpine")
    if engine.base_image != freeze["base_image"]:
        raise ValueError("Base image changed")
    args = [
        sys.executable,
        "-m",
        "ai_error_check_agent.experiments.study_v4",
        "run",
        "--live",
        "--config",
        "config/study.gemma-api.v4.cross-program.json",
        "--output",
        "results/cross-program-v4-live",
    ]
    sys.exit(subprocess.run(args, check=False).returncode)


if __name__ == "__main__":
    main()
