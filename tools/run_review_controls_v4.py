"""Review-only 120-row harness. Live is explicit; never retry a dispatched request."""

import argparse
import asyncio
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from xml.etree import ElementTree

from review_controls_common_v4 import (
    DEFAULT_CONFIG,
    ROOT,
    VERSION,
    assess_final,
    assess_primary,
    automatic_decision,
    canonical,
    configuration,
    digest,
    fresh,
    model_request,
    now,
    original_routes,
    packet_files,
    prepared_manifest,
    read,
    schedule,
    sha,
    verify_freeze,
    verify_prepared,
    write,
)

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.experiments.backends import BackendFailure, GeminiBackend
from ai_error_check_agent.experiments.study import exclusive
from ai_error_check_agent.experiments.study_v4 import save
from ai_error_check_agent.experiments.study_v4_runtime import Session
from ai_error_check_agent.graph_compact import FALLBACK_CODES

TESTS = [
    "tools/tests/test_review_controls_v4.py",
    "tests/test_study_v4.py",
    "tests/test_graph_compact.py",
    "tests/test_diagnosis_routing.py",
    "tools/tests/test_cross_program_v4.py",
    "tools/tests/test_remediation_eval_v4.py",
]


def plan(config_path):
    _config, backend, root = configuration(config_path)
    cases = read(root / "dataset.json")["cases"]
    count = sum(c["eligible"] for c in cases)
    return {
        "eligible_contexts": count,
        "ineligible_contexts": len(cases) - count,
        "rows": count * 10,
        "automatic_rows": count,
        "primary_generation_calls": count * 9,
        "maximum_fallback_calls": count * 9,
        "maximum_total_generation_calls": count * 18,
        "configured_hard_cap": backend.max_calls,
        "repeats_llm": 3,
        "repeats_auto": 1,
        "minimum_minutes_at_maximum_calls": max(0, count * 18 - 1)
        * backend.request_interval_seconds
        / 60,
        "note": "Maximum pacing estimate, not an actual duration; no live calls made.",
    }


def call_records(output):
    return [(p, read(p)) for p in sorted((output / "cells").glob("*/calls/*.json"))]


class ReviewSession(Session):
    def __init__(self, output, backend, backend_config, config, *, live):
        self.stage = "primary"
        self.review_config = config
        self.backend_config = backend_config
        super().__init__(output, backend, backend_config, self.record_save, read, live=live)

    def record_save(self, path, record):
        record = copy.deepcopy(record)
        if "input" in record and "schema" in record:
            record["stage"] = self.stage
            # request_body is pure: this obtains the exact serialization contract without a request.
            body = GeminiBackend.request_body(
                SimpleNamespace(config=self.backend_config),
                record["prompt"],
                record["input"],
                record["seed"],
                schema=record["schema"],
            )
            record.update(
                prompt_sha256=digest(record["prompt"]),
                actual_input_sha256=digest(record["input"]),
                schema_sha256=digest(record["schema"]),
                generation_config_sha256=digest(body["generationConfig"]),
                request_body_sha256=digest(body),
                requested_model_id=self.backend_config.model_id,
            )
        save(path, record)

    def assert_budget(self, stage):
        records = [r for _, r in call_records(self.output) if r.get("dispatched")]
        stage_count = sum(r["stage"] == stage for r in records)
        if (
            len(records) >= self.backend_config.max_calls
            or stage_count >= self.review_config[stage + "_call_cap"]
        ):
            raise DiagnosisError("STUDY_CALL_BUDGET", "사전에 고정한 호출 예산에 도달했습니다.")


async def generate(session, cell, seed, stage, request):
    session.stage = stage
    if session.live:
        session.assert_budget(stage)
    runtime = session.factory(cell, seed)()
    started = time.perf_counter()
    answer, error = None, None
    try:
        answer = await runtime.run(*request)
    except DiagnosisError as exc:
        error = {"code": exc.code, "message": str(exc)}
    finally:
        await runtime.close()
    return answer.text if answer else None, error, (time.perf_counter() - started) * 1000


def first_plan(analysis):
    plans = ((analysis or {}).get("remediation") or {}).get("plans") or []
    return plans[0] if plans else None


