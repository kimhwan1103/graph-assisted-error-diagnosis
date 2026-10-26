import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automatic_study_v4 import (
    anchor,
    anchor_hit,
    citation_integrity,
    digest,
    final_checks,
    micro,
    run,
    stats,
    summarize_final,
    write,
)


def reference(statuses=None):
    return {
        "acceptable_statuses": statuses or ["diagnosed"],
        "graph": {"facts": [["SourceRead", "path", "src/tasks.js"], ["SourceRead", "line", "8"]]},
    }


def test_graph_micro_uses_counts_not_mean_of_case_precision():
    result = micro([{"tp": 1, "fp": 0, "fn": 0}, {"tp": 0, "fp": 9, "fn": 1}])
    assert result["precision"] == 0.1
    assert result["recall"] == 0.5
    assert result["f1"] == 2 / 12


def test_missing_graph_and_empty_denominators_are_not_perfect_scores():
    assert micro([None]) is None
    result = micro([{"tp": 0, "fp": 0, "fn": 0}])
    assert result["precision"] is result["recall"] is result["f1"] is None
    result = micro([{"tp": 0, "fp": 0, "fn": 3}])
    assert result["precision"] is None
    assert result["recall"] == result["f1"] == 0


def test_normal_or_ambiguous_status_reference_has_no_required_read_anchor():
    assert anchor(reference(["no_failure_evidence"])) is None
    assert anchor(reference(["diagnosed", "insufficient_evidence"])) is None
    assert anchor(reference()) == {"path": "src/tasks.js", "line": 8}


def test_anchor_requires_exact_cited_line_not_just_broad_span():
    expected = anchor(reference())
    finding = {
        "path": "src/tasks.js",
        "start_line": 1,
        "end_line": 100,
        "source_evidence_ids": ["SC1"],
    }
    evidence = [{"id": "SC1", "path": "src/tasks.js", "line": 7}]
    assert anchor_hit(expected, [finding], evidence) is False
    evidence[0]["line"] = 8
    assert anchor_hit(expected, [finding], evidence) is True
    evidence[0]["path"] = "src/other.js"
    assert anchor_hit(expected, [finding], evidence) is False


def test_citation_checks_count_unknown_ids_and_incorrect_source_text():
    analysis = {"observations": [{"evidence_ids": ["EV1", "EV404", "EV1"]}]}
    findings = [
        {
            "path": "a.js",
            "start_line": 1,
            "end_line": 1,
            "source_evidence_ids": ["SC1", "SC404"],
            "evidence_ids": ["EV1"],
        }
    ]
    evidence = [{"id": "EV1"}]
    source = [{"id": "SC1", "path": "a.js", "line": 1, "text": "changed"}]
    files = [{"path": "a.js", "content": "original\n"}]
    result = citation_integrity(analysis, findings, evidence, source, files)
    assert result["log_ids"]["rate"] == 0.5
    assert result["source_bindings"] == {"numerator": 0, "denominator": 2, "rate": 0}
    source[0]["text"] = "original"
    assert (
        citation_integrity(analysis, findings, evidence, source, files)["source_bindings"]["rate"]
        == 0.5
    )


def test_no_citations_is_unmeasured_not_one_hundred_percent():
    result = citation_integrity({}, [], [], [], [])
    assert result["log_ids"]["rate"] is None
    assert result["source_bindings"]["rate"] is None


def test_contract_failure_remains_zero_even_if_raw_analysis_has_right_label():
    result = final_checks(
        {"job_status": "failed", "analysis": {"analysis_status": "diagnosed"}},
        [],
        [],
        {"source_files": []},
        reference(),
    )
    assert result["valid_contract"] is False
    assert result["status_match"] is False
    assert result["anchor_hit"] is False
    assert result["observed_status"] is None
    assert result["citations"] is None


def test_summary_preserves_failure_denominators_and_conditional_results():
    valid = final_checks(
        {"job_status": "succeeded", "analysis": {"analysis_status": "diagnosed"}, "evidence": []},
        [],
        [],
        {"source_files": []},
        reference(),
    )
    failed = final_checks({"job_status": "failed"}, [], [], {"source_files": []}, reference())
    result = summarize_final([{"original": valid}, {"original": failed}], "original")
    assert result["status_match_all"]["rate"] == 0.5
    assert result["status_match_given_valid"]["rate"] == 1
    assert result["anchor_hit_all_eligible"]["denominator"] == 2
    assert result["anchor_hit_given_valid"]["denominator"] == 1


def test_partial_token_measurements_do_not_turn_into_complete_total():
    result = stats([10, None, 20])
    assert result["observed"] == 2 and result["total"] == 3
    assert result["sum"] is None
    assert result["mean"] == result["median"] == 15
    assert result["p95_nearest_rank"] == 20


def test_saved_graph_missing_atoms_roundtrip_and_report_rerun(tmp_path):
    from automatic_study_v4 import graph_score

    source, output = tmp_path / "source", tmp_path / "report"
    slot = {
        "cell_id": "01234567890123456789",
        "case_id": "case",
        "arm": "graph_compact",
        "repeat": 0,
    }
    ref = reference()
    ref["graph"].update(relations=[], candidates=[])
    actual = {"facts": [], "relations": [], "candidates": []}
    cell = {
        **slot,
        "result": {"job_status": "failed", "evidence": []},
        "graph_scores_scope": "pipeline_extraction",
        "graph_trace": {"atoms": actual},
        "graph_scores": graph_score(actual, ref["graph"]),
        "graph_report": None,
        "wall_ms": 1,
    }
    frozen = {
        "live": False,
        "source_manifest": {},
        "schedule": [slot],
        "config": {"arms": ["graph_compact"]},
        "cases": [{"id": "case", "cluster_id": "one", "source_files": []}],
        "references": {"cases": {"case": ref}},
    }
    write(source / "frozen.json", frozen)
    folder = source / "cells" / slot["cell_id"]
    write(folder / "result.json", cell)
    write(folder / "state.json", {"status": "completed", "result_sha256": digest(cell)})
    first = run(source, output)
    before = (output / "report.json").read_bytes()
    second = run(source, output)
    assert digest(first) == digest(second)
    assert (output / "report.json").read_bytes() == before
    assert first["overall"]["graph"]["all_cells"]["facts"]["fn"] == 2
