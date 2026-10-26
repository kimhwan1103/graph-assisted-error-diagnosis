"""Offline, versioned analyses of immutable runs and independent human ratings."""

import copy
import random
import statistics
from collections import Counter

from .design import ROOT, digest
from .reproduce import sha
from .run import save
from .study import read
from .study_analysis import bootstrap_clusters, mean
from .study_statistics import paired_binary

ANALYSIS_VERSION = "iris-study-analysis.v3.1"
RUBRIC = {
    "observation_correct": "0/.5/1: selected test observation incorrect/partial/correct; separate process errors",
    "mechanism_correct": "0/1 failure only; complete, evidence-supported code condition/data flow; null for pass",
    "grounded": "0/.5/1: unsupported/mixed/all material claims supported by supplied evidence",
    "unsupported_causal_claim": "0/1: absent/present",
    "essential_claim_coverage": "Map each frozen claim ID to 0/1/not_assessable; null means unreviewed",
    "uncertainty_appropriate": "0/1: confidence and limitations match evidence",
    "next_check_valid": "0/1: technically valid and relevant discriminating check",
    "material_contradiction": "0/1: absent/present across fields or evidence",
}


def immutable(item):
    return {k: v for k, v in item.items() if k not in {"ratings", "review_notes"}}


def review_packets(output, protocol, rows, inputs):
    document = read(output / "reviewer-references.json")
    refs = {r["bug_id"]: r for r in document["items"]}
    mapping, template = {}, []
    for row in rows:
        if row["is_mock"] or row["state"] != "returned":
            continue
        slot = row["slot"]
        identifier = digest([sha(output / "protocol.json"), slot["id"], row["response"]["text"]])[
            :24
        ]
        ref = refs.get(slot["bug_id"])
        claim_ids = (
            [c["id"] for c in ref["essential_claims"]]
            if ref and slot["label"] == "test_failure"
            else []
        )
        item = {
            "review_id": identifier,
            "response": row["response"]["text"],
            "contract_valid": row["contract_valid"],
            "evidence": inputs[slot["case"]]["prepared"]["base"],
            "reference": {
                "selected_test_label": slot["label"],
                "root_reference": ref,
                "published_patch": document["patches"].get(slot["bug_id"]),
            },
            "ratings": {
                **{k: None for k in RUBRIC},
                "essential_claim_coverage": {k: None for k in claim_ids},
            },
            "review_notes": "",
        }
        mapping[identifier] = {
            "slot_id": slot["id"],
            "arm": slot["arm"],
            "immutable_sha256": digest(immutable(item)),
        }
        template.append(item)
    for letter, seed in (("A", 4201), ("B", 4202)):
        path = output / f"review-{letter}.json"
        old = (
            read(path)
            if path.exists()
            else {"reviewer_id": "", "reviewer_type": "unassigned", "items": []}
        )
        previous = {i["review_id"]: i for i in old["items"]}
        if len(previous) != len(old["items"]) or not previous.keys() <= mapping.keys():
            raise ValueError("Unknown or duplicate review item")
        items = []
        for item in template:
            item = copy.deepcopy(item)
            if item["review_id"] in previous:
                prior = previous[item["review_id"]]
                if digest(immutable(prior)) != digest(immutable(item)):
                    raise ValueError("Immutable review response/evidence changed")
                item = prior
            items.append(item)
        random.Random(seed).shuffle(items)
        result = {
            "reviewer_id": old["reviewer_id"],
            "reviewer_type": old["reviewer_type"],
            "instructions": "Independent A/B humans; edit only identity, ratings, review_notes. Do not share review-key.json. Length is visible; incomplete blinding. References are reviewer-only and approval is distinct from response scoring.",
            "rubric_version": "whole-diagnosis.v3",
            "rubric": RUBRIC,
            "items": items,
        }
        if result != old:
            save(path, result)
    key = {
        "protocol_sha256": sha(output / "protocol.json"),
        "private_mapping_do_not_share": mapping,
    }
    path = output / "review-key.json"
    if not path.exists() or read(path) != key:
        save(path, key)
    return mapping


