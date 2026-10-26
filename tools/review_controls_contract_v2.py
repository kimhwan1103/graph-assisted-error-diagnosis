"""Versioned contract explanations and diagnostic-only checks; legacy policy stays frozen."""

import copy
import random
import re
from pathlib import Path
from types import SimpleNamespace

import review_controls_common_v4 as legacy
from jsonschema import Draft202012Validator

from ai_error_check_agent.errors import DiagnosisError
from ai_error_check_agent.experiments.backends import GeminiBackend
from ai_error_check_agent.experiments.design import ExperimentConfig
from ai_error_check_agent.validation import strict_json

ROOT = legacy.ROOT
VERSION = "review-controls.contract-v2"
PREFIX = "review-controls-v4-contract-v2"
LEGACY_ROOT = legacy.DEFAULT_ROOT
DEFAULT_ROOT = ROOT / "evaluation" / PREFIX
DEFAULT_CONFIG = ROOT / "config/study.gemma-api.v4.review-controls.contract-v2.json"
DEFAULT_AUDIT = ROOT / "results" / (PREFIX + "-audit")
ARMS = legacy.ARMS
read, write, sha, digest = legacy.read, legacy.write, legacy.sha, legacy.digest
fresh, hashes, now = legacy.fresh, legacy.hashes, legacy.now
canonical, ordered = legacy.canonical, legacy.ordered
packet_files, automatic_decision = legacy.packet_files, legacy.automatic_decision
assess_primary, assess_final = legacy.assess_primary, legacy.assess_final
validate_full, original_routes = legacy.validate_full, legacy.original_routes
project_hints, verify_files = legacy.project_hints, legacy.verify_files

BOUND_CONTRACT = """
[출력 필드의 역할]
code_path와 code_line은 오류 로그의 스택과 연결되는 파일 읽기 소스의 위치이다.
수정할 Dockerfile의 위치를 이 필드에 대신 넣지 않는다.
source_evidence_ids에는 code_path와 같은 파일에 속한 SC ID만 넣는다.
선택한 SC 행에는 code_line에 해당하는 행이 포함되어야 한다.
Dockerfile이나 다른 파일의 SC ID를 이 배열에 함께 넣지 않는다.
다른 파일의 내용을 읽고 판단에 활용하는 것은 가능하며,
이 배열은 모든 참고 자료의 목록이 아니라 읽기 소스 위치의 근거이다.
evidence_ids는 입력 logs에 실제 존재하는 EV ID이다.
accept 시에는 비어 있지 않아야 한다. SC ID를 대신 넣지 않는다.
add_docker_copy를 선택하면 packaging.repo_path에 아카이브의 파일 경로를,
packaging.insert_after_line에 Dockerfile의 마지막 관련 COPY 지시문의 실제 행 번호를 넣는다.
SC ID의 숫자 부분이나 전체 소스 배열의 인덱스를 쓰지 않는다.
Dockerfile 근거는 원문의 source_evidence에서 검토하고,
서버가 packaging 연결값과 원문을 이용해 배포 근거를 구성한다.
defer 시에는 기존 보류용 필드 규약을 사용한다.
판단에 반하는 실제 로그를 인용할 때는 기존 지시대로 counter_evidence_ids에 기록한다.
근거 ID나 행 번호를 만들어 내지 않는다.
"""
FALLBACK_CONTRACT = """
[응답 계약 확인]
변경 코드의 language는 제공된 스키마의 enum 중에서 선택한다.
Dockerfile 코드의 language는 "text"를 사용한다.
"dockerfile"은 이 응답 스키마가 허용하는 값이 아니다.
코드의 내용과 변경 대상은 실제 Dockerfile 계획에 맞게 작성한다.
observations의 각 evidence_ids는 비어 있지 않아야 하며,
해당 관찰을 뒷받침하는 실제 로그 EV ID만 사용한다.
소스에서만 확인한 사실을 로그 관찰로 만들고 무관한 EV ID를 붙이지 않는다.
그 사실은 적절한 source_findings와 실제 SC 인용으로 표현한다.
source_findings의 각 finding에는 같은 path에 속한 SC ID만 넣는다.
start_line과 end_line은 그 finding에서 인용한 SC 행 번호의 최솟값과 최댓값이다.
서로 다른 파일의 근거는 각각 다른 finding으로 나눈다.
evidence_ids와 hypothesis_ids의 연결도 스키마 및 기존 참조 규약을 따른다.
근거가 없으면 관찰이나 위치를 꾸며 채우지 말고 기존 불확실성·정보 부족 규약을 따른다.
"""


