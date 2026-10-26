"""Author controlled context and audit it before any model generation."""

import asyncio
import copy
import json
import time

from cross_program_common_v4 import REGISTRY
from review_controls_common_v4 import (
    ARMS,
    CONTEXTS,
    DEFAULT_CONFIG,
    NORMALIZATION_POLICY,
    ROOT,
    VERSION,
    assess_primary,
    automatic_decision,
    canonical,
    configuration,
    digest,
    fresh,
    model_request,
    now,
    ordered,
    packet_files,
    prepared_manifest,
    project_hints,
    read,
    schedule,
    sha,
    verify_files,
    write,
)

from ai_error_check_agent.backend_contracts import BackendEnvelope, adapt_backend, prepare_backend
from ai_error_check_agent.diagnosis_routing import candidate_ranges
from ai_error_check_agent.experiments.study_v4 import archive, diagnose_cell, save, source_manifest
from ai_error_check_agent.experiments.study_v4_runtime import Session
from ai_error_check_agent.knowledge.compact import build as graph_build
from ai_error_check_agent.source_analysis import select_source, source_index
from ai_error_check_agent.source_archive import ArchivePolicy, read_archive


def evidence(case, source_budget=8192):
    data = BackendEnvelope.model_validate_json(json.dumps(case["payload"])).data
    request = adapt_backend(data, profile_id="profile-demo-a").diagnosis
    bundle = prepare_backend(data, request, 98304)
    hints = candidate_ranges(bundle)
    # Fixed selection, including for an ineligible audit case; never trim full logs.
    spec = REGISTRY[case["program_id"]]
    primary = [
        {
            "path": spec["reader"],
            "start_line": 1,
            "end_line": 120,
            "reason": "사전에 고정한 읽기 소스 전체를 제공합니다.",
            "evidence_ids": [bundle.lines[0].id],
        }
    ]
    context = {}
    blob = archive(case)
    snapshot, ranges, limits, archive_sha = read_archive(
        blob, data.source, primary, bundle, ArchivePolicy(), context
    )
    lines, selected, source_limits = select_source(
        source_index(snapshot), ranges, max_bytes=source_budget
    )
    # Same metadata enrichment performed by source_analysis after full-range selection.
    context["complete_paths"] = [r["path"] for r in selected]
    context["masked_paths"] = [r["path"] for r in selected if r["masked"]]
    common = ordered(
        {
            **bundle.model_payload(),
            "source_evidence": lines,
            "source_limitations": limits + source_limits,
            "archive_observations": context,
        }
    )
    return request, bundle, common, selected, hints, archive_sha


def authored_case(base, context, index):
    cid = "case-" + digest({"ordinal": index, "salt": "review-controls-v1"})[:12]
    result = copy.deepcopy(base)
    program = base["cluster_id"]
    result.update(
        id=cid, program_id=program, base_incident_id=base["pair_id"], context_variant=context
    )
    data = result["payload"]["data"]
    original_ids = {k: data[k] for k in ("projectId", "serviceId", "deploymentId", "attemptId")}
    source_map = {x["sourceId"]: "producer-" + digest(x["sourceId"])[:12] for x in data["logs"]}
    for key in original_ids:
        data[key] = "scope-" + digest({"key": key, "program": program})[:12]
    for event in data["logs"]:
        event["sourceId"] = source_map[event["sourceId"]]
    data["source"]["downloadUrl"] = "https://example.s3.ap-northeast-2.amazonaws.com/source.tar.gz"
    target = "/app/" + REGISTRY[program]["target"]
    build_id = "artifact-" + digest(base["source_files"])[:12]
    observation = {
        "event": "deployment_observation",
        "target": target,
        "supply_kind": "image_static",
        "repository_file_role": "approved_static_asset",
        "provided_build_id": build_id,
        "observed_build_id": build_id,
    }
    if context == "P_unknown":
        observation.update(
            supply_kind="unknown", repository_file_role="unconfirmed", observed_build_id=None
        )
    elif context == "N_external_supply":
        observation.update(supply_kind="external_volume", repository_file_role="example_only")
    elif context == "N_snapshot_conflict":
        observation["observed_build_id"] = (
            "artifact-" + digest({"artifact": build_id, "ordinal": 91})[:12]
        )
    last = data["logs"][-1]
    event = {
        "id": "observation-" + digest(cid)[:12],
        "timestamp": last["timestamp"],
        "sourceId": last["sourceId"],
        "stage": last["stage"],
        "stream": last["stream"],
        "sequence": max(e["sequence"] for e in data["logs"]) + 1,
        "text": canonical(observation),
    }
    data["logs"].append(event)
    provenance = {
        "case_id": cid,
        "captured_base_case": base["id"],
        "captured_payload_sha256": digest(base["payload"]),
        "captured_log_sha256": digest(base["payload"]["data"]["logs"]),
        "original_identifiers": original_ids,
        "source_identifier_pseudonyms": source_map,
        "authored_context_event": event,
        "context_fields": observation,
        "origin": "captured_base + authored_controlled_context; context is NOT Docker measurement",
        "timestamp_policy": "same supplied observation boundary as last captured event; authored sequence after capture",
        "capture_text_status_and_timestamps_unchanged": True,
    }
    return result, provenance