def usable(item):
    r = item["ratings"]
    claims = r["essential_claim_coverage"]
    return int(
        item["contract_valid"]
        and all(
            r[k] == 1
            for k in (
                "observation_correct",
                "mechanism_correct",
                "grounded",
                "uncertainty_appropriate",
                "next_check_valid",
            )
        )
        and r["unsupported_causal_claim"] == 0
        and r["material_contradiction"] == 0
        and bool(claims)
        and all(v == 1 for v in claims.values())
    )


def agreement(pairs):
    if not pairs:
        return {"n": 0, "agreement": None, "kappa": None, "reason": "no_complete_human_pairs"}
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    a, b = Counter(p[0] for p in pairs), Counter(p[1] for p in pairs)
    expected = sum(a[k] * b[k] for k in a.keys() | b.keys()) / n**2
    return {
        "n": n,
        "agreement": observed,
        "disagreements": n - sum(a == b for a, b in pairs),
        "kappa": (observed - expected) / (1 - expected) if expected < 1 else None,
        "reason": "constant_marginals" if expected == 1 else None,
    }


def collect_reviews(output, mapping):
    documents, raters, scored = [], [], []
    for letter in ("A", "B"):
        doc = read(output / f"review-{letter}.json")
        documents.append(doc)
        name, kind = doc["reviewer_id"].strip(), doc["reviewer_type"]
        if kind not in {"unassigned", "human", "assistant"} or kind == "human" and not name:
            raise ValueError("Invalid reviewer identity/type")
        if kind == "human" and any(
            word in name.casefold() for word in ("assistant", "chatgpt", "codex", "llm")
        ):
            raise ValueError("Assistant cannot be declared a human reviewer")
        raters.append({"id": name, "type": kind})
        ratings, seen = {}, set()
        for item in doc["items"]:
            identifier = item["review_id"]
            if identifier in seen or identifier not in mapping:
                raise ValueError("Unknown or duplicate review item")
            seen.add(identifier)
            if digest(immutable(item)) != mapping[identifier]["immutable_sha256"]:
                raise ValueError("Immutable review item modified")
            r = item["ratings"]
            if set(r) != set(RUBRIC):
                raise ValueError("Unexpected rubric fields")
            failure = item["reference"]["selected_test_label"] == "test_failure"
            ref = item["reference"]["root_reference"]
            expected_claims = (
                {c["id"] for c in ref["essential_claims"]} if failure and ref else set()
            )
            claims = r["essential_claim_coverage"]
            if (
                not isinstance(claims, dict)
                or set(claims) != expected_claims
                or any(
                    v is not None and not (type(v) is int and v in (0, 1)) and v != "not_assessable"
                    for v in claims.values()
                )
            ):
                raise ValueError("Invalid essential claim coverage")
            for key, value in r.items():
                if key == "essential_claim_coverage":
                    continue
                allowed = (0, 0.5, 1) if key in {"observation_correct", "grounded"} else (0, 1)
                if value is not None and (type(value) not in (int, float) or value not in allowed):
                    raise ValueError("Invalid rating")
            if not failure and r["mechanism_correct"] is not None:
                raise ValueError("Passing observations require null mechanism")
            touched = any(
                v is not None for k, v in r.items() if k != "essential_claim_coverage"
            ) or any(v is not None for v in claims.values())
            if touched and not item["review_notes"].strip():
                raise ValueError("Nonempty review_notes with evidence/claim rationale required")
            required = (
                set(RUBRIC)
                - {"essential_claim_coverage"}
                - ({"mechanism_correct"} if not failure else set())
            )
            complete = all(r[k] is not None for k in required) and all(
                v is not None for v in claims.values()
            )
            if complete and kind == "human":
                ratings[mapping[identifier]["slot_id"]] = item
        if seen != set(mapping):
            raise ValueError("Review packet items missing")
        scored.append(ratings)
    if (
        raters[0]["type"] == raters[1]["type"] == "human"
        and raters[0]["id"].casefold() == raters[1]["id"].casefold()
    ):
        raise ValueError("Two different reviewers required")
    humans = all(r["type"] == "human" for r in raters)
    scores, pairs = {}, {k: [] for k in RUBRIC}
    if humans:
        for slot in scored[0].keys() & scored[1].keys():
            a, b = scored[0][slot], scored[1][slot]
            failure = a["reference"]["selected_test_label"] == "test_failure"
            scores[slot] = {
                "usable_diagnosis": min(usable(a), usable(b)) if failure else None,
                "mechanism_joint": int(
                    a["ratings"]["mechanism_correct"] == b["ratings"]["mechanism_correct"] == 1
                )
                if failure
                else None,
                "mechanism_mean": mean(
                    [a["ratings"]["mechanism_correct"], b["ratings"]["mechanism_correct"]]
                )
                if failure
                else None,
                "observation_mean": mean(
                    [a["ratings"]["observation_correct"], b["ratings"]["observation_correct"]]
                ),
                "appropriate_uncertainty_joint": min(
                    a["ratings"]["uncertainty_appropriate"], b["ratings"]["uncertainty_appropriate"]
                ),
            }
            for key in RUBRIC:
                av, bv = a["ratings"][key], b["ratings"][key]
                if key == "essential_claim_coverage":
                    pairs[key].extend((av[c], bv[c]) for c in av)
                elif av is not None and bv is not None:
                    pairs[key].append((av, bv))
    return scores, {
        "raters": raters,
        "two_independent_humans_declared": humans,
        "complete_per_rater": [len(s) for s in scored],
        "jointly_reviewed": len(scores),
        "agreement": {k: agreement(v) for k, v in pairs.items()},
        "identity_note": "human identity/independence is declared, not externally authenticated",
    }


