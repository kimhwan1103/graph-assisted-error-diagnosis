"""One-command research workflow. No network unless `run --live` is supplied."""

import argparse
import contextlib
import json
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .backends import BackendFailure, GeminiBackend
from .design import ROOT, ExperimentConfig, digest, load_cases
from .reproduce import build_dataset, sha
from .run import environment, save
from .study_inputs import (
    ARMS,
    FIELDS,
    TARGETS,
    VERSION,
    StudyDecision,
    prepare,
    prompt,
    treatment,
    validate,
)

DEFAULT_CONFIG = ROOT / "config/study.gemma-api.v2.json"


def read(path):
    return json.loads(Path(path).read_text("utf-8"))


def now():
    return datetime.now(UTC).isoformat()


def save_attempt(path, record):
    record["record_sha256"] = digest({k: v for k, v in record.items() if k != "record_sha256"})
    save(path, record)


def read_attempt(path):
    record = read(path)
    if record["record_sha256"] != digest({k: v for k, v in record.items() if k != "record_sha256"}):
        raise ValueError("Attempt checkpoint was modified")
    return record


def resolve(path):
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


@contextlib.contextmanager
def exclusive(output):
    """OS-owned lock releases after crash; no stale-file deletion or parallel API runs."""
    output.mkdir(parents=True, exist_ok=True)
    with (output / "writer.lock").open("a+b") as handle:
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another process owns this study output") from None
        try:
            yield
        finally:
            if __import__("os").name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def validate_config(config):
    required = {
        "version",
        "role",
        "dataset",
        "selection",
        "reproduction",
        "exposure_ledger",
        "references",
        "backend",
        "input_profiles",
        "input_token_limit",
        "max_generation_attempts",
        "max_attempts_per_slot",
        "max_count_requests",
        "start_interval_seconds",
        "retry_wait_seconds",
        "noninferiority_margin",
        "minimum_long_quality",
        "minimum_independent_bugs",
        "bootstrap_samples",
    }
    if set(config) != required or config["version"] != VERSION:
        raise ValueError("Unexpected study config fields/version")
    if config["role"] not in {"development", "evaluation"}:
        raise ValueError("Unknown study role")
    backend = ExperimentConfig.model_validate(config["backend"])
    if backend.model_id != "gemma-4-31b-it" or backend.warmups != 0:
        raise ValueError("This entrypoint uses the hosted Gemma alias and zero warmups")
    if backend.max_calls != config["max_generation_attempts"]:
        raise ValueError("Generation caps must match")
    for key in (
        "input_token_limit",
        "max_generation_attempts",
        "max_count_requests",
        "minimum_independent_bugs",
        "bootstrap_samples",
    ):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"Invalid {key}")
    if config["max_attempts_per_slot"] not in {1, 2, 3}:
        raise ValueError("Retries must be bounded at 1..3 attempts")
    if (
        not 60 <= config["start_interval_seconds"] <= 3600
        or not 60 <= config["retry_wait_seconds"] <= 3600
    ):
        raise ValueError("Hosted study pacing must be at least 60 seconds")
    if (
        not 0 <= config["noninferiority_margin"] <= 0.2
        or not 0 < config["minimum_long_quality"] <= 1
    ):
        raise ValueError("Invalid preregistered quality criteria")
    if not config["input_profiles"] or any(
        set(p) != {"source_chars", "log_chars"}
        or any(type(n) is not int or n < 256 for n in p.values())
        for p in config["input_profiles"]
    ):
        raise ValueError("Invalid input profiles")
    return backend


