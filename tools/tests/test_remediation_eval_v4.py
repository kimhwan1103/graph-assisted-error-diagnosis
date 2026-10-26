"""Evaluator tests use synthetic responses; never count as agent performance."""

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_remediation_v4 as evaluator
from remediation_adapter_v4 import POLICY, PlanError, adapt
from remediation_docker_v4 import REQUIRED_CHECKS, DockerReplay, file_hash, materialize

ROOT = Path(__file__).resolve().parents[2]
DATA = evaluator.read(ROOT / "evaluation/study-v4.development.json")
COPY_CASE = next(c for c in DATA["cases"] if c["id"] == "docker-copy-omission")
FILES = COPY_CASE["source_files"]
NODE = shutil.which("node")


def plan(snippet="COPY --chown=node:node data/tasks.json ./data/tasks.json", **extra):
    change = {
        "target": "Dockerfile",
        "target_known": True,
        "snippet": snippet,
        "instruction": "COPY 추가",
        "placeholders": [],
        **extra,
    }
    return {"id": "R1", "changes": [change], "apply_when": ["provided static fixture"]}


def response(status="diagnosed", plans=None, valid=True):
    if plans is None:
        plans = [plan()] if status == "diagnosed" else []
    analysis = {
        "analysis_status": status,
        "remediation": {
            "status": {
                "diagnosed": "proposed",
                "no_failure_evidence": "not_needed",
                "insufficient_evidence": "needs_more_evidence",
            }[status],
            "plans": plans,
        },
        "missing_information": [{"requested_data": "source", "reason": "verify target"}],
    }
    return {
        "response_valid": valid,
        "analysis": analysis if valid else None,
        "response_error": None if valid else {"code": "INVALID_STATE"},
    }


@pytest.mark.parametrize(
    "snippet",
    [
        "COPY --chown=node:node data/tasks.json ./data/tasks.json",
        " copy  --chown=node:node 'data/tasks.json' './data/tasks.json' ",
        'COPY --chown=node:node ["data/tasks.json", "./data/tasks.json"]',
        "```dockerfile\nCOPY --chown=node:node data/tasks.json ./data/tasks.json\n```",
    ],
)
def test_equivalent_copy_forms_use_same_adapter(snippet):
    assert (
        adapt(plan(snippet), FILES, POLICY)["dockerfile"]
        == adapt(plan(), FILES, POLICY)["dockerfile"]
    )


def test_directory_copy_and_unknown_flag_get_no_oracle_correction():
    result = adapt(plan("COPY data/ /app/data/", target_known=False), FILES, POLICY)
    assert result["operations"][0]["destination"] == "/app/data/"
    assert {a["field"] for a in result["adapter_supplied_fields"]} == {
        "target_existence",
        "insertion_position",
    }
    wrong = adapt(plan("COPY data/tasks.json /app/wrong.json"), FILES, POLICY)
    assert wrong["operations"][0]["destination"] == "/app/wrong.json"
    assert wrong["normalized_patch_sha256"] != result["normalized_patch_sha256"]


@pytest.mark.parametrize(
    "snippet",
    [
        "RUN echo hello",
        "COPY ../secret /app/data",
        "COPY /secret /app/data",
        "COPY data /etc/data",
        "COPY --from=host data /app/data",
        "COPY $SRC /app/data",
        "COPY *.json /app/data",
        "COPY a b c",
    ],
)
def test_unsupported_or_escaping_snippets_are_never_commands(snippet):
    with pytest.raises(PlanError):
        adapt(plan(snippet), FILES, POLICY)


def test_whole_first_plan_is_required_and_later_plan_is_not_selected():
    first = plan("RUN false")
    spec = {"case_group": "repairable", "allowed_states": ["diagnosed"]}
    outcome, chosen = evaluator.classify(response(plans=[first, plan()]), spec)
    assert outcome == "plan_selected" and chosen == first
    with pytest.raises(PlanError):
        adapt(chosen, FILES, POLICY)
    mixed = plan()
    mixed["changes"].append({**mixed["changes"][0], "target": ".dockerignore"})
    with pytest.raises(PlanError):
        adapt(mixed, FILES, POLICY)