def distribution(values):
    values = sorted(v for v in values if v is not None)
    n = len(values)
    return {
        "n": n,
        "mean": mean(values),
        "median": statistics.median(values) if n else None,
        "p95": values[max(0, __import__("math").ceil(0.95 * n) - 1)] if n else None,
    }


def costs(rows, mock=False):
    result = {"attempts": len(rows)}
    for metric in (
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "total_tokens",
        "request_seconds",
    ):
        values = [
            r.get("request_seconds")
            if metric == "request_seconds"
            else r.get("response", {}).get(metric)
            for r in rows
        ]
        if mock:
            values = [None] * len(rows)
        result[metric] = {
            **distribution(values),
            "known_subtotal": sum(v for v in values if v is not None)
            if any(v is not None for v in values)
            else None,
            "unknown_attempts": sum(v is None for v in values),
        }
    result["usage_note"] = (
        "provider total and thought counts retained separately; no assumption that prompt+output equals total"
    )
    return result


def cost_per_success(cost, successes, complete, mock=False):
    reason = (
        "mock"
        if mock
        else "unreviewed"
        if not complete
        else "zero_success"
        if successes == 0
        else "unknown_usage"
        if cost["total_tokens"]["unknown_attempts"]
        else None
    )
    return {
        "ratio": cost["total_tokens"]["known_subtotal"] / successes if reason is None else None,
        "reason": reason,
        "successes": successes if not mock else None,
        "known_usage_subtotal": cost["total_tokens"]["known_subtotal"],
        "unknown_usage_attempts": cost["total_tokens"]["unknown_attempts"],
    }