async def run_cell(slot, root, output, session):
    started = time.perf_counter()
    load_start = time.perf_counter()
    common, compact, hints = packet_files(root, slot["case_id"], slot["arm"])
    eligibility = read(root / "packets" / slot["case_id"] / "eligibility.json")
    preparation_ms = (time.perf_counter() - load_start) * 1000
    request = model_request(slot["arm"], common, compact, hints)
    cell = output / "cells" / slot["cell_id"]
    seed = session.backend_config.seed + slot["repeat"]
    primary_model_ms = primary_parse_ms = fallback_ms = 0.0
    row = {
        **slot,
        "evidence_sha256": digest(common),
        "prompt_sha256": digest(request[0]),
        "schema_sha256": digest(request[2]),
        "actual_input_sha256": digest(request[1]),
        "candidate_hint_sha256": digest(hints) if hints else None,
        "packet_sha256": digest(compact) if compact else None,
        "candidate_eligible": eligibility["eligible"],
        "graph_status": "offline_prepared",
        "original_route": original_routes(root).get(slot["case_id"]),
        "model_id": session.backend_config.model_id if slot["arm"] != "R_auto" else None,
        "seed": seed if slot["arm"] != "R_auto" else None,
        "mode": "live" if session.live else "mock",
        "fallback_reason": None,
        "fallback_call_id": None,
        "primary_error": None,
        "fallback_error": None,
        "primary_text": None,
        "fallback_text": None,
        "raw_response_path": None,
        "source_manifest_sha256": digest(read(root / "method-before.json")),
        "protocol_sha256": sha(root / "protocol.json"),
    }
    if slot["arm"] == "R_auto":
        text = canonical(automatic_decision(compact))
    else:
        text, row["primary_error"], primary_model_ms = await generate(
            session, cell, seed, "primary", request
        )
    row["primary_text"] = text
    if text is not None:
        begin = time.perf_counter()
        primary = assess_primary(text, slot["arm"], common, compact)
        primary_parse_ms = (time.perf_counter() - begin) * 1000
    else:
        primary = {
            "parsed_decision": None,
            "raw_declared_decision": None,
            "raw_declared_plan": None,
            "schema_valid": False,
            "decision_consistent": None,
            "assessment_error": ["provider_or_runtime_error"],
            "counter_evidence_ids": [],
            "invalid_reference_ids": [],
            "server_transition_code": None,
            "validation_error": row["primary_error"],
            "effective_decision": "error",
            "analysis": None,
        }
    row["primary"] = primary
    original = {
        "valid": primary["analysis"] is not None,
        "analysis": primary["analysis"],
        "error": primary["validation_error"],
        "normalization_changes": [],
    }
    normalized = copy.deepcopy(original)
    transition = primary["server_transition_code"]
    if slot["arm"] != "R_auto" and not row["primary_error"] and transition in FALLBACK_CODES:
        row["fallback_reason"] = transition
        fallback_start = time.perf_counter()
        fallback_request = model_request(slot["arm"], common, fallback=True)
        fallback_text, row["fallback_error"], _ = await generate(
            session, cell, seed, "fallback", fallback_request
        )
        row["fallback_text"] = fallback_text
        row["fallback_request_hashes"] = {
            "prompt": digest(fallback_request[0]),
            "input": digest(fallback_request[1]),
            "schema": digest(fallback_request[2]),
        }
        if fallback_text is not None:
            original = assess_final(fallback_text, common)
            normalized = assess_final(fallback_text, common, allow_normalization=True)
        else:
            original = {
                "valid": False,
                "analysis": None,
                "error": row["fallback_error"],
                "normalization_changes": [],
            }
            normalized = copy.deepcopy(original)
        fallback_ms = (time.perf_counter() - fallback_start) * 1000
    row.update(final_original=original, final_normalized=normalized)
    calls = [(p, read(p)) for p in sorted((cell / "calls").glob("*.json"))]
    queue = sum(c.get("queue_ms") or 0 for _, c in calls)
    row["calls"] = [
        {
            "path": p.relative_to(output).as_posix(),
            "sha256": sha(p),
            "stage": c["stage"],
            "status": c["status"],
            "dispatched": c["dispatched"],
        }
        for p, c in calls
    ]
    row["provider_call_ids"] = [c["path"] for c in row["calls"] if c["dispatched"]]
    row["raw_response_path"] = row["calls"][0]["path"] if calls else None
    row["fallback_call_id"] = next(
        (c["path"] for c in row["calls"] if c["stage"] == "fallback"), None
    )
    row["returned_model_versions"] = [c.get("raw", {}).get("model_version") for _, c in calls]
    for token in ("input_tokens", "output_tokens", "thinking_tokens"):
        values = [c.get("raw", {}).get(token) for _, c in calls]
        row[token] = sum(values) if all(v is not None for v in values) else None
        row[token + "_known"] = sum(v for v in values if v is not None)
        row[token + "_missing_calls"] = sum(v is None for v in values)
    row["usage_missing"] = any(row[k + "_missing_calls"] for k in ("input_tokens", "output_tokens"))
    row["stage_times"] = {
        "preparation_ms": preparation_ms,
        "graph_ms": None,
        "graph_cost_scope": "measured once offline in eligibility.json, not repeated here",
        "model_ms": sum(c.get("network_ms") or 0 for _, c in calls) if session.live else None,
        "primary_call_wall_ms": primary_model_ms,
        "primary_model_ms": sum(
            c.get("network_ms") or 0 for _, c in calls if c["stage"] == "primary"
        )
        if session.live
        else None,
        "primary_pacing_wait_ms": sum(
            c.get("queue_ms") or 0 for _, c in calls if c["stage"] == "primary"
        ),
        "parse_assemble_validate_ms": primary_parse_ms,
        "fallback_ms": fallback_ms,
        "pacing_wait_ms": queue,
        "wall_ms": (time.perf_counter() - started) * 1000,
    }
    row["wall_ms"] = row["stage_times"]["wall_ms"]
    row["pacing_wait_ms"] = queue
    return row