def test_explicit_locations_are_preserved_or_rejected_without_repair():
    instruction = "Dockerfile 5줄의 `COPY --chown=node:node public ./public` 바로 뒤에 추가합니다."
    result = adapt(plan(instruction=instruction), FILES, POLICY)
    assert result["adapter_supplied_fields"] == []
    assert result["dockerfile"].splitlines()[5].startswith("COPY --chown=node:node [")
    with pytest.raises(PlanError, match="anchor and line"):
        adapt(plan(instruction=instruction.replace("5줄", "6줄")), FILES, POLICY)
    with pytest.raises(PlanError):
        adapt(plan(instruction="somewhere after initialization"), FILES, POLICY)


def test_no_change_failure_is_not_correct_abstention():
    spec = {"case_group": "no_change", "allowed_states": ["no_failure_evidence"]}
    assert evaluator.classify(response("no_failure_evidence"), spec)[0] == "no_change_correct"
    assert evaluator.classify(response(valid=False), spec)[0] == "contract_failed"
    assert evaluator.classify(response(), spec)[0] == "unnecessary_change"


def test_insufficient_requires_nonempty_required_fields():
    spec = {"case_group": "insufficient_context", "allowed_states": ["insufficient_evidence"]}
    r = response("insufficient_evidence")
    assert evaluator.classify(r, spec)[0] == "structural_deferral_correct"
    r["analysis"]["missing_information"] = []
    assert evaluator.classify(r, spec)[0] == "inappropriate_response"
    r["analysis"]["missing_information"] = [{"requested_data": "x", "reason": " "}]
    assert evaluator.classify(r, spec)[0] == "inappropriate_response"


def metric_row(arm="adaptive", group="repairable", **updates):
    return {
        "cell_id": arm,
        "case_id": "case",
        "incident_id": "incident",
        "cluster_id": "cluster",
        "repeat": 0,
        "arm": arm,
        "case_group": group,
        "evaluation_complete": True,
        "response_valid": True,
        "plan_present": True,
        "outcome": "restored",
        "complete_restoration": True,
        "application_pass": True,
        "regression_pass": True,
        **updates,
    }


def test_pending_stays_in_denominator_and_has_no_final_rate():
    rows = [
        metric_row(),
        metric_row(
            "graph_compact",
            evaluation_complete=False,
            outcome="evaluation_error",
            application_pass=None,
            complete_restoration=None,
            regression_pass=None,
        ),
    ]
    summary = evaluator.summarize(rows)
    metric = summary["by_arm"]["graph_compact"]["repairable"]["complete_restoration_rate"]
    assert metric == {
        "numerator": 0,
        "denominator": 1,
        "rate": None,
        "confirmed_success_lower_bound": 0.0,
    }
    assert summary["status"] == "incomplete"
    assert summary["paired_repairable"]["unresolved"] == 1


def test_failures_and_unfinished_regression_keep_their_denominators():
    rows = [
        metric_row(),
        metric_row(
            case_id="bad",
            outcome="contract_failed",
            response_valid=False,
            application_pass=None,
            regression_pass=None,
            complete_restoration=False,
        ),
        metric_row(
            case_id="startup",
            outcome="startup_failed",
            regression_pass=None,
            complete_restoration=False,
        ),
        metric_row(
            case_id="regression",
            outcome="regression_failed",
            regression_pass=False,
            complete_restoration=False,
        ),
    ]
    result = evaluator.summarize(rows)["by_arm"]["adaptive"]["repairable"]
    assert result["complete_restoration_rate"]["rate"] == 1 / 4
    assert result["application_rate"]["rate"] == 3 / 4
    assert result["regression_rate"]["rate"] == 1 / 2
    assert result["regression_uncompleted"] == 1


