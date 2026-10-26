"""Prepare/run isolated BugsJS pairs; publish only verified selected-test pairs.

No source code executes on the host. Docker execution is an explicit CLI step.
"""

import argparse
import difflib
import hashlib
import io
import json
import re
import shlex
import subprocess
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .bugsjs import policy_matches, read_mocha, source_snapshot
from .design import ROOT
from .run import save

SELECTION = ROOT / "evaluation/public/bugsjs_evaluation/selection.v1.json"
PAIRED = SELECTION.parent / "paired.v2.json"
ERROR_REPORTER = Path(__file__).with_name("mocha-errors.cjs")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_selection(path=SELECTION):
    value = json.loads(path.read_text("utf-8"))
    ids = [c["id"] for c in value["cases"]]
    if (
        len(ids) != value["required_bugs"]
        or len(set(ids)) != len(ids)
        or set(ids) & set(value["development_ids"])
    ):
        raise ValueError("Invalid or overlapping frozen split")
    independent = set()
    for case in value["cases"]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", case["id"]):
            raise ValueError("Unsafe case ID")
        if case["selection_status"] != "selected" or not case["test_full_title"]:
            raise ValueError(
                "Frozen case lacks a selected regression test; record exclusion, do not replace"
            )
        for state in case["states"].values():
            if not re.fullmatch(r"[a-f0-9]{40}", state["commit"]):
                raise ValueError("Source commit is not pinned")
        key = (
            case.get("repository", "").lower().rstrip("/").removesuffix(".git"),
            case["states"]["fixed"]["commit"],
        )
        if key in independent:
            raise ValueError("Duplicate independent bug/fixed commit in selection")
        independent.add(key)
    return value


def artifact(selection, name, selection_path=SELECTION):
    if Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("Unsafe artifact name")
    path = selection_path.parent / "source" / name
    if sha(path) != selection["artifacts"][name]["sha256"]:
        raise ValueError(f"Source artifact hash mismatch: {name}")
    return path


