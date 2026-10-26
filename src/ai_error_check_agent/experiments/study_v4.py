"""End-to-end, mechanism, graph integrity and human review experiment runner."""

import argparse
import asyncio
import gzip
import hashlib
import importlib.metadata
import io
import json
import platform
import random
import re
import tarfile
import time
from pathlib import Path

import httpx

from ..api import create_app
from ..diagnosis_routing import DiagnosisSettings
from ..knowledge.compact import resources
from ..source_contracts import safe_source_path
from .backends import GeminiBackend
from .design import ROOT, ExperimentConfig, digest
from .study import exclusive
from .study_v4_arms import ARMS, ResearchService
from .study_v4_graph import score
from .study_v4_runtime import Session

VERSION = "iris-study.v4"
DEFAULT_CONFIG = ROOT / "config/study.gemma-api.v4.development.json"


def read(path):
    return json.loads(Path(path).read_text("utf-8-sig"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", "utf-8"
    )
    # Windows indexers/antivirus may briefly hold the existing destination open.
    # Retry ONLY the atomic local replacement, never the provider request.
    for attempt in range(6):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.01 * 2**attempt)


def archive(case):
    """Reproducible bytes; source contents alone reach the archive adapter."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for source in sorted(case["source_files"], key=lambda f: f["path"]):
            safe_source_path(source["path"])
            raw = source["content"].encode()
            entry = tarfile.TarInfo("study-repository-abcdef1/" + source["path"])
            entry.size, entry.mtime, entry.mode = len(raw), 0, 0o644
            tar.addfile(entry, io.BytesIO(raw))
    return gzip.compress(stream.getvalue(), mtime=0)


def load(config_path):
    config = read(config_path)
    if config.get("version") != VERSION or config.get("role") not in ("development", "evaluation"):
        raise ValueError("Unknown study version/role")
    backend = ExperimentConfig.model_validate(config["backend"])
    if backend.model_id != "gemma-4-31b-it" or backend.warmups != 0:
        raise ValueError(
            "v4 uses the configured native hosted Gemma API with no hidden warmup calls"
        )
    if (
        not config["arms"]
        or len(set(config["arms"])) != len(config["arms"])
        or set(config["arms"]) - ARMS.keys()
    ):
        raise ValueError("Invalid or duplicate arms")
    if not 0 < config.get("graph_timeout_seconds", 0.5) <= 60:
        raise ValueError("Invalid graph timeout")
    statistics_config = config.get("statistics", {})
    if (
        not 0 < statistics_config.get("quality_noninferiority_margin", 0.05) < 1
        or statistics_config.get("minimum_clusters_for_ni", 20) < 2
    ):
        raise ValueError("Invalid prespecified non-inferiority settings")
    dataset = read(ROOT / config["dataset"])
    references = read(ROOT / config["references"])
    cases = dataset["cases"]
    if not cases or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Empty dataset or duplicate case IDs")
    for case in cases:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", case["id"]):
            raise ValueError("Case ID must be a safe, short identifier")
        if not case.get("cluster_id") or case["id"] not in references["cases"]:
            raise ValueError("Each case needs a lineage cluster and reference entry")
        if set(case) - {
            "id",
            "cluster_id",
            "pair_id",
            "variant",
            "split",
            "exposure",
            "payload",
            "source_files",
        }:
            raise ValueError("Unexpected dataset fields: keep answers in separate references")
        if len({s["path"] for s in case["source_files"]}) != len(case["source_files"]):
            raise ValueError("Duplicate source path")
        for source in case["source_files"]:
            safe_source_path(source["path"])
        if config["role"] == "evaluation":
            ref = references["cases"][case["id"]]
            if case["split"] != "held_out" or case["exposure"] != "never_development":
                raise ValueError("Evaluation requires a held-out, previously unexposed dataset")
            if (
                not ref.get("approved_by")
                or not ref.get("approved_at")
                or ref.get("status") != "approved"
            ):
                raise ValueError("Evaluation references require recorded human approval")
            if ref.get("case_sha256") != digest(case):
                raise ValueError("Reference approval does not match the current case")
    return config, backend, cases, references


def source_manifest():
    paths = list((ROOT / "src").rglob("*"))
    paths += [ROOT / "pyproject.toml", ROOT / "requirements-research-win-py312.lock"]
    return {
        str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
        if p.is_file() and p.suffix in {".py", ".json", ".ttl", ".rq", ".md", ".toml", ".lock"}
    }


def plan(config_path):
    config, backend, cases, references = load(config_path)
    cells = len(cases) * len(config["arms"]) * backend.repeats
    return {
        "version": VERSION,
        "role": config["role"],
        "cases": len(cases),
        "lineage_clusters": len({c["cluster_id"] for c in cases}),
        "arms": config["arms"],
        "cells": cells,
        "generation_call_upper_bound": cells * 3,
        "hard_generation_call_budget": backend.max_calls,
        "count_token_requests": 0,
        "automatic_retries": 0,
        "maximum_pacing_minutes_excluding_generation": cells
        * 3
        * backend.request_interval_seconds
        / 60,
        "human_reference_approvals": sum(
            bool(r.get("approved_by")) for r in references["cases"].values()
        ),
        "warning": "Authored development fixtures are not independent held-out bugs."
        if config["role"] == "development"
        else None,
    }


async def diagnose_cell(case, arm, factory, *, graph_timeout=0.5, fault=None):
    service = ResearchService(arm, timeout_seconds=graph_timeout, fault=fault)
    blob = archive(case)
    app = create_app(
        factory,
        dev=True,
        diagnosis_settings=DiagnosisSettings(ARMS[arm]["mode"]),
        compact_service=service,
        archive_transport=httpx.MockTransport(lambda _: httpx.Response(200, content=blob)),
    )
    started = time.monotonic()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://study"
    ) as client:
        response = await client.post("/diagnose", json=case["payload"])
    elapsed = (time.monotonic() - started) * 1000
    # Graph workers retain their production timeout behavior; collect late trace separately.
    graph_completed_before_drain = service._task is None or service._task.done()
    drain_error = None
    if service._task is not None:
        try:
            await asyncio.shield(service._task)
        except Exception as exc:  # noqa: BLE001 - record optional graph worker failures
            drain_error = type(exc).__name__
    return {
        "http_status": response.status_code,
        "result": response.json(),
        "wall_ms": elapsed,
        "graph_report": service.last_report,
        "graph_trace": service.trace.export(),
        "graph_completed_before_drain": graph_completed_before_drain,
        "graph_drain_error": drain_error,
        "archive_sha256": hashlib.sha256(blob).hexdigest(),
        "factorial_applied": ARMS[arm]["experiment"] == "E2" and service.common is not None,
    }


def schedule(cases, config, backend):
    rng = random.Random(backend.seed)
    order = []
    for repeat in range(backend.repeats):
        shuffled = list(cases)
        rng.shuffle(shuffled)
        for case in shuffled:
            arms = list(config["arms"])
            rng.shuffle(arms)
            for arm in arms:
                order.append(
                    {
                        "case_id": case["id"],
                        "arm": arm,
                        "repeat": repeat,
                        "cell_id": digest([case["id"], arm, repeat])[:20],
                    }
                )
    return order


async def run(config_path, output, *, live=False):
    config, backend_config, cases, references = load(config_path)
    output = Path(output).resolve()
    with exclusive(output):
        frozen = {
            "version": VERSION,
            "config": config,
            "cases": cases,
            "references": references,
            "source_manifest": source_manifest(),
            "live": live,
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "packages": {
                    p: importlib.metadata.version(p)
                    for p in ("rdflib", "pyshacl", "pydantic", "httpx")
                },
            },
            "schedule": schedule(cases, config, backend_config),
        }
        path = output / "frozen.json"
        if path.exists() and digest(read(path)) != digest(frozen):
            raise ValueError(
                "Frozen inputs/code/environment/mode changed. Use a new output directory."
            )
        save(path, frozen)
        for pending in (output / "cells").glob("*/state.json"):
            if read(pending)["status"] == "started":
                raise ValueError(
                    f"Unfinished cell {pending.parent.name}; inspect calls, then resolve it explicitly. No automatic resend."
                )
        backend = GeminiBackend(backend_config) if live else None
        try:
            identity = (
                backend.preflight()
                if live
                else {"real_model": False, "backend": "mock", "tokens": "not measured"}
            )
            provider_path = output / "provider.json"
            if provider_path.exists() and read(provider_path) != identity:
                raise ValueError("Provider identity changed; use a new output directory")
            save(provider_path, identity)
            init_started = time.perf_counter()
            resources()  # Identical local resource initialization; no hidden provider warmups.
            if not (output / "initialization.json").exists():
                save(
                    output / "initialization.json",
                    {
                        "resources_ms": (time.perf_counter() - init_started) * 1000,
                        "note": "Local initialization before first API cell; graph validation cold-start remains in the first applicable cell.",
                    },
                )
            session = Session(output, backend, backend_config, save, read, live=live)
            by_id = {c["id"]: c for c in cases}
            for index, slot in enumerate(frozen["schedule"]):
                cell = output / "cells" / slot["cell_id"]
                if (cell / "state.json").exists() and read(cell / "state.json")["status"] in {
                    "completed",
                    "excluded",
                }:
                    state = read(cell / "state.json")
                    if state["status"] == "completed" and (
                        not (cell / "result.json").exists()
                        or state.get("result_sha256") != digest(read(cell / "result.json"))
                    ):
                        raise ValueError(
                            "Completed result missing or changed; preserve and investigate the run"
                        )
                    continue
                if live and session.calls + 3 > backend_config.max_calls:
                    print(
                        "Stopped before the next cell: reserve of three calls exceeds the configured budget.",
                        flush=True,
                    )
                    break
                print(
                    f"[{index + 1}/{len(frozen['schedule'])}] {slot['case_id']} / {slot['arm']}",
                    flush=True,
                )
                save(cell / "state.json", {**slot, "status": "started"})
                case = by_id[slot["case_id"]]
                result = await diagnose_cell(
                    case,
                    slot["arm"],
                    session.factory(cell, backend_config.seed + slot["repeat"]),
                    graph_timeout=config.get("graph_timeout_seconds", 0.5),
                )
                result["graph_scores"] = score(
                    result["graph_trace"]["atoms"], references["cases"][case["id"]].get("graph")
                )
                result["graph_scores_scope"] = (
                    "pipeline_extraction"
                    if slot["arm"] not in {"adaptive", "F", "C"}
                    else "not_applicable_no_graph"
                )
                if slot["arm"] == "procedural_RC":
                    result["graph_scores_scope"] = "not_applicable_plain_python_no_rdf"
                if result["graph_scores_scope"] != "pipeline_extraction":
                    result["graph_scores"] = None
                save(cell / "result.json", {**slot, **result})
                save(
                    cell / "state.json",
                    {
                        **slot,
                        "status": "completed",
                        "result_sha256": digest(read(cell / "result.json")),
                    },
                )
                # Do not burn the rest of a study on a persistent provider configuration failure.
                error = (result["result"].get("error") or {}).get("code", "")
                if live and (
                    error.startswith("MODEL_HTTP_")
                    or error
                    in {"MODEL_TIMEOUT", "MODEL_CONNECTION_OR_JSON_ERROR", "STUDY_CALL_BUDGET"}
                ):
                    print(
                        f"Provider failure {error}; saved this cell and stopped. A rerun resumes remaining cells without retrying it.",
                        flush=True,
                    )
                    break
        finally:
            if backend:
                backend.close()
    from .study_v4_review import report

    report(output)
    print(f"Saved: {output}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="IRIS v4 actual pipeline research")
    parser.add_argument(
        "command",
        choices=[
            "plan",
            "mock",
            "run",
            "audit",
            "report",
            "review-export",
            "review-import",
            "resolve",
            "reproduce",
        ],
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "results/research-study-v4-development"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--cell")
    parser.add_argument("--reason")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "plan":
        print(json.dumps(plan(args.config), ensure_ascii=False, indent=2))
    elif args.command in {"mock", "run"}:
        if args.command == "run" and not args.live:
            parser.error("run requires --live; use mock for offline execution")
        if args.command == "mock" and args.live:
            parser.error("mock cannot use --live")
        asyncio.run(run(args.config, args.output, live=args.command == "run"))
    elif args.command == "audit":
        from .study_v4_audit import audit

        asyncio.run(audit(args.config, args.output))
    elif args.command == "resolve":
        if (
            not args.cell
            or not re.fullmatch(r"[a-f0-9]{20}", args.cell)
            or not args.reason
            or not (args.output / "cells" / args.cell / "state.json").is_file()
        ):
            parser.error("resolve needs an existing --cell ID and a recorded --reason")
        with exclusive(args.output):
            path = args.output / "cells" / args.cell / "state.json"
            state = read(path)
            if state["status"] != "started":
                raise ValueError("Only an unfinished cell can be resolved")
            save(path, {**state, "status": "excluded", "reason": args.reason})
    elif args.command == "reproduce":
        from .study_v4_reproduce import reproduce

        reproduce(args.output, execute=args.execute)
    else:
        from .study_v4_review import export_reviews, import_reviews, report

        if args.command == "report":
            report(args.output)
        elif args.command == "review-export":
            export_reviews(args.output)
        else:
            if not args.file:
                parser.error("review-import needs --file")
            import_reviews(args.output, args.file)


if __name__ == "__main__":
    main()
