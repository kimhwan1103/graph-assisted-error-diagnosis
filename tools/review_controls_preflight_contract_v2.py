"""Versioned preflight orchestration; same old checker and pinned image, explicit user command."""

import asyncio
import copy

from review_controls_contract_v2 import (
    VERSION,
    canonical,
    configuration,
    digest,
    fresh,
    model_request,
    packet_files,
    prepared_manifest,
    project_hints,
    read,
    verify_files,
    write,
)
from review_controls_prepare_v4 import route_audit


def preflight(root, output, config_path, *, offline=False):
    fresh(output)
    output.mkdir(parents=True)
    _config, backend, configured_root = configuration(config_path)
    if root.resolve() != configured_root:
        raise ValueError("Preflight root differs from config")
    verify_files(read(root / "protocol.json")["dependencies"])
    rows = []
    for case in read(root / "dataset.json")["cases"]:
        cid = case["id"]
        eligibility = read(root / "packets" / cid / "eligibility.json")
        if case["eligible"]:
            common, compact, _ = packet_files(root, cid, "R_auto")
            hints = read(root / "packets" / cid / "candidate-hints.json")
            b = model_request("B_eq", common)
            g = model_request("G_eq", common, hints=hints)
            if (
                b[0] != g[0]
                or b[2] != g[2]
                or b[1] != {k: v for k, v in g[1].items() if k != "rule_candidates"}
            ):
                raise ValueError("Matched inputs differ")
            if canonical(g[1]["rule_candidates"]) != canonical(project_hints(compact)):
                raise ValueError("Hints differ from permitted projection")
            if not all(eligibility["checks"].values()):
                raise ValueError("Eligibility mismatch")
        rows.append({"case_id": cid, "eligible": case["eligible"], "checks": eligibility["checks"]})
    routing = asyncio.run(route_audit(root, output, backend))
    write(output / "routing-audit.json", routing)
    expected = {r["case_id"] for r in rows if r["eligible"]}
    observed = {r["case_id"] for r in routing if r["review_reached"] and r["candidate_generated"]}
    if not expected.issubset(observed):
        raise ValueError(
            "Eligible packets did not reach the unchanged v4 review route; inspect routing-audit"
        )
    if any(
        r["candidate_ranges"] or r["candidate_generated"]
        for r in routing
        if r["case_id"] not in expected and r["case_id"] != "expected-test-error"
    ):
        raise ValueError("Existing recovery/other-source/optional-fallback routing guard changed")
    # The existing expected-test-error fixture yields a read candidate, but no COPY candidate.
    # Preserve and report that behavior instead of inventing a stronger production guard.
    if any(r["candidate_generated"] for r in routing if r["case_id"] == "expected-test-error"):
        raise ValueError("Existing test-error fixture unexpectedly acquired a packaging candidate")
    docker_rows = []
    if not offline:
        from remediation_cross_program_docker_v4 import CrossReplay, EvaluationError

        replay = CrossReplay(output)
        try:
            replay.initialize("node:22-alpine")
            if replay.base_image != read(root / "protocol.json")["base_image"]:
                raise EvaluationError("Pinned Node base image changed")
            refs = read(root / "references.json")["cases"]
            for case in read(root / "dataset.json")["cases"]:
                if not case["eligible"] or not refs[case["id"]]["execution_eligible"]:
                    continue
                spec = refs[case["id"]]["execution_spec"]
                modes = {}
                for mode in (
                    "unmodified",
                    "reference",
                    "wrong_destination",
                    "empty_target",
                    "wrong_content",
                ):
                    files = copy.deepcopy(case["source_files"])
                    dockerfile = spec["reference_dockerfile"]
                    if mode == "unmodified":
                        dockerfile = next(f["content"] for f in files if f["path"] == "Dockerfile")
                    if mode == "wrong_destination":
                        dockerfile = dockerfile.replace(
                            "./" + spec["tests"]["target"] + "\n", "./incorrect-location\n"
                        )
                    if mode in ("empty_target", "wrong_content"):
                        next(f for f in files if f["path"] == spec["tests"]["target"])[
                            "content"
                        ] = (
                            ""
                            if mode == "empty_target"
                            else (
                                "<p>wrong</p>\n" if spec["tests"]["lifecycle"] == "http" else "{}\n"
                            )
                        )
                    folder = output / "docker" / case["id"] / mode
                    result = replay.replay(
                        files,
                        dockerfile,
                        spec["tests"],
                        folder / "context",
                        digest([case["id"], mode])[:16],
                    )
                    write(folder / "result.json", result)
                    if result["evaluation_error"]:
                        raise EvaluationError(result["evaluation_error"])
                    required = [
                        "build_pass",
                        "execution_pass",
                        "functional_pass",
                        "regression_pass",
                        "data_preservation_pass",
                        "target_failure_resolved",
                    ]
                    if spec["tests"]["lifecycle"] == "http":
                        required.append("health_pass")
                    modes[mode] = all(result.get(k) is True for k in required)
                docker_rows.append(
                    {
                        "case_id": case["id"],
                        "modes": modes,
                        "passed": modes["reference"]
                        and all(not v for k, v in modes.items() if k != "reference"),
                    }
                )
        finally:
            write(
                output / "docker-execution.json",
                {"commands": replay.commands, "cleanup": replay.cleanup(), "new_model_calls": 0},
            )
    summary = {
        "version": VERSION,
        "manifest": prepared_manifest(root, config_path),
        "cases": rows,
        "docker": docker_rows,
        "docker_executed": not offline,
        "passed": bool(docker_rows) and all(r["passed"] for r in docker_rows),
        "new_model_calls": 0,
        "offline_only": offline,
    }
    write(output / "summary.json", summary)
    return {k: v for k, v in summary.items() if k not in ("manifest", "cases")}