def unpack(raw, destination):
    """Bounded regular files only, never archive extractall or host symlinks."""
    destination.mkdir(parents=True, exist_ok=False)
    total, seen = 0, set()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        roots = {item.filename.split("/", 1)[0] for item in archive.infolist()}
        if len(roots) != 1 or len(archive.infolist()) > 5000:
            raise ValueError("Invalid source archive")
        for item in archive.infolist():
            if item.is_dir():
                continue
            relative = PurePosixPath(item.filename).parts[1:]
            if (
                not relative
                or any(p in {".", "..", ".git", "node_modules"} for p in relative)
                or "\\" in item.filename
                or ":" in item.filename
                or (item.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Unsafe source archive member")
            path = destination.joinpath(*relative).resolve()
            if not path.is_relative_to(destination.resolve()) or path in seen:
                raise ValueError("Duplicate or escaping archive member")
            seen.add(path)
            total += item.file_size
            if item.file_size > 2_000_000 or total > 32_000_000:
                raise ValueError("Oversized source archive")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(item))


def prepare(output, selection_path=SELECTION):
    selection = load_selection(selection_path)
    output.mkdir(parents=True, exist_ok=False)
    plan = {
        "version": "bugsjs-reproduction.v1",
        "selection_sha256": sha(selection_path),
        "selection_path": str(selection_path.resolve()),
        "state": "prepared_not_executed",
        "created_at": datetime.now(UTC).isoformat(),
        "cases": [],
        "base_image": "node:8.0.0",
        "independently_reproduced": False,
    }
    for case in selection["cases"]:
        directory = output / case["id"]
        directory.mkdir()
        for state, info in case["states"].items():
            unpack(
                artifact(selection, info["archive"], selection_path).read_bytes(), directory / state
            )
        # One dependency installation is shared by both states. Do not attribute
        # a dependency change to a production-code fix.
        packages = [
            json.loads((directory / state / "package.json").read_text("utf-8"))
            for state in ("failed", "fixed")
        ]
        keys = ("dependencies", "devDependencies", "optionalDependencies", "engines")
        compatible = all(packages[0].get(k) == packages[1].get(k) for k in keys)
        plan["cases"].append(
            {"id": case["id"], "status": "prepared" if compatible else "dependency_mismatch"}
        )
        save(directory / "case.json", case)
        # Reviewer material stays out of model input and Kaggle's default bundle.
        patch = []
        for path in sorted(
            {
                p.relative_to(directory / "failed").as_posix()
                for p in (directory / "failed").rglob("*.js")
            }
            | {
                p.relative_to(directory / "fixed").as_posix()
                for p in (directory / "fixed").rglob("*.js")
            }
        ):
            policy = case.get("source_policy", selection.get("source_policy"))
            if not (
                policy_matches(path, policy)
                if policy
                else path == "index.js" or path.startswith("lib/")
            ):
                continue

            def lines(state, directory=directory, path=path):
                source = directory / state / path
                return (
                    source.read_text("utf-8").splitlines(keepends=True) if source.exists() else []
                )

            patch.extend(
                difflib.unified_diff(
                    lines("failed"),
                    lines("fixed"),
                    fromfile="failed/" + path,
                    tofile="fixed/" + path,
                )
            )
        (directory / "reference.patch").write_text("".join(patch), encoding="utf-8")
    save(output / "reproduction.json", plan)
    return plan


def docker(args, *, stdout=None, timeout=60, check=True):
    return subprocess.run(
        ["docker", *args],
        stdout=stdout or subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=check,
    )


def test_capture_command(state, argv):
    """Keep Node 8/Mocha's process.exit from truncating asynchronous pipe writes."""
    if state not in {"failed", "fixed"}:
        raise ValueError("Unexpected test state")
    return (
        f"cp -R /cases/{state}/. /app/ && ln -s /deps/node_modules /app/node_modules && {{ "
        + shlex.join(argv)
        + " > /tmp/mocha-report.stdout 2>&1; test_status=$?; "
        + "cat /tmp/mocha-report.stdout; capture_status=$?; "
        + 'if [ "$capture_status" -ne 0 ]; then exit 125; fi; exit "$test_status"; }'
    )


def validate_pair(case, records, *, selected_test_only=False):
    """Selected target must fail before and pass after, with no skip/duplicate."""
    for state, expected in (("failed", "failures"), ("fixed", "passes")):
        record = records[state]
        report = record["report"]
        if record["returncode"] not in range(125):
            raise ValueError("Test process did not complete normally")
        if not report["stats"].get("tests") or report["stats"].get("pending", 0) < 0:
            raise ValueError("Empty or invalid test execution")
        if selected_test_only and (
            report["stats"]["tests"] != 1
            or len(report["tests"]) != 1
            or report["stats"].get("pending", 0) != 0
            or report.get("pending", [])
        ):
            raise ValueError("Selected-only execution must run exactly one non-skipped test")
        selected = [t for t in report[expected] if t.get("fullTitle") == case["test_full_title"]]
        other = "passes" if expected == "failures" else "failures"
        if (
            len(selected) != 1
            or any(
                t.get("fullTitle") == case["test_full_title"]
                for t in report[other] + report.get("pending", [])
            )
            or len([t for t in report["tests"] if t.get("fullTitle") == case["test_full_title"]])
            != 1
        ):
            raise ValueError(f"Selected test not uniquely {expected} in {state}")
        if state == "failed" and (
            record["returncode"] == 0 or not selected[0].get("err", {}).get("stack")
        ):
            raise ValueError("Failed target lacks exit failure or original error stack")
        # Other historical suite failures are preserved, not silently treated as a green build.
        if (record["returncode"] == 0) != (len(report["failures"]) == 0):
            raise ValueError("Exit status contradicts reported suite failures")
    if records["failed"]["image_id"] != records["fixed"]["image_id"]:
        raise ValueError("Paired states used different dependency images")


def execute(
    output, selection_path=SELECTION, *, selected_test_only=False, capture_error_details=False
):
    selection = load_selection(selection_path)
    plan = json.loads((output / "reproduction.json").read_text("utf-8"))
    if plan["selection_sha256"] != sha(selection_path) or plan["state"] != "prepared_not_executed":
        raise ValueError("Stale or already executed plan; prepare a fresh directory")
    docker(["info", "--format", "{{.ServerVersion}}"])
    inspected = json.loads(docker(["image", "inspect", plan["base_image"]]).stdout)[0]
    if not inspected.get("RepoDigests"):
        raise ValueError("Pull node:8.0.0 first; a registry digest is required")
    base_digest = inspected["RepoDigests"][0]
    plan.update(
        state="running",
        base_digest=base_digest,
        output_capture="regular-file-then-stream.v1",
        test_scope="selected_regression" if selected_test_only else "full_suite",
        report_collector="mocha-errors.v1" if capture_error_details else "builtin-json",
    )
    if capture_error_details:
        plan["reporter_sha256"] = sha(ERROR_REPORTER)
    save(output / "reproduction.json", plan)
    for result, case in zip(plan["cases"], selection["cases"], strict=True):
        if result["status"] != "prepared":
            continue
        directory = output / case["id"]
        try:
            # Check prepared files have not changed since extraction.
            for state, info in case["states"].items():
                with zipfile.ZipFile(
                    artifact(selection, info["archive"], selection_path)
                ) as archive:
                    for item in archive.infolist():
                        if not item.is_dir():
                            path = directory / state / Path(*PurePosixPath(item.filename).parts[1:])
                            if path.read_bytes() != archive.read(item):
                                raise ValueError("Prepared source changed")
            reporter_copy = ""
            if capture_error_details:
                (directory / "mocha-errors.cjs").write_bytes(ERROR_REPORTER.read_bytes())
                reporter_copy = "COPY mocha-errors.cjs /mocha-errors.cjs\n"
            recipe = f"""FROM {base_digest}
WORKDIR /deps
COPY failed/package.json ./package.json
RUN npm install --ignore-scripts && (npm ls --json --all > /dependency-tree.json || true)
{reporter_copy}COPY failed/ /cases/failed/
COPY fixed/ /cases/fixed/
WORKDIR /app
USER node
"""
            (directory / "Dockerfile").write_text(recipe, encoding="utf-8")
            tag = "iris-bugsjs-" + str(case["bug"]) + "-" + uuid.uuid4().hex[:8]
            with (directory / "build.log").open("wb") as log:
                docker(["build", "-t", tag, str(directory.resolve())], stdout=log, timeout=1800)
            image = json.loads(docker(["image", "inspect", tag]).stdout)[0]
            image_id = image["Id"]
            (directory / "dependencies.json").write_bytes(
                docker(
                    ["run", "--rm", "--network", "none", image_id, "cat", "/dependency-tree.json"]
                ).stdout
            )
            version = (
                docker(["run", "--rm", "--network", "none", image_id, "node", "--version"])
                .stdout.decode()
                .strip()
            )
            if version != "v" + case["node_version"]:
                raise ValueError("Unexpected Node version")
            records = {}
            for state in ("failed", "fixed"):
                name = "iris-repro-" + uuid.uuid4().hex[:12]
                test_argv = list(case["test_argv"])
                if capture_error_details:
                    reporter_index = test_argv.index("--reporter") + 1
                    if test_argv[reporter_index] != "json":
                        raise ValueError("Expected the original JSON reporter")
                    test_argv[reporter_index] = "/mocha-errors.cjs"
                if selected_test_only:
                    test_argv += ["--grep", "^" + re.escape(case["test_full_title"]) + "$"]
                command = test_capture_command(state, test_argv)
                args = [
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "128",
                    "--memory",
                    "2g",
                    "--cpus",
                    "2",
                    "--tmpfs",
                    "/app:rw,exec,size=256m,mode=1777",
                    "--tmpfs",
                    "/tmp:rw,size=128m,mode=1777",
                    image_id,
                    "sh",
                    "-c",
                    command,
                ]
                try:
                    with (directory / (state + ".stdout")).open("wb") as log:
                        ran = docker(args, stdout=log, timeout=300, check=False)
                finally:
                    # Remove only our uniquely named temporary container, including after interrupts.
                    docker(["rm", "-f", name], check=False)
                raw = (directory / (state + ".stdout")).read_bytes()
                records[state] = {
                    "returncode": ran.returncode,
                    "report": read_mocha(raw),
                    "stdout_sha256": hashlib.sha256(raw).hexdigest(),
                    "image_id": image_id,
                    "command": args,
                }
                # Preserve completed attempts even when validation later rejects a pair.
                save(directory / "attempt-records.json", records)
            validate_pair(case, records, selected_test_only=selected_test_only)
            receipt = {
                "version": "bugsjs-pair-receipt.v1",
                "bug_id": case["id"],
                "selection_sha256": sha(selection_path),
                "source_states": case["states"],
                "node_version": version,
                "base_digest": base_digest,
                "dependencies_sha256": sha(directory / "dependencies.json"),
                "reference_patch_sha256": sha(directory / "reference.patch"),
                "output_capture": plan["output_capture"],
                "test_scope": plan["test_scope"],
                "report_collector": plan["report_collector"],
                "reporter_sha256": plan.get("reporter_sha256"),
                "records": records,
                "verified_at": datetime.now(UTC).isoformat(),
            }
            save(directory / "receipt.json", receipt)
            result.update(status="verified", receipt_sha256=sha(directory / "receipt.json"))
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            result.update(status="reproduction_failed", error=str(exc))
        finally:
            save(output / "reproduction.json", plan)
    plan["state"] = "completed"
    plan["independently_reproduced"] = all(c["status"] == "verified" for c in plan["cases"])
    save(output / "reproduction.json", plan)
    return plan


def failure_text(target):
    """Preserve a separately stored message when historical Mocha stripped it from stack."""
    err = target["err"]
    stack = err["stack"]
    message = err.get("message", "")
    if message and message not in stack:
        return f"{err.get('name', 'Error')}: {message}\n{stack}"
    return stack


def source_selection_frames(text):
    """Map observed /app frames for archive lookup only; leave model logs unchanged."""
    return "\n".join(
        f"({path}:{line}:{column})"
        for path, line, column in re.findall(
            r"(?<=[(\s])(?:/app/)?([\w$.-]+(?:/[\w$.-]+)*\.(?:js|mjs|cjs)):(\d+):(\d+)",
            text,
        )
    )


def build_dataset(output, destination=PAIRED, selection_path=SELECTION):
    selection = load_selection(selection_path)
    plan = json.loads((output / "reproduction.json").read_text("utf-8"))
    if (
        plan["selection_sha256"] != sha(selection_path)
        or not plan["independently_reproduced"]
        or {c["id"] for c in plan["cases"] if c["status"] == "verified"}
        != {c["id"] for c in selection["cases"]}
    ):
        raise ValueError(
            "All frozen bugs must be verified; no automatic replacement or silent exclusion"
        )
    cases = []
    for case in selection["cases"]:
        directory = output / case["id"]
        receipt_path = directory / "receipt.json"
        result = next(r for r in plan["cases"] if r["id"] == case["id"])
        if sha(receipt_path) != result["receipt_sha256"]:
            raise ValueError("Receipt hash mismatch")
        receipt = json.loads(receipt_path.read_text("utf-8"))
        collector = receipt.get("report_collector", "builtin-json")
        if collector not in {"builtin-json", "mocha-errors.v1"} or collector != plan.get(
            "report_collector", "builtin-json"
        ):
            raise ValueError("Mixed or unknown report collector")
        if collector == "mocha-errors.v1" and (
            sha(directory / "mocha-errors.cjs") != receipt.get("reporter_sha256")
            or receipt["reporter_sha256"] != plan.get("reporter_sha256")
        ):
            raise ValueError("Error reporter hash mismatch")
        scope = receipt.get("test_scope", "full_suite")
        if scope not in {"full_suite", "selected_regression"} or scope != plan.get(
            "test_scope", "full_suite"
        ):
            raise ValueError("Mixed or unknown test execution scope")
        if receipt["source_states"] != case["states"] or receipt["selection_sha256"] != sha(
            selection_path
        ):
            raise ValueError("Receipt source or selection mismatch")
        if (
            sha(directory / "dependencies.json") != receipt["dependencies_sha256"]
            or sha(directory / "reference.patch") != receipt["reference_patch_sha256"]
        ):
            raise ValueError("Dependency or reviewer evidence changed")
        for state, record in receipt["records"].items():
            stdout = directory / (state + ".stdout")
            if (
                sha(stdout) != record["stdout_sha256"]
                or read_mocha(stdout.read_bytes()) != record["report"]
            ):
                raise ValueError("Original report hash/content mismatch")
        validate_pair(case, receipt["records"], selected_test_only=scope == "selected_regression")
        failed = next(
            t
            for t in receipt["records"]["failed"]["report"]["failures"]
            if t["fullTitle"] == case["test_full_title"]
        )
        selection_stack = source_selection_frames(failure_text(failed))
        failed_files, _ = source_snapshot(
            artifact(selection, case["states"]["failed"]["archive"], selection_path).read_bytes(),
            selection_stack,
            policy=case.get("source_policy", selection.get("source_policy")),
        )
        test_selection = "\n".join(
            f"({file['path']}:1:1)" for file in failed_files if file["path"].startswith("test/")
        )
        for state, expected in (("failed", "failures"), ("fixed", "passes")):
            record = receipt["records"][state]
            target = next(
                t for t in record["report"][expected] if t["fullTitle"] == case["test_full_title"]
            )
            source_audit = {}
            files, _ = source_snapshot(
                artifact(selection, case["states"][state]["archive"], selection_path).read_bytes(),
                test_selection,
                policy=case.get("source_policy", selection.get("source_policy")),
                audit=source_audit,
            )
            # Same production-file selection in both states; never select by patch lines.
            text = (
                target["fullTitle"]
                + "\n"
                + (
                    failure_text(target)
                    if state == "failed"
                    else json.dumps({"passes": [target]}, ensure_ascii=False)
                )
            )
            identifier = case["id"] + "-" + state
            cases.append(
                {
                    "id": identifier,
                    "bug_id": case["id"],
                    "split": "evaluation",
                    "task_kind": "reproduced_test_observation",
                    "expected_route": "research_source_links",
                    "acceptable_statuses": [
                        "diagnosed" if state == "failed" else "no_failure_evidence"
                    ],
                    "gold": {
                        "label": "test_failure" if state == "failed" else "test_pass",
                        "causal_review": "pending",
                    },
                    "provenance": {
                        "independently_reproduced": True,
                        "receipt_sha256": sha(receipt_path),
                        "source_commit": case["states"][state]["commit"],
                        "selection_sha256": sha(selection_path),
                        "test_full_title": case["test_full_title"],
                        "scope": (
                            "selected regression test only; remaining suite not executed"
                            if scope == "selected_regression"
                            else "selected regression test only; full suite may have unrelated failures"
                        ),
                        "test_execution_scope": scope,
                        "executed_test_failures": len(record["report"]["failures"]),
                        "suite_failures": None
                        if scope == "selected_regression"
                        else len(record["report"]["failures"]),
                    },
                    "payload": {
                        "success": True,
                        "message": "selected test observation",
                        "data": {
                            "projectId": "public-source-evaluation",
                            "serviceId": case.get("repository", "unknown-project")
                            .rstrip("/")
                            .split("/")[-1]
                            .lower()
                            + "-library",
                            "deploymentId": "test-observation",
                            "attemptId": "selected-test",
                            "deploymentStatus": "UNKNOWN",
                            "failedStage": None,
                            "exitCode": None,
                            "logRange": {"from": None, "to": None, "isComplete": False},
                            "logs": [
                                {
                                    "id": "test-report",
                                    "sourceId": case.get("repository", "unknown-project")
                                    .rstrip("/")
                                    .split("/")[-1]
                                    .lower()
                                    + "-library",
                                    "stream": "unknown",
                                    "sequence": 1,
                                    "text": text,
                                }
                            ],
                            "source": None,
                        },
                    },
                    "source_files": files,
                }
            )
            if source_audit:
                cases[-1]["provenance"]["source_selection_audit"] = source_audit
    document = {
        "version": "iris-factorial-eval.v1",
        "dataset_kind": "public_reproduced_test_pairs",
        "split": "evaluation",
        "task_kind": "reproduced_test_observation",
        "note": f"{len(selection['cases'])} fixed public bugs, failed/fixed selected-test observations; not {2 * len(selection['cases'])} independent bugs",
        "provenance": {
            "selection_sha256": sha(selection_path),
            "reproduction_sha256": sha(output / "reproduction.json"),
        },
        "evaluation_scope": {
            "independently_reproduced": True,
            "root_cause_ground_truth_available": False,
            "semantic_accuracy": "pending independent patch/test review",
        },
        "observation_transform": "selected-observation.v2: fullTitle plus original stack; prepend original error name/message only if message is absent from stack; passing report remains JSON. Container path mapping is only for archive selection, not log rewriting.",
        "cases": cases,
    }
    if destination.exists():
        raise FileExistsError("Refuse to overwrite a frozen evaluation dataset")
    save(destination, document)
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "build"))
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--selection", type=Path, default=SELECTION)
    parser.add_argument("--output", type=Path, default=PAIRED)
    parser.add_argument(
        "--capture-error-details",
        action="store_true",
        help="Use a hashed observer reporter that preserves actual failure errors; run only",
    )
    parser.add_argument(
        "--selected-test-only",
        action="store_true",
        help="Run only the exact preselected fullTitle for every frozen bug; run action only",
    )
    args = parser.parse_args(argv)
    if (args.selected_test_only or args.capture_error_details) and args.action != "run":
        parser.error("Execution flags apply only to run")
    try:
        value = (
            prepare(args.work, args.selection)
            if args.action == "prepare"
            else execute(
                args.work,
                args.selection,
                selected_test_only=args.selected_test_only,
                capture_error_details=args.capture_error_details,
            )
            if args.action == "run"
            else build_dataset(args.work, args.output, args.selection)
        )
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"Reproduction stopped: {exc}\n")
    print(
        json.dumps(
            {"state": value.get("state", "dataset_built"), "cases": len(value["cases"])}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