def completed_rows(output, frozen):
    rows, pending, excluded = [], [], []
    for slot in frozen["schedule"]:
        cell = output / "cells" / slot["cell_id"]
        if not (cell / "state.json").exists():
            pending.append(slot)
            continue
        state = read(cell / "state.json")
        if state["status"] == "excluded":
            excluded.append({**slot, "reason": state["reason"]})
            continue
        if state["status"] != "completed":
            pending.append(slot)
            continue
        row = read(cell / "result.json")
        if digest(row) != state["result_sha256"] or any(row.get(k) != v for k, v in slot.items()):
            raise ValueError("Completed result identity/hash mismatch")
        for call in row["calls"]:
            path = (output / call["path"]).resolve()
            if output.resolve() not in path.parents or sha(path) != call["sha256"]:
                raise ValueError("Saved request/response changed")
        rows.append(row)
    return rows, pending, excluded


def summarize(output, frozen):
    rows, pending, excluded = completed_rows(output, frozen)
    calls = [r for _, r in call_records(output) if r.get("dispatched")]
    return {
        "version": VERSION,
        "mode": frozen["mode"],
        "scheduled": len(frozen["schedule"]),
        "completed": len(rows),
        "pending": len(pending),
        "excluded": len(excluded),
        "new_model_calls": len(calls),
        "primary_calls": sum(c["stage"] == "primary" for c in calls),
        "fallback_calls": sum(c["stage"] == "fallback" for c in calls),
    }


async def run(config_path, output, *, live=False):
    config, backend_config, root = configuration(config_path)
    freeze = verify_freeze(root, config_path) if live else None
    cases = read(root / "dataset.json")["cases"]
    eligible = sum(c["eligible"] for c in cases)
    if (
        not eligible
        or config["primary_call_cap"] != eligible * 9
        or config["fallback_call_cap"] != eligible * 9
    ):
        raise ValueError("Adjust both call caps and max_calls to eligible cases BEFORE freeze")
    frozen = {
        "version": VERSION,
        "mode": "live" if live else "mock",
        "config_path": str(config_path),
        "config": config,
        "root": str(root),
        "manifest": prepared_manifest(root, config_path),
        "freeze_sha256": digest(freeze) if freeze else None,
        "schedule": schedule(cases),
    }
    with exclusive(output):
        if (output / "frozen.json").exists():
            if read(output / "frozen.json") != frozen:
                raise ValueError("Frozen inputs/code/environment/mode changed")
        else:
            if any(p.name != "writer.lock" for p in output.iterdir()):
                raise ValueError("Output is not a new study directory")
            save(output / "frozen.json", frozen)
        for state in (output / "cells").glob("*/state.json"):
            if read(state)["status"] == "started":
                raise ValueError(
                    f"Unfinished cell {state.parent.name}; no automatic resend. Inspect calls then recover-undispatched or resolve explicitly."
                )
        completed_rows(output, frozen)
        backend = None
        try:
            if live:
                backend = GeminiBackend(backend_config)
                identity = backend.preflight()
                if (output / "provider.json").exists() and read(
                    output / "provider.json"
                ) != identity:
                    raise ValueError("Provider identity changed")
                save(output / "provider.json", identity)
            session = ReviewSession(output, backend, backend_config, config, live=live)
            for i, slot in enumerate(frozen["schedule"], 1):
                cell = output / "cells" / slot["cell_id"]
                if (cell / "state.json").exists() and read(cell / "state.json")["status"] in (
                    "completed",
                    "excluded",
                ):
                    continue
                if (
                    live
                    and slot["arm"] != "R_auto"
                    and session.calls + 2 > backend_config.max_calls
                ):
                    print("Stopped: reserve of two calls exceeds budget.", flush=True)
                    break
                save(cell / "state.json", {**slot, "status": "started"})
                row = await run_cell(slot, root, output, session)
                save(cell / "result.json", row)
                save(
                    cell / "state.json",
                    {**slot, "status": "completed", "result_sha256": digest(row)},
                )
                print(
                    f"[{i}/{len(frozen['schedule'])}] {slot['case_id']} / {slot['arm']} r{slot['repeat']}: {row['primary']['effective_decision']}",
                    flush=True,
                )
                if row["primary_error"] or row["fallback_error"]:
                    print("Runtime/provider failure saved. Stopped without retry.", flush=True)
                    break
        except BackendFailure as exc:
            save(
                output / "provider-error.json",
                {"at": now(), "error": str(exc), "automatic_retry": False},
            )
            raise
        finally:
            if backend:
                backend.close()
            save(output / "summary.json", summarize(output, frozen))
    return summarize(output, frozen)


