"""Replay one saved input through graph/contract code; never call a model or Docker."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from review_controls_contract_v2 import assess_primary, automatic_decision, canonical, read
from review_controls_prepare_v4 import evidence, graph_build

DATA = ROOT / "evaluation/review-controls-v4-contract-v2"
PROGRAMS = ("shipping-cli", "template-http", "settlement-batch")


def replay(program="shipping-cli"):
    case = next(
        c
        for c in read(DATA / "dataset.json")["cases"]
        if c["program_id"] == program and c["context_variant"] == "P_supported"
    )
    request, bundle, common, _ranges, _hints, _archive_hash = evidence(case)
    saved = read(DATA / "packets" / case["id"] / "common.json")
    if common != saved:
        raise ValueError("Reconstructed source/log selection differs from the published packet")
    graph = graph_build(request, bundle, common["source_evidence"], common["archive_observations"])
    if not graph or not graph["report"]["metrics"]["conforms"]:
        raise ValueError("Graph construction or SHACL validation failed")
    packet = graph["packet"]
    saved_packet = read(DATA / "packets" / case["id"] / "compact.json")
    if packet != saved_packet:
        raise ValueError("Reconstructed candidate packet differs from the published packet")
    assessment = assess_primary(canonical(automatic_decision(packet)), "R_auto", common, packet)
    if assessment["effective_decision"] != "accept":
        raise ValueError("The recorded automatic decision no longer assembles successfully")
    return {
        "mode": "offline_saved_input_example",
        "program": program,
        "case_id": case["id"],
        "saved_input_matches": True,
        "saved_candidate_matches": True,
        "shacl_conforms": True,
        "automatic_contract": assessment["effective_decision"],
        "target": packet["candidate"]["target"],
        "reader": packet["candidate"]["code_path"],
        "source_line": packet["candidate"]["code_line"],
        "dockerfile_insert_after_line": packet["packaging_candidate"]["insert_after_line"],
        "model_calls": 0,
        "docker_calls": 0,
        "note": "Implementation example only; no LLM decision or execution-recovery score.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", choices=PROGRAMS, default=PROGRAMS[0])
    args = parser.parse_args()
    print(json.dumps(replay(args.program), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