def test_no_change_rates_do_not_reward_silence():
    rows = [
        metric_row(group="no_change", outcome="unnecessary_change"),
        metric_row(group="no_change", outcome="no_change_correct", plan_present=False),
        metric_row(
            group="no_change", outcome="contract_failed", response_valid=False, plan_present=False
        ),
    ]
    g = evaluator.summarize(rows)["by_arm"]["adaptive"]
    assert g["no_change"]["unnecessary_change_rate"]["rate"] == 1 / 3
    assert g["no_change"]["unnecessary_change_given_valid"]["rate"] == 1 / 2
    assert g["no_change"]["appropriate_no_change_rate"]["rate"] == 1 / 3
    assert g["no_change"]["response_failures"] == 1
    assert g["repairable"]["complete_restoration_rate"]["rate"] is None


@pytest.mark.parametrize(
    "field,outcome",
    [
        ("functional_pass", "functional_failed"),
        ("regression_pass", "regression_failed"),
        ("data_preservation_pass", "preservation_failed"),
    ],
)
def test_every_required_check_is_needed_for_restoration(field, outcome):
    row = {k: True for k in REQUIRED_CHECKS}
    assert evaluator.execution_outcome(row) == "restored"
    row[field] = False
    assert evaluator.execution_outcome(row) == outcome
    row[field] = None
    assert evaluator.execution_outcome(row) == "evaluation_error"


