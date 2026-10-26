"""Pinned JavaScript AST regions and deterministic observation-only retrieval."""

import math
import re
from functools import lru_cache

import tree_sitter_javascript
from tree_sitter import Language, Parser

from ..preprocessing import redact
from ..source_contracts import SourceSnapshot
from .design import digest
from .study_inputs import STOP, WORDS

PARSER_VERSION = "tree-sitter-0.25.2.javascript-0.25.0.v1"
RANKER_VERSION = "observed-ast-regions.v1"
FRAME = re.compile(r"(?<![\w/])(?:/app/)?([\w$.-]+(?:/[\w$.-]+)*\.(?:js|mjs|cjs)):(\d+)(?::\d+)?")
FUNCTIONS = {
    "function_declaration",
    "function_expression",
    "generator_function_declaration",
    "generator_function",
    "arrow_function",
    "method_definition",
}


@lru_cache(maxsize=512)
def parse_regions(content, parser_version=PARSER_VERSION, ranker_version=RANKER_VERSION):
    tree = Parser(Language(tree_sitter_javascript.language())).parse(content.encode("utf-8"))
    regions, stack = [], [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in FUNCTIONS:
            regions.append(
                {
                    "start_line": node.start_point.row + 1,
                    "end_line": node.end_point.row + (1 if node.end_point.column else 0),
                    "kind": node.type,
                }
            )
        stack.extend(reversed(node.named_children))
    return {
        "regions": regions,
        "parse_fallback": tree.root_node.has_error,
        "reason": "javascript_parse_error_or_unsupported_syntax"
        if tree.root_node.has_error
        else None,
        "cache_key": digest([content, parser_version, ranker_version]),
    }


def spans(numbers):
    groups = []
    for n in sorted(numbers):
        if groups and n == groups[-1][1] + 1:
            groups[-1][1] = n
        else:
            groups.append([n, n])
    return groups


def retrieve(files, logs, char_budget, policy):
    snapshot = SourceSnapshot.model_validate({"files": files})
    raw = {f.path: f.content for f in snapshot.files}
    index = {p: redact(c).splitlines() for p, c in raw.items()}
    parses = {p: parse_regions(c) for p, c in raw.items()}
    query = "\n".join(r["text"] for r in logs)
    titles = set(WORDS.findall(logs[0]["text"])) - STOP if logs else set()
    terms = sorted(titles | (set(WORDS.findall(query[:2500])) - STOP))[:120]
    frequencies = {t: sum(t in text for lines in index.values() for text in lines) for t in terms}
    frames = {
        (p, int(n)) for p, n in FRAME.findall(query) if p in index and 0 < int(n) <= len(index[p])
    }
    candidates = []
    for path, lines in sorted(index.items()):
        for n, text in enumerate(lines, 1):
            score = sum(
                (3 if t in titles else 1) / math.sqrt(max(1, frequencies[t]))
                for t in set(WORDS.findall(text)) & set(terms)
            )
            if (path, n) in frames:
                score += 20
            if score:
                candidates.append((-score, path, n))
    if not candidates:
        candidates = [(0, p, 1) for p in sorted(index) if index[p]]
    selected, used, per_file, visited, regions = {}, 0, {}, set(), []
    for neg, path, anchor in sorted(candidates):
        if (path, anchor) in selected:
            continue
        parsed, lines = parses[path], index[path]
        enclosing = [r for r in parsed["regions"] if r["start_line"] <= anchor <= r["end_line"]]
        region = (
            min(enclosing, key=lambda r: r["end_line"] - r["start_line"])
            if enclosing and not parsed["parse_fallback"]
            else None
        )
        lo, hi = (
            (region["start_line"], region["end_line"])
            if region
            else (
                max(1, anchor - policy["fallback_radius"]),
                min(len(lines), anchor + policy["fallback_radius"]),
            )
        )
        identity = path, lo, hi
        if identity in visited:
            continue
        visited.add(identity)
        capacity = min(
            char_budget - used,
            policy["per_region_chars"],
            policy["per_file_chars"] - per_file.get(path, 0),
        )
        cost = lambda n, lines=lines, path=path: len(lines[n - 1]) + len(path) + 20
        missing = [n for n in range(lo, hi + 1) if (path, n) not in selected]
        if sum(cost(n) for n in missing) <= capacity:
            chosen = missing
        else:
            # Connected anchor neighborhood, never claim a disconnected slice is a full region.
            chosen, remaining = [], capacity
            for n in sorted(range(lo, hi + 1), key=lambda n: (abs(n - anchor), n)):
                if (path, n) in selected:
                    continue
                if cost(n) > remaining:
                    break
                chosen.append(n)
                remaining -= cost(n)
        for n in chosen:
            selected[path, n] = lines[n - 1]
            used += cost(n)
            per_file[path] = per_file.get(path, 0) + cost(n)
        included = [n for n in range(lo, hi + 1) if (path, n) in selected]
        if chosen:
            regions.append(
                {
                    "path": path,
                    "anchor": anchor,
                    "score": -neg,
                    "start_line": lo,
                    "end_line": hi,
                    "kind": region["kind"] if region else "line_window",
                    "included_spans": spans(included),
                    "omitted_lines": hi - lo + 1 - len(included),
                    "partial": region is None or len(included) != hi - lo + 1,
                    "reason": parsed["reason"] or (None if region else "no_enclosing_function"),
                }
            )
    sources = [
        {"id": f"SC{i + 1:06d}", "path": p, "line": n, "text": text}
        for i, ((p, n), text) in enumerate(sorted(selected.items()))
    ]
    return sources, {
        "parser_version": PARSER_VERSION,
        "ranker_version": RANKER_VERSION,
        "source_chars_used": used,
        "available_lines": sum(map(len, index.values())),
        "included_lines": len(sources),
        "regions": regions,
        "parse_files": {
            p: {k: v for k, v in r.items() if k != "regions"} for p, r in parses.items()
        },
        "partial_region_count": sum(r["partial"] for r in regions),
        "parse_fallback_count": sum(r["parse_fallback"] for r in parses.values()),
    }


def audit_source_coverage(prepared, files, reference):
    if not reference:
        return {
            "status": "reference_missing",
            "any_line": None,
            "full_function": None,
            "essential_support": None,
            "relation_hits_root": None,
            "archive_has_root": None,
        }
    regions = reference.get("acceptable_root_regions", [])
    present = {(s["path"], s["line"]) for s in prepared["base"]["source_evidence"]}
    archive = {f["path"]: len(f["content"].splitlines()) for f in files}
    wanted = [{(r["path"], n) for n in range(r["start_line"], r["end_line"] + 1)} for r in regions]
    support = [
        {(r["path"], n) for n in range(r["start_line"], r["end_line"] + 1)}
        for c in reference.get("essential_claims", [])
        for r in c["support_regions"]
    ]
    ids = {s["id"]: (s["path"], s["line"]) for s in prepared["base"]["source_evidence"]}
    targets = {ids[e["to"]] for e in prepared["edges"]}
    return {
        "status": reference["reference_status"],
        "any_line": any(w & present for w in wanted) if wanted else None,
        "full_function": all(w <= present for w in wanted) if wanted else None,
        "essential_support": all(w <= present for w in support) if support else None,
        "relation_hits_root": any(w & targets for w in wanted) if wanted else None,
        "archive_has_root": all(
            r["path"] in archive and r["end_line"] <= archive[r["path"]] for r in regions
        )
        if regions
        else None,
    }