def freeze(config, output):
    backend_config = validate_config(config)
    cases, datasets = load_cases([resolve(config["dataset"])])
    ledger = read(resolve(config["exposure_ledger"]))
    known = set(ledger["development_bug_ids"])
    # The original study cases are always known development, even if a ledger is edited.
    known.update(f"bugsjs-express-{i}" for i in range(1, 11))
    known.update(f"bugsjs-hessian-{i}" for i in (3, 5, 9))
    if config["role"] == "evaluation" and (known & {c["bug_id"] for c in cases}):
        raise ValueError("Previously inspected bugs cannot be final holdout; use development role")
    selection_path, reproduction = resolve(config["selection"]), resolve(config["reproduction"])
    selection = read(selection_path)
    # Offline revalidation of original archives, receipts, stdout, and published patches.
    destination = output / "verified-dataset.json"
    temporary = destination.exists()
    if temporary:
        destination = output / f".dataset-check-{uuid.uuid4().hex}.json"
    try:
        verified = build_dataset(reproduction, destination, selection_path)
    finally:
        if temporary and destination.exists():
            destination.unlink()  # One generated file, never a recursive cleanup.
    verified_cases = {c["id"]: c for c in verified["cases"]}
    if set(verified_cases) != {c["id"] for c in cases}:
        raise ValueError("Dataset does not match the entire frozen reproduction selection")
    for c in cases:
        if any(
            c[k] != verified_cases[c["id"]][k]
            for k in ("payload", "source_files", "provenance", "gold")
        ):
            raise ValueError("Dataset evidence differs from verified reproduction")
    reference_path = resolve(config["references"]) if config["references"] else None
    refs = read(reference_path) if reference_path else {"items": []}
    reference_ids = [r["bug_id"] for r in refs["items"]]
    if len(reference_ids) != len(set(reference_ids)):
        raise ValueError("Duplicate reviewer references")
    if config["role"] == "evaluation" and set(reference_ids) != {c["bug_id"] for c in cases}:
        raise ValueError("Holdout requires frozen reviewer references before generation")
    for r in refs["items"]:
        if sha(reproduction / r["bug_id"] / "reference.patch") != r["reference_patch_sha256"]:
            raise ValueError("Reviewer patch reference changed")
    planned = len(cases) * len(ARMS) * backend_config.repeats
    if planned > config["max_generation_attempts"]:
        raise ValueError(f"{planned} planned calls exceed explicit generation cap")
    fingerprint = {
        "config": config,
        "datasets": datasets,
        "selection_sha256": sha(selection_path),
        "reproduction_sha256": sha(reproduction / "reproduction.json"),
        "references_sha256": sha(reference_path) if reference_path else None,
        "exposure_ledger_sha256": sha(resolve(config["exposure_ledger"])),
        "schema": StudyDecision.model_json_schema(),
        "prompts": {length: prompt(length) for length in TARGETS},
        "code_sha256": {
            str(p.relative_to(ROOT)): sha(p)
            for p in sorted((ROOT / "src").rglob("*"))
            if p.is_file() and p.suffix in {".py", ".ttl", ".rq"}
        },
    }
    frozen_path = output / "protocol.json"
    if frozen_path.exists():
        if read(frozen_path)["fingerprint"] != fingerprint:
            raise ValueError("Frozen config/data/code changed. Use a NEW output directory")
        if (output / "protocol.sha256").read_text("ascii").strip() != sha(frozen_path):
            raise ValueError("Frozen protocol was modified")
        return read(frozen_path), cases
    rng = random.Random(backend_config.seed)
    slots = []
    for repeat in range(backend_config.repeats):
        order = list(cases)
        rng.shuffle(order)
        for case in order:
            arms = list(ARMS)
            rng.shuffle(arms)
            for arm in arms:
                slots.append(
                    {
                        "id": f"{len(slots):05d}",
                        "case": case["id"],
                        "bug_id": case["bug_id"],
                        "label": case["gold"]["label"],
                        "repeat": repeat,
                        "arm": arm,
                    }
                )
    protocol = {
        "version": VERSION,
        "frozen_at": now(),
        "fingerprint": fingerprint,
        "schedule": slots,
        "planned_generation_calls": planned,
        "minimum_minutes_at_configured_pacing": (planned - 1)
        * config["start_interval_seconds"]
        / 60,
        "independent_bugs": len(selection["cases"]),
        "reviewer_references": refs,
        "primary_comparisons": [
            "graph_short minus graph_long: mechanism noninferiority",
            "graph_short minus raw_short: mechanism improvement",
        ],
        "secondary_comparisons": [
            "graph minus text: structure with matched relationships",
            "text minus raw: supplied relationship information",
        ],
        "limits": [
            "Hosted alias is not verified QAT weights",
            "Independent human scoring required",
            "Bootstrap units are bugs, never repeated calls",
            "No generated repair is executed",
            "Development runs cannot establish the primary hypotheses",
        ],
    }
    save(frozen_path, protocol)
    (output / "protocol.sha256").write_text(sha(frozen_path) + "\n", encoding="ascii")
    (output / "inputs").mkdir(exist_ok=True)
    (output / "attempts").mkdir(exist_ok=True)
    return protocol, cases