def build(root, config_path=DEFAULT_CONFIG):
    fresh(root)
    config, _, configured_root = configuration(config_path)
    if root.resolve() != configured_root:
        raise ValueError("--root differs from config")
    base_root = ROOT / "evaluation/cross-program-v4"
    before = source_manifest()
    bases = [
        c
        for c in read(base_root / "dataset.json")["cases"]
        if c["variant"] == "supported_copy_omission"
    ]
    oldspec = read(base_root / "remediation-spec.json")
    oldrefs = read(base_root / "references.json")["cases"]
    specs = {s["case_id"]: s for s in oldspec["cases"]}
    root.mkdir(parents=True)
    cases, refs, provenance, ranges_all = [], {}, [], {}
    start = time.perf_counter()
    for i, (base, context) in enumerate((b, v) for b in bases for v in CONTEXTS):
        begun = time.perf_counter()
        case, prov = authored_case(base, context, i)
        request, bundle, common, ranges, hints, archive_sha = evidence(
            case, config["backend"]["max_source_bytes"]
        )
        preparation_ms = (time.perf_counter() - begun) * 1000
        graph_start = time.perf_counter()
        graph = graph_build(
            request, bundle, common["source_evidence"], common["archive_observations"]
        )
        graph_ms = (time.perf_counter() - graph_start) * 1000
        packet = graph["packet"] if graph else None
        anchor = oldrefs[base["id"]]["expected_anchor"]
        context_ids = [
            r["id"] for r in common["logs"] if r["log_id"] == prov["authored_context_event"]["id"]
        ]
        retained = {o["id"] for r in (packet or {}).get("logs", []) for o in r["occurrences"]}
        source_paths = {r["path"] for r in common["source_evidence"]}
        full_files = {f["path"]: f["content"].splitlines() for f in case["source_files"]}
        full_selected = all(
            [r["line"] for r in common["source_evidence"] if r["path"] == p]
            == list(range(1, len(full_files[p]) + 1))
            for p in (anchor["path"], "Dockerfile", common["archive_observations"]["ignore_path"])
        )
        checks = {
            "candidate_ranges": bool(hints),
            "graph_generated": bool(graph),
            "shacl_conforms": bool(graph and graph["report"]["metrics"]["conforms"]),
            "both_candidates": bool(
                packet and packet.get("candidate") and packet.get("packaging_candidate")
            ),
            "all_logs_retained": retained == bundle.evidence_ids,
            "context_retained": bool(context_ids) and set(context_ids) <= retained,
            "full_source_ranges": full_selected and {anchor["path"], "Dockerfile"} <= source_paths,
            "target_and_source_match": bool(
                packet
                and packet["candidate"]["target"] == anchor["target"]
                and packet["candidate"]["code_path"] == anchor["path"]
                and packet["candidate"]["code_line"] == anchor["line"]
            ),
            "packaging_match": bool(
                packet
                and packet.get("packaging_candidate", {}).get("repo_path")
                == REGISTRY[base["cluster_id"]]["target"]
            ),
        }
        if packet:
            # Check actual common output validator before any model result is available.
            assembled = assess_primary(
                canonical(automatic_decision(packet)), "R_auto", common, packet
            )
            checks["automatic_assembly_valid"] = assembled["effective_decision"] == "accept"
        case["eligible"] = all(checks.values())
        folder = root / "packets" / case["id"]
        write(folder / "common.json", common)
        write(folder / "compact.json", packet)
        write(folder / "candidate-hints.json", project_hints(packet) if packet else None)
        (folder / "graph.ttl").write_text(
            graph["report"]["graph_ttl"] if graph else "", encoding="utf-8"
        )
        write(
            folder / "eligibility.json",
            {
                "case_id": case["id"],
                "eligible": case["eligible"],
                "checks": checks,
                "candidate_ranges": hints,
                "graph_report": graph["report"] if graph else None,
                "preparation_ms": preparation_ms,
                "graph_ms": graph_ms,
                "normal_route": "not_measured_until_preflight",
                "candidate_generation": "offline_fixed_input_no_timeout",
            },
        )
        ref = {
            "case_id": case["id"],
            "program_id": case["program_id"],
            "base_incident_id": case["base_incident_id"],
            "context_variant": context,
            "case_sha256": digest(case),
            "source_archive_sha256": archive_sha,
            "common_evidence_sha256": digest(common),
            "expected_review_decision": "defer" if context.startswith("N_") else "accept",
            "expected_review_reason": dict(
                zip(CONTEXTS, ("applies", "unknown_only", "external_supply", "snapshot_conflict"))
            )[context],
            "allowed_plan_if_accepted": "add_docker_copy",
            "allowed_target": anchor["target"],
            "allowed_source_locations": [{"path": anchor["path"], "line": anchor["line"]}],
            "allowed_repo_path": REGISTRY[base["cluster_id"]]["target"],
            "allowed_insert_after_lines": [
                j for j, line in enumerate(full_files["Dockerfile"], 1) if line.startswith("COPY ")
            ],
            "counter_evidence_groups": [context_ids] if context.startswith("N_") else [],
            "context_evidence_ids": context_ids,
            "context_provenance": prov["origin"],
            "copy_plan_applicable": False
            if context.startswith("N_")
            else (True if context == "P_supported" else None),
            "execution_eligible": context == "P_supported",
            "reference_status": "provisional_author_control",
            "approved_by": None,
            "approved_at": None,
            "execution_spec": specs[base["id"]],
        }
        cases.append(case)
        refs[case["id"]] = ref
        provenance.append(prov)
        ranges_all[case["id"]] = ranges
    write(root / "dataset.json", {"version": VERSION, "cases": cases})
    write(root / "references.json", {"version": VERSION, "cases": refs})
    write(root / "context-provenance.json", provenance)
    write(root / "source-ranges.json", ranges_all)
    write(root / "method-before.json", before)
    dependencies = [
        base_root / x
        for x in ("dataset.json", "references.json", "programs.json", "remediation-spec.json")
    ]
    dependencies += list((ROOT / "fixtures/cross-program-v4").rglob("*"))
    write(
        root / "protocol.json",
        {
            "version": VERSION,
            "created_at": now(),
            "role": "development",
            "arms": list(ARMS),
            "eligible_cases": sum(c["eligible"] for c in cases),
            "scheduled_rows": len(schedule(cases)),
            "base_incidents": 3,
            "authored_contexts": len(cases),
            "generation_cap": 216,
            "primary_cap": 108,
            "fallback_cap": 108,
            "source_budget_bytes": config["backend"]["max_source_bytes"],
            "base_image": oldspec["base_image"],
            "normalization": NORMALIZATION_POLICY,
            "preparation_wall_ms": (time.perf_counter() - start) * 1000,
            "dependencies": {
                p.relative_to(ROOT).as_posix(): sha(p) for p in dependencies if p.is_file()
            },
            "scope": "fixed-input stage experiment; authored contextual conditions; not end-to-end routing or independent natural bugs",
            "automatic_policy": "unconditional candidate adoption, not the strongest deterministic context checker",
            "seed_policy": "requested 42/43/44, hosted determinism not guaranteed",
            "deviations": [
                "New research modules live under tools to preserve all existing src-manifest freezes.",
                "Original producer IDs are pseudonymized with a provenance mapping; captured text/status/time unchanged.",
                "Only P_supported is execution eligible; no optional stage_stress.",
            ],
        },
    )
    if source_manifest() != before:
        raise ValueError("Production sources changed during preparation")
    return {"cases": len(cases), "eligible": sum(c["eligible"] for c in cases), "root": str(root)}


