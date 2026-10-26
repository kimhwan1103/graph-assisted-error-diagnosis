"""Shared offline experiment infrastructure; never changes production diagnosis."""

import copy
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from evaluate_remediation_v4 import hashes, read, readonly_run_lock, separate, write

from ai_error_check_agent.experiments.design import digest
from ai_error_check_agent.experiments.study_v4 import source_manifest

__all__ = ["hashes", "read", "readonly_run_lock", "separate", "write"]

REGISTRY = {
    "shipping-cli": {
        "prefix": "shipping",
        "lifecycle": "cli",
        "entry": "bin/quote.js",
        "reader": "lib/load-tariffs.js",
        "target": "assets/tariffs.json",
        "optional": "config/local-labels.json",
        "optional_reader": "lib/load-labels.js",
        "call_chain": ["bin/quote.js", "lib/load-tariffs.js", "pricing/calculate.js"],
        "format": "JSON",
    },
    "template-http": {
        "prefix": "http",
        "lifecycle": "http",
        "entry": "server.js",
        "reader": "view/load-template.js",
        "target": "views/index.html",
        "optional": "config/local-theme.json",
        "optional_reader": "view/load-theme.js",
        "call_chain": ["server.js", "view/load-template.js", "routes/page.js"],
        "format": "HTML",
    },
    "settlement-batch": {
        "prefix": "batch",
        "lifecycle": "batch",
        "entry": "src/job.js",
        "reader": "readers/load-policy.js",
        "target": "config/settlement-rules.json",
        "optional": "config/local-labels.json",
        "optional_reader": "readers/load-labels.js",
        "call_chain": ["src/job.js", "readers/load-policy.js", "pipeline/aggregate.js"],
        "format": "JSON",
    },
}
VARIANTS = {
    "A": ("copy-missing", "supported_copy_omission"),
    "B": ("optional-recovered", "optional_fallback"),
    "C": ("alias-copy-missing", "unsupported_alias"),
}
REQUIRED = [
    "baseline_failure_reproduced",
    "application_pass",
    "build_pass",
    "execution_pass",
    "target_failure_resolved",
    "functional_pass",
    "regression_pass",
    "data_preservation_pass",
]


def now():
    return datetime.now(UTC).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fresh(path):
    path = Path(path).resolve()
    if path.exists():
        raise ValueError(f"Preserve existing output; choose a new path: {path}")
    return path


def fixtures(program):
    folder = ROOT / "fixtures/cross-program-v4" / program
    return [
        {"path": p.relative_to(folder).as_posix(), "content": p.read_text("utf-8")}
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    ]


def variant_files(program, variant):
    spec = REGISTRY[program]
    files = {x["path"]: x["content"] for x in fixtures(program)}
    reference_copy = f"COPY --chown=node:node {spec['target']} ./{spec['target']}\n"
    if files["Dockerfile"].count(reference_copy) != 1:
        raise ValueError("Fixture COPY baseline must occur exactly once")
    if variant in ("A", "C"):
        files["Dockerfile"] = files["Dockerfile"].replace(reference_copy, "")
    else:
        files["Dockerfile"] = files["Dockerfile"].replace(
            "USER node", "ENV OPTIONAL_CONFIG=1\nUSER node"
        )
    if variant == "C":
        code = files[spec["reader"]]
        files[spec["reader"]] = code.replace(
            "{ readFileSync }", "{ readFileSync as readText }"
        ).replace("readFileSync(", "readText(")
    return [{"path": k, "content": v} for k, v in sorted(files.items())]


def reference_dockerfile(files, spec):
    content = next(x["content"] for x in files if x["path"] == "Dockerfile")
    line = f"COPY --chown=node:node {spec['target']} ./{spec['target']}\n"
    return content if line in content else content.replace("USER node", line + "USER node")


def checker_spec(program, files):
    spec = copy.deepcopy(REGISTRY[program])
    spec["program"] = program
    spec["protected_files"] = {
        x["path"]: text_hash(x["content"])
        for x in files
        if x["path"] not in ("Dockerfile", ".dockerignore", "README.md")
    }
    spec["tests"] = {
        "shipping-cli": [
            {"args": [], "code": 0, "expected": {"shipping_fee": 1400, "label": "shipping"}},
            {
                "args": ['{"amount":4000,"weightKg":0}'],
                "code": 0,
                "expected": {"shipping_fee": 1000, "label": "shipping"},
            },
            {
                "args": ['{"amount":10000,"weightKg":2}'],
                "code": 0,
                "expected": {"shipping_fee": 0, "label": "shipping"},
            },
            {"args": ['{"amount":4000,"weightKg":-1}'], "code": 2, "error": "INVALID_INPUT"},
            {"args": ["bad-json"], "code": 2, "error": "INVALID_INPUT"},
        ],
        "settlement-batch": [
            {
                "args": [],
                "code": 0,
                "expected": {"count": 2, "fees": 750, "settlement": 29250, "label": "settlement"},
            },
            {
                "args": ["[]"],
                "code": 0,
                "expected": {"count": 0, "fees": 0, "settlement": 0, "label": "settlement"},
            },
            {"args": ['[{"amount":-1}]'], "code": 2, "error": "INVALID_TRANSACTIONS"},
            {
                "args": ['[{"amount":20},{"amount":19}]'],
                "code": 0,
                "expected": {"count": 2, "fees": 1, "settlement": 38, "label": "settlement"},
            },
        ],
        "template-http": [],
    }[program]
    spec["html"] = (
        '<!doctype html><html lang="en"><head><title>Parcel status</title></head><body><h1>Your parcel dashboard</h1><p>Track deliveries here.</p></body></html>\n'
    )
    spec["health"] = {"status": "ok", "app": "template-http", "theme": "plain"}
    return spec


def verify_diagnosis():
    before = read(ROOT / "evaluation/cross-program-v4-source-before.json")
    if before != source_manifest():
        raise ValueError("Fixed v4 diagnosis source changed")


def verify_freeze(root):
    freeze = read(root / "freeze-manifest.json")
    verify_diagnosis()
    for name, expected in freeze["files"].items():
        path = (ROOT / name).resolve()
        if ROOT not in path.parents or sha(path) != expected:
            raise ValueError(f"Frozen experiment file changed: {name}")
    return freeze


def source_cell(source, slot):
    folder = source / "cells" / slot["cell_id"]
    state_path, result_path = folder / "state.json", folder / "result.json"
    state = read(state_path) if state_path.exists() else {"status": "not_started"}
    result = read(result_path) if result_path.exists() else None
    if state["status"] == "completed":
        if result is None or digest(result) != state.get("result_sha256"):
            raise ValueError("Source terminal digest mismatch")
        if any(result.get(k) != v for k, v in slot.items()):
            raise ValueError("Source identity mismatch")
    return state, result


def load_source(source, root):
    verify_freeze(root)
    frozen = read(source / "frozen.json")
    config = read(ROOT / "config/study.gemma-api.v4.cross-program.json")
    dataset = read(root / "dataset.json")["cases"]
    if (
        not frozen["live"]
        or frozen["config"] != config
        or frozen["cases"] != dataset
        or frozen["source_manifest"] != source_manifest()
    ):
        raise ValueError("Run does not match frozen live experiment")
    from ai_error_check_agent.experiments.study_v4 import load, schedule

    c, b, cases, _ = load(ROOT / "config/study.gemma-api.v4.cross-program.json")
    if frozen["schedule"] != schedule(cases, c, b) or len(frozen["schedule"]) != 54:
        raise ValueError("Schedule mismatch")
    return frozen
