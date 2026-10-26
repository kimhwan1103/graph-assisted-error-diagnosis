"""Frozen v3 research workflow. Only explicit `run --live` can use the network."""

import argparse
import copy
import importlib.metadata
import json
import platform
import random
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from .backends import BackendFailure, GeminiBackend
from .design import ROOT, ExperimentConfig, digest, load_cases
from .reproduce import build_dataset, sha
from .run import save
from .study import (
    exclusive,
    mock_response,
    now,
    read,
    read_attempt,
    resolve,
    save_attempt,
    scope_for,
)
from .study_dataset import validate_references, validate_split
from .study_source_index import PARSER_VERSION, RANKER_VERSION, audit_source_coverage
from .study_statistics import METHOD, simulate_plan
from .study_v3_inputs import (
    FIELDS,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    TARGETS,
    VERSION,
    StudyDecision,
    arms,
    build_relations,
    prepare_evidence,
    prompt,
    treatment,
    validate,
)

DEFAULT_CONFIG = ROOT / "config/study.gemma-api.v3.development.json"
RETRYABLE = {
    "MODEL_HTTP_429",
    "MODEL_HTTP_500",
    "MODEL_HTTP_502",
    "MODEL_HTTP_503",
    "MODEL_TIMEOUT",
    "MODEL_CONNECTION_OR_JSON_ERROR",
}


def validate_config(config):
    if config["version"] != VERSION or config["role"] not in {"development", "evaluation"}:
        raise ValueError("Unknown v3 version/role")
    if (
        config["study_scope"] != "bounded-observation-lexical-relations"
        or config["graph_adapter_id"] != "observed-relations.v3"
    ):
        raise ValueError("Unsupported research scope/adapter")
    backend = ExperimentConfig.model_validate(config["backend"])
    if backend.model_id != "gemma-4-31b-it" or backend.warmups:
        raise ValueError("Hosted Gemma with zero warmups required")
    arm_config = config["arms"]
    if arm_config["representations"] != ["raw", "text", "graph"] or arm_config[
        "verbosities"
    ] not in (["short", "natural"], ["short", "natural", "long"]):
        raise ValueError("Expected six core arms, optionally three long arms")
    if (
        arm_config["prompt_version"] != PROMPT_VERSION
        or arm_config["schema_version"] != SCHEMA_VERSION
    ):
        raise ValueError("Prompt/schema version mismatch")
    expected = [
        ("H_structure", "text_short", "graph_short"),
        ("H_compact", "graph_natural", "graph_short"),
    ]
    primary = config["primary"]
    if [(p["id"], p["before"], p["after"]) for p in primary] != expected or any(
        p["endpoint"] != "usable_diagnosis"
        or p["eligible_label"] != "test_failure"
        or p["primary_repeat"] != 0
        for p in primary
    ):
        raise ValueError("Unsupported primary comparison/endpoint/repeat")
    stat = config["statistics"]
    if (
        stat["method_id"] != METHOD
        or not 0 < stat["family_alpha"] < 0.5
        or not 0 < stat["comparison_alpha"] <= stat["family_alpha"] / len(primary)
    ):
        raise ValueError("Invalid preregistered alpha allocation/method")
    if not 0 < stat["margin"] <= 0.2 or not 0 < stat["minimum_reference_quality"] <= 1:
        raise ValueError("Invalid quality criteria")
    budget = config["budget"]
    for key in ("generation_cap", "per_slot_cap", "count_cap", "circuit_threshold"):
        if type(budget[key]) is not int or budget[key] < 1:
            raise ValueError(f"Invalid {key}")
    if budget["per_slot_cap"] > 3 or budget["generation_cap"] != backend.max_calls:
        raise ValueError("Generation caps mismatch/unbounded retry")
    if (
        not 60 <= budget["start_interval_seconds"] <= 3600
        or not 60 <= budget["retry_wait_seconds"] <= 3600
    ):
        raise ValueError("Hosted pacing must be 60..3600 seconds")
    policy = config["input"]
    if policy["parser_version"] != PARSER_VERSION or policy["ranker_version"] != RANKER_VERSION:
        raise ValueError("Parser/ranker version mismatch")
    if not policy["profiles"] or any(
        set(p) != {"source_chars", "log_chars"}
        or any(type(n) is not int or n < 256 for n in p.values())
        for p in policy["profiles"]
    ):
        raise ValueError("Invalid shared input profiles")
    for key in (
        "token_limit",
        "per_region_chars",
        "per_file_chars",
        "fallback_radius",
        "relation_limit",
    ):
        if type(policy[key]) is not int or policy[key] < 1:
            raise ValueError(f"Invalid input {key}")
    review = config["review"]
    if review != {
        "rubric_version": "whole-diagnosis.v3",
        "binary_rule": "both_humans",
        "required_humans": 2,
    }:
        raise ValueError("Unsupported review rule")
    sampling = config["sampling"]
    if sampling["repeats"] != backend.repeats or sampling["seeds"] != [
        backend.seed + i for i in range(backend.repeats)
    ]:
        raise ValueError("Repeat/seed plan mismatch")
    if config["role"] == "evaluation" and (
        not stat.get("margin_justification")
        or sampling["design_status"] != "fixed"
        or not sampling.get("power_assumptions")
        or not sampling.get("selection_rule")
        or not sampling.get("target_bugs")
        or not sampling.get("project_strata")
    ):
        raise ValueError(
            "Evaluation sampling plan and margin justification must be fixed before live connection"
        )
    if config["role"] == "evaluation":
        assumptions = sampling["power_assumptions"]
        required = {"probabilities", "probability_source", "target_power", "simulations", "seed"}
        if not isinstance(assumptions, dict) or not required <= assumptions.keys():
            raise ValueError(
                "Evaluation power assumptions need probabilities/source/target/simulations/seed"
            )
        probabilities = assumptions["probabilities"]
        if (
            len(probabilities) != 4
            or any(not 0 <= p <= 1 for p in probabilities)
            or abs(sum(probabilities) - 1) > 1e-10
            or not 0 < assumptions["target_power"] < 1
        ):
            raise ValueError("Invalid evaluation power assumptions")
    return backend


