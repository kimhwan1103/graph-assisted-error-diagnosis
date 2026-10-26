"""E4: review all proposals, execute only one frozen, allowlisted Docker repair recipe."""

import json
import subprocess
import time

from .design import ROOT, digest
from .study import exclusive
from .study_v4 import read, save
from .study_v4_review import cells

COPY = "COPY --chown=node:node data/tasks.json ./data/tasks.json"
CHECK = "Promise.all(['/healthz','/api/tasks'].map(async p=>{const r=await fetch('http://127.0.0.1:3000'+p);if(!r.ok)throw Error(p);return r.json()})).then(([h,t])=>{if(h.status!=='ok'||!Array.isArray(t.tasks)||t.tasks.length===0)process.exit(2);console.log(JSON.stringify({health:true,function:true,task_count:t.tasks.length}))}).catch(()=>process.exit(3))"


def reproduce(output, *, execute=False):
    with exclusive(output):
        frozen = read(output / "frozen.json")
        known = next(
            c
            for c in read(ROOT / "evaluation/packaging.v1.json")["cases"]
            if c["id"] == "docker-copy-omission"
        )
        cases_by_id = {c["id"]: c for c in frozen["cases"]}
        entries = []
        for cell in cells(output):
            public = cell["result"]
            plans = (public.get("analysis") or {}).get("remediation", {}).get("plans", [])
            case = cases_by_id[cell["case_id"]]
            # No LLM command, package install, or unconstrained patch reaches subprocess.
            changes = [change for plan in plans for change in plan["changes"]]
            supported = (
                case["id"] == known["id"]
                and digest(case["source_files"]) == digest(known["source_files"])
                and public.get("job_status") == "succeeded"
                and len(changes) == 1
                and changes[0].get("target") == "Dockerfile"
                and changes[0].get("snippet", "").strip() == COPY
            )
            entries.append(
                {
                    "cell_id": cell["cell_id"],
                    "case_id": case["id"],
                    "response_sha256": digest(public),
                    "proposals": plans,
                    "recipe": "frozen-docker-copy.v1" if supported else None,
                    "status": "eligible_for_controlled_reproduction"
                    if supported
                    else "manual_review_or_unsupported",
                    "application_conditions_reviewed": None,
                    "repair_success": None,
                    "note": "A controlled fixture repair does not validate production applicability, data privacy, or arbitrary remediation plans.",
                }
            )
        manifest = {
            "version": "study-v4-remediation.v1",
            "study_sha256": digest(frozen),
            "executed": False,
            "entries": entries,
        }
        save(output / "remediation/manifest.json", manifest)
        if not execute:
            print(
                f"Remediation review/eligibility manifest: {output / 'remediation/manifest.json'}"
            )
            return manifest
        if not frozen["live"]:
            raise ValueError("Mock model proposals cannot be reported as repair successes")
        eligible = [entry for entry in entries if entry["recipe"]]
        if not eligible:
            print(
                "No final response matched the frozen executable recipe; manual review remains required."
            )
            return manifest
        receipt_path = output / "remediation/receipt.json"
        if receipt_path.exists():
            raise ValueError(
                "A reproduction receipt already exists; preserve it instead of rerunning silently"
            )
        logs = []

        def docker(*args, timeout=60, required=True):
            result = subprocess.run(
                ["docker", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            logs.append(
                {
                    "argv": ["docker", *args],
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            if required and result.returncode:
                raise ValueError(f"Docker command failed: {args[0]}; inspect receipt")
            return result

        receipt = {
            "recipe": "frozen-docker-copy.v1",
            "study_sha256": digest(frozen),
            "linked_cells": [e["cell_id"] for e in eligible],
            "commands": logs,
            "repair_success": None,
            "failure_reproduced": None,
            "rollback_reproduced": None,
        }
        prefix = "iris-study-v4-" + digest(frozen)[:12]
        containers, tags = [], []
        try:
            base_info = json.loads(docker("image", "inspect", "node:22-alpine").stdout)[0]
            base = next(
                (ref for ref in base_info.get("RepoDigests", []) if "@sha256:" in ref), None
            )
            if base is None:
                raise ValueError(
                    "A local node:22-alpine image with a repository digest is required"
                )
            receipt["base_image_id"] = base_info["Id"]
            receipt["base_repository_digest"] = base
            workspace = output / "remediation/fixture"
            workspace.mkdir(parents=True, exist_ok=True)
            for source in known["source_files"]:
                path = workspace / source["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source["content"], "utf-8")
            source = (
                (workspace / "Dockerfile")
                .read_text("utf-8")
                .replace("FROM node:22-alpine", "FROM " + base, 1)
            )
            lines = source.splitlines()
            if lines[4] != "COPY --chown=node:node public ./public":
                raise ValueError("Frozen recipe insertion anchor changed")
            for mode, content in (
                ("before", source),
                ("after", "\n".join(lines[:5] + [COPY] + lines[5:]) + "\n"),
            ):
                path = workspace / ("Dockerfile." + mode)
                path.write_text(content, "utf-8")
                tag = prefix + "-" + mode
                tags.append(tag)
                print(f"Controlled reproduction: build {mode}", flush=True)
                docker(
                    "build",
                    "--pull=false",
                    "--network=none",
                    "-t",
                    tag,
                    "-f",
                    str(path),
                    str(workspace),
                    timeout=180,
                )
            for mode in ("before", "after", "rollback"):
                tag = prefix + ("-after" if mode == "after" else "-before")
                name = prefix + "-run-" + mode
                docker(
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=64",
                    "--memory=128m",
                    "--cpus=1",
                    tag,
                )
                containers.append(name)
                passed = False
                for _ in range(20):
                    state = json.loads(
                        docker("inspect", name, "--format", "{{json .State}}").stdout
                    )
                    if mode == "after":
                        check = docker("exec", name, "node", "-e", CHECK, required=False)
                        if check.returncode == 0:
                            passed = True
                            break
                    elif not state["Running"]:
                        observed = docker("logs", name).stdout + logs[-1]["stderr"]
                        passed = (
                            state["ExitCode"] != 0
                            and "ENOENT" in observed
                            and "/app/data/tasks.json" in observed
                        )
                        break
                    time.sleep(0.5)
                receipt[
                    {
                        "before": "failure_reproduced",
                        "after": "repair_success",
                        "rollback": "rollback_reproduced",
                    }[mode]
                ] = passed
            receipt["repair_success"] = all(
                receipt[k] for k in ("failure_reproduced", "repair_success", "rollback_reproduced")
            )
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            receipt["error"] = str(exc)
            receipt["repair_success"] = False
        finally:
            cleanup_errors = []
            for name in containers:
                try:
                    docker("rm", "-f", name, required=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_errors.append(str(exc))
            for tag in tags:
                try:
                    docker("image", "rm", tag, required=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    cleanup_errors.append(str(exc))
            receipt["cleanup_errors"] = cleanup_errors
            save(receipt_path, receipt)
        print(f"Controlled reproduction receipt: {receipt_path}")
        return receipt
