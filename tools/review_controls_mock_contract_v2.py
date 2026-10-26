"""Synthetic contract fixtures ONLY. Never used by live model requests or quality scores."""

import copy
import json
import re
from types import SimpleNamespace

from review_controls_contract_v2 import automatic_decision, canonical, digest, legacy, read, write


def bound_fixture(common):
    reader = next(
        s for s in common["source_evidence"] if re.search(r"readFileSync\(['\"]", s["text"])
    )
    target = re.search(r"readFileSync\(['\"]([^'\"]+)", reader["text"]).group(1)
    anchor = max(
        s["line"]
        for s in common["source_evidence"]
        if s["path"] == "Dockerfile" and s["text"].startswith("COPY ")
    )
    return {
        "decision": "accept",
        "plan": "add_docker_copy",
        "target": target,
        "code_path": reader["path"],
        "code_line": reader["line"],
        "evidence_ids": [common["logs"][0]["id"], common["logs"][2]["id"]],
        "source_evidence_ids": [reader["id"]],
        "packaging": {"repo_path": target.removeprefix("/app/"), "insert_after_line": anchor},
        "summary": "오프라인 계약 시험의 유효한 합성 응답입니다.",
        "uncertainty": "실제 모델 판단이나 성능 근거가 아닙니다.",
        "counter_evidence_ids": [],
    }


def full_fixture(common):
    text = legacy.bound_expand(canonical(bound_fixture(common)), common)
    legacy.validate_full(text, common)
    return json.loads(text)


def scenario(slot):
    arm, repeat = slot["arm"], slot["repeat"]
    if arm == "R_auto":
        return "auto_accept"
    if repeat == 0 or (repeat == 2 and arm in ("B_eq", "G_eq")):
        return "e1_accept" if arm == "R_llm" else "e2_accept"
    if repeat == 1:
        return {
            "R_llm": "defer_valid_fallback",
            "B_eq": "assembly_invalid_valid_fallback",
            "G_eq": "fallback_schema_invalid",
        }[arm]
    return "fallback_reference_invalid"


class ContractMockRuntime:
    def __init__(self, session, cell, seed):
        self.session, self.cell, self.seed = session, cell, seed

    async def run(self, prompt, data, schema):
        slot = self.session.current_slot
        case = scenario(slot)
        if self.session.stage == "primary":
            if slot["arm"] == "R_llm":
                answer = automatic_decision(data)
            else:
                answer = bound_fixture(data)
            if case in (
                "defer_valid_fallback",
                "fallback_schema_invalid",
                "fallback_reference_invalid",
            ):
                answer.update(decision="defer", plan="defer")
                if "packaging" in answer:
                    answer["packaging"] = None
            if case == "assembly_invalid_valid_fallback":
                answer["source_evidence_ids"].append(
                    next(s["id"] for s in data["source_evidence"] if s["path"] == "Dockerfile")
                )
        else:
            answer = full_fixture(data)
            if case == "fallback_schema_invalid":
                answer["remediation"]["plans"][0]["changes"][0]["language"] = "dockerfile"
            if case == "fallback_reference_invalid":
                answer["observations"][0]["evidence_ids"] = ["EV999999"]
        text = canonical(answer)
        directory = self.cell / "calls"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{len(list(directory.glob('*.json'))):02d}.json"
        record = {
            "prompt": prompt,
            "input": data,
            "schema": schema,
            "seed": self.seed,
            "status": "prepared",
            "dispatched": False,
            "real_model": False,
            "synthetic_contract_test": True,
            "mock_scenario": case,
        }
        self.session.record_save(path, record)
        record.update(
            status="completed",
            normalization="none",
            network_ms=None,
            queue_ms=None,
            raw={
                "text": text,
                "input_tokens": None,
                "output_tokens": None,
                "thinking_tokens": None,
                "finish_reason": "mock",
                "model_version": "synthetic_contract_test",
                "provider_calls": 0,
            },
        )
        self.session.record_save(path, record)
        return SimpleNamespace(text=text)

    async def close(self):
        pass


