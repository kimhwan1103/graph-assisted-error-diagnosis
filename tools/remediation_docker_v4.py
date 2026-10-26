"""Isolated, network-disabled Docker replay of the pinned IRIS COPY fixture."""

import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

VERSION = "iris-copy-replay.v1"
CHECKS = Path(__file__).with_name("remediation_checks_v4.cjs")
REQUIRED_CHECKS = [
    "baseline_failure_reproduced",
    "application_pass",
    "build_pass",
    "startup_pass",
    "target_failure_resolved",
    "health_pass",
    "functional_pass",
    "regression_pass",
    "data_preservation_pass",
]


class EvaluationError(RuntimeError):
    pass


def file_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def materialize(folder, files, dockerfile):
    folder.mkdir(parents=True, exist_ok=False)
    for item in files:
        relative = Path(item["path"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in item["path"]
            or ":" in item["path"]
        ):
            raise ValueError("Unsafe fixture path")
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item["content"].encode("utf-8"))
    (folder / "Dockerfile").write_bytes(dockerfile.encode("utf-8"))


class DockerReplay:
    def __init__(self, output):
        self.output = output
        self.commands = []
        self.containers, self.images = [], []
        self.run_id = "iris-remed-" + uuid.uuid4().hex[:16]
        self.base = None
        self.probe_count = 0
        self.agent_attempts = 0
        self.base_image = None

    def command(self, *args, timeout=30):
        command = ["docker", *map(str, args)]
        start = time.monotonic()
        entry = {"argv": command, "timeout_seconds": timeout}
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
            entry.update(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            entry.update(
                exit_code=None,
                stdout=str(exc.stdout or ""),
                stderr=str(exc.stderr or ""),
                timed_out=True,
            )
        except OSError as exc:
            entry.update(exit_code=None, stdout="", stderr=str(exc), timed_out=False)
        entry["elapsed_seconds"] = time.monotonic() - start
        self.commands.append(entry)
        return entry

    def ensure_available(self):
        result = self.command("info", "--format", "{{.ServerVersion}}", timeout=10)
        if result["exit_code"] != 0:
            raise EvaluationError("Docker engine unavailable; see execution command log")

    def initialize(self, image):
        self.ensure_available()
        result = self.command("image", "inspect", image)
        if result["exit_code"] != 0:
            raise EvaluationError(
                "Required local base image is unavailable; evaluator never pulls images"
            )
        info = json.loads(result["stdout"])[0]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", info["Id"]) or not info.get("RepoDigests"):
            raise EvaluationError("Base image requires an immutable ID and repository digest")
        self.base = next(
            (
                ref
                for ref in info["RepoDigests"]
                if re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", ref)
            ),
            None,
        )
        if self.base is None:
            raise EvaluationError("Base image has no usable immutable repository reference")
        self.base_image = {
            "requested": image,
            "id": info["Id"],
            "pinned_from": self.base,
            "repo_digests": info["RepoDigests"],
        }

    def build(self, workspace, label):
        tag = self.run_id + "-" + label
        self.images.append(tag)
        r = self.command(
            "build",
            "--pull=false",
            "--network=none",
            "--no-cache",
            "-t",
            tag,
            "-f",
            workspace / "Dockerfile",
            workspace,
            timeout=180,
        )
        if r["exit_code"] != 0:
            self.ensure_available()  # distinguish engine interruption from a failed build
            return tag, False
        return tag, True

    @staticmethod
    def restrictions():
        return [
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=192m",
            "--cpus=1",
        ]

    def start(self, image, label):
        name = self.run_id + "-" + label
        self.containers.append(name)
        r = self.command("run", "-d", "--name", name, *self.restrictions(), image)
        if r["exit_code"] != 0:
            raise EvaluationError("Container creation failed; not a model repair verdict")
        return name

    def failure(self, image, label, target):
        name = self.start(image, label)
        for _ in range(25):
            state = self.command("inspect", name, "--format", "{{json .State}}")
            if state["exit_code"] != 0:
                raise EvaluationError("Cannot inspect controlled container")
            state = json.loads(state["stdout"])
            if not state["Running"]:
                logs = self.command("logs", name)
                text = logs["stdout"] + logs["stderr"]
                return state["ExitCode"] != 0 and "ENOENT" in text and target in text
            time.sleep(0.2)
        return False

    def check_script(self, mode, spec):
        # The fixed checker is serialized directly, without a shell or host mount.
        code = CHECKS.read_text(encoding="utf-8")
        fn = (
            "regression('/app')"
            if mode == "regression"
            else "probe(" + json.dumps(spec, ensure_ascii=False) + ")"
        )
        return (
            code
            + "\n"
            + fn
            + ".then(x=>console.log(JSON.stringify(x))).catch(e=>{console.error(String(e));process.exitCode=2})"
        )

    def regression(self, image, spec):
        name = self.run_id + "-reg-" + str(self.probe_count)
        self.probe_count += 1
        self.containers.append(name)
        r = self.command(
            "run",
            "--name",
            name,
            *self.restrictions(),
            "--entrypoint",
            "node",
            image,
            "-e",
            self.check_script("regression", spec),
        )
        if r["exit_code"] != 0:
            self.ensure_available()
            return {"pass": False, "checks": {"probe_exit": False}}
        return json.loads(r["stdout"].strip().splitlines()[-1])

    def inspect_after(self, image, label, spec):
        name = self.start(image, label)
        r = self.command("exec", name, "node", "-e", self.check_script("full", spec), timeout=45)
        if r["exit_code"] != 0:
            self.ensure_available()
            # Independent config regression can still be tested on a failed-start image.
            reg = self.regression(image, spec)
            return {
                "startup_pass": False,
                "health_pass": None,
                "functional_pass": None,
                "regression_pass": reg["pass"],
                "data_preservation_pass": None,
                "target_failure_resolved": False,
                "details": {"regression": reg},
            }
        values = json.loads(r["stdout"].strip().splitlines()[-1])
        logs = self.command("logs", name)
        if "ENOENT" in logs["stdout"] + logs["stderr"]:
            values["target_failure_resolved"] = False
        return values

    def pinned(self, content):
        if content.splitlines()[0] != "FROM node:22-alpine" or content.count("\nFROM "):
            raise EvaluationError(
                "Unsupported fixture base/stage; v1 requires the frozen Node fixture"
            )
        return content.replace("FROM node:22-alpine", "FROM " + self.base, 1)

    def preflight(self, case, fixture):
        start = len(self.commands)
        result = {
            "case_id": case["case_id"],
            "status": "evaluation_error",
            "baseline_failure_reproduced": None,
            "baseline_regression_pass": None,
            "reference_restored": None,
            "rollback_reproduced": None,
        }
        try:
            if self.base is None:
                self.initialize(case["execution"]["base_image"])
            files = fixture["source_files"]
            original = next(f["content"] for f in files if f["path"] == "Dockerfile")
            directory = self.output / "preflight" / case["case_id"]
            before, reference = directory / "before", directory / "reference"
            materialize(before, files, self.pinned(original))
            image, built = self.build(before, "baseline")
            if not built:
                raise EvaluationError("Baseline image cannot build; case not technically qualified")
            result["baseline_failure_reproduced"] = self.failure(
                image, "baseline", case["tests"]["target_path"]
            )
            result["baseline_regression_pass"] = self.regression(image, case["tests"])["pass"]
            # Oracle is used only in preflight, never by the plan adapter.
            anchor = "COPY --chown=node:node public ./public\n"
            if original.count(anchor) != 1:
                raise EvaluationError("Frozen preflight anchor mismatch")
            repair = original.replace(
                anchor, anchor + "COPY --chown=node:node data/tasks.json ./data/tasks.json\n"
            )
            materialize(reference, files, self.pinned(repair))
            ref_image, built = self.build(reference, "reference")
            if not built:
                raise EvaluationError("Reference image cannot build")
            checks = self.inspect_after(ref_image, "reference", case["tests"])
            result["reference_checks"] = checks
            result["reference_restored"] = all(
                checks.get(k) is True
                for k in REQUIRED_CHECKS
                if k not in {"application_pass", "build_pass", "baseline_failure_reproduced"}
            )
            result["rollback_reproduced"] = self.failure(
                image, "rollback", case["tests"]["target_path"]
            )
            if not all(
                result[k] is True
                for k in (
                    "baseline_failure_reproduced",
                    "baseline_regression_pass",
                    "reference_restored",
                    "rollback_reproduced",
                )
            ):
                raise EvaluationError("Baseline/reference/rollback qualification failed")
            result["status"] = "passed"
        except (EvaluationError, OSError, ValueError, KeyError) as exc:
            result["error"] = str(exc)
        result["commands"] = self.commands[start:]
        result["base_image"] = self.base_image
        return result

    def evaluate(self, row, case, fixture, adapted, cell_folder):
        start = len(self.commands)
        result = {
            "executed": False,
            "execution_id": self.run_id + "-" + row["cell_id"],
            "application_pass": None,
            "build_pass": None,
            "evaluation_error": None,
        }
        try:
            workspace = cell_folder / "fixture"
            materialize(workspace, fixture["source_files"], self.pinned(adapted["dockerfile"]))
            result["application_pass"] = True
            (cell_folder / "patch.diff").write_text(adapted["patch_diff"], encoding="utf-8")
            result["environment_transform"] = {
                "base_image_pin": self.base,
                "model_dockerfile_sha256": adapted["normalized_patch_sha256"],
            }
            self.agent_attempts += 1
            result["executed"] = True
            image, built = self.build(workspace, row["cell_id"])
            result["build_pass"] = built
            if built:
                result.update(self.inspect_after(image, row["cell_id"], case["tests"]))
        except (EvaluationError, OSError, ValueError, KeyError) as exc:
            result["evaluation_error"] = str(exc)
        result["commands"] = self.commands[start:]
        return result

    def cleanup(self):
        failures = []
        for name in self.containers:
            r = self.command("rm", "-f", name)
            if r["exit_code"] != 0:
                failures.append({"container": name, "stderr": r["stderr"]})
        for image in self.images:
            r = self.command("image", "rm", image)
            if r["exit_code"] != 0:
                failures.append({"image": image, "stderr": r["stderr"]})
        return failures