def without_descriptions(value):
    if isinstance(value, dict):
        return {k: without_descriptions(v) for k, v in value.items() if k != "description"}
    if isinstance(value, list):
        return [without_descriptions(v) for v in value]
    return value


def bound_schema():
    schema = copy.deepcopy(legacy.BoundDecision.model_json_schema())
    descriptions = {
        "code_path": "파일 읽기 스택 위치의 소스 경로. 수정할 Dockerfile 경로가 아니다.",
        "code_line": "code_path의 실제 읽기 행 번호. 선택한 SC 행에 포함되어야 한다.",
        "source_evidence_ids": "code_path 한 파일의 SC ID만 선택한다. Dockerfile 등 다른 파일의 SC를 혼합하지 않는다.",
        "evidence_ids": "입력 logs의 실제 EV ID. accept는 최소 하나를 선택한다.",
    }
    for key, text in descriptions.items():
        schema["properties"][key]["description"] = text
    packaging = schema["$defs"]["PackagingBinding"]["properties"]
    packaging["repo_path"]["description"] = "제공 아카이브 목록에 실제 존재하는 저장소 파일 경로."
    packaging["insert_after_line"]["description"] = (
        "Dockerfile의 마지막 관련 COPY 실제 행 번호. SC 숫자나 배열 인덱스가 아니다."
    )
    assert without_descriptions(schema) == without_descriptions(
        legacy.BoundDecision.model_json_schema()
    )
    return ordered(schema)


def model_request(arm, common, compact=None, hints=None, *, fallback=False):
    prompt, data, schema = legacy.model_request(arm, common, compact, hints, fallback=fallback)
    if fallback:
        return prompt + FALLBACK_CONTRACT, data, schema
    if arm in ("B_eq", "G_eq"):
        return prompt + BOUND_CONTRACT, data, bound_schema()
    return prompt, data, schema


def configuration(path):
    config = read(path)
    expected = read(legacy.DEFAULT_CONFIG)
    if config.get("version") != VERSION:
        raise ValueError("Expected review-controls.contract-v2")
    if {k: v for k, v in config.items() if k not in ("version", "root")} != {
        k: v for k, v in expected.items() if k not in ("version", "root")
    }:
        raise ValueError("Arms, role, backend settings and budgets must equal legacy")
    root = (ROOT / config["root"]).resolve()
    if root == LEGACY_ROOT or ROOT not in root.parents:
        raise ValueError("New study must stay in project and separate from legacy")
    return config, ExperimentConfig.model_validate(config["backend"]), root


def code_manifest():
    paths = list((ROOT / "tools").glob("*contract_v2.py"))
    paths += [ROOT / "tools/tests/test_review_controls_v4_contract_v2.py"]
    paths += [
        ROOT / "docs" / n
        for n in (
            "PAPER_STUDY_V4_OUTPUT_CONTRACT_REPAIR_RERUN_HANDOFF.md",
            "PAPER_STUDY_V4_CONTRACT_V2_COMMANDS.md",
            "PAPER_STUDY_V4_CONTRACT_V2_IMPLEMENTATION.md",
        )
    ]
    return {**legacy.code_manifest(), **{p.relative_to(ROOT).as_posix(): sha(p) for p in paths}}