async def coverage(output, root, config, backend_config, rows, frozen):
    from recover_undispatched_cross_program_v4 import inspect_cell, recover
    from run_review_controls_v4_contract_v2 import (
        ReviewSession,
        completed_rows,
        initial_check,
        run_cell,
    )

    from ai_error_check_agent.errors import DiagnosisError
    from ai_error_check_agent.experiments.study_v4 import save

    expected = {
        "auto_accept",
        "e1_accept",
        "e2_accept",
        "defer_valid_fallback",
        "assembly_invalid_valid_fallback",
        "fallback_schema_invalid",
        "fallback_reference_invalid",
    }
    records = []
    for row in rows:
        case = scenario(row)
        primary = row["primary"]["effective_decision"]
        valid = row["final_original"]["valid"]
        ok = (
            (primary == "accept" and valid)
            if case.endswith("accept")
            else (
                primary == "defer" and valid
                if case == "defer_valid_fallback"
                else primary == "error" and valid
                if case == "assembly_invalid_valid_fallback"
                else primary == "defer" and not valid
            )
        )
        records.append({"cell_id": row["cell_id"], "path": case, "passed": bool(ok)})
    # Keep synthetic recovery paths below Windows MAX_PATH even under pytest basetemp.
    probes = output / "probes"
    probe = frozen["schedule"][12]
    fake_output = probes / "inflight"
    common, _, _ = legacy.packet_files(root, probe["case_id"], "B_eq")

    class FakeBackend:
        def generate(self, *args, **kwargs):
            calls = list((fake_output / "cells" / probe["cell_id"] / "calls").glob("*.json"))
            checkpoint = read(calls[0])
            assert checkpoint["status"] == "in_flight" and checkpoint["dispatched"] is True
            return {
                "text": canonical(bound_fixture(common)),
                "finish_reason": "stop",
                "input_tokens": 1,
                "output_tokens": 1,
                "thinking_tokens": None,
                "model_version": "synthetic_contract_test",
                "provider_calls": 1,
            }

    b = backend_config.model_copy(update={"request_interval_seconds": 0})
    s = ReviewSession(fake_output, FakeBackend(), b, config, live=True)
    fake_slot = {**probe, "arm": "B_eq"}
    result = await run_cell(fake_slot, root, fake_output, s)
    assert result["primary"]["effective_decision"] == "accept"
    write(
        fake_output / "cells" / fake_slot["cell_id"] / "state.json",
        {**fake_slot, "status": "started"},
    )
    ambiguous_blocked = False
    try:
        inspect_cell(fake_output.resolve(), fake_slot["cell_id"], {"schedule": [fake_slot]})
    except ValueError:
        ambiguous_blocked = True
    safe = (probes / "safe").resolve()
    write(safe / "cells" / fake_slot["cell_id"] / "state.json", {**fake_slot, "status": "started"})
    write(
        safe / "cells" / fake_slot["cell_id"] / "calls/00.json",
        {"status": "prepared", "dispatched": False},
    )
    recovered = recover(safe, fake_slot["cell_id"], {"schedule": [fake_slot]}, apply=True)
    # Completed records are immutable and selected only once; runner skips these on resume.
    write(fake_output / "cells" / fake_slot["cell_id"] / "result.json", result)
    save(
        fake_output / "cells" / fake_slot["cell_id"] / "state.json",
        {**fake_slot, "status": "completed", "result_sha256": digest(result)},
    )
    completed, pending, excluded = completed_rows(fake_output, {"schedule": [fake_slot]})
    initial = initial_check(output, frozen)
    failed = copy.deepcopy(rows)
    first = {x["cell_id"] for x in frozen["schedule"][12:21] if x["arm"] in ("B_eq", "G_eq")}
    for row in failed:
        if row["cell_id"] in first:
            row["primary"]["effective_decision"] = "error"
            row["primary"]["server_transition_code"] = "INVALID_COMPACT_RESULT"
    blocked = initial_check(output, frozen, supplied_rows=failed)
    # Exercise the real dispatch-budget guard with a synthetic ledger, never a provider call.
    budget_output = probes / "budget"
    budget_session = ReviewSession(budget_output, None, b, config, live=True)
    for i in range(216):
        write(
            budget_output / "cells" / ("a" * 20) / "calls" / f"{i:03}.json",
            {
                "synthetic_contract_test": True,
                "dispatched": True,
                "status": "in_flight",
                "stage": "primary" if i < 108 else "fallback",
            },
        )
    budget_blocked = {}
    for phase in ("primary", "fallback"):
        try:
            budget_session.assert_budget(phase)
            budget_blocked[phase] = False
        except DiagnosisError:
            budget_blocked[phase] = True
    checks = {
        "all_required_paths": {r["path"] for r in records} == expected,
        "expected_outcomes": all(r["passed"] for r in records),
        "schedule_120": len(rows) == 120,
        "checkpoint_before_fake_dispatch": result["primary"]["effective_decision"] == "accept",
        "ambiguous_resend_blocked": ambiguous_blocked,
        "undispatched_archive": recovered["safe_to_resume"],
        "completed_not_pending": len(completed) == 1 and not pending and not excluded,
        "initial_pass": initial["passed"],
        "initial_all_six_blocked_stops": blocked["stop_reason"]
        == "initial_e2_all_six_contract_blocked",
        "primary_108_limit_blocks_dispatch": budget_blocked["primary"],
        "fallback_108_limit_blocks_dispatch": budget_blocked["fallback"],
        "total_216_call_cap": backend_config.max_calls == 216,
    }
    record = {
        "synthetic_contract_test": True,
        "performance_evidence": False,
        "new_model_calls": 0,
        "docker_calls": 0,
        "checks": checks,
        "paths": records,
        "passed": all(checks.values()),
        "probe_note": "In-flight dispatch flags exist only in probes with an in-memory fake backend; no network model call occurred.",
    }
    write(output / "mock_trace_coverage.json", record)
    return record