def dependency_manifest():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": dict(
            sorted(
                (d.metadata["Name"].lower(), d.version)
                for d in importlib.metadata.distributions()
                if d.metadata["Name"]
            )
        ),
    }


def code_manifest():
    paths = [
        p
        for p in (ROOT / "src").rglob("*")
        if p.is_file() and p.suffix in {".py", ".ttl", ".rq", ".cjs"}
    ]
    paths += [
        ROOT / "pyproject.toml",
        ROOT / "evaluation/public/study-development-identities.v3.json",
    ]
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(paths)}


def load_protocol(output):
    if (output / "protocol.sha256").read_text("ascii").strip() != sha(output / "protocol.json"):
        raise ValueError("Frozen protocol was modified")
    protocol = read(output / "protocol.json")
    for name, checksum in protocol["artifacts"].items():
        if sha(output / name) != checksum:
            raise ValueError(f"Frozen artifact changed: {name}")
    return protocol


def freeze(config, output):
    config = copy.deepcopy(config)
    backend = validate_config(config)
    data = config["dataset"]
    selection = read(resolve(data["selection"]))
    identities = validate_split(selection, read(resolve(data["exposure_ledger"])), config["role"])
    references = read(resolve(data["references"]))
    refs = validate_references(references, identities, config["role"] == "evaluation")
    if config["role"] == "evaluation":
        projects = dict(Counter(i["project"] for i in identities.values()))
        if (
            len(identities) != config["sampling"]["target_bugs"]
            or projects != config["sampling"]["project_strata"]
        ):
            raise ValueError("Evaluation sample does not match fixed target/project allocation")
        if any(
            not c.get("source_policy", selection.get("source_policy")) for c in selection["cases"]
        ):
            raise ValueError("Evaluation requires a fixed project source policy")
    cases, datasets = load_cases([resolve(data["paired"])])
    output.mkdir(parents=True, exist_ok=True)
    check_path = output / f".dataset-check-{uuid.uuid4().hex}.json"
    try:
        verified = build_dataset(
            resolve(data["reproduction"]), check_path, resolve(data["selection"])
        )
    finally:
        if check_path.exists():
            check_path.unlink()
    indexed = {c["id"]: c for c in verified["cases"]}
    if set(indexed) != {c["id"] for c in cases} or {c["bug_id"] for c in cases} != set(identities):
        raise ValueError("Dataset must match all selected independent bugs")
    for case in cases:
        if any(
            case[k] != indexed[case["id"]][k]
            for k in ("payload", "source_files", "provenance", "gold")
        ):
            raise ValueError("Dataset differs from verified original archives/receipts")
    for bug in identities:
        labels = [c["gold"]["label"] for c in cases if c["bug_id"] == bug]
        if sorted(labels) != ["test_failure", "test_pass"]:
            raise ValueError("Exactly one failed/fixed pair required per independent bug")
    patches = {}
    for bug, ref in refs.items():
        path = resolve(data["reproduction"]) / bug / "reference.patch"
        if sha(path) != ref["reference_patch_sha256"]:
            raise ValueError("Reviewer patch reference changed")
        patches[bug] = path.read_text("utf-8")
    arm_list = arms(config)
    planned = len(cases) * len(arm_list) * backend.repeats
    max_counts = len(cases) * len(arm_list) * len(config["input"]["profiles"]) * backend.repeats
    if planned > config["budget"]["generation_cap"] or max_counts > config["budget"]["count_cap"]:
        raise ValueError(
            f"Plan exceeds explicit caps: generation={planned}, maximum count requests={max_counts}"
        )
    fingerprint = {
        "config": config,
        "datasets": datasets,
        "code": code_manifest(),
        "dependencies": dependency_manifest(),
        "data_hashes": {
            k: sha(resolve(v) / "reproduction.json" if k == "reproduction" else resolve(v))
            for k, v in data.items()
        },
        "schema": StudyDecision.model_json_schema(),
        "prompts": {v: prompt(v) for v in config["arms"]["verbosities"]},
    }
    if (output / "protocol.json").exists():
        protocol = load_protocol(output)
        if protocol["fingerprint"] != fingerprint:
            raise ValueError(
                "Frozen config/code/schema/data/dependencies changed; choose a new output"
            )
        return protocol, cases
    if any(output.iterdir()):
        extras = {p.name for p in output.iterdir()} - {"writer.lock"}
        if extras:
            raise ValueError("Unrecognized nonempty output; choose a new output")
    rng, schedule = random.Random(backend.seed), []
    for repeat in range(backend.repeats):
        shuffled = list(cases)
        rng.shuffle(shuffled)
        for case in shuffled:
            ordered = list(arm_list)
            rng.shuffle(ordered)
            for arm in ordered:
                schedule.append(
                    {
                        "id": f"{len(schedule):05d}",
                        "case": case["id"],
                        "bug_id": case["bug_id"],
                        "label": case["gold"]["label"],
                        "repeat": repeat,
                        **arm,
                        **identities[case["bug_id"]],
                    }
                )
    artifacts = {
        "code-manifest.json": fingerprint["code"],
        "dependency-manifest.json": fingerprint["dependencies"],
        "frozen-dataset.json": {"cases": cases},
        "frozen-selection.json": selection,
        "reviewer-references.json": {**references, "patches": patches},
        "frozen-exposure-ledger.json": read(resolve(data["exposure_ledger"])),
    }
    for filename, content in artifacts.items():
        save(output / filename, content)
    protocol = {
        "version": VERSION,
        "frozen_at": now(),
        "fingerprint": fingerprint,
        "schedule": schedule,
        "artifacts": {name: sha(output / name) for name in artifacts},
        "arms": arm_list,
        "independent_bugs": len(identities),
        "projects": dict(Counter(i["project"] for i in identities.values())),
        "planned_generation_calls": planned,
        "maximum_count_requests": max_counts,
        "retry_allowance": config["budget"]["generation_cap"] - planned,
        "minimum_minutes_at_configured_pacing": max(0, planned - 1)
        * config["budget"]["start_interval_seconds"]
        / 60,
        "primary_comparisons": config["primary"],
    }
    save(output / "protocol.json", protocol)
    (output / "protocol.sha256").write_text(sha(output / "protocol.json") + "\n", encoding="ascii")
    for name in ("inputs", "attempts"):
        (output / name).mkdir()
    return protocol, cases