def scope_for(case, reproduction):
    receipt = read(reproduction / case["bug_id"] / "receipt.json")
    state = "failed" if case["gold"]["label"] == "test_failure" else "fixed"
    record = receipt["records"][state]
    other = [
        t
        for t in record["report"]["failures"]
        if t["fullTitle"] != case["provenance"]["test_full_title"]
    ]
    return {
        "kind": "selected_test_observation",
        "process_exit_code": record["returncode"],
        "other_reported_failures": [
            {"title": t["fullTitle"], "message": t.get("err", {}).get("message", "")[:300]}
            for t in other
        ],
        "whole_suite_executed": False,
    }


def prepare_inputs(config, protocol, cases, output, backend=None):
    """All actual tokenizer checks finish before the first generation; identical evidence per block."""
    cache_path = output / "token-count-cache.json"
    cache = read(cache_path) if cache_path.exists() else {"attempts": 0, "counts": {}}
    inputs = {}
    for number, case in enumerate(cases):
        path = output / "inputs" / f"{number:04d}.json"
        if path.exists():
            value = read(path)
            if value["case"] != case["id"] or value["protocol_sha256"] != sha(
                output / "protocol.json"
            ):
                raise ValueError("Input checkpoint mismatch")
            if value["content_sha256"] != digest(
                {k: v for k, v in value.items() if k != "content_sha256"}
            ):
                raise ValueError("Input checkpoint changed")
            if value["token_counts_verified"] == (backend is not None):
                inputs[case["id"]] = value
                continue
            raise ValueError("Do not mix offline/mock and live inputs; use a separate output")
        for profile in config["input_profiles"]:
            prepared = prepare(
                case, **profile, process_scope=scope_for(case, resolve(config["reproduction"]))
            )
            counts = {}
            for arm in ARMS:
                representation, length = arm.split("_")
                payload = treatment(prepared, representation)
                key = digest([prompt(length), payload, StudyDecision.model_json_schema()])
                if backend is not None and key not in cache["counts"]:
                    if cache["attempts"] >= config["max_count_requests"]:
                        raise ValueError("Tokenizer request cap reached before generation")
                    cache["attempts"] += 1
                    save(cache_path, cache)
                    cache["counts"][key] = backend.count_tokens(
                        prompt(length),
                        payload,
                        config["backend"]["seed"],
                        schema=StudyDecision.model_json_schema(),
                    )
                    save(cache_path, cache)
                counts[arm] = cache["counts"][key] if backend is not None else None
            if backend is None or max(counts.values()) <= config["input_token_limit"]:
                value = {
                    "case": case["id"],
                    "protocol_sha256": sha(output / "protocol.json"),
                    "profile": profile,
                    "token_counts_verified": backend is not None,
                    "counts": counts,
                    "prepared": prepared,
                }
                value["content_sha256"] = digest(value)
                save(path, value)
                inputs[case["id"]] = value
                token_note = (
                    str(max(counts.values())) if backend is not None else "unmeasured (offline)"
                )
                print(
                    f"Input {number + 1}/{len(cases)} ready: max_tokens={token_note}, source_lines={len(prepared['base']['source_evidence'])}",
                    flush=True,
                )
                break
        else:
            save(
                output / "preflight-blocked.json",
                {
                    "case": case["id"],
                    "counts": counts,
                    "input_limit": config["input_token_limit"],
                    "generation_calls": 0,
                },
            )
            raise ValueError(
                "Input exceeds token budget even at the smallest shared profile; no generation started"
            )
    return inputs


def mock_response():
    return {
        "text": json.dumps(
            {
                "analysis_status": "insufficient_evidence",
                "statement": "Offline plumbing check only",
                "evidence_ids": [],
                "source_evidence_ids": [],
                "counter_evidence_ids": [],
                "target": None,
                "source_path": None,
                "source_line": None,
                "summary": "No actual inference",
                "uncertainty": "Quality is unmeasured",
                "next_check": "Run the configured live study",
                "cause_status": "insufficient_evidence",
                "mechanism": "No model was called",
                "predicted_root_path": None,
                "predicted_root_line": None,
            }
        ),
        "finish_reason": "stop",
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }


