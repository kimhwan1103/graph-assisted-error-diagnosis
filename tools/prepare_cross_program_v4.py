"""Build authentic captures, qualify fixtures, and freeze/bind the authored study."""

import argparse
import copy
import difflib
import platform
import re
import sys
from pathlib import Path

from cross_program_common_v4 import (
    REGISTRY,
    ROOT,
    VARIANTS,
    checker_spec,
    digest,
    fixtures,
    fresh,
    now,
    read,
    reference_dockerfile,
    sha,
    source_manifest,
    variant_files,
    verify_diagnosis,
    write,
)
from remediation_cross_program_docker_v4 import VERSION, CrossReplay, EvaluationError


def payload(capture, case_id, program, files):
    logs = [
        {
            "id": f"captured-{i}",
            "timestamp": r["timestamp"],
            "stage": "runtime",
            "sourceId": capture["container"],
            "stream": r["stream"],
            "sequence": r["sequence"],
            "text": r["text"],
        }
        for i, r in enumerate(capture["records"], 1)
    ]
    success = (
        bool(capture["http"] and capture["http"]["status"] == 200)
        if REGISTRY[program]["lifecycle"] == "http"
        else capture["state"]["ExitCode"] == 0
    )
    if not logs:
        raise ValueError("No actual logs")
    return {
        "success": True,
        "message": "Actual controlled Docker capture",
        "data": {
            "projectId": program,
            "serviceId": program,
            "deploymentId": case_id,
            "attemptId": capture["container"],
            "deploymentStatus": "SUCCEEDED" if success else "FAILED",
            "failedStage": None if success else "runtime",
            "exitCode": None if capture["state"]["Running"] else capture["state"]["ExitCode"],
            "logRange": {
                "from": logs[0]["timestamp"],
                "to": logs[-1]["timestamp"],
                "isComplete": not capture["state"]["Running"],
            },
            "logs": logs,
            "source": {
                "format": "tar.gz",
                "downloadUrl": f"https://example.s3.ap-northeast-2.amazonaws.com/{program}.tar.gz",
                "commitSha": digest(files)[:40],
                "rootDirectory": ".",
            },
        },
    }


def reference(case, spec, variant, capture):
    path = spec["optional_reader"] if variant == "B" else spec["reader"]
    target = "/app/" + (spec["optional"] if variant == "B" else spec["target"])
    lines = next(x["content"] for x in case["source_files"] if x["path"] == path).splitlines()
    line = next(
        i for i, text in enumerate(lines, 1) if ("readFileSync(" in text or "readText(" in text)
    )
    pattern = re.compile(re.escape("/app/" + path) + r":" + str(line) + r":\d+")
    if not any(pattern.search(r["text"]) for r in capture["records"]):
        raise ValueError("Actual stack does not match fixture source line")
    facts = [
        ["ReadFailure", "type", "ReadFailure"],
        ["ReadFailure", "code", "ENOENT"],
        ["ReadFailure", "target", target],
    ]
    for kind in ("SourceRead", "StackFrame"):
        facts += [
            [kind, "type", kind],
            [kind, "path", path],
            [kind, "line", str(line)],
            [kind, "target", target],
        ]
    candidates = None
    if variant == "A":
        candidates = [
            ["Candidate", "rule", "observed-file-read.v1"],
            ["Candidate", "target", target],
            ["PackagingCandidate", "rule", "docker-copy-omission.v1"],
            ["PackagingCandidate", "target", target],
            ["PackagingCandidate", "repoPath", spec["target"]],
        ]
        for kind, prop in [
            ("RepositoryEntry", "regular"),
            ("CopyCoverage", "omitted"),
            ("IgnorePolicy", "allows"),
        ]:
            facts += [
                [kind, "type", kind],
                [kind, "target", target],
                [kind, "repoPath", spec["target"]],
                [kind, prop, "true"],
            ]
    return {
        "status": "provisional_author_fixture",
        "approved_by": None,
        "approved_at": None,
        "case_sha256": digest(case),
        "expected_route": "compact" if variant == "A" else "general",
        "acceptable_statuses": ["no_failure_evidence" if variant == "B" else "diagnosed"],
        "expected_anchor": {"path": path, "line": line, "target": target},
        "graph": {"facts": facts, "relations": None, "candidates": candidates},
        "graph_reference_scope": "Authored semantic facts from real stack/source; relation IDs not scored; B/C candidate metric not applicable, not empty gold",
        "quality_rubric": [],
        "provenance": {
            "capture": case["id"],
            "source_line": line,
            "method": "app specification and real stack, not extractor output",
        },
    }