def read_input(path, protocol_hash):
    value = read(path)
    if value["protocol_sha256"] != protocol_hash or value["content_sha256"] != digest(
        {k: v for k, v in value.items() if k != "content_sha256"}
    ):
        raise ValueError("Input checkpoint changed")
    return value


def prepare_inputs(config, protocol, cases, output, backend=None):
    started = time.perf_counter()
    protocol_hash = sha(output / "protocol.json")
    cache_path = output / "token-count-cache.json"
    cache = (
        read(cache_path)
        if cache_path.exists()
        else {"protocol_sha256": protocol_hash, "requests": [], "counts": {}}
    )
    if cache["protocol_sha256"] != protocol_hash:
        raise ValueError("Count cache protocol mismatch")
    if cache_path.exists() and cache.get("sha256") != digest(
        {k: v for k, v in cache.items() if k != "sha256"}
    ):
        raise ValueError("Count cache changed")

    def persist_cache():
        cache["sha256"] = digest({k: v for k, v in cache.items() if k != "sha256"})
        save(cache_path, cache)

    inputs = {}
    refs = {r["bug_id"]: r for r in read(output / "reviewer-references.json")["items"]}
    for number, case in enumerate(cases):
        path = output / "inputs" / f"{number:04d}.json"
        if path.exists():
            value = read_input(path, protocol_hash)
            if value["case"] != case["id"] or value["token_counts_verified"] != (
                backend is not None
            ):
                raise ValueError("Input case/mode mismatch; use separate outputs")
            inputs[case["id"]] = value
            continue
        for profile in config["input"]["profiles"]:
            preparation_start = time.perf_counter()
            prepared = prepare_evidence(
                case["payload"],
                case["source_files"],
                scope_for(case, resolve(config["dataset"]["reproduction"])),
                {**config["input"], **profile},
            )
            prepared["edges"] = build_relations(
                prepared, {"limit": config["input"]["relation_limit"]}
            )
            retrieval_seconds = time.perf_counter() - preparation_start
            count_start, counts = time.perf_counter(), {}

            def byte_size(value):
                return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

            if (
                byte_size(prepared["base"]["source_evidence"])
                > config["backend"]["max_source_bytes"]
                or byte_size(prepared["base"]["logs"]) > config["backend"]["max_evidence_bytes"]
            ):
                counts = {"blocked": "source_or_log_byte_budget"}
                continue
            schema_text = "\n[공통 출력 JSON Schema]\n" + json.dumps(
                StudyDecision.model_json_schema(), ensure_ascii=False
            )
            if any(
                len(
                    (
                        prompt(arm["verbosity"])
                        + schema_text
                        + json.dumps(
                            {"untrusted_evidence": treatment(prepared, arm["representation"])},
                            ensure_ascii=False,
                        )
                    ).encode("utf-8")
                )
                > config["backend"]["max_prompt_bytes"]
                for arm in protocol["arms"]
            ):
                counts = {"blocked": "prompt_byte_budget"}
                continue
            for arm in protocol["arms"]:
                payload = treatment(prepared, arm["representation"])
                per_repeat = []
                for seed in config["sampling"]["seeds"]:
                    request = {
                        "model": config["backend"]["model_id"],
                        "settings": config["backend"],
                        "prompt": prompt(arm["verbosity"]),
                        "payload": payload,
                        "schema": StudyDecision.model_json_schema(),
                        "seed": seed,
                    }
                    key = digest(request)
                    if backend is not None and key not in cache["counts"]:
                        if len(cache["requests"]) >= config["budget"]["count_cap"]:
                            raise ValueError(
                                "Tokenizer request cap reached; generation not started"
                            )
                        entry = {"request_sha256": key, "state": "in_flight", "started_at": now()}
                        cache["requests"].append(entry)
                        persist_cache()
                        request_start = time.perf_counter()
                        try:
                            count = backend.count_tokens(
                                request["prompt"], payload, seed, schema=request["schema"]
                            )
                            if type(count) is not int or count < 1:
                                raise BackendFailure("MODEL_INVALID_TOKEN_COUNT")
                            cache["counts"][key] = count
                            entry.update(state="returned", count=count)
                        except BackendFailure as exc:
                            entry.update(state="error", error=str(exc).split(":", 1)[0])
                            raise
                        finally:
                            entry["request_seconds"] = time.perf_counter() - request_start
                            persist_cache()
                    per_repeat.append(cache["counts"].get(key) if backend else None)
                counts[arm["arm"]] = max(per_repeat) if backend else None
            if backend is None or max(counts.values()) <= config["input"]["token_limit"]:
                value = {
                    "case": case["id"],
                    "bug_id": case["bug_id"],
                    "label": case["gold"]["label"],
                    "project": next(
                        s["project"] for s in protocol["schedule"] if s["case"] == case["id"]
                    ),
                    "protocol_sha256": protocol_hash,
                    "profile": profile,
                    "token_counts_verified": backend is not None,
                    "counts": counts,
                    "prepared": prepared,
                    "coverage": audit_source_coverage(
                        prepared,
                        case["source_files"],
                        refs.get(case["bug_id"])
                        if case["gold"]["label"] == "test_failure"
                        else None,
                    ),
                    "retrieval_preparation_seconds": retrieval_seconds if backend else None,
                    "token_check_seconds": time.perf_counter() - count_start if backend else None,
                }
                value["content_sha256"] = digest(value)
                save(path, value)
                inputs[case["id"]] = value
                break
        else:
            save(
                output / "preflight-blocked.json",
                {"case": case["id"], "counts": counts, "generation_calls": 0},
            )
            raise ValueError(
                "Smallest common profile exceeds input token/byte budget; generation not started"
            )
    audit = {
        "protocol_sha256": protocol_hash,
        "items": {
            k: {
                "profile": v["profile"],
                "counts": v["counts"],
                "project": v["project"],
                "label": v["label"],
                "coverage": v["coverage"],
                **v["prepared"]["audit"],
                "relation_count": len(v["prepared"]["edges"]),
            }
            for k, v in inputs.items()
        },
        "preflight_wall_seconds": time.perf_counter() - started if backend else None,
    }
    if not (output / "input-audit.json").exists():

        def aggregate(values):
            values = list(values)
            failures = [v for v in values if v["label"] == "test_failure"]
            return {
                "observations": len(values),
                "failure_observations": len(failures),
                "coverage": {
                    k: {
                        "assessable": sum(v["coverage"][k] is not None for v in failures),
                        "covered": sum(v["coverage"][k] is True for v in failures),
                    }
                    for k in (
                        "archive_has_root",
                        "any_line",
                        "full_function",
                        "essential_support",
                        "relation_hits_root",
                    )
                },
                "relation_present": sum(bool(v["prepared"]["edges"]) for v in values),
                "partial_regions": sum(
                    v["prepared"]["audit"]["retrieval"]["partial_region_count"] for v in values
                ),
                "parse_fallback_files": sum(
                    v["prepared"]["audit"]["retrieval"]["parse_fallback_count"] for v in values
                ),
                "omitted_source_lines": sum(
                    v["prepared"]["audit"]["retrieval"]["available_lines"]
                    - v["prepared"]["audit"]["retrieval"]["included_lines"]
                    for v in values
                ),
                "omitted_log_lines": sum(
                    v["prepared"]["audit"]["logs"]["omitted_lines"] for v in values
                ),
                "profiles": dict(Counter(str(v["profile"]) for v in values)),
            }

        audit["overall"] = aggregate(inputs.values())
        audit["projects"] = {
            p: aggregate(v for v in inputs.values() if v["project"] == p)
            for p in protocol["projects"]
        }
        save(output / "input-audit.json", audit)
    return inputs