def validate_record(record, prepared, is_mock):
    try:
        if record["response"]["finish_reason"] != "stop":
            raise ValueError("Non-stop model response")
        decision, normalized = validate(record["response"]["text"], prepared)
        lo, hi = TARGETS[record["slot"]["arm"].split("_")[1]]
        expected = (
            "diagnosed" if record["slot"]["label"] == "test_failure" else "no_failure_evidence"
        )
        record.update(
            contract_valid=True,
            decision=decision,
            format_normalized=normalized,
            status_match=decision["analysis_status"] == expected if not is_mock else None,
            length_target_met=all(lo <= len(decision[f]) <= hi for f in FIELDS)
            if not is_mock
            else None,
            mechanism_target_met=40 <= len(decision["mechanism"]) <= 180 if not is_mock else None,
        )
    except ValueError:
        record.update(contract_valid=False, error="INVALID_DECISION")
    finally:
        record["validation_complete"] = True


def execute(
    config, protocol, inputs, output, backend=None, *, retry_uncertain=False, sleeper=time.sleep
):
    manifest_path = output / "manifest.json"
    manifest = (
        read(manifest_path)
        if manifest_path.exists()
        else {
            "version": VERSION,
            "protocol_sha256": sha(output / "protocol.json"),
            "mode": "live" if backend else "mock",
            "started_at": now(),
            "environment": environment(),
        }
    )
    if manifest["mode"] != ("live" if backend else "mock") or manifest["protocol_sha256"] != sha(
        output / "protocol.json"
    ):
        raise ValueError("Run identity mismatch")
    manifest["state"] = "running"
    save(manifest_path, manifest)
    consecutive_backend_errors = 0
    previous_start = max(
        (
            datetime.fromisoformat(read_attempt(p)["started_at"]).timestamp()
            for p in (output / "attempts").glob("*.json")
        ),
        default=0.0,
    )
    try:
        for slot in protocol["schedule"]:
            attempts = sorted((output / "attempts").glob(slot["id"] + "-*.json"))
            records = [read_attempt(p) for p in attempts]
            if any(r["state"] == "returned" for r in records):
                for path, record in zip(attempts, records, strict=True):
                    if record["state"] == "returned" and not record.get("validation_complete"):
                        validate_record(record, inputs[slot["case"]]["prepared"], backend is None)
                        save_attempt(path, record)
                continue  # Invalid/short/long responses are retained, never retried for quality.
            if records and records[-1]["state"] == "in_flight" and not retry_uncertain:
                continue  # Delivery outcome unknown: explicit flag needed to spend again.
            if records and records[-1].get("error") not in {
                None,
                "MODEL_HTTP_429",
                "MODEL_HTTP_500",
                "MODEL_HTTP_502",
                "MODEL_HTTP_503",
                "MODEL_TIMEOUT",
                "MODEL_CONNECTION_OR_JSON_ERROR",
            }:
                continue
            prepared = inputs[slot["case"]]["prepared"]
            representation, length = slot["arm"].split("_")
            for attempt in range(len(attempts), config["max_attempts_per_slot"]):
                total_attempts = len(list((output / "attempts").glob("*.json")))
                if backend and total_attempts >= config["max_generation_attempts"]:
                    manifest["state"] = "call_cap_reached"
                    return
                if backend:
                    interval = max(
                        config["start_interval_seconds"],
                        config["retry_wait_seconds"] if attempt else 0,
                    )
                    sleeper(max(0, interval - (time.time() - previous_start)))
                record = {
                    "slot": slot,
                    "attempt": attempt + 1,
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
                path = output / "attempts" / f"{slot['id']}-{attempt + 1}.json"
                save_attempt(path, record)  # Durable reservation BEFORE network I/O.
                previous_start = time.time()
                start = time.perf_counter()
                try:
                    response = (
                        backend.generate(
                            prompt(length),
                            treatment(prepared, representation),
                            config["backend"]["seed"] + slot["repeat"],
                            schema=StudyDecision.model_json_schema(),
                        )
                        if backend
                        else mock_response()
                    )
                    record.update(
                        state="returned",
                        response=response,
                        request_seconds=time.perf_counter() - start if backend else None,
                    )
                    # Persist raw response before parsing; a validator crash cannot cause a repeat.
                    save_attempt(path, record)
                    validate_record(record, prepared, backend is None)
                except BackendFailure as exc:
                    record.update(
                        state="backend_error",
                        error=str(exc).split(":", 1)[0],
                        request_seconds=time.perf_counter() - start,
                    )
                except ValueError:
                    record.update(error="INVALID_DECISION")
                record["finished_at"] = now()
                save_attempt(path, record)
                print(
                    f"[{slot['id']}/{len(protocol['schedule'])}] {slot['arm']} attempt={attempt + 1} {record['state']} valid={record['contract_valid']}",
                    flush=True,
                )
                if record["state"] == "returned":
                    consecutive_backend_errors = 0
                    break
                consecutive_backend_errors += 1
                if consecutive_backend_errors >= 3:
                    manifest["state"] = "provider_error_circuit_open"
                    return
                if record["error"] in {"MODEL_HTTP_401", "MODEL_HTTP_403", "MODEL_HTTP_404"}:
                    manifest["state"] = "authentication_or_model_error"
                    return
                if record["error"] not in {
                    "MODEL_HTTP_429",
                    "MODEL_HTTP_500",
                    "MODEL_HTTP_502",
                    "MODEL_HTTP_503",
                    "MODEL_TIMEOUT",
                    "MODEL_CONNECTION_OR_JSON_ERROR",
                }:
                    break
        manifest["state"] = "schedule_finished"
    except KeyboardInterrupt:
        manifest["state"] = "interrupted"
        raise
    finally:
        if manifest["state"] == "running":
            manifest["state"] = "interrupted_or_failed"
        manifest.update(
            updated_at=now(), attempt_files=len(list((output / "attempts").glob("*.json")))
        )
        save(manifest_path, manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("plan", "mock", "run", "report"), nargs="?", default="plan"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--retry-uncertain", action="store_true")
    args = parser.parse_args(argv)
    config = read(args.config)
    if args.command == "run" and not args.live:
        parser.error("Generation is opt-in: run --live. Use plan or mock for offline checks")
    output = args.output or ROOT / "results" / (
        "research-study-v2-mock" if args.command == "mock" else "research-study-v2"
    )
    from .study_analysis import summarize

    with exclusive(output):
        protocol, cases = freeze(config, output)
        print(
            f"role={config['role']}; bugs={protocol['independent_bugs']}; planned={protocol['planned_generation_calls']}; cap={config['max_generation_attempts']}; minimum pacing={protocol['minimum_minutes_at_configured_pacing']:.0f} min",
            flush=True,
        )
        if args.command == "plan":
            print(
                "Offline protocol frozen. No tokenizer/generation request. Run --live uses this frozen dataset, input policy and code."
            )
            return 0
        if args.command == "report":
            summarize(output)
            return 0
        # Completed runs do not even call provider metadata on a repeated launch.
        if (output / "summary.json").exists() and read(output / "summary.json").get(
            "all_responses_received"
        ):
            summarize(output)
            return 0
        backend = GeminiBackend(validate_config(config)) if args.command == "run" else None
        try:
            if backend:
                server = backend.preflight()
                server_path = output / "server.json"
                if server_path.exists() and read(server_path) != server:
                    raise ValueError("Provider model identity changed; use a new study")
                save(server_path, server)
            inputs = prepare_inputs(config, protocol, cases, output, backend)
            execute(config, protocol, inputs, output, backend, retry_uncertain=args.retry_uncertain)
        finally:
            if backend:
                backend.close()
            if (output / "manifest.json").exists():
                summarize(output)
    return 0 if read(output / "summary.json")["all_responses_received"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, BackendFailure, OSError) as exc:
        # Never print HTTP exception objects containing request headers or env values.
        print(
            f"Study stopped: {str(exc) if isinstance(exc, (ValueError, BackendFailure)) else type(exc).__name__}"
        )
        raise SystemExit(2) from None