def copied_files(root):
    names = [
        "dataset.json",
        "references.json",
        "context-provenance.json",
        "source-ranges.json",
        "method-before.json",
    ]
    return [root / n for n in names] + sorted((root / "packets").rglob("*"))


def verify_equivalence(root):
    record = read(root / "data-equivalence.json")
    source = Path(record["source_root"])
    actual = {p.relative_to(root).as_posix() for p in copied_files(root) if p.is_file()}
    if actual != set(record["files"]):
        raise ValueError("Cloned file set differs")
    for name, expected in record["files"].items():
        if sha(root / name) != expected or sha(source / name) != expected:
            raise ValueError(f"Cloned bytes changed: {name}")
    if read(root / "dataset.json")["version"] != "review-controls.v1":
        raise ValueError("Cloned dataset version must remain review-controls.v1")
    return record


def prepared_manifest(root, config_path):
    verify_equivalence(root)
    return {
        "code": code_manifest(),
        "files": {
            **{
                p.relative_to(ROOT).as_posix(): sha(p)
                for p in root.rglob("*")
                if p.is_file() and p.name != "freeze-manifest.json"
            },
            config_path.resolve().relative_to(ROOT).as_posix(): sha(config_path),
        },
        "environment": legacy.environment(),
    }


def verify_prepared(root, config_path, manifest):
    if configuration(config_path)[2] != root.resolve():
        raise ValueError("Wrong configured root")
    current = prepared_manifest(root, config_path)
    if current != manifest:
        raise ValueError("Frozen code, full prepared file set, config or environment changed")
    verify_files(manifest["code"])
    verify_files(manifest["files"])


def verify_freeze(root, config_path):
    frozen = read(root / "freeze-manifest.json")
    if frozen.get("protocol_version") != VERSION:
        raise ValueError("Wrong freeze protocol")
    verify_prepared(root, config_path, frozen["manifest"])
    verify_files(frozen["verification_files"])
    if not all(
        frozen.get(k) for k in ("preflight_passed", "mock_passed", "tests_passed", "audit_passed")
    ):
        raise ValueError("Pre-live checks incomplete")
    from audit_review_controls_v4_contract_v2 import verify_preserved

    verify_preserved(Path(frozen["preservation_manifest"]))
    return frozen


def schedule(cases, seed=42):
    if len(cases) != 12 or not all(c["eligible"] for c in cases):
        raise ValueError("All original 12 contexts required; never silently remove a case")
    rng = random.Random(seed)
    rows = [{"case_id": c["id"], "arm": "R_auto", "repeat": 0} for c in cases]
    initial = [(c["id"], 0) for c in cases if c["context_variant"] == "P_supported"]
    rng.shuffle(initial)
    rest = [(c["id"], n) for c in cases for n in range(3) if (c["id"], n) not in initial]
    rng.shuffle(rest)
    for cid, repeat in initial + rest:
        arms = ["R_llm", "B_eq", "G_eq"]
        rng.shuffle(arms)
        rows.extend({"case_id": cid, "arm": a, "repeat": repeat} for a in arms)
    for row in rows:
        row["cell_id"] = digest(row)[:20]
    assert len(rows) == 120
    return rows


def request_parity(root, config):
    checks = []
    backend = ExperimentConfig.model_validate(config["backend"])
    for c in read(root / "dataset.json")["cases"]:
        common, compact, _ = packet_files(root, c["id"], "R_llm")
        hints = read(root / "packets" / c["id"] / "candidate-hints.json")
        b, g = model_request("B_eq", common), model_request("G_eq", common, hints=hints)
        e1 = model_request("R_llm", common, compact) == legacy.model_request(
            "R_llm", common, compact
        )
        fallback = [model_request(a, common, fallback=True) for a in ARMS[1:]]
        for repeat in range(3):
            bodies = [
                GeminiBackend.request_body(
                    SimpleNamespace(config=backend), r[0], r[1], backend.seed + repeat, schema=r[2]
                )
                for r in (b, g)
            ]
            check = {
                "case_id": c["id"],
                "repeat": repeat,
                "e1_unchanged": e1,
                "prompt_equal": b[0] == g[0],
                "schema_equal": b[2] == g[2],
                "payload_equal_without_hints": b[1]
                == {k: v for k, v in g[1].items() if k != "rule_candidates"},
                "generation_config_equal": bodies[0]["generationConfig"]
                == bodies[1]["generationConfig"],
                "fallback_equal": fallback[0] == fallback[1] == fallback[2],
                "fallback_schema_unchanged": fallback[0][2]
                == legacy.model_request("B_eq", common, fallback=True)[2],
            }
            if not all(v for k, v in check.items() if k not in ("case_id", "repeat")):
                raise ValueError("Request parity failed")
            checks.append(check)
    return checks


