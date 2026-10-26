"""Fixed-input review experiments. Research-only helpers; production sources stay frozen."""

import copy
import hashlib
import importlib.metadata
import json
import platform
import random
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cross_program_common_v4 import fresh, hashes, now, read, sha, write
from revalidate_study_v4 import POLICY as NORMALIZATION_POLICY
from revalidate_study_v4 import decode, normalize

from ai_error_check_agent.diagnosis_routing import CONCISE_PROMPT, PREFETCH_PROMPT
from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.experiments.design import ExperimentConfig
from ai_error_check_agent.experiments.study_v4 import source_manifest
from ai_error_check_agent.experiments.study_v4_arms import BOUND_PROMPT, BoundDecision, bound_expand
from ai_error_check_agent.graph_compact import PROMPT, CompactDecision, expand
from ai_error_check_agent.source_analysis import (
    CODE_PROMPT,
    SourceFindings,
    parse_stage,
    stage_schema,
)
from ai_error_check_agent.validation import load_prompt

VERSION = "review-controls.v1"
__all__ = ["NORMALIZATION_POLICY", "fresh", "hashes", "now", "write"]
DEFAULT_ROOT = ROOT / "evaluation/review-controls-v4"
DEFAULT_CONFIG = ROOT / "config/study.gemma-api.v4.review-controls.json"
ARMS = ("R_auto", "R_llm", "B_eq", "G_eq")
CONTEXTS = ("P_supported", "P_unknown", "N_external_supply", "N_snapshot_conflict")
BOUND_REVIEW_PROMPT = BOUND_PROMPT + (
    "\n보류할 때 decision=defer, plan=defer로 쓴다. target/code_path는 빈 문자열, "
    "code_line=1, evidence_ids/source_evidence_ids는 빈 배열, packaging=null로 둘 수 있다.\n"
)
FALLBACK_PROMPT = load_prompt() + CODE_PROMPT + PREFETCH_PROMPT + CONCISE_PROMPT
HINT_KEYS = {
    "candidate": {
        "id",
        "kind",
        "target",
        "code_path",
        "code_line",
        "rule",
        "evidence_ids",
        "source_evidence_ids",
        "claim",
    },
    "packaging_candidate": {
        "id",
        "kind",
        "rule",
        "repo_path",
        "target",
        "dockerfile",
        "archive_sha256",
        "root_directory",
        "ignore_path",
        "ignore_state",
        "source_evidence_ids",
        "ignore_evidence_ids",
        "claim",
    },
}


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def ordered(value):
    return json.loads(canonical(value))


def environment():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {
            p: importlib.metadata.version(p)
            for p in ("rdflib", "pyshacl", "pydantic", "httpx", "jsonschema")
        },
    }


def code_manifest():
    paths = list((ROOT / "tools").glob("*review_controls_v4.py"))
    paths += [
        ROOT / "docs" / name
        for name in (
            "PAPER_STUDY_V4_REVIEW_CONTROLS_COMMANDS.md",
            "PAPER_STUDY_V4_REVIEW_CONTROLS_IMPLEMENTATION.md",
            "PAPER_STUDY_V4_REVIEW_ABLATION_MATCHED_BASELINE_HANDOFF.md",
        )
    ]
    paths += [
        ROOT / "tools/review_controls_common_v4.py",
        ROOT / "tools/review_controls_prepare_v4.py",
        ROOT / "tools/tests/test_review_controls_v4.py",
        ROOT / "run_review_controls_v4.cmd",
    ]
    paths += [
        ROOT / "tools" / p
        for p in (
            "cross_program_common_v4.py",
            "remediation_cross_program_docker_v4.py",
            "remediation_docker_v4.py",
            "remediation_adapter_v4.py",
            "evaluate_remediation_v4.py",
            "revalidate_study_v4.py",
            "recover_undispatched_cross_program_v4.py",
            "report_cross_program_v4.py",
            "checks/cross_program/check.cjs",
        )
    ]
    return {
        **source_manifest(),
        **{p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(paths)) if p.is_file()},
    }


def verify_files(manifest):
    for name, expected in manifest.items():
        path = (ROOT / name).resolve()
        if ROOT not in path.parents or not path.is_file() or sha(path) != expected:
            raise ValueError(f"Frozen file mismatch: {name}")