def validate_record(record, prepared, is_mock):
    try:
        if record["response"]["finish_reason"] != "stop":
            raise ValueError("Non-stop response")
        decision, normalized = validate(record["response"]["text"], prepared)
        target = TARGETS[record["slot"]["verbosity"]]
        expected = (
            "diagnosed" if record["slot"]["label"] == "test_failure" else "no_failure_evidence"
        )
        record.update(
            contract_valid=True,
            decision=decision,
            format_normalized=normalized,
            status_match=decision["analysis_status"] == expected if not is_mock else None,
            field_lengths={k: len(decision[k]) for k in (*FIELDS, "mechanism")},
            length_target_met=all(target[0] <= len(decision[k]) <= target[1] for k in FIELDS)
            if target and not is_mock
            else None,
        )
    except ValueError:
        record.update(contract_valid=False, error="INVALID_DECISION")
    record["validation_complete"] = True


def records_for(output, protocol, inputs):
    slots = {s["id"]: s for s in protocol["schedule"]}
    rows = []
    for path in sorted((output / "attempts").glob("*.json")):
        row = read_attempt(path)
        slot = row["slot"]
        if (
            slots.get(slot["id"]) != slot
            or slot["case"] not in inputs
            or row["input_sha256"] != inputs[slot["case"]]["content_sha256"]
            or path.name != f"{slot['id']}-{row['attempt']}.json"
        ):
            raise ValueError("Attempt schedule/input identity changed")
        rows.append(row)
    return rows


