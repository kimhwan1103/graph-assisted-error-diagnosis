"""Pinned BugsJS historical test reports and matching test-only source snapshots.

Reads public artifacts only. Never installs dependencies or executes project code.
"""

import argparse
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

from ..source_contracts import SourceSnapshot, safe_source_path
from .design import ROOT
from .public_data import sha256

REPOSITORY = "https://github.com/BugsJS/bug-dataset"
REVISION = "7abbad3e4df12cd5294110bb5db11b7d5bc758a6"
PROJECT_REPOSITORY = "https://github.com/BugsJS/hessian.js"
DEFAULT_OUTPUT = ROOT / "evaluation/public/bugsjs_hessian/development.v2.json"
TASK = "bug_regression_diagnosis"
COMMITS = {
    3: "133ab71c142b4e8032d970fd972c1d1a9659d5c5",
    5: "02b3fea2b86ca7a2a74371051ad4c53a97ee8e0d",
    9: "8f8699ea924bdaa07cb7cbc44899c97f4cbc8587",
}
REPORT_HASHES = {
    1: "66660480e8a531d1289e75712a960a77f1005d8c4e07f26f7d9d5bb2a25e052b",
    2: "1904c43fe45c20ac1f3381c791dff2dfe6e9887f935d1959c0c690dcfa2d83ef",
    3: "0c8ffacfcec15b3efcac712bfb55dbb61a7f16444bed63d0f7cd2afeca8bbba7",
    4: "2a6e6a9b824a904fc93db5ed5a949653541f9657ae8c9af88e73fb978d105a57",
    5: "a105d18b6b38f507322afa22a666ef0a7097a38df3a5cfddd7e8e4d961c67d13",
    6: "afd745e0b7794e1336e9b3381304b4c071b17ab9c78e3f76b4d242a70179c057",
    7: "eece3bf864c37640a3b3f02c78dea9e6e649935cfbe4b4aed02a541c1abd44fa",
    8: "66bc8739fbce5099906bb2e53cc8640b505cfb8c69d4e9de42319dc1ed6d83bc",
    9: "4fa6743933046918edf63c1350ad27d26358277fa65097b184bd8a943c5d4268",
}
ARCHIVE_HASHES = {
    3: "e984db5525bbd91bff106e2e99d47b5b559d90513edc0a9590e3183181adf475",
    5: "045f798e0b57a98af73e0a9c47687b3b96a582ce3315bdeae64b5c72735f1f84",
    9: "12437077d98e6a69d521bff4ca572a473e4c4da7a6a7e14cc11b354ccf3ddb1e",
}
METADATA = {
    "LICENSE.benchmark": (
        "LICENSE",
        "5953d3d453ef8b521b5dde0750a37573d2a1a3eff0bb7e03f2cc57d095bbe95c",
    ),
    "README.benchmark.md": (
        "README.md",
        "092c75258c4d86c5ac907d344a098a93de98226155e5fecf5c6f1ec0e39bfe60",
    ),
    "Hessian.js_commands.csv": (
        "Projects/Hessian.js/Hessian.js_commands.csv",
        "7881c360af89b504e4603850b935c4d409fa82d4c4ed696cb7c904059edf9528",
    ),
}
# Exact relative stack frames; exclude dependency frames such as node_modules/should/lib/.
APP_FRAME = re.compile(r"\((lib/[^():\s]+\.js|index\.js):(\d+):(\d+)\)")
TEST_FRAME = re.compile(r"\((test/[^():\s]+\.js):(\d+):(\d+)\)")


def upstream_files():
    base = f"https://raw.githubusercontent.com/BugsJS/bug-dataset/{REVISION}"
    files = {
        f"Hessian.js-{bug}.zip": (f"{base}/Projects/Hessian.js/Hessian.js-{bug}.zip", checksum)
        for bug, checksum in REPORT_HASHES.items()
    }
    files.update(
        {name: (f"{base}/{path}", checksum) for name, (path, checksum) in METADATA.items()}
    )
    files.update(
        {
            f"hessian-{bug}-{commit}.zip": (
                f"https://codeload.github.com/BugsJS/hessian.js/zip/{commit}",
                ARCHIVE_HASHES[bug],
            )
            for bug, commit in COMMITS.items()
        }
    )
    return files