def configuration(path):
    config = read(path)
    backend = ExperimentConfig.model_validate(config["backend"])
    if (
        config["version"] != VERSION
        or config["role"] != "development"
        or config["arms"] != list(ARMS)
    ):
        raise ValueError("Unsupported review experiment contract")
    if (
        backend.model_id != "gemma-4-31b-it"
        or backend.temperature != 0
        or backend.max_tokens != 4096
        or backend.timeout_seconds != 180
        or backend.enable_thinking
        or backend.repeats != 3
        or backend.seed != 42
        or backend.warmups != 0
        or backend.request_interval_seconds != 60
        or backend.max_calls > 216
        or config["primary_call_cap"] > 108
        or config["fallback_call_cap"] > 108
        or backend.max_calls != config["primary_call_cap"] + config["fallback_call_cap"]
    ):
        raise ValueError("Model settings or call budget differ from protocol")
    root = (ROOT / config["root"]).resolve()
    if ROOT not in root.parents:
        raise ValueError("Study root must stay inside project")
    return config, backend, root


def project_hints(packet):
    return ordered(
        {
            kind: {k: v for k, v in packet[kind].items() if k in allowed}
            for kind, allowed in HINT_KEYS.items()
            if kind in packet
        }
    )


def validate_hints(hints):
    if set(hints) != set(HINT_KEYS):
        raise ValueError("Both eligible candidates are required")
    if any(set(hints[k]) - HINT_KEYS[k] for k in hints):
        raise ValueError("Candidate hint contains forbidden fields")


def model_request(arm, common, compact=None, hints=None, *, fallback=False):
    if fallback:
        return (
            FALLBACK_PROMPT,
            ordered(common),
            ordered(stage_schema("source_findings", SourceFindings)),
        )
    if arm in ("R_auto", "R_llm"):
        return PROMPT, ordered(compact), ordered(CompactDecision.model_json_schema())
    if arm not in ("B_eq", "G_eq"):
        raise ValueError("Unknown condition")
    data = copy.deepcopy(common)
    if arm == "G_eq":
        validate_hints(hints)
        data["rule_candidates"] = copy.deepcopy(hints)
    return BOUND_REVIEW_PROMPT, ordered(data), ordered(BoundDecision.model_json_schema())


def bundle_view(common):
    return SimpleNamespace(
        context=common["context"], evidence_ids={r["id"] for r in common["logs"]}
    )


def validate_full(text, common):
    return parse_stage(
        text,
        bundle_view(common),
        "source_findings",
        SourceFindings,
        source_lines=common["source_evidence"],
    )


def automatic_decision(compact):
    # No references, case labels, contract parser or backend is consulted here.
    return {
        "decision": "accept",
        "candidate_id": compact["candidate"]["id"],
        "plan": "add_docker_copy"
        if compact.get("packaging_candidate")
        else "provide_verified_file",
        "summary": "규칙으로 연결된 파일 읽기 및 제공 Dockerfile의 복사 누락 후보입니다.",
        "uncertainty": "문맥 검토 없이 구성한 조건부 제안이며 실제 배포 설정과 파일 공급 계약을 확인해야 합니다.",
        "counter_evidence_ids": [],
    }