async def route_audit(root, output, backend):
    from report_cross_program_v4 import routes

    cases = read(root / "dataset.json")["cases"]
    cross = read(ROOT / "evaluation/cross-program-v4/dataset.json")["cases"]
    oldconfig = read(ROOT / "config/study.gemma-api.v4.development.json")
    legacy = read(ROOT / oldconfig["dataset"])["cases"]
    cases += [c for c in cross if c["variant"] == "optional_fallback"]
    cases += [
        c
        for c in legacy
        if c["id"] in ("recovered-same-stream", "expected-test-error", "other-stream-healthy")
    ]
    session = Session(output / "routing-audit", None, backend, save, read, live=False)
    rows = []
    for case in cases:
        data = BackendEnvelope.model_validate_json(json.dumps(case["payload"])).data
        request = adapt_backend(data, profile_id="profile-demo-a").diagnosis
        bundle = prepare_backend(data, request, 98304)
        result = await diagnose_cell(
            case, "graph_compact", session.factory(output / "routing-audit/cells" / case["id"], 42)
        )
        write(output / "routing-audit" / (case["id"] + ".json"), result)
        calls = [
            read(p)
            for p in sorted((output / "routing-audit/cells" / case["id"] / "calls").glob("*.json"))
        ]
        route, reason, visited, candidate = routes(result, calls, "graph_compact")
        rows.append(
            {
                "case_id": case["id"],
                "candidate_ranges": candidate_ranges(bundle),
                "http_status": result["http_status"],
                "graph_report": result["graph_report"],
                "source_route": (result["result"].get("source_analysis") or {}).get("route"),
                "normal_route": route,
                "route_reason": reason,
                "graph_entered": visited,
                "candidate_generated": candidate,
                "review_reached": any(
                    "decision" in c["schema"].get("properties", {}) for c in calls
                ),
                "live_model": False,
            }
        )
    return rows


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