def read_pinned(directory, name):
    raw = (directory / name).read_bytes()
    if sha256(raw) != upstream_files()[name][1]:
        raise ValueError(f"Pinned BugsJS source checksum mismatch: {name}")
    return raw


def zip_member(archive, name, *, limit=4_000_000):
    members = [item for item in archive.infolist() if item.filename == name]
    if len(members) != 1 or members[0].file_size > limit:
        raise ValueError("Missing, duplicate or oversized archive member")
    return archive.read(members[0])


def read_reports(raw):
    reports = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for state in ("buggy", "only_test_changes"):
            nested = zip_member(archive, f"{state}/test_results.json.zip")
            with zipfile.ZipFile(io.BytesIO(nested)) as report_zip:
                reports[state] = read_mocha(zip_member(report_zip, "test_results.json"))
    return reports


def read_mocha(raw):
    """Extract one Mocha JSON/JSON-COV object, retaining noisy stdout separately."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    found = []
    for match in re.finditer(r'(?m)^\s*\{(?=\s*"(?:stats|instrumentation)"\s*:)', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.start() :].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and all(
            key in value for key in ("stats", "tests", "passes", "failures")
        ):
            found.append(value)
    if len(found) != 1:
        raise ValueError("Expected exactly one complete Mocha JSON report")
    return found[0]


def regression_failures(reports):
    prior = {test["fullTitle"] for test in reports["buggy"]["failures"]}
    return [
        test for test in reports["only_test_changes"]["failures"] if test["fullTitle"] not in prior
    ]


def source_snapshot(raw, stack, *, policy=None, audit=None):
    """Select all production JS plus the stack-referenced test, never a fix diff."""
    if policy is not None:
        return policy_source_snapshot(raw, policy, audit)
    test_paths = {path for path, _, _ in TEST_FRAME.findall(stack)}
    files = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        roots = {item.filename.split("/", 1)[0] for item in archive.infolist()}
        if len(roots) != 1:
            raise ValueError("Unexpected source archive roots")
        root = roots.pop() + "/"
        license_raw = zip_member(archive, root + "LICENSE", limit=65_536)
        for item in sorted(archive.infolist(), key=lambda value: value.filename):
            path = item.filename.removeprefix(root)
            selected = (
                path == "index.js"
                or (path.startswith("lib/") and path.endswith(".js"))
                or path in test_paths
            )
            if not selected or item.is_dir():
                continue
            safe_source_path(path)
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Source symlinks are not supported")
            files.append(
                {
                    "path": path,
                    "content": zip_member(archive, item.filename, limit=65_536).decode("utf-8"),
                }
            )
    snapshot = SourceSnapshot.model_validate({"files": files})
    index = {file.path: file.content.splitlines() for file in snapshot.files}
    for path, line, _ in APP_FRAME.findall(stack) + TEST_FRAME.findall(stack):
        if path not in index or not 0 < int(line) <= len(index[path]):
            raise ValueError("Reported source frame is outside the pinned snapshot")
    return snapshot.model_dump()["files"], license_raw


def policy_matches(path, policy):
    from fnmatch import fnmatchcase

    if not policy.get("version") or not policy.get("include") or "exclude" not in policy:
        raise ValueError("Source policy requires version/include/exclude")

    def matches(pattern):
        return fnmatchcase(path, pattern) or (
            "**/" in pattern and fnmatchcase(path, pattern.replace("**/", ""))
        )

    return any(matches(p) for p in policy["include"]) and not any(
        matches(p) for p in policy["exclude"]
    )


def policy_source_snapshot(raw, policy, audit=None):
    """Explicit bounded project policy; exceeding a bound is an error, not truncation."""
    files, excluded, excluded_bytes = [], 0, 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        roots = {i.filename.split("/", 1)[0] for i in archive.infolist()}
        if len(roots) != 1 or len(archive.infolist()) > 5000:
            raise ValueError("Unexpected source archive")
        root = roots.pop() + "/"
        license_raw = zip_member(archive, root + policy.get("license_file", "LICENSE"), limit=65536)
        for item in sorted(archive.infolist(), key=lambda i: i.filename):
            if item.is_dir():
                continue
            path = item.filename.removeprefix(root)
            if not policy_matches(path, policy):
                excluded += 1
                excluded_bytes += item.file_size
                continue
            safe_source_path(path)
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Source symlinks are not supported")
            files.append(
                {
                    "path": path,
                    "content": zip_member(archive, item.filename, limit=65536).decode("utf-8"),
                }
            )
    snapshot = SourceSnapshot.model_validate({"files": files})
    if audit is not None:
        audit.update(
            policy=policy,
            selected_files=len(files),
            excluded_files=excluded,
            selected_bytes=sum(len(f["content"].encode("utf-8")) for f in files),
            excluded_bytes=excluded_bytes,
            boundary="operational SourceSnapshot: <=20 files, <=65536 bytes/file, <=262144 bytes total",
        )
    return snapshot.model_dump()["files"], license_raw


def build_pilot(directory):
    audit, cases = [], []
    for bug in range(1, 10):
        report_name = f"Hessian.js-{bug}.zip"
        reports = read_reports(read_pinned(directory, report_name))
        regressions = regression_failures(reports)
        eligible = [
            test for test in regressions if APP_FRAME.search(test.get("err", {}).get("stack", ""))
        ]
        audit.append(
            {
                "bug_id": bug,
                "original_failure_count": len(reports["buggy"]["failures"]),
                "test_only_failure_count": len(reports["only_test_changes"]["failures"]),
                "additional_failure_titles": len(regressions),
                "eligible_project_stack_failures": len(eligible),
                "selected": bool(eligible),
                "reason": "first additional failure with a production source stack frame"
                if eligible
                else "no additional failure with a relative production source frame",
            }
        )
        if not eligible:
            continue
        if bug not in COMMITS:
            raise ValueError(
                "Sampling changed; pin the additional eligible source before proceeding"
            )
        test = eligible[0]
        stack = test["err"]["stack"]
        commit = COMMITS[bug]
        archive_name = f"hessian-{bug}-{commit}.zip"
        files, license_raw = source_snapshot(read_pinned(directory, archive_name), stack)
        # Full title + original error stack only. No rewritten or fabricated runtime log.
        text = test["fullTitle"] + "\n" + stack
        case_id = f"bugsjs-hessian-{bug}"
        cases.append(
            {
                "id": case_id,
                "task_kind": TASK,
                "source_kind": "public_buggy_source_and_historical_test_report",
                "expected_route": "research_source_links",
                "split": "development",
                "bug_id": case_id,
                "acceptable_statuses": ["diagnosed", "insufficient_evidence"],
                "gold": {
                    "label": "reported_regression_failure",
                    "root_cause_review": "pending; fixed patch not scored by this pilot",
                },
                "provenance": {
                    "report_url": upstream_files()[report_name][0],
                    "report_sha256": REPORT_HASHES[bug],
                    "report_member": "only_test_changes/test_results.json.zip!test_results.json",
                    "test_full_title": test["fullTitle"],
                    "source_tag": f"Bug-{bug}-test",
                    "source_commit": commit,
                    "source_url": f"{PROJECT_REPOSITORY}/tree/{commit}",
                    "source_archive_sha256": ARCHIVE_HASHES[bug],
                    "project_license_sha256": sha256(license_raw),
                    "model_source_selection": "all lib/**/*.js and index.js; only test files referenced by selected stack; no fixed code or gold patch",
                    "independently_reproduced": False,
                },
                "quality_rubric": [
                    "Identify the observed failing operation and cite original EV and matching SC evidence.",
                    "Distinguish a stack frame from the causal defective code; a dependency frame is not automatically the root cause.",
                    "State missing arguments/runtime/dependencies and propose a conditional next check.",
                    "Do not claim independent reproduction, a verified repair, deployment failure or root-cause certainty.",
                ],
                "payload": {
                    "success": True,
                    "message": "public historical test report",
                    "data": {
                        "projectId": "public-source-pilot",
                        "serviceId": "hessian-library",
                        "deploymentId": "historical-test-observation",
                        "attemptId": case_id,
                        "deploymentStatus": "UNKNOWN",
                        "failedStage": None,
                        "exitCode": None,
                        "logRange": {"from": None, "to": None, "isComplete": False},
                        "logs": [
                            {
                                "id": "test-failure",
                                "sourceId": "hessian-library",
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
    return {
        "version": "iris-factorial-eval.v1",
        "dataset_kind": "public_real_bug_source_pilot",
        "split": "development",
        "task_kind": TASK,
        "note": "BugsJS Hessian.js 3 historical regression failures with pinned Bug-N-test sources. Source integration preflight, not a representative RCA or graph-effect benchmark.",
        "provenance": {
            "repository": REPOSITORY,
            "revision": REVISION,
            "project_repository": PROJECT_REPOSITORY,
            "sampling": "inspect all 9 Hessian.js IDs in ascending order; select one additional failing test per bug with an exact relative lib/*.js or index.js stack frame; choose first report-order eligible failure; no selection by graph success or fix patch",
            "selection_audit": audit,
            "source_version": "publisher-associated Bug-N-test (test changes on buggy production code); pinned commits and archive SHA-256",
            "model_input_transform": "extract fullTitle and err.stack verbatim from selected official Mocha JSON failure; use unchanged source contents; UNKNOWN deployment metadata",
            "licenses": "BugsJS framework MIT and Hessian.js MIT; original licenses retained in source archives and adjacent LICENSE files",
        },
        "evaluation_scope": {
            "source_code_available": True,
            "root_cause_ground_truth_available": False,
            "independently_reproduced": False,
            "graph_effect_identifiable": True,
            "semantic_accuracy": "pending independent review against BugsJS fixes and reproduced tests; status agreement is not causal accuracy",
            "limitations": [
                "3 bugs from one library, selected for source-frame availability; not representative, no significance or held-out contamination claim.",
                "Historical reports include unrelated failures; this pilot retains one additional failing test per eligible bug, not a full CI/build log.",
                "No healthy controls or fixed-build report; cannot estimate false-positive rate or verified repair success.",
                "Original tests have not been re-executed locally; source/report association follows the benchmark publisher.",
                "Research source-link graph connects observed error frames to source lines. These three development cases influenced the rule; exclude from held-out quality claims. Candidates are not causal ground truth.",
            ],
        },
        "cases": cases,
    }


def download_sources(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name, (url, checksum) in upstream_files().items():
        if (directory / name).exists():
            read_pinned(directory, name)
            continue
        request = urllib.request.Request(url, headers={"User-Agent": "IRIS-public-source-pilot"})
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read(10_000_001)
        if len(raw) > 10_000_000 or sha256(raw) != checksum:
            raise ValueError(f"Pinned download size/checksum mismatch: {name}")
        (directory / name).write_bytes(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch pinned public artifacts locally; not needed in offline Kaggle",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    directory = args.output.parent / "source"
    if args.fetch:
        download_sources(directory)
    for name in upstream_files():
        read_pinned(directory, name)
    document = build_pilot(directory)
    serialized = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if args.output.exists() and args.output.read_text("utf-8") != serialized:
        raise ValueError("Refuse to replace a different pilot; choose a new --output")
    args.output.write_text(serialized, encoding="utf-8")
    provenance = {
        name: {"url": url, "sha256": checksum} for name, (url, checksum) in upstream_files().items()
    }
    (directory / "PROVENANCE.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    _, license_raw = source_snapshot(read_pinned(directory, f"hessian-3-{COMMITS[3]}.zip"), "")
    (directory / "LICENSE.hessian").write_bytes(license_raw)
    print(
        json.dumps(
            {
                "manifest": str(args.output),
                "case_ids": [case["id"] for case in document["cases"]],
                "independently_reproduced": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
