"""Fetch a pinned public Loghub BGL sample and prepare a reproducible alert pilot."""

import argparse
import hashlib
import json
import random
import urllib.request
from collections import Counter
from pathlib import Path

from .design import ROOT

REPOSITORY = "https://github.com/logpai/loghub"
REVISION = "dd61d0952749ee7963bde24220d1be5ede023033"
TASK = "log_alert_detection"
DEFAULT_OUTPUT = ROOT / "evaluation/public/loghub_bgl/pilot.v1.json"
UPSTREAM_FILES = {
    "BGL_2k.log": "BGL/BGL_2k.log",
    "README.upstream.md": "BGL/README.md",
    "LICENSE": "LICENSE",
}
SOURCE_HASHES = {
    "BGL_2k.log": "2a819ea540909db682005c9cf948387a40729b5c2e9f19d430e29ce704825496",
    "README.upstream.md": "4ec19cee4f8b4086ab511cae2fe901a9e298475f4d678f8c9e84e484691031ef",
    "LICENSE": "9ffa6ae259833cdc6e7ed8a0219fec72ef6455fb457b3c44096cb1ac1f0696d4",
}


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def parse_logs(raw):
    records = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        parts = line.split(maxsplit=9)
        if len(parts) != 10 or not parts[1].isdigit():
            raise ValueError(f"Malformed BGL line {number}")
        records.append(
            {
                "line_number": number,
                "source_label": parts[0],
                "label": "non_alert" if parts[0] == "-" else "alert",
                # Remove only the leading ground-truth category; preserve the log verbatim.
                "text": line.split(maxsplit=1)[1],
                "event_key": tuple(parts[6:]),
            }
        )
    return records


def build_pilot(raw, *, count=20, seed=42):
    if count < 2 or count % 2:
        raise ValueError("count must be positive, even and at least 2")
    records = parse_logs(raw)
    pools = {"alert": [], "non_alert": []}
    seen = set()
    for record in records:
        key = (record["label"], record["event_key"])
        if key not in seen:
            seen.add(key)
            pools[record["label"]].append(record)
    if min(map(len, pools.values())) < count // 2:
        raise ValueError("Not enough distinct events for a balanced pilot")
    rng = random.Random(seed)
    groups = [rng.sample(pools[label], count // 2) for label in ("alert", "non_alert")]
    selected = []
    for pair in zip(*groups, strict=True):
        ordered = list(pair)
        rng.shuffle(ordered)
        selected.extend(ordered)
    cases = []
    for record in selected:
        number = record["line_number"]
        case_id = f"bgl-line-{number:04d}"
        cases.append(
            {
                "id": case_id,
                "task_kind": TASK,
                "source_kind": "public_log_without_source",
                "expected_route": "unsupported_no_source",
                "acceptable_statuses": [
                    "diagnosed" if record["label"] == "alert" else "no_failure_evidence"
                ],
                "gold": {
                    "label": record["label"],
                    "source_label": record["source_label"],
                    "line_number": number,
                },
                "provenance": {"url": f"{REPOSITORY}/blob/{REVISION}/BGL/BGL_2k.log#L{number}"},
                "quality_rubric": [
                    "Classify the observed log's alert evidence, citing its EV reference.",
                    "Do not claim a root cause, source location, repair or recovery without evidence.",
                    "Non-alert labels do not prove an entire system is healthy.",
                ],
                "payload": {
                    "success": True,
                    "message": "public log observation",
                    "data": {
                        "projectId": "public-log-pilot",
                        "serviceId": "observed-system",
                        "deploymentId": "observation",
                        "attemptId": case_id,
                        "deploymentStatus": "UNKNOWN",
                        "failedStage": None,
                        "exitCode": None,
                        "logRange": {"from": None, "to": None, "isComplete": False},
                        "logs": [
                            {
                                "id": "observed-log",
                                "sourceId": "observed-system",
                                "stream": "unknown",
                                "sequence": 1,
                                "text": record["text"],
                            }
                        ],
                        "source": None,
                    },
                },
                "source_files": [],
            }
        )
    return {
        "version": "iris-factorial-eval.v1",
        "dataset_kind": "public_real_logs_pilot",
        "task_kind": TASK,
        "note": "Loghub BGL_2k public real logs; balanced distinct-event line-level alert pilot. Not full BGL, deployment root-cause accuracy, or a graph-effect benchmark.",
        "provenance": {
            "repository": REPOSITORY,
            "revision": REVISION,
            "file": "BGL/BGL_2k.log",
            "sha256": sha256(raw),
            "source_lines": len(records),
            "original_label_counts": dict(Counter(record["label"] for record in records)),
            "distinct_event_counts": {label: len(pool) for label, pool in pools.items()},
            "sampling": "seeded stratified sampling, equal alert/non-alert, exact event tuple deduplication; pairs interleaved for balanced two-case smoke tests",
            "seed": seed,
            "sample_size": count,
            "model_input_transform": "remove first whitespace-delimited alert label only; preserve remaining raw log, no fabricated code/logs; UNKNOWN deployment context for both classes",
            "license": "Loghub research/academic license; see adjacent source/LICENSE and source/README.upstream.md",
        },
        "evaluation_scope": {
            "metric": "line-level alert detection, abstentions count as incorrect in overall accuracy; report coverage separately",
            "graph_effect_identifiable": False,
            "root_cause_ground_truth_available": False,
            "source_code_available": False,
            "limitations": [
                "BGL_2k is a public 2,000-line sample; balanced subset is not representative of production prevalence.",
                "Lines are not independent deployment incidents; sampling by exact event tuple does not establish independence.",
                "Public pretraining contamination cannot be excluded; no held-out-model claim.",
                "Source code is unavailable; graph arms fall back to identical raw input. Do not claim graph improvements.",
            ],
        },
        "cases": cases,
    }


def download_sources(directory):
    directory.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, upstream in UPSTREAM_FILES.items():
        url = f"https://raw.githubusercontent.com/logpai/loghub/{REVISION}/{upstream}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "IRIS-public-evaluation-pilot"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Public source exceeds download size limit")
        if sha256(raw) != SOURCE_HASHES[name]:
            raise ValueError(f"Pinned source checksum mismatch: {name}")
        path = directory / name
        if path.exists() and path.read_bytes() != raw:
            raise ValueError(f"Refuse to replace a different source file: {path}")
        path.write_bytes(raw)
        hashes[name] = {"url": url, "sha256": sha256(raw), "bytes": len(raw)}
    (directory / "PROVENANCE.json").write_text(
        json.dumps(hashes, indent=2) + "\n", encoding="utf-8"
    )
    return directory / "BGL_2k.log"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Download pinned upstream files; run locally, not in an offline Kaggle session",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    source = args.output.parent / "source/BGL_2k.log"
    if args.fetch:
        source = download_sources(source.parent)
    raw = source.read_bytes()
    if sha256(raw) != SOURCE_HASHES["BGL_2k.log"]:
        raise ValueError("Pinned BGL source checksum mismatch")
    document = build_pilot(raw, count=args.count, seed=args.seed)
    serialized = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists() and args.output.read_text(encoding="utf-8") != serialized:
        raise ValueError("Refuse to overwrite a different sample; choose a new --output")
    args.output.write_text(serialized, encoding="utf-8")
    print(
        json.dumps(
            {
                "manifest": str(args.output),
                "cases": len(document["cases"]),
                "source": document["provenance"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
