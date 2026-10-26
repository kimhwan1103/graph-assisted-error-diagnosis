"""Equivalent positive candidates using plain Python, without an RDF graph or SPARQL."""

import time

from ..diagnosis_routing import FRAME, MISSING_FILE, candidate_ranges
from ..knowledge.node_source import file_read
from ..knowledge.packaging import prove_copy_omission


def direct_candidates(request, bundle, evidence, context=None):
    started = time.perf_counter()
    ranges = candidate_ranges(bundle)
    if not ranges or not evidence:
        return None
    selected = ranges[0]
    path = selected["path"]
    rows = [r for r in evidence if r["path"] == path]
    if not rows or [r["line"] for r in rows] != list(range(1, len(rows) + 1)):
        return None
    by_id = {r.id: r for r in bundle.lines}
    failure_id, frame_id = selected["evidence_ids"]
    failure, frame = by_id[failure_id], by_id[frame_id]
    target = MISSING_FILE.search(failure.text)[1]
    match = FRAME.fullmatch(frame.text)
    runtime_path, number = match[1].removeprefix("file://"), int(match[2])
    read = file_read(rows, runtime_path, number, target)
    if read is None:
        return None
    # The selector requires one producer/scope; the recognizer binds the exact path/line.
    candidate = {
        "id": "C1",
        "kind": "observed_file_read_failure",
        "target": target,
        "code_path": path,
        "code_line": number,
        "rule": "observed-file-read.v1",
        "evidence_ids": sorted([failure_id, frame_id]),
        "source_evidence_ids": sorted(read["source_ids"]),
        "claim": "로그의 ENOENT 대상과 스택 위치의 소스 읽기 경로가 일치한다. 배포상 원인은 미확정이다.",
    }
    packaging = prove_copy_omission(evidence, context, path, runtime_path, target)
    if not packaging and any(r["path"] != path for r in evidence):
        return None
    packet = {"candidate": candidate}
    if packaging:
        packet["packaging_candidate"] = packaging
    return {
        "packet": packet,
        "report": {
            "stage": "source",
            "variant": "procedural_RC",
            "status": "ok",
            "engine": "plain-python-no-rdf",
            "candidate": candidate,
            "packaging_candidate": packaging,
            "metrics": {"total_ms": (time.perf_counter() - started) * 1000, "conforms": None},
        },
    }