def execute(
    config,
    protocol,
    inputs,
    output,
    backend=None,
    *,
    retry_uncertain=False,
    sleeper=time.sleep,
    clock=time.time,
):
    path = output / "manifest.json"
    manifest = (
        read(path)
        if path.exists()
        else {
            "version": VERSION,
            "protocol_sha256": sha(output / "protocol.json"),
            "mode": "live" if backend else "mock",
            "started_at": now(),
            "active_wall_seconds": 0 if backend else None,
        }
    )
    if manifest["mode"] != ("live" if backend else "mock") or manifest["protocol_sha256"] != sha(
        output / "protocol.json"
    ):
        raise ValueError("Run identity mismatch")
    prior = records_for(output, protocol, inputs)
    if any(r["is_mock"] != (backend is None) for r in prior):
        raise ValueError("Attempt mode changed")
    previous_start = max(
        (datetime.fromisoformat(r["started_at"]).timestamp() for r in prior), default=0
    )
    errors = 0
    for row in sorted(prior, key=lambda r: r["started_at"], reverse=True):
        if row["state"] == "returned":
            break
        if row["state"] == "backend_error":
            errors += 1
    if backend and (
        errors >= config["budget"]["circuit_threshold"]
        or any(
            r.get("error") in {"MODEL_HTTP_401", "MODEL_HTTP_403", "MODEL_HTTP_404"} for r in prior
        )
    ):
        raise ValueError(
            "Previous authentication/model failure or open error circuit; inspect run before starting a new output"
        )
    start = time.perf_counter()
    manifest["state"] = "running"
    save(path, manifest)
    try:
        for slot in protocol["schedule"]:
            paths = sorted((output / "attempts").glob(slot["id"] + "-*.json"))
            records = [read_attempt(p) for p in paths]
            prepared = inputs[slot["case"]]["prepared"]
            if any(r["state"] == "returned" for r in records):
                for p, record in zip(paths, records, strict=True):
                    if record["state"] == "returned" and not record.get("validation_complete"):
                        validate_record(record, prepared, backend is None)
                        save_attempt(p, record)
                continue
            if records and (
                (records[-1]["state"] == "in_flight" and not retry_uncertain)
                or records[-1].get("error") not in RETRYABLE | {None}
            ):
                continue
            for attempt in range(len(records) + 1, config["budget"]["per_slot_cap"] + 1):
                if (
                    len(list((output / "attempts").glob("*.json")))
                    >= config["budget"]["generation_cap"]
                ):
                    manifest["state"] = "call_cap_reached"
                    return
                if backend:
                    interval = max(
                        config["budget"]["start_interval_seconds"],
                        config["budget"]["retry_wait_seconds"] if attempt > 1 else 0,
                    )
                    sleeper(max(0, interval - (clock() - previous_start)))
                record = {
                    "slot": slot,
                    "attempt": attempt,
                    "state": "in_flight",
                    "started_at": now(),
                    "is_mock": backend is None,
                    "input_sha256": inputs[slot["case"]]["content_sha256"],
                    "contract_valid": False,
                    "request_seconds": None,
                    "status_match": None,
                    "length_target_met": None,
                    "error": None,
                }
                filename = output / "attempts" / f"{slot['id']}-{attempt}.json"
                save_attempt(filename, record)
                previous_start, request_start = clock(), time.perf_counter()
                try:
                    response = (
                        backend.generate(
                            prompt(slot["verbosity"]),
                            treatment(prepared, slot["representation"]),
                            config["sampling"]["seeds"][slot["repeat"]],
                            schema=StudyDecision.model_json_schema(),
                        )
                        if backend
                        else mock_response()
                    )
                    record.update(
                        state="returned",
                        response=response,
                        request_seconds=time.perf_counter() - request_start if backend else None,
                    )
                    save_attempt(filename, record)
                    validate_record(record, prepared, backend is None)
                except BackendFailure as exc:
                    record.update(
                        state="backend_error",
                        error=str(exc).split(":", 1)[0],
                        request_seconds=time.perf_counter() - request_start,
                    )
                record["finished_at"] = now()
                save_attempt(filename, record)
                if record["state"] == "returned":
                    errors = 0
                    break
                errors += 1
                if record["error"] in {"MODEL_HTTP_401", "MODEL_HTTP_403", "MODEL_HTTP_404"}:
                    manifest["state"] = "authentication_or_model_error"
                    return
                if errors >= config["budget"]["circuit_threshold"]:
                    manifest["state"] = "provider_error_circuit_open"
                    return
                if record["error"] not in RETRYABLE:
                    break
        manifest["state"] = "schedule_finished"
    finally:
        if manifest["state"] == "running":
            manifest["state"] = "interrupted_or_failed"
        if backend:
            manifest["active_wall_seconds"] += time.perf_counter() - start
        manifest.update(
            updated_at=now(), attempt_files=len(list((output / "attempts").glob("*.json")))
        )
        save(path, manifest)