def assess_primary(text, arm, common, compact=None):
    """Keep production transition AND independent consistency/reference assessment."""
    result = {
        "parsed_decision": None,
        "raw_declared_decision": None,
        "raw_declared_plan": None,
        "schema_valid": False,
        "decision_consistent": None,
        "assessment_error": [],
        "counter_evidence_ids": [],
        "invalid_reference_ids": [],
        "server_transition_code": None,
        "validation_error": None,
        "effective_decision": "error",
        "analysis": None,
    }
    try:
        raw = decode(text)
        if isinstance(raw, dict):
            result.update(
                raw_declared_decision=raw.get("decision"), raw_declared_plan=raw.get("plan")
            )
        model = CompactDecision if arm in ("R_auto", "R_llm") else BoundDecision
        d = model.model_validate(raw).model_dump()
        result.update(
            parsed_decision=d, schema_valid=True, counter_evidence_ids=d["counter_evidence_ids"]
        )
    except (ValueError, TypeError, RecursionError):
        result["assessment_error"].append("invalid_json_or_schema")
    d = result["parsed_decision"]
    if d:
        ev = {r["id"] for r in common["logs"]}
        sc = {r["id"] for r in common["source_evidence"]}
        invalid = (set(d["counter_evidence_ids"]) | set(d.get("evidence_ids", []))) - ev
        invalid |= set(d.get("source_evidence_ids", [])) - sc
        result["invalid_reference_ids"] = sorted(invalid)
        if invalid:
            result["assessment_error"].append("invalid_reference")
        if arm in ("R_auto", "R_llm") and d["candidate_id"] != compact["candidate"]["id"]:
            result["assessment_error"].append("invalid_candidate_reference")
        consistent = (d["decision"] == "defer" and d["plan"] == "defer") or (
            d["decision"] == "accept" and d["plan"] != "defer" and not d["counter_evidence_ids"]
        )
        result["decision_consistent"] = consistent
        if not consistent:
            result["assessment_error"].append("inconsistent_decision")
    try:
        assembled = (
            expand(text, compact) if arm in ("R_auto", "R_llm") else bound_expand(text, common)
        )
        result["analysis"] = validate_full(assembled, common)
        result["effective_decision"] = "accept"
    except DiagnosisError as exc:
        result["server_transition_code"] = exc.code
        result["validation_error"] = {"code": exc.code, "message": str(exc)}
        if exc.code == "COMPACT_DEFERRED":
            result["effective_decision"] = (
                "defer"
                if d and not result["assessment_error"]
                else "blocked_inconsistent_or_invalid"
            )
    return result


def assess_final(text, common, *, allow_normalization=False):
    changes = []
    try:
        raw = decode(text)
        if allow_normalization:
            raw, changes = normalize(raw)
        value = validate_full(json.dumps(raw, ensure_ascii=False), common)
        return {"valid": True, "analysis": value, "error": None, "normalization_changes": changes}
    except (DiagnosisError, ValueError, TypeError, RecursionError) as exc:
        return {
            "valid": False,
            "analysis": None,
            "error": getattr(exc, "code", "INVALID_JSON"),
            "normalization_changes": changes,
        }


def schedule(cases, seed=42):
    rng = random.Random(seed)
    eligible = [c for c in cases if c["eligible"]]
    rows = [{"case_id": c["id"], "arm": "R_auto", "repeat": 0} for c in eligible]
    blocks = [(c["id"], repeat) for repeat in range(3) for c in eligible]
    rng.shuffle(blocks)
    for cid, repeat in blocks:
        arms = ["R_llm", "B_eq", "G_eq"]
        rng.shuffle(arms)
        rows += [{"case_id": cid, "arm": a, "repeat": repeat} for a in arms]
    for row in rows:
        row["cell_id"] = digest(row)[:20]
    return rows


def packet_files(root, case_id, arm=None):
    folder = root / "packets" / case_id
    common = read(folder / "common.json")
    # B_eq never even loads the candidate packet, including during fallback.
    compact = read(folder / "compact.json") if arm in ("R_auto", "R_llm") else None
    hints = read(folder / "candidate-hints.json") if arm == "G_eq" else None
    return common, compact, hints


def original_routes(root):
    path = root / "freeze-manifest.json"
    return {r["case_id"]: r for r in read(path).get("routing_audit", [])} if path.is_file() else {}


def prepared_manifest(root, config_path):
    files = {
        p.relative_to(ROOT).as_posix(): sha(p)
        for p in root.rglob("*")
        if p.is_file() and p.name != "freeze-manifest.json"
    }
    files[config_path.resolve().relative_to(ROOT).as_posix()] = sha(config_path)
    return {"code": code_manifest(), "files": files, "environment": environment()}


def verify_prepared(root, config_path, manifest):
    if configuration(config_path)[2] != root.resolve():
        raise ValueError("Prepared root differs from configured root")
    verify_files(manifest["code"])
    verify_files(manifest["files"])
    if code_manifest() != manifest["code"] or environment() != manifest["environment"]:
        raise ValueError("Frozen code/environment changed")
    if config_path.resolve().relative_to(ROOT).as_posix() not in manifest["files"]:
        raise ValueError("Configuration is not frozen")


def verify_freeze(root, config_path):
    freeze = read(root / "freeze-manifest.json")
    verify_prepared(root, config_path, freeze["manifest"])
    if not freeze["preflight_passed"] or not freeze["mock_passed"] or not freeze["tests_passed"]:
        raise ValueError("Pre-live prerequisites did not pass")
    verify_files(freeze["verification_files"])
    return freeze
