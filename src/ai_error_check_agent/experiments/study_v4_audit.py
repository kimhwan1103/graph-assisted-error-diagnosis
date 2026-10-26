"""Offline SHACL/rule interventions through the real API plus procedural equivalence."""

import threading

from rdflib import Graph

from ..graph_compact import CompactService
from ..knowledge.compact import packaging_query, resources
from .study_v4 import diagnose_cell, load, read, save
from .study_v4_graph import procedural, score
from .study_v4_runtime import Session


async def audit(config_path, output):
    from .study import exclusive

    config, backend, cases, references = load(config_path)
    with exclusive(output):
        session = Session(output, None, backend, save, read, live=False)
        resources()
        records = []
        for case in cases:
            for arm in (
                "graph_compact",
                "no_shacl",
                "no_rules",
                "no_shacl_no_rules",
                "procedural_RC",
            ):
                folder = output / "audit-calls" / case["id"] / arm
                result = await diagnose_cell(
                    case, arm, session.factory(folder, backend.seed), graph_timeout=5
                )
                graph = Graph().parse(data=result["graph_trace"]["graph_ttl"], format="turtle")
                equivalence = {
                    kind: set(procedural(graph, kind)) == set(graph.query(query).graph)
                    for kind, query in (("read", resources()[2]), ("packaging", packaging_query()))
                }
                records.append(
                    {
                        "case_id": case["id"],
                        "arm": arm,
                        "report": result["graph_report"],
                        "trace": result["graph_trace"],
                        "http_status": result["http_status"],
                        "scores": None
                        if arm == "procedural_RC"
                        else score(
                            result["graph_trace"]["atoms"],
                            references["cases"][case["id"]].get("graph"),
                        ),
                        "procedural_equivalence": equivalence,
                    }
                )
        supported = next(
            c
            for c in cases
            if any(
                r["case_id"] == c["id"] and (r["report"] or {}).get("status") == "ok"
                for r in records
            )
        )
        faults = []
        for fault in ("dangling_evidence", "invalid_line", "cross_scope", "target_mismatch"):
            for arm in ("graph_compact", "no_shacl", "no_rules", "no_shacl_no_rules"):
                result = await diagnose_cell(
                    supported,
                    arm,
                    session.factory(output / "fault-calls" / fault / arm, backend.seed),
                    graph_timeout=5,
                    fault=fault,
                )
                faults.append(
                    {
                        "fault": fault,
                        "arm": arm,
                        "report": result["graph_report"],
                        "trace": result["graph_trace"],
                        "api_valid": result["http_status"] == 200,
                        "candidate_generated": bool(result["graph_trace"]["atoms"]["candidates"]),
                    }
                )
        # Real service timeout/busy lifecycle with a deliberately held worker. No fake timing scores.
        release = threading.Event()
        started = threading.Event()

        def slow(*_):
            started.set()
            release.wait(5)

        service = CompactService(timeout_seconds=0.01, builder=slow)
        try:
            first = await service.prepare(None, None, [])
            second = await service.prepare(None, None, [])
        finally:
            release.set()
            if service._task:
                await service._task
        service.timeout_seconds = 2  # Released worker may need Windows thread scheduling time.
        third = await service.prepare(None, None, [])
        lifecycle = [first[1]["status"], second[1]["status"], third[1]["status"]]
        direct_equivalence = []
        for case in cases:
            matching = {r["arm"]: r for r in records if r["case_id"] == case["id"]}

            def hints(record):
                report = record["report"] or {}
                return (
                    {k: report.get(k) for k in ("candidate", "packaging_candidate")}
                    if report.get("status") == "ok"
                    else None
                )

            direct_equivalence.append(
                {
                    "case_id": case["id"],
                    "equal": hints(matching["graph_compact"]) == hints(matching["procedural_RC"]),
                }
            )
        document = {
            "version": "study-v4-graph-audit.v1",
            "real_model": False,
            "reference_status": "provisional authored fixtures; graph atoms are not root-cause truth",
            "audit_graph_timeout_seconds": 5,
            "production_graph_timeout_seconds": config.get("graph_timeout_seconds", 0.5),
            "records": records,
            "faults": faults,
            "worker_lifecycle": lifecycle,
            "worker_lifecycle_pass": lifecycle == ["timed_out", "busy", "unsupported"],
            "procedural_equivalence_pass": all(
                all(r["procedural_equivalence"].values()) for r in records
            ),
            "plain_python_candidate_equivalence": direct_equivalence,
            "plain_python_candidate_equivalence_pass": all(r["equal"] for r in direct_equivalence),
            "facts_note": "Empty graph on routing rejection means no emitted facts. Do not treat this as semantic falsehood; inspect FN and not-visited coverage separately.",
        }
        save(output / "graph-audit.json", document)
    print(f"Graph audit: {output / 'graph-audit.json'}")
    return document