STAGES = (
    "json_parse",
    "decision_schema",
    "decision_consistency",
    "reference_existence",
    "source_single_file",
    "source_contains_code_line",
    "target_path_contract",
    "packaging_binding_contract",
    "response_assembly",
    "full_response_schema",
    "full_response_references",
    "full_response_source_locations",
)


def diagnostics(text, arm, common, compact=None, *, fallback=False):
    """Never changes a response, server acceptance, fallback policy, or model input."""
    stages = dict.fromkeys(STAGES, None)
    errors = []

    def check(stage, passed, code, path, **detail):
        stages[stage] = bool(passed) if stages[stage] is None else stages[stage] and bool(passed)
        if not passed:
            errors.append(
                {
                    "stage": stage,
                    "error_code": code,
                    "json_path": path,
                    "diagnostic_only": True,
                    **detail,
                }
            )

    def finish(server_code=None):
        return {
            "diagnostic_only": True,
            "stages": stages,
            "errors": errors,
            "first_failure": errors[0] if errors else None,
            "original_server_code": server_code,
        }

    try:
        value = strict_json(text)
        stages["json_parse"] = True
    except (ValueError, TypeError, RecursionError):
        check("json_parse", False, "INVALID_JSON", "$")
        return finish("INVALID_JSON")
    schema = (
        legacy.stage_schema("source_findings", legacy.SourceFindings)
        if fallback
        else (
            legacy.CompactDecision if arm in ("R_auto", "R_llm") else legacy.BoundDecision
        ).model_json_schema()
    )
    schema_stage = "full_response_schema" if fallback else "decision_schema"
    schema_errors = list(Draft202012Validator(schema).iter_errors(value))
    stages[schema_stage] = not schema_errors
    for e in schema_errors:
        errors.append(
            {
                "stage": schema_stage,
                "error_code": "SCHEMA_" + str(e.validator).upper(),
                "json_path": ".".join(str(x) for x in e.absolute_path) or "$",
                "message": e.message,
                "diagnostic_only": True,
            }
        )
    ev = {x["id"] for x in common["logs"]}
    sc = {x["id"]: x for x in common["source_evidence"]}
    if not fallback:
        result = assess_primary(text, arm, common, compact)
        if schema_errors:
            return finish(result["server_transition_code"])
        check(
            "decision_consistency",
            result["decision_consistent"],
            "INCONSISTENT_DECISION",
            "decision/plan/counter_evidence_ids",
        )
        check(
            "reference_existence",
            not result["invalid_reference_ids"],
            "UNKNOWN_EVIDENCE_ID",
            "evidence_ids/source_evidence_ids/counter_evidence_ids",
            invalid_ids=result["invalid_reference_ids"],
        )
        if arm in ("B_eq", "G_eq") and value["decision"] == "accept":
            chosen = [sc[x] for x in value["source_evidence_ids"] if x in sc]
            check(
                "source_single_file",
                bool(chosen) and all(x["path"] == value["code_path"] for x in chosen),
                "SOURCE_EVIDENCE_PATH_MISMATCH",
                "source_evidence_ids",
                selected_source_paths=sorted({x["path"] for x in chosen}),
                expected_path=value["code_path"],
            )
            check(
                "source_contains_code_line",
                any(x["line"] == value["code_line"] for x in chosen),
                "SOURCE_CODE_LINE_NOT_CITED",
                "code_line",
            )
            check(
                "target_path_contract",
                bool(re.fullmatch(r"/[A-Za-z0-9_./-]{1,180}", value["target"]))
                and ".." not in value["target"].split("/"),
                "INVALID_TARGET_PATH",
                "target",
            )
            p = value.get("packaging")
            valid = p is None
            if value["plan"] == "add_docker_copy":
                valid = bool(
                    p
                    and re.fullmatch(r"[A-Za-z0-9_/-]+(?:\.[A-Za-z0-9_-]+)*", p["repo_path"])
                    and ".." not in p["repo_path"].split("/")
                    and p["repo_path"] in common["archive_observations"].get("regular_files", [])
                    and any(
                        s["path"] == "Dockerfile" and s["line"] == p["insert_after_line"]
                        for s in sc.values()
                    )
                )
            check("packaging_binding_contract", valid, "INVALID_PACKAGING_BINDING", "packaging")
        if result["server_transition_code"] == "COMPACT_DEFERRED":
            return finish("COMPACT_DEFERRED")
        try:
            assembled = (
                legacy.expand(text, compact)
                if arm in ("R_auto", "R_llm")
                else legacy.bound_expand(text, common)
            )
            stages["response_assembly"] = True
        except DiagnosisError as exc:
            check("response_assembly", False, exc.code, "$")
            return finish(exc.code)
        full = diagnostics(assembled, arm, common, fallback=True)
        for stage in STAGES[-3:]:
            stages[stage] = full["stages"][stage]
        errors.extend(full["errors"])
        return finish(result["server_transition_code"])
    if isinstance(value, dict):
        # Independent location checks still reveal extra errors after a schema failure.
        source_findings = value.get("source_findings")
        findings = source_findings.get("findings", []) if isinstance(source_findings, dict) else []
        if isinstance(findings, list):
            for i, f in enumerate(findings):
                if not isinstance(f, dict) or not isinstance(f.get("source_evidence_ids"), list):
                    continue
                ids = f["source_evidence_ids"]
                known = bool(ids) and all(isinstance(x, str) and x in sc for x in ids)
                check(
                    "full_response_references",
                    known,
                    "INVALID_SOURCE_REFERENCE",
                    f"source_findings.findings.{i}.source_evidence_ids",
                )
                if known:
                    chosen = [sc[x] for x in ids]
                    check(
                        "full_response_source_locations",
                        all(x["path"] == f.get("path") for x in chosen)
                        and min(x["line"] for x in chosen) == f.get("start_line")
                        and max(x["line"] for x in chosen) == f.get("end_line"),
                        "INVALID_SOURCE_LOCATION",
                        f"source_findings.findings.{i}",
                        cited_rows=[{k: s[k] for k in ("id", "path", "line")} for s in chosen],
                    )
        observations = value.get("observations")
        if isinstance(observations, list):
            for i, obs in enumerate(observations):
                if isinstance(obs, dict) and isinstance(obs.get("evidence_ids"), list):
                    check(
                        "full_response_references",
                        all(isinstance(x, str) and x in ev for x in obs["evidence_ids"]),
                        "INVALID_EVIDENCE",
                        f"observations.{i}.evidence_ids",
                    )
    try:
        validate_full(text, common)
        for key in STAGES[-3:]:
            stages[key] = True
        return finish()
    except (DiagnosisError, ValueError, TypeError, KeyError) as exc:
        code = getattr(exc, "code", "UNKNOWN")
        if not schema_errors and not any(e["error_code"] == code for e in errors):
            stage = (
                "full_response_source_locations"
                if code == "INVALID_SOURCE_LOCATION"
                else "full_response_references"
            )
            check(stage, False, code, "$", message=str(exc))
        return finish(code)