def build(root):
    fresh(root)
    verify_diagnosis()
    root.mkdir(parents=True)
    replay = CrossReplay(root)
    cases, refs, specs, programs = [], {}, [], []
    try:
        replay.initialize("node:22-alpine")
        for program, item in REGISTRY.items():
            original = fixtures(program)
            programs.append(
                {
                    "program": program,
                    **item,
                    "source_files": len(original),
                    "javascript_files": sum(x["path"].endswith(".js") for x in original),
                    "javascript_lines": sum(
                        len(x["content"].splitlines())
                        for x in original
                        if x["path"].endswith(".js")
                    ),
                    "provenance": "new author-created application, not external natural bug",
                    "source_sha256": digest(original),
                }
            )
            for variant, (suffix, label) in VARIANTS.items():
                cid = item["prefix"] + "-" + suffix
                files = variant_files(program, variant)
                tests = checker_spec(program, files)
                dockerfile = next(x["content"] for x in files if x["path"] == "Dockerfile")
                folder = root / "captures" / cid
                image, built = replay.prepare_image(
                    files, dockerfile, tests, folder / "context", cid
                )
                if not built:
                    raise EvaluationError(f"Capture image failed: {cid}")
                capture = replay.capture(image, tests, "capture-" + cid)
                write(folder / "capture.json", capture)
                case = {
                    "id": cid,
                    "cluster_id": program,
                    "pair_id": item["prefix"]
                    + ("-optional-config" if variant == "B" else "-required-copy"),
                    "variant": label,
                    "split": "development",
                    "exposure": "used_in_development",
                    "payload": payload(capture, cid, program, files),
                    "source_files": files,
                }
                cases.append(case)
                refs[cid] = reference(case, item, variant, capture)
                spec = {
                    "case_id": cid,
                    "program": program,
                    "stratum": variant,
                    "case_group": "no_change" if variant == "B" else "repairable",
                    "allowed_states": ["no_failure_evidence" if variant == "B" else "diagnosed"],
                    "engine": VERSION,
                    "case_sha256": digest(case),
                    "source_files_sha256": digest(files),
                    "payload_sha256": digest(case["payload"]),
                    "tests": tests,
                    "reference_dockerfile": reference_dockerfile(files, item),
                    "capture_sha256": sha(folder / "capture.json"),
                }
                specs.append(spec)
                original_map = {x["path"]: x["content"] for x in original}
                diff = "".join(
                    "".join(
                        difflib.unified_diff(
                            original_map[x["path"]].splitlines(True),
                            x["content"].splitlines(True),
                            "original/" + x["path"],
                            cid + "/" + x["path"],
                        )
                    )
                    for x in files
                    if original_map[x["path"]] != x["content"]
                )
                (folder / "variant.patch").write_text(diff, encoding="utf-8")
                print("Captured " + cid, flush=True)
        write(root / "dataset.json", {"version": "study-v4-dataset.v1", "cases": cases})
        write(
            root / "references.json",
            {
                "version": "study-v4-reference.v1",
                "provenance": "Author-created additional development evaluation; no human approval",
                "cases": refs,
            },
        )
        write(root / "programs.json", programs)
        write(
            root / "remediation-spec.json",
            {
                "version": VERSION,
                "engine": VERSION,
                "cases": specs,
                "base_image": replay.base_image,
            },
        )
        config = read(ROOT / "config/study.gemma-api.v4.development.json")
        config.update(
            dataset=root.relative_to(ROOT).as_posix() + "/dataset.json",
            references=root.relative_to(ROOT).as_posix() + "/references.json",
            arms=["adaptive", "graph_compact"],
        )
        config["backend"].update(repeats=3, max_calls=162)
        write(ROOT / "config/study.gemma-api.v4.cross-program.json", config)
        write(
            root / "protocol.json",
            {
                "version": "cross-program-v4.v1",
                "created_at": now(),
                "role": "development",
                "human_approval": None,
                "programs": 3,
                "scenarios": 9,
                "cells": 54,
                "repeats": 3,
                "arms": config["arms"],
                "base_incidents": 3,
                "generation_budget": 162,
                "model": config["backend"],
                "selection": "first terminal plan only; all changes atomic; no outcome-based selection",
                "normalization": "limitations-text-unwrapping.v1 fixed before live; no new model calls",
                "runtime_isolation": replay.restrictions(),
                "source_commit_field": "40-character content digest used as synthetic archive identifier, not a real Git commit",
                "inputs": "same source archive and app contract per scenario; actual model-visible subset may differ by routing",
                "claims": "author-created development programs in same Node ENOENT/COPY error family; repetitions not independent bugs; no human semantic ratings",
                "preflight_output": "results/cross-program-v4-preflight",
                "mock_output": "results/cross-program-v4-mock",
                "audit_output": "results/cross-program-v4-audit",
                "interpreter_policy": "existing research v3 venv dependencies, v4 PYTHONPATH; no production method changes",
                "graph_scoring": "A reference facts and candidates; B/C semantic facts retained, candidates N/A; relations not scored because evidence ID binding is not independently approved",
            },
        )
    finally:
        cleanup = replay.cleanup()
        write(
            root / "capture-execution.json",
            {"commands": replay.commands, "cleanup": cleanup, "new_model_calls": 0},
        )