def status(output):
    """Read only: no lock, mkdir, normalization, or manifest update."""
    protocol = load_protocol(output)
    rows = [read_attempt(p) for p in (output / "attempts").glob("*.json")]
    return {
        "planned": len(protocol["schedule"]),
        "attempts": len(rows),
        "states": dict(Counter(r["state"] for r in rows)),
        "manifest": read(output / "manifest.json") if (output / "manifest.json").exists() else None,
        "process_liveness": "not_inferred_from_manifest",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("plan", "mock", "run", "report", "status", "power-plan"),
        nargs="?",
        default="plan",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--retry-uncertain", action="store_true")
    parser.add_argument("--power-config", type=Path)
    args = parser.parse_args(argv)
    if args.command == "run" and not args.live:
        parser.error("Generation requires run --live")
    if args.live and args.command != "run":
        parser.error("--live applies only to run")
    output = args.output or ROOT / "results" / (
        "research-study-v3-mock" if args.command == "mock" else "research-study-v3-development"
    )
    if args.command == "status":
        print(status(output))
        return 0
    if args.command == "power-plan":
        if not args.power_config:
            parser.error("power-plan requires --power-config")
        value = simulate_plan(read(args.power_config))
        output.mkdir(parents=True, exist_ok=False)
        save(output / "power-plan.json", value)
        print(output / "power-plan.json")
        return 0
    from .study_v3_analysis import summarize

    with exclusive(output):
        if args.command == "report":
            summarize(output)
            return 0
        config = (
            read(args.config)
            if args.config
            else load_protocol(output)["fingerprint"]["config"]
            if (output / "protocol.json").exists()
            else read(DEFAULT_CONFIG)
        )
        protocol, cases = freeze(config, output)
        print(
            {
                k: protocol[k]
                for k in (
                    "independent_bugs",
                    "planned_generation_calls",
                    "maximum_count_requests",
                    "retry_allowance",
                    "minimum_minutes_at_configured_pacing",
                )
            },
            flush=True,
        )
        if args.command == "plan":
            return 0
        existing = [read_attempt(p) for p in (output / "attempts").glob("*.json")]
        expected = "mock" if args.command == "mock" else "live"
        if (output / "manifest.json").exists() and read(output / "manifest.json")[
            "mode"
        ] != expected:
            raise ValueError("Cannot mix live and mock outputs")
        complete = len({r["slot"]["id"] for r in existing if r["state"] == "returned"}) == len(
            protocol["schedule"]
        )
        if complete:
            if read(output / "manifest.json")["mode"] != expected:
                raise ValueError("Cannot mix live and mock outputs")
            for path in (output / "attempts").glob("*.json"):
                record = read_attempt(path)
                if record["state"] == "returned" and not record.get("validation_complete"):
                    saved = {
                        v["case"]: v
                        for v in (
                            read_input(p, sha(output / "protocol.json"))
                            for p in (output / "inputs").glob("*.json")
                        )
                    }
                    validate_record(
                        record, saved[record["slot"]["case"]]["prepared"], expected == "mock"
                    )
                    save_attempt(path, record)
            summarize(output)
            return 0
        if (
            args.command == "run"
            and (output / "manifest.json").exists()
            and read(output / "manifest.json")["state"]
            in {"authentication_or_model_error", "provider_error_circuit_open"}
        ):
            raise ValueError(
                "Previous provider error circuit/auth failure requires inspection and a new run"
            )
        invocation_start = time.perf_counter()
        backend = GeminiBackend(validate_config(config)) if args.command == "run" else None
        try:
            if backend:
                server = backend.preflight()
                path = output / "server.json"
                if path.exists() and (output / "server.sha256").read_text("ascii").strip() != sha(
                    path
                ):
                    raise ValueError("Provider metadata checkpoint changed")
                if path.exists() and read(path) != server:
                    raise ValueError("Provider model metadata changed")
                save(path, server)
                (output / "server.sha256").write_text(sha(path) + "\n", encoding="ascii")
            inputs = prepare_inputs(config, protocol, cases, output, backend)
            execute(config, protocol, inputs, output, backend, retry_uncertain=args.retry_uncertain)
        finally:
            if backend:
                backend.close()
            if (output / "manifest.json").exists():
                if backend:
                    manifest = read(output / "manifest.json")
                    manifest["active_run_seconds_including_preflight"] = (
                        manifest.get("active_run_seconds_including_preflight", 0)
                        + time.perf_counter()
                        - invocation_start
                    )
                    save(output / "manifest.json", manifest)
                summarize(output)
    return 0 if status(output)["states"].get("returned", 0) == len(protocol["schedule"]) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, BackendFailure, OSError) as exc:
        print(
            f"Study stopped: {str(exc) if isinstance(exc, (ValueError, BackendFailure)) else type(exc).__name__}"
        )
        raise SystemExit(2) from None
