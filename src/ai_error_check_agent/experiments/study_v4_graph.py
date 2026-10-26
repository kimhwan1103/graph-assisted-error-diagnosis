"""Research-only instrumentation. Default production graph behavior is unchanged."""

import itertools
import time

from rdflib import RDF, Graph, Literal, URIRef

from ..knowledge.compact import resources, validate


def procedural(graph, kind):
    """Explicit relational joins equivalent to the two frozen CONSTRUCT rules."""
    c = resources()[0]
    output = Graph()
    if kind == "read":
        types = [c.ReadFailure, c.StackFrame, c.SourceRead]
        rule, output_type, suffix = "observed-file-read.v1", c.Candidate, "/candidate"
    else:
        types = [c.Candidate, c.RepositoryEntry, c.CopyCoverage, c.IgnorePolicy]
        rule, output_type, suffix = "docker-copy-omission.v1", c.PackagingCandidate, "/packaging"
    for nodes in itertools.product(*(list(graph.subjects(RDF.type, t)) for t in types)):

        def equal(predicate, indexes, nodes=nodes):
            # Rules use a join: multi-valued properties must preserve all matching solutions.
            return set.intersection(*(set(graph.objects(nodes[i], predicate)) for i in indexes))

        scopes = equal(c.scope, range(len(nodes)))
        targets = equal(c.target, range(len(nodes)))
        if kind == "read":
            valid = (nodes[0], c.code, Literal("ENOENT")) in graph and bool(
                equal(c.path, [1, 2]) and equal(c.line, [1, 2])
            )
            paths = [None]
        else:
            paths = equal(c.repoPath, [1, 2, 3])
            valid = bool(equal(c.archiveHash, [1, 2, 3])) and all(
                (nodes[i], p, Literal(True)) in graph
                for i, p in ((1, c.regular), (2, c.omitted), (3, c.allows))
            )
        if not valid:
            continue
        for scope, target, path in itertools.product(scopes, targets, paths):
            candidate = URIRef(str(nodes[0]) + suffix)
            for predicate, value in (
                (RDF.type, output_type),
                (c.rule, Literal(rule)),
                (c.scope, scope),
                (c.target, target),
            ):
                output.add((candidate, predicate, value))
            for node in nodes:
                output.add((candidate, c.input, node))
            if path is not None:
                output.add((candidate, c.repoPath, path))
    return output


class GraphTrace:
    def __init__(self, *, shacl=True, rules=True, engine="sparql", fault=None):
        self.shacl, self.rules, self.engine, self.fault = shacl, rules, engine, fault
        self.events = []
        self.graph = Graph()
        self.injected = False

    def snapshot(self, graph):
        self.graph = Graph()
        for triple in graph:
            self.graph.add(triple)

    def check(self, graph, shapes):
        c = resources()[0]
        if self.fault and not self.injected:
            self.injected = True
            read = next(graph.subjects(RDF.type, c.SourceRead))
            if self.fault == "dangling_evidence":
                ref = next(graph.objects(read, c.evidence))
                graph.remove((ref, RDF.type, c.Evidence))
            elif self.fault == "invalid_line":
                graph.set((read, c.line, Literal(0)))
            elif self.fault == "cross_scope":
                graph.set((read, c.scope, Literal("different-scope")))
            elif self.fault == "target_mismatch":
                graph.set((read, c.target, Literal("/different.json")))
            else:
                raise ValueError("unknown graph fault")
        self.snapshot(graph)
        started = time.perf_counter()
        event = {"stage": "shacl", "enabled": self.shacl, "conforms": None}
        try:
            if self.shacl:
                validate(graph, shapes)
                event["conforms"] = True
        except ValueError:
            event["conforms"] = False
            raise
        finally:
            event["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.events.append(event)

    def construct(self, graph, query, kind):
        started = time.perf_counter()
        output = Graph()
        if self.rules:
            output = (
                procedural(graph, kind) if self.engine == "procedural" else graph.query(query).graph
            )
        self.events.append(
            {
                "stage": "rules",
                "kind": kind,
                "enabled": self.rules,
                "engine": self.engine,
                "derived_triples": len(output),
                "elapsed_ms": (time.perf_counter() - started) * 1000,
            }
        )
        return output

    def export(self):
        return {
            "events": self.events,
            "graph_ttl": self.graph.serialize(format="turtle"),
            "atoms": atoms(self.graph),
            "fault": self.fault,
        }


def atoms(graph):
    """Canonical semantic facts and reference/reasoning edges, independent of scope URI names."""
    c = resources()[0]
    kinds = (
        "ReadFailure",
        "StackFrame",
        "SourceRead",
        "RepositoryEntry",
        "CopyCoverage",
        "IgnorePolicy",
    )
    facts, relations, candidates = set(), set(), set()
    labels = {}
    for kind in (*kinds, "Candidate", "PackagingCandidate"):
        for node in graph.subjects(RDF.type, c[kind]):
            labels[node] = kind
    for node, kind in labels.items():
        if kind in kinds:
            facts.add((kind, "type", kind))
            for p in ("target", "path", "line", "code", "repoPath", "regular", "omitted", "allows"):
                for value in graph.objects(node, c[p]):
                    facts.add((kind, p, str(value)))
            for ref in graph.objects(node, c.evidence):
                for value in graph.objects(ref, c.id):
                    relations.add((kind, "evidence", str(value)))
        else:
            for p in ("rule", "target", "repoPath"):
                for value in graph.objects(node, c[p]):
                    candidates.add((kind, p, str(value)))
            for source in graph.objects(node, c.input):
                relations.add((kind, "input", labels.get(source, "UNKNOWN")))
    return {
        k: sorted(v)
        for k, v in (("facts", facts), ("relations", relations), ("candidates", candidates))
    }


def score(actual, reference):
    result = {}
    for dimension in ("facts", "relations", "candidates"):
        if reference is None or reference.get(dimension) is None:
            result[dimension] = None
            continue
        observed = {tuple(row) for row in actual[dimension]}
        expected = {tuple(row) for row in reference[dimension]}
        tp = len(observed & expected)
        result[dimension] = {
            "tp": tp,
            "fp": len(observed - expected),
            "fn": len(expected - observed),
            "precision": tp / len(observed) if observed else None,
            "recall": tp / len(expected) if expected else None,
            "false_atoms": sorted(observed - expected),
            "missing_atoms": sorted(expected - observed),
        }
    return result