def preflight(root, output):
    fresh(output)
    verify_diagnosis()
    dataset = {c["id"]: c for c in read(root / "dataset.json")["cases"]}
    replay = CrossReplay(output)
    rows = []
    try:
        replay.initialize("node:22-alpine")
        if replay.base_image != read(root / "remediation-spec.json")["base_image"]:
            raise EvaluationError("Base image changed since capture")
        for spec in read(root / "remediation-spec.json")["cases"]:
            case = dataset[spec["case_id"]]
            files = case["source_files"]
            captured = read(root / "captures" / case["id"] / "capture.json")
            text = captured["raw_stdout"] + captured["raw_stderr"]
            expected = read(root / "references.json")["cases"][case["id"]]["expected_anchor"]
            baseline = "ENOENT" in text and expected["target"] in text
            healthy = (
                (captured["http"] is not None and captured["http"]["status"] == 200)
                if spec["tests"]["lifecycle"] == "http"
                else captured["state"]["ExitCode"] == 0
            )
            baseline = baseline and (
                healthy and "[recovered]" in text if spec["stratum"] == "B" else not healthy
            )
            results = {}
            modes = (
                ["reference"]
                if spec["stratum"] == "B"
                else [
                    "unmodified",
                    "reference",
                    "wrong_destination",
                    "empty_target",
                    "wrong_content",
                ]
            )
            for mode in modes:
                selected = copy.deepcopy(files)
                dockerfile = spec["reference_dockerfile"]
                if mode == "unmodified":
                    dockerfile = next(x["content"] for x in files if x["path"] == "Dockerfile")
                if mode == "wrong_destination":
                    dockerfile = dockerfile.replace(
                        "./" + spec["tests"]["target"] + "\n", "./incorrect-location\n"
                    )
                if mode in ("empty_target", "wrong_content"):
                    next(x for x in selected if x["path"] == spec["tests"]["target"])["content"] = (
                        ""
                        if mode == "empty_target"
                        else ("<p>wrong</p>\n" if spec["tests"]["lifecycle"] == "http" else "{}\n")
                    )
                label = case["id"] + "-" + mode
                result = replay.replay(
                    selected,
                    dockerfile,
                    spec["tests"],
                    output / case["id"] / mode / "context",
                    label,
                )
                write(output / case["id"] / mode / "result.json", result)
                if result["evaluation_error"]:
                    raise EvaluationError(result["evaluation_error"])
                results[mode] = all(
                    result.get(k) is True
                    for k in [
                        "build_pass",
                        "execution_pass",
                        "functional_pass",
                        "regression_pass",
                        "data_preservation_pass",
                    ]
                ) and (spec["tests"]["lifecycle"] != "http" or result["health_pass"] is True)
            passed = (
                baseline
                and results["reference"]
                and all(not v for k, v in results.items() if k != "reference")
            )
            rows.append(
                {
                    "case_id": case["id"],
                    "baseline_qualified": baseline,
                    "checks": results,
                    "passed": passed,
                }
            )
            print(case["id"] + ": " + str(passed), flush=True)
        # C's reference restoration establishes semantics equivalent to A; B checks optional recovery.
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
        write(
            output / "summary.json",
            {
                "status": "passed"
                if len(rows) == 9 and all(r["passed"] for r in rows)
                else "failed",
                "cases": rows,
                "dataset_sha256": sha(root / "dataset.json"),
                "spec_sha256": sha(root / "remediation-spec.json"),
                "base_image": replay.base_image,
                "checker_sha256": sha(ROOT / "tools/checks/cross_program/check.cjs"),
                "new_model_calls": 0,
            },
        )
    if not all(r["passed"] for r in rows):
        raise ValueError("Preflight failed; no live execution permitted")