def test_real_probe_rejects_empty_incomplete_or_corrupt_expected_data(tmp_path):
    if not NODE:
        pytest.skip("Node is required for fixed-checker validation")
    checker = ROOT / "tools/remediation_checks_v4.cjs"
    expected = json.loads(next(f["content"] for f in FILES if f["path"] == "data/tasks.json"))
    script = """
const {functionalBody} = require(process.argv[1]);
const tasks = JSON.parse(process.argv[2]);
const spec = {title:'작은 완료',expected_tasks:tasks};
const withIds = tasks.map((x,i)=>({id:`00000000-0000-0000-0000-${String(i).padStart(12,'0')}`,...x}));
console.log(JSON.stringify([
 functionalBody({title:spec.title,tasks:withIds.slice().reverse()},spec),
 functionalBody({title:spec.title,tasks:[]},spec),
 functionalBody({title:spec.title,tasks:withIds.slice(1)},spec),
 functionalBody({title:spec.title,tasks:withIds.map(x=>({...x,done:false}))},spec),
 functionalBody({title:spec.title,tasks:withIds.map(x=>({...x,unexpected:1}))},spec)
]));
"""
    r = subprocess.run(
        [NODE, "-e", script, str(checker), json.dumps(expected, ensure_ascii=False)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert json.loads(r.stdout) == [True, False, False, False, False]


def test_real_probe_detects_broken_config_and_changed_protected_data(tmp_path):
    if not NODE:
        pytest.skip("Node is required for fixed-checker validation")
    for name, broken in [("good", False), ("bad", True)]:
        folder = tmp_path / name
        materialize(folder, FILES, next(f["content"] for f in FILES if f["path"] == "Dockerfile"))
        if broken:
            (folder / "src/config.js").write_text(
                "export function loadConfig(){ return {}; }", encoding="utf-8"
            )
            (folder / "data/tasks.json").write_text("[]", encoding="utf-8")
        spec = {
            "protected_files": {
                f["path"]: file_hash(f["content"]) for f in FILES if f["path"] == "data/tasks.json"
            }
        }
        script = "const c=require(process.argv[1]); c.regression(process.argv[2]).then(r=>console.log(JSON.stringify({regression:r.pass,preservation:c.preserved(process.argv[2],JSON.parse(process.argv[3])).pass})));"
        r = subprocess.run(
            [
                NODE,
                "-e",
                script,
                str(ROOT / "tools/remediation_checks_v4.cjs"),
                str(folder),
                json.dumps(spec),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert json.loads(r.stdout) == {"regression": not broken, "preservation": not broken}


@pytest.mark.parametrize("empty_tasks", [False, True])
def test_fixed_checker_against_actual_local_http_fixture(tmp_path, empty_tasks):
    """Actual Node HTTP, not Docker; validates checker behavior, not a model repair."""
    if not NODE:
        pytest.skip("Node is required for HTTP checker integration")
    root = tmp_path / "app"
    materialize(root, FILES, next(f["content"] for f in FILES if f["path"] == "Dockerfile"))
    if empty_tasks:
        p = root / "src/app.js"
        p.write_text(
            p.read_text(encoding="utf-8").replace("let tasks = createTasks();", "let tasks = [];"),
            encoding="utf-8",
        )
    spec = {
        "title": "작은 완료",
        "health_app": "iris-demo-web",
        "expected_tasks": json.loads(
            next(f["content"] for f in FILES if f["path"] == "data/tasks.json")
        ),
        "protected_files": {
            f["path"]: file_hash(f["content"])
            for f in FILES
            if f["path"].startswith(("src/", "data/"))
        },
    }
    script = """
const c = require(process.argv[1]);
const root = process.argv[2], spec = JSON.parse(process.argv[3]);
const {pathToFileURL} = require('node:url');
import(pathToFileURL(root+'/src/app.js').href).then(async ({createApp})=>{
 const server = createApp({title:spec.title});
 await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)});
 try { console.log(JSON.stringify(await c.probe(spec,root,'http://127.0.0.1:'+server.address().port))); }
 finally { server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)); }
}).catch(e=>{console.error(e);process.exitCode=1});
"""
    r = subprocess.run(
        [
            NODE,
            "-e",
            script,
            str(ROOT / "tools/remediation_checks_v4.cjs"),
            str(root),
            json.dumps(spec, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    values = json.loads(r.stdout)
    assert values["startup_pass"] and values["health_pass"] and values["regression_pass"], values
    assert values["functional_pass"] is not empty_tasks
    assert values["data_preservation_pass"] is not empty_tasks


def saved_run(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "writer.lock").write_bytes(b"0")
    cases = [
        copy.deepcopy(c)
        for c in DATA["cases"]
        if c["id"]
        in {
            "docker-copy-omission",
            "recovered-same-stream",
            "expected-test-error",
            "source-target-mismatch",
        }
    ]
    slots = [
        {"case_id": c["id"], "arm": a, "repeat": 0, "cell_id": f"cell-{i}-{a}"}
        for i, c in enumerate(cases)
        for a in ("adaptive", "graph_compact")
    ]
    frozen = {
        "version": "iris-study.v4",
        "live": True,
        "config": {"arms": ["adaptive", "graph_compact"]},
        "source_manifest": {},
        "cases": cases,
        "schedule": slots,
    }
    evaluator.write(source / "frozen.json", frozen)
    for slot in slots:
        status = (
            "no_failure_evidence"
            if slot["case_id"] in {"recovered-same-stream", "expected-test-error"}
            else "insufficient_evidence"
            if slot["case_id"] == "source-target-mismatch"
            else "diagnosed"
        )
        valid = slot["case_id"] != "expected-test-error"
        r = response(status, valid=valid)
        public = {
            "job_status": "succeeded" if valid else "failed",
            "analysis": r["analysis"],
            "error": r["response_error"],
        }
        record = {**slot, "result": public}
        evaluator.write(source / "cells" / slot["cell_id"] / "result.json", record)
        evaluator.write(
            source / "cells" / slot["cell_id"] / "state.json",
            {"status": "completed", "result_sha256": evaluator.digest(record)},
        )
    manifest = tmp_path / "cases.json"
    evaluator.write(manifest, evaluator.development_manifest(frozen))
    return source, manifest


class UnavailableReplay:
    def __init__(self, output):
        self.commands, self.base_image, self.agent_attempts = [], None, 0

    def preflight(self, case, fixture):
        return {
            "status": "evaluation_error",
            "error": "engine unavailable",
            "baseline_failure_reproduced": None,
        }

    def cleanup(self):
        return []


class ReferenceFakeReplay(UnavailableReplay):
    """Only evaluator regression tests, explicitly synthetic, not Docker evidence."""

    def preflight(self, case, fixture):
        return {"status": "passed", "baseline_failure_reproduced": True}

    def evaluate(self, row, case, fixture, adapted, folder):
        self.agent_attempts += 1
        (folder / "patch.diff").write_text(adapted["patch_diff"], encoding="utf-8")
        return {
            **dict.fromkeys(REQUIRED_CHECKS, True),
            "evaluation_error": None,
            "commands": [],
            "execution_id": "test-only",
        }


def test_dry_run_is_immutable_repeatable_and_not_restored(tmp_path):
    source, manifest = saved_run(tmp_path)
    before = evaluator.hashes(source)
    one = evaluator.run(source, tmp_path / "one", manifest)
    two = evaluator.run(source, tmp_path / "two", manifest)
    assert one == two
    assert evaluator.hashes(source) == before
    assert one["status"] == "incomplete" and one["agent_execution_attempts"] == 0
    assert one["by_arm"]["adaptive"]["repairable"]["complete_restoration_rate"]["rate"] is None
    assert not list((tmp_path / "one/cells").glob("*/patch.diff"))
    with pytest.raises(ValueError, match="already exists"):
        evaluator.run(source, tmp_path / "one", manifest)


def test_infrastructure_failure_and_mock_test_success_are_distinct(tmp_path):
    source, manifest = saved_run(tmp_path)
    failed = evaluator.run(
        source, tmp_path / "infra", manifest, execute=True, replay_class=UnavailableReplay
    )
    passed = evaluator.run(
        source, tmp_path / "fake-success", manifest, execute=True, replay_class=ReferenceFakeReplay
    )
    assert failed["status"] == "incomplete" and failed["agent_execution_attempts"] == 0
    assert passed["status"] == "complete" and passed["agent_execution_attempts"] == 2
    assert passed["paired_repairable"]["both_restored"] == 1
    assert evaluator.read(tmp_path / "fake-success/preservation.json")["source_unchanged"]


def test_incomplete_source_and_overlapping_outputs_are_rejected(tmp_path):
    source, manifest = saved_run(tmp_path)
    with pytest.raises(ValueError, match="input path"):
        evaluator.run(source, source / "nested", manifest)
    state = next((source / "cells").glob("*/state.json"))
    state.write_text('{"status":"running"}', encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        evaluator.run(source, tmp_path / "bad", manifest)
    assert not (tmp_path / "bad").exists()


def test_source_lock_is_read_only_and_respects_active_writer(tmp_path):
    source, _ = saved_run(tmp_path)
    before = (source / "writer.lock").read_bytes()
    with evaluator.readonly_run_lock(source) as handle:
        assert evaluator.locked_bytes(handle) == before
    if sys.platform == "win32":
        import msvcrt

        with (source / "writer.lock").open("r+b") as h:
            msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                with (
                    pytest.raises(ValueError, match="active writer"),
                    evaluator.readonly_run_lock(source),
                ):
                    pass
            finally:
                h.seek(0)
                msvcrt.locking(h.fileno(), msvcrt.LK_UNLCK, 1)
    assert (source / "writer.lock").read_bytes() == before


def test_missing_docker_is_evaluation_error_without_model_dispatch(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker absent")

    monkeypatch.setattr(subprocess, "run", unavailable)
    # Only initialize needs to be tested; no reference success can be fabricated.
    replay = DockerReplay(tmp_path)
    with pytest.raises(RuntimeError, match="unavailable"):
        replay.initialize("node:22-alpine")
    assert replay.agent_attempts == 0
    assert replay.commands[0]["exit_code"] is None


def test_dockerfile_from_pins_repository_digest_not_config_image_id(tmp_path):
    replay = DockerReplay(tmp_path)
    image_id = "sha256:" + "1" * 64
    repository = "node@sha256:" + "2" * 64
    replay.command = lambda *args, **kwargs: {
        "exit_code": 0,
        "stdout": json.dumps([{"Id": image_id, "RepoDigests": [repository]}])
        if args[:2] == ("image", "inspect")
        else "test-server",
    }
    replay.initialize("node:22-alpine")
    assert replay.base_image["id"] == image_id
    assert (
        replay.pinned("FROM node:22-alpine\nWORKDIR /app\n").splitlines()[0] == "FROM " + repository
    )