def test(config_path, output):
    _config, _, root = configuration(config_path)
    fresh(output)
    manifest = prepared_manifest(root, config_path)
    # pytest may remove --basetemp: only give it a fresh, verified workspace child.
    temporary_root = (ROOT / ".review-controls-test-tmp").resolve()
    if ROOT not in temporary_root.parents:
        raise ValueError("Test temporary directory escaped workspace")
    temporary_root.mkdir(exist_ok=True)
    temporary = temporary_root / uuid4().hex
    fresh(temporary)
    junit = output.with_suffix(".junit.xml")
    fresh(junit)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *TESTS,
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        f"--basetemp={temporary}",
        f"--junitxml={junit}",
    ]
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1")
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    counts = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
    if junit.is_file():
        for suite in ElementTree.parse(junit).getroot().iter("testsuite"):
            for key in counts:
                counts[key] += int(suite.get(key, "0"))
    write(
        output,
        {
            "at": now(),
            "exit_code": result.returncode,
            "manifest": manifest,
            "command": command,
            "counts": counts,
            "junit_path": str(junit),
            "junit_sha256": sha(junit) if junit.is_file() else None,
            "model_calls": 0,
            "docker_calls": 0,
            "validation": "offline tests with mocks/fakes only",
        },
    )
    return result.returncode


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "command",
        choices=("plan", "mock", "live", "status", "test", "recover-undispatched", "resolve"),
    )
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--output", type=Path)
    p.add_argument("--cell")
    p.add_argument("--reason")
    a = p.parse_args()
    a.config = a.config.resolve()
    if a.command == "test":
        sys.exit(
            test(a.config, (a.output or ROOT / "results/review-controls-v4-tests.json").resolve())
        )
    if a.command == "plan":
        result = plan(a.config)
    else:
        output = (
            a.output
            or ROOT
            / (
                "results/review-controls-v4-mock"
                if a.command == "mock"
                else "results/review-controls-v4-live"
            )
        ).resolve()
        if a.command in ("live", "mock"):
            result = asyncio.run(run(a.config, output, live=a.command == "live"))
        else:
            frozen = read(output / "frozen.json")
            verify_prepared(Path(frozen["root"]), a.config, frozen["manifest"])
            with exclusive(output):
                if a.command == "status":
                    result = summarize(output, frozen)
                elif a.command == "recover-undispatched":
                    from recover_undispatched_cross_program_v4 import recover

                    result = recover(output, a.cell or "", frozen, apply=True)
                else:
                    if not a.reason or a.cell not in {s["cell_id"] for s in frozen["schedule"]}:
                        raise ValueError("Known --cell and recorded --reason required")
                    path = output / "cells" / a.cell / "state.json"
                    state = read(path)
                    if state["status"] != "started":
                        raise ValueError("Only started cells can be excluded")
                    save(path, {**state, "status": "excluded", "reason": a.reason})
                    result = {"excluded": a.cell, "reason": a.reason}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