def summarize(output):
    from .study_v3 import dependency_manifest, load_protocol, read_input, records_for

    protocol = load_protocol(output)
    protocol_hash = sha(output / "protocol.json")
    config = protocol["fingerprint"]["config"]
    inputs = {}
    for path in sorted((output / "inputs").glob("*.json")):
        item = read_input(path, protocol_hash)
        if item["case"] in inputs:
            raise ValueError("Duplicate input case")
        inputs[item["case"]] = item
    rows = records_for(output, protocol, inputs)
    manifest = (
        read(output / "manifest.json")
        if (output / "manifest.json").exists()
        else {"mode": "unstarted"}
    )
    if manifest.get("protocol_sha256", protocol_hash) != protocol_hash:
        raise ValueError("Manifest protocol mismatch")
    mock = manifest["mode"] != "live"
    if any(r["is_mock"] != mock for r in rows):
        raise ValueError("Attempt mode mismatch")
    if (output / "server.json").exists() and (output / "server.sha256").read_text(
        "ascii"
    ).strip() != sha(output / "server.json"):
        raise ValueError("Provider metadata checkpoint changed")
    mapping = review_packets(output, protocol, rows, inputs)
    scores, validation = collect_reviews(output, mapping)
    returned = {}
    for row in rows:
        if row["state"] == "returned":
            if row["slot"]["id"] in returned:
                raise ValueError("More than one returned response for a slot")
            returned[row["slot"]["id"]] = row
    refs = {r["bug_id"]: r for r in read(output / "reviewer-references.json")["items"]}

    def metric(slot, key):
        row = returned.get(slot["id"])
        if mock or row is None or not row.get("validation_complete"):
            return None
        if key in {"usable_diagnosis", "mechanism_joint", "mechanism_mean"}:
            if not row["contract_valid"]:
                return 0
            return scores.get(slot["id"], {}).get(key)
        return row.get(key)

    def group(slots):
        ids = {s["id"] for s in slots}
        selected = [r for r in rows if r["slot"]["id"] in ids]
        received = [r for r in selected if r["state"] == "returned"]
        valid = [r for r in received if r["contract_valid"]]
        failures = [s for s in slots if s["label"] == "test_failure"]
        usable_values = [metric(s, "usable_diagnosis") for s in failures]
        mechanism_values = [metric(s, "mechanism_joint") for s in failures]
        passed = [
            returned[s["id"]].get("decision", {})
            for s in slots
            if s["label"] == "test_pass" and s["id"] in returned
        ]
        failure_cost = costs([r for r in selected if r["slot"]["label"] == "test_failure"], mock)
        location = []
        for s in failures:
            d = returned.get(s["id"], {}).get("decision")
            reference = refs.get(s["bug_id"])
            if not mock and d and reference and reference["acceptable_root_regions"]:
                location.append(
                    int(
                        any(
                            d["predicted_root_path"] == r["path"]
                            and d["predicted_root_line"] is not None
                            and r["start_line"] <= d["predicted_root_line"] <= r["end_line"]
                            for r in reference["acceptable_root_regions"]
                        )
                    )
                )
        return {
            "planned": len(slots),
            "attempted_slots": len({r["slot"]["id"] for r in selected}),
            "attempts": len(selected),
            "received": len(received),
            "contract_valid": len(valid),
            "missing": len(slots) - len(received),
            "invalid": len(received) - len(valid),
            "jointly_reviewed": sum(s["id"] in scores for s in slots),
            "unreviewed_received": sum(
                s["id"] in returned and s["id"] not in scores for s in slots
            ),
            "length_target": {
                "eligible_received": sum(r["length_target_met"] is not None for r in received),
                "met": sum(r["length_target_met"] is True for r in received) if not mock else None,
            },
            "usable_diagnosis": distribution(usable_values),
            "mechanism_joint": distribution(mechanism_values),
            "mechanism_mean_auxiliary": distribution(
                [metric(s, "mechanism_mean") for s in failures]
            ),
            "function_region_location_match": distribution(location),
            "pass_false_positive": None
            if mock
            else sum(d.get("analysis_status") == "diagnosed" for d in passed),
            "pass_abstention": None
            if mock
            else sum(d.get("analysis_status") == "insufficient_evidence" for d in passed),
            "pass_received": len(passed),
            "operational_usable_failures_including_missing": None
            if mock
            else sum(
                s["id"] not in returned or metric(s, "usable_diagnosis") == 0 for s in failures
            ),
            "cost_all_attempts": costs(selected, mock),
            "cost_all_received": costs(received, mock),
            "cost_valid_received": costs(valid, mock),
            "tokens_per_usable_diagnosis": cost_per_success(
                failure_cost,
                sum(v == 1 for v in usable_values),
                all(v is not None for v in usable_values),
                mock,
            ),
            "tokens_per_correct_mechanism": cost_per_success(
                failure_cost,
                sum(v == 1 for v in mechanism_values),
                all(v is not None for v in mechanism_values),
                mock,
            ),
        }

    schedule = protocol["schedule"]
    groups = {
        a["arm"]: group([s for s in schedule if s["arm"] == a["arm"]]) for a in protocol["arms"]
    }
    project_results = {
        p: {
            a["arm"]: group([s for s in schedule if s["project"] == p and s["arm"] == a["arm"]])
            for a in protocol["arms"]
        }
        for p in protocol["projects"]
    }
    global_blocks = []
    versions = {
        r.get("response", {}).get("model_version")
        for r in rows
        if r.get("response", {}).get("model_version")
    }
    if len(versions) > 1:
        global_blocks.append("response_model_version_changed")
    if mock:
        global_blocks.append("live_run_required")
    if config["role"] != "evaluation":
        global_blocks.append("development_data")
    if not validation["two_independent_humans_declared"]:
        global_blocks.append("two_independent_human_reviews_required")
    if (
        any(r["reference_status"] != "approved" for r in refs.values())
        or len(refs) != protocol["independent_bugs"]
    ):
        global_blocks.append("reference_approval_pending")
    if (
        config["sampling"]["design_status"] != "fixed"
        or not config["statistics"]["margin_justification"]
        or not config["sampling"]["power_assumptions"]
    ):
        global_blocks.append("sample_plan_or_margin_not_justified")
    if protocol["independent_bugs"] != config["sampling"]["target_bugs"]:
        global_blocks.append("independent_sample_target_mismatch")
    primary = {}
    for comparison in protocol["primary_comparisons"]:
        before = {
            s["independent_key"]: s
            for s in schedule
            if s["arm"] == comparison["before"]
            and s["repeat"] == comparison["primary_repeat"]
            and s["label"] == comparison["eligible_label"]
        }
        after = {
            s["independent_key"]: s
            for s in schedule
            if s["arm"] == comparison["after"]
            and s["repeat"] == comparison["primary_repeat"]
            and s["label"] == comparison["eligible_label"]
        }
        pairs, blocks, reference_values = {}, list(global_blocks), []
        for bug, first in before.items():
            second = after[bug]
            a, b = metric(first, "usable_diagnosis"), metric(second, "usable_diagnosis")
            if a is not None and b is not None:
                pairs[bug] = (a, b)
                reference_values.append(a)
            if any(s["id"] not in returned for s in (first, second)):
                blocks.append("primary_response_missing")
            if any(
                not returned.get(s["id"], {}).get("validation_complete") for s in (first, second)
            ):
                blocks.append("primary_validation_incomplete")
            if any(s["id"] not in scores for s in (first, second)):
                blocks.append("primary_human_review_incomplete")
        if len(pairs) != protocol["independent_bugs"]:
            blocks.append("primary_pairs_incomplete")
        stat = config["statistics"]
        result = paired_binary(
            pairs, alpha=stat["comparison_alpha"], family_alpha=stat["family_alpha"]
        )
        threshold = -stat["margin"] if comparison["id"] == "H_compact" else 0
        quality = mean(reference_values)
        quality_gate = (
            comparison["id"] != "H_compact"
            or quality is not None
            and quality >= stat["minimum_reference_quality"]
        )
        if not quality_gate:
            blocks.append("reference_quality_gate_not_met")
        passed = result["one_sided_lower"] is not None and result["one_sided_lower"] > threshold
        primary[comparison["id"]] = {
            **comparison,
            **result,
            "threshold": threshold,
            "reference_quality": quality,
            "reference_quality_gate_met": quality_gate,
            "blocking_reasons": sorted(set(blocks)),
            "judgment": "not_established"
            if blocks
            else "criterion_met"
            if passed
            else "criterion_not_met",
        }
    # Auxiliary comparisons and paired resource changes: one mean per independent bug.
    contrasts = [(c["id"], c["before"], c["after"]) for c in protocol["primary_comparisons"]]
    for v in config["arms"]["verbosities"]:
        contrasts += [
            (f"total_effect_{v}", f"raw_{v}", f"graph_{v}"),
            (f"information_{v}", f"raw_{v}", f"text_{v}"),
            (f"structure_{v}", f"text_{v}", f"graph_{v}"),
        ]
    exploratory = {}
    for name, before_arm, after_arm in contrasts:
        comparisons = {}
        for key in (
            "usable_diagnosis",
            "output_tokens",
            "input_tokens",
            "total_tokens",
            "request_seconds",
        ):
            differences = {}
            for s in schedule:
                if s["arm"] != before_arm or s["label"] != "test_failure":
                    continue
                other = next(
                    t
                    for t in schedule
                    if t["case"] == s["case"]
                    and t["repeat"] == s["repeat"]
                    and t["arm"] == after_arm
                )
                a, b = returned.get(s["id"]), returned.get(other["id"])
                if mock or not a or not b:
                    continue
                if key == "usable_diagnosis":
                    av, bv = metric(s, key), metric(other, key)
                else:
                    av = a.get(key) if key == "request_seconds" else a["response"].get(key)
                    bv = b.get(key) if key == "request_seconds" else b["response"].get(key)
                if av is not None and bv is not None:
                    differences.setdefault(s["independent_key"], []).append(bv - av)
            comparisons[key] = {
                "paired_bugs": len(differences),
                "mean_difference": mean([mean(v) for v in differences.values()]),
                "exploratory_bootstrap": bootstrap_clusters(
                    differences,
                    config["backend"]["seed"],
                    config["statistics"]["bootstrap_samples"],
                ),
            }
        exploratory[name] = {"before": before_arm, "after": after_arm, "metrics": comparisons}
    coverage_subsets = {
        name: {
            a["arm"]: group(
                [
                    s
                    for s in schedule
                    if s["arm"] == a["arm"] and s["case"] in inputs and condition(inputs[s["case"]])
                ]
            )
            for a in protocol["arms"]
        }
        for name, condition in {
            "relations_present": lambda i: bool(i["prepared"]["edges"]),
            "relations_empty": lambda i: not i["prepared"]["edges"],
            "root_any_line": lambda i: i["coverage"]["any_line"] is True,
            "root_missing": lambda i: i["coverage"]["any_line"] is False,
            "root_full_function": lambda i: i["coverage"]["full_function"] is True,
            "essential_support_present": lambda i: i["coverage"]["essential_support"] is True,
        }.items()
    }
    interaction = {}
    slot_lookup = {(s["case"], s["repeat"], s["arm"]): s for s in schedule}
    for verbosity in config["arms"]["verbosities"]:
        if verbosity == "short":
            continue
        differences = {}
        for slot in schedule:
            if slot["label"] != "test_failure" or slot["arm"] != "graph_short":
                continue
            values = [
                metric(slot_lookup[slot["case"], slot["repeat"], arm], "usable_diagnosis")
                for arm in ("graph_short", "text_short", f"graph_{verbosity}", f"text_{verbosity}")
            ]
            if all(v is not None for v in values):
                differences.setdefault(slot["independent_key"], []).append(
                    (values[0] - values[1]) - (values[2] - values[3])
                )
        interaction[f"structure_short_minus_{verbosity}"] = {
            "paired_bugs": len(differences),
            "mean_difference_in_differences": mean([mean(v) for v in differences.values()]),
            "exploratory_bootstrap": bootstrap_clusters(
                differences, config["backend"]["seed"], config["statistics"]["bootstrap_samples"]
            ),
        }
    analysis_plan = {
        "version": ANALYSIS_VERSION,
        "primary": protocol["primary_comparisons"],
        "statistics": config["statistics"],
        "review": config["review"],
        "primary_unit": "canonical independent bug, failed observation, preregistered repeat",
        "secondary_unit": "mean within canonical independent bug across repeats",
    }
    provenance = {
        "protocol_sha256": protocol_hash,
        "analysis_plan_sha256": digest(analysis_plan),
        "analysis_dependencies": dependency_manifest(),
        "server_sha256": sha(output / "server.json") if (output / "server.json").exists() else None,
        "input_audit_sha256": sha(output / "input-audit.json")
        if (output / "input-audit.json").exists()
        else None,
        "analysis_code": {
            p.name: sha(p)
            for p in (ROOT / "src/ai_error_check_agent/experiments").glob("study*.py")
        },
        "input_hashes": {k: v["content_sha256"] for k, v in inputs.items()},
        "attempt_hashes": {p.name: sha(p) for p in (output / "attempts").glob("*.json")},
        "reviews": {p.name: sha(p) for p in (output / "review-A.json", output / "review-B.json")},
        "manifest_sha256": sha(output / "manifest.json")
        if (output / "manifest.json").exists()
        else None,
    }
    analysis_id = digest(provenance)[:20]
    result = {
        "analysis_id": analysis_id,
        "role": config["role"],
        "mode": manifest["mode"],
        "independent_bugs": protocol["independent_bugs"],
        "projects": protocol["projects"],
        "all_responses_received": len(returned) == len(schedule),
        "collection": group(schedule),
        "groups": groups,
        "primary": primary,
        "project_results": project_results,
        "project_macro_usable": {
            a: mean([v[a]["usable_diagnosis"]["mean"] for v in project_results.values()])
            for a in groups
        },
        "exploratory": exploratory,
        "exploratory_interactions": interaction,
        "coverage_subsets": coverage_subsets,
        "review_validation": validation,
        "reference_status": dict(Counter(r["reference_status"] for r in refs.values())),
        "active_generation_wall_seconds_including_sleep": manifest.get("active_wall_seconds")
        if not mock
        else None,
        "active_run_seconds_including_preflight": manifest.get(
            "active_run_seconds_including_preflight"
        )
        if not mock
        else None,
        "retrieval_preparation_seconds": distribution(
            [v.get("retrieval_preparation_seconds") if not mock else None for v in inputs.values()]
        ),
        "token_check_seconds": distribution(
            [v.get("token_check_seconds") if not mock else None for v in inputs.values()]
        ),
        "response_model_versions": sorted(versions),
        "provenance": provenance,
        "limits": [
            "Hosted alias is not verified QAT weights; thinkingLevel=minimal does not mean no internal reasoning.",
            "Public pretraining contamination unknown; few projects and within-project dependence limit inference.",
            "Retrieval omissions and observation/lexical edges do not establish causation or repair success.",
            "Natural is the same structured prompt without length instructions; no universal product baseline claim.",
            "Mock quality, time and token measurements are null; JSON validity checks plumbing only.",
        ],
    }
    destination = output / "analyses" / analysis_id
    if destination.exists():
        if read(destination / "summary.json") != result:
            raise ValueError("Existing analysis changed; cannot overwrite")
        return result
    destination.mkdir(parents=True)
    save(destination / "analysis-plan.json", analysis_plan)
    save(destination / "review-validation.json", validation)
    save(destination / "summary.json", result)
    # Preserve exact source review files for each analysis, even after future ratings change.
    for letter in ("A", "B"):
        (destination / f"review-{letter}.json").write_bytes(
            (output / f"review-{letter}.json").read_bytes()
        )
    report = [
        f"# 논문 실험 v3 분석 {analysis_id}",
        "",
        f"역할 {config['role']} · 모드 {manifest['mode']} · 독립 버그 {protocol['independent_bugs']} · 프로젝트 {len(protocol['projects'])} · 수집 완료 {result['all_responses_received']}",
        "",
        "## 수집과 분모",
        "",
        "|조건|계획|시도|수신|형식 유효|미수신|미채점|길이 준수|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a, g in groups.items():
        report.append(
            f"|{a}|{g['planned']}|{g['attempts']}|{g['received']}|{g['contract_valid']}|{g['missing']}|{g['unreviewed_received']}|{g['length_target']['met']}/{g['length_target']['eligible_received']}|"
        )
    report += [
        "",
        "## 근거 선택·관계 범위",
        "",
        "input-audit.json의 원본 존재/any-line/full-function/essential-support 범위를 구분한다. 빈 관계 사례도 전체 분석에 포함한다. 포함 범위별 결과는 summary.json의 coverage_subsets에 있다.",
        "",
        "## 위치·원인·전체 설명·통과 관측",
        "",
        "|조건|함수 영역 위치 일치|원인 joint|usable 평균|usable n|pass 오탐|pass 유보|",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for a, g in groups.items():
        report.append(
            f"|{a}|{g['function_region_location_match']['mean']}|{g['mechanism_joint']['mean']}|{g['usable_diagnosis']['mean']}|{g['usable_diagnosis']['n']}|{g['pass_false_positive']}|{g['pass_abstention']}|"
        )
    report += ["", "## 주검정", ""]
    for name, p in primary.items():
        report += [
            f"- {name}: {p['after']} − {p['before']}; {p['judgment']}. n={p['n']}, cells={[p[k] for k in ('n00', 'n01', 'n10', 'n11')]}, 차이={p['delta_hat']}, 단측 하한={p['one_sided_lower']}, 비교 alpha={p['comparison_alpha']}, family alpha={p['family_alpha']}. 미충족: {p['blocking_reasons']}"
        ]
    report += [
        "",
        "## 토큰·요청 시간·전체 비용",
        "",
        "모든 시도/모든 수신/형식 유효 수신의 n·평균·중앙값·P95와 known subtotal·unknown attempts를 summary.json에 각각 보존했다. 요청 왕복 시간과 sleep 포함 wall time은 별도다. 누락 비용과 0 성공의 비율은 null이다. H_compact와 출력 토큰 감소를 따로 판단한다.",
        "",
    ]
    report += [
        "|조건|모든 시도 토큰 subtotal|사용량 미상 시도|수신 출력 토큰 평균 (n)|요청 초 평균 (n)|",
        "|---|---:|---:|---:|---:|",
    ]
    for arm, g in groups.items():
        total = g["cost_all_attempts"]["total_tokens"]
        tokens = g["cost_all_received"]["output_tokens"]
        seconds = g["cost_all_attempts"]["request_seconds"]
        report.append(
            f"|{arm}|{total['known_subtotal']}|{total['unknown_attempts']}|{tokens['mean']} ({tokens['n']})|{seconds['mean']} ({seconds['n']})|"
        )
    report += [
        "",
        "대응 차이는 after − before이며 출력 토큰 감소는 음수다. 다음 구간은 탐색적 bootstrap이다.",
        "",
    ]
    for name in ("H_structure", "H_compact"):
        resource = exploratory[name]["metrics"]
        report.append(
            f"- {name}: 출력 토큰 {resource['output_tokens']}; 총 토큰 {resource['total_tokens']}; 요청 시간 {resource['request_seconds']}"
        )
    report += [
        "",
        "## 프로젝트·보조 분석",
        "",
        "project_results, project_macro_usable, exploratory, coverage_subsets를 참조한다. bootstrap은 탐색용이며 확증 하한을 대체하지 않는다.",
        "",
        "## 사람 평가·참조 승인",
        "",
        f"참조 상태: {result['reference_status']}. 공동 채점 수: {validation['jointly_reviewed']}. 항목별 kappa·불일치는 review-validation.json. 미채점은 null이고 assistant는 human으로 계수하지 않는다.",
        "",
        "## 한계",
        "",
        *[f"- {line}" for line in result["limits"]],
        "",
        "## 보존 자료",
        "",
        "../../protocol.json, ../../code-manifest.json, ../../dependency-manifest.json, ../../inputs/, ../../attempts/, analysis-plan.json, review-A.json, review-B.json. reviewer-references.json과 review-key.json은 모델 입력과 분리한다.",
    ]
    (destination / "report.ko.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"Analysis saved: {destination}", flush=True)
    return result
