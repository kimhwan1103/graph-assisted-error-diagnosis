"""Research graph: observed error -> reported location -> supplied source line.

This rule connects evidence; it does not infer the defect or a repair from a stack.
Production compact remediation continues to use its stricter ENOENT rules.
"""

import hashlib
import re
import time
from functools import lru_cache
from importlib.resources import files

from ..error_locations import extract_error_locations
from .compact import validate


@lru_cache(maxsize=1)
def resources():
    from rdflib import Graph, Namespace
    from rdflib.plugins.sparql import prepareQuery

    root = files("ai_error_check_agent.knowledge")
    shapes = root.joinpath("source_link_shapes.ttl").read_text("utf-8")
    rule = root.joinpath("rules/source_link.rq").read_text("utf-8")
    return (
        Namespace("urn:iris:source-link:v1:"),
        Graph().parse(data=shapes, format="turtle"),
        prepareQuery(rule),
        hashlib.sha256((shapes + rule).encode()).hexdigest(),
    )


def operation(text):
    if re.search(r"\bthrow\b", text):
        return "explicit_throw"
    if re.search(r"\.[A-Za-z_$][\w$]*\s*\(", text):
        return "member_call"
    if re.search(r"\b[A-Za-z_$][\w$]*\s*\(", text):
        return "call_or_declaration"
    return "reported_source_line"


def build(bundle, source_lines):
    from rdflib import RDF, Graph, Literal, URIRef

    started = time.perf_counter()
    by_position = {(line["path"], line["line"]): line for line in source_lines}
    locations = [item for item in extract_error_locations(bundle) if item.prefetch]
    if not locations or not by_position:
        return None
    ns, shapes, query, resource_hash = resources()
    graph = Graph()
    selected = []
    for item in locations:
        source = by_position.get((item.path, item.line))
        if not source or source.get("masked") or not source["text"].strip():
            continue
        if source["text"].lstrip().startswith(("//", "/*", "*")):
            continue
        # Scope binds the error anchor and frame to their original producer.
        scope = hashlib.sha256(
            repr((item.scope, item.evidence_ids, item.path, item.line)).encode()
        ).hexdigest()
        frame, code = (
            URIRef(f"urn:iris:source-link:{scope}:{kind}") for kind in ("frame", "source")
        )
        for node, kind in ((frame, ns.Frame), (code, ns.Source)):
            graph.add((node, RDF.type, kind))
            graph.add((node, ns.scope, Literal(scope)))
            graph.add((node, ns.path, Literal(item.path)))
            graph.add((node, ns.line, Literal(item.line)))
        for ref in item.evidence_ids:
            graph.add((frame, ns.evidence, Literal(ref)))
        graph.add((code, ns.evidence, Literal(source["id"])))
        graph.add((code, ns.operation, Literal(operation(source["text"]))))
        selected.append(frame)
        if len(selected) == 3:
            break
    if not selected:
        return None
    validate(graph, shapes)
    for triple in graph.query(query).graph:
        graph.add(triple)
    validate(graph, shapes)
    candidates = []
    for frame in selected:
        if (frame, RDF.type, ns.Candidate) not in graph:
            continue
        code = graph.value(frame, ns.source)
        candidates.append(
            {
                "id": f"SL{len(candidates) + 1}",
                "kind": "observed_error_source_link",
                "rule": "error-source-link.v1",
                "code_path": str(graph.value(frame, ns.path)),
                "code_line": int(graph.value(frame, ns.line)),
                "observed_operation": str(graph.value(code, ns.operation)),
                "evidence_ids": sorted(str(ref) for ref in graph.objects(frame, ns.evidence)),
                "source_evidence_ids": [str(graph.value(code, ns.evidence))],
                "claim": "같은 오류 블록의 보고 위치가 제공된 소스 줄과 연결된다. 호출·예외 발생 위치이며 결함 원인이나 수정 성공의 증명은 아니다.",
            }
        )
    if not candidates:
        return None
    return {
        "hints": {"source_candidates": candidates},
        "report": {
            "variant": "research_source_links",
            "rule": "error-source-link.v1",
            "resources_sha256": resource_hash,
            "graph_ttl": graph.serialize(format="turtle"),
            "metrics": {
                "conforms": True,
                "triples": len(graph),
                "candidate_count": len(candidates),
                "total_ms": round((time.perf_counter() - started) * 1000, 3),
            },
            "semantic_limit": "observed evidence links only; no causal or repair proof",
        },
    }