def freeze(root, config, output):
    fresh(output)
    verify_diagnosis()
    protocol = read(root / "protocol.json")
    pre = read(ROOT / protocol["preflight_output"] / "summary.json")
    if (
        pre["status"] != "passed"
        or pre["dataset_sha256"] != sha(root / "dataset.json")
        or pre["spec_sha256"] != sha(root / "remediation-spec.json")
        or pre["checker_sha256"] != sha(ROOT / "tools/checks/cross_program/check.cjs")
    ):
        raise ValueError("Missing or stale preflight")
    mock = read(ROOT / protocol["mock_output"] / "frozen.json")
    if len(mock["schedule"]) != 54 or mock["live"]:
        raise ValueError("Mock schedule incomplete")
    if len(list((ROOT / protocol["mock_output"] / "cells").glob("*/result.json"))) != 54:
        raise ValueError("Mock incomplete")
    audit = read(ROOT / protocol["audit_output"] / "graph-audit.json")
    if not audit.get("worker_lifecycle_pass") or not audit.get("procedural_equivalence_pass"):
        raise ValueError("Audit failed")
    tests = read(ROOT / "results/cross-program-v4-tests.json")
    if tests["exit_code"] != 0:
        raise ValueError("Tests failed")
    for name, expected in tests["tool_sha256"].items():
        if sha(ROOT / name) != expected:
            raise ValueError("Tools changed since tests")
    engine = CrossReplay(output.parent)
    engine.initialize("node:22-alpine")
    if engine.base_image != pre["base_image"]:
        raise ValueError("Base image changed")
    node = engine.command("run", "--rm", *engine.restrictions(), engine.base, "node", "--version")
    if node["exit_code"] != 0:
        raise EvaluationError("Cannot measure Node version")
    paths = [
        config,
        ROOT / "evaluation/cross-program-v4-source-before.json",
        ROOT / "results/cross-program-v4-tests.json",
    ]
    for folder in [
        root,
        ROOT / "fixtures/cross-program-v4",
        ROOT / "tools",
        ROOT / protocol["preflight_output"],
        ROOT / protocol["audit_output"],
        ROOT / protocol["mock_output"],
    ]:
        paths.extend(
            p
            for p in folder.rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and p.name != "writer.lock"
            and p.suffix in {".py", ".cjs", ".js", ".json", ".md", ".patch", ".xml"}
        )
    paths.extend(ROOT / k for k in source_manifest())
    paths.extend(
        ROOT / "docs" / name
        for name in [
            "PAPER_STUDY_V4_CROSS_PROGRAM_IMPLEMENTATION.md",
            "PAPER_STUDY_V4_CROSS_PROGRAM_COMMANDS.md",
        ]
    )
    write(
        output,
        {
            "version": "cross-program-freeze.v1",
            "frozen_at": now(),
            "files": {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(paths))},
            "source_manifest": source_manifest(),
            "python": sys.version,
            "platform": platform.platform(),
            "base_image": pre["base_image"],
            "runtime_commands": engine.commands,
            "node_version": node["stdout"].strip(),
            "actual_human_approval": None,
            "generation_calls_before_freeze": 0,
            "policy": "no method changes; original primary, fixed limited normalization secondary",
        },
    )


def bind(root, source, output):
    from cross_program_common_v4 import load_source, readonly_run_lock

    fresh(output)
    with readonly_run_lock(source):
        frozen = load_source(source, root)
        spec = read(root / "remediation-spec.json")
        write(
            output,
            {
                **spec,
                "study_sha256": digest(frozen),
                "freeze_sha256": sha(root / "freeze-manifest.json"),
                "spec_sha256": sha(root / "remediation-spec.json"),
            },
        )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("build", "preflight", "freeze", "bind-manifest"):
        q = sub.add_parser(name)
        q.add_argument("--root", type=Path, required=True)
        if name != "build":
            q.add_argument("--output", type=Path, required=True)
        if name == "freeze":
            q.add_argument("--config", type=Path, required=True)
        if name == "bind-manifest":
            q.add_argument("--source-run", type=Path, required=True)
    a = p.parse_args()
    root = a.root.resolve()
    if a.command == "build":
        build(root)
    elif a.command == "preflight":
        preflight(root, a.output.resolve())
    elif a.command == "freeze":
        freeze(root, a.config.resolve(), a.output.resolve())
    else:
        bind(root, a.source_run.resolve(), a.output.resolve())


if __name__ == "__main__":
    main()
