"""Registered Node CLI/HTTP/batch replay, independent of the IRIS-only v1 engine."""

import json
import time

from cross_program_common_v4 import REQUIRED, ROOT, sha, write
from remediation_docker_v4 import DockerReplay, EvaluationError, materialize

VERSION = "node-copy-replay.v2"
CHECKER = ROOT / "tools/checks/cross_program/check.cjs"


class CrossReplay(DockerReplay):
    def prepare_image(self, files, dockerfile, spec, folder, label):
        if any(x["path"].startswith("__evaluation") for x in files):
            raise EvaluationError("Fixture overlaps checker namespace")
        pinned = self.pinned(dockerfile)
        pinned = pinned.replace(
            "USER node", "COPY --chown=node:node __evaluation /__evaluation\nUSER node"
        )
        materialize(folder, files, pinned)
        evaluator = folder / "__evaluation"
        evaluator.mkdir()
        (evaluator / "check.cjs").write_bytes(CHECKER.read_bytes())
        write(evaluator / "spec.json", spec)
        write(
            folder.parent / (label + "-transform.json"),
            {
                "base_image": self.base_image,
                "checker_sha256": sha(CHECKER),
                "original_dockerfile": dockerfile,
                "execution_dockerfile": pinned,
                "checker_location": "/__evaluation (not in model archive)",
            },
        )
        return self.build(folder, label)

    def probe(self, image):
        name = self.run_id + "-check-" + str(self.probe_count)
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
            "/__evaluation/check.cjs",
            timeout=60,
        )
        if r["exit_code"] != 0:
            self.ensure_available()
            raise EvaluationError("Checker execution failed; inspect command log")
        try:
            result = json.loads(r["stdout"].strip())
            for key in (
                "execution_pass",
                "functional_pass",
                "regression_pass",
                "data_preservation_pass",
                "target_failure_resolved",
            ):
                if type(result[key]) is not bool:
                    raise ValueError(key)
            return result
        except (ValueError, KeyError, TypeError) as exc:
            raise EvaluationError("Invalid checker output") from exc

    def capture(self, image, spec, label):
        name = self.start(image, label)
        deadline = time.monotonic() + 10
        state, http = None, None
        while time.monotonic() < deadline:
            r = self.command("inspect", name, "--format", "{{json .State}}")
            if r["exit_code"] != 0:
                raise EvaluationError("Capture inspect failed")
            state = json.loads(r["stdout"])
            if not state["Running"]:
                break
            if spec["lifecycle"] == "http":
                r = self.command(
                    "exec",
                    name,
                    "node",
                    "-e",
                    "fetch('http://127.0.0.1:8080/healthz').then(async r=>console.log(JSON.stringify({status:r.status,body:await r.json()}))).catch(()=>process.exitCode=1)",
                )
                if r["exit_code"] == 0:
                    http = json.loads(r["stdout"])
                    break
            time.sleep(0.1)
        if state is None or (state["Running"] and http is None):
            raise EvaluationError("Capture lifecycle timeout")
        logs = self.command("logs", "--timestamps", name)
        if logs["exit_code"] != 0:
            raise EvaluationError("Capture logs failed")
        # Docker keeps stdout/stderr identities; timestamps order streams without inventing sequence.
        records = []
        for stream in ("stdout", "stderr"):
            for sequence, line in enumerate(logs[stream].splitlines(), 1):
                timestamp, sep, text = line.partition(" ")
                if not sep or "T" not in timestamp:
                    raise EvaluationError("Missing Docker log timestamp")
                records.append(
                    {"timestamp": timestamp, "stream": stream, "sequence": sequence, "text": text}
                )
        records.sort(key=lambda x: (x["timestamp"], x["stream"], x["sequence"]))
        return {
            "container": name,
            "state": state,
            "http": http,
            "raw_stdout": logs["stdout"],
            "raw_stderr": logs["stderr"],
            "records": records,
            "capture_policy": "docker logs --timestamps, stream identity retained; cross-stream ordering by timestamp; no log filtering",
        }

    def replay(self, files, dockerfile, spec, folder, label):
        start = len(self.commands)
        result = dict.fromkeys(REQUIRED)
        result.update(
            health_pass=None, evaluation_error=None, executed=True, base_image=self.base_image
        )
        try:
            image, built = self.prepare_image(files, dockerfile, spec, folder, label)
            result.update(application_pass=True, build_pass=built)
            if built:
                result.update(self.probe(image))
        except (EvaluationError, OSError, ValueError) as exc:
            result["evaluation_error"] = str(exc)
        result["commands"] = self.commands[start:]
        return result
