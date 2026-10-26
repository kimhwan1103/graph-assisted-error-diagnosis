"""Paired factorial contrasts; lineage is the resampling unit, never model repeats."""

import random
import statistics
from collections import defaultdict


def interval(groups):
    means = [statistics.mean(values) for values in groups.values()]
    if not means:
        return {"clusters": 0, "estimate": None, "ci95": None}
    ci = None
    if len(means) >= 2:
        rng = random.Random(4204)
        estimates = sorted(statistics.mean(rng.choices(means, k=len(means))) for _ in range(2000))
        ci = [estimates[49], estimates[1949]]
    return {"clusters": len(means), "estimate": statistics.mean(means), "ci95": ci}


def factorial(rows, metric):
    matched = defaultdict(dict)
    for row in rows:
        matched[row["case_id"], row["repeat"]][row["arm"]] = row
    groups = {key: defaultdict(list) for key in ("rules_main", "compact_main", "interaction")}
    count = 0
    for cells in matched.values():
        if not all(
            arm in cells and cells[arm]["factorial_applied"] and cells[arm].get(metric) is not None
            for arm in ("F", "RF", "C", "RC")
        ):
            continue
        f, rf, c, rc = (cells[arm][metric] for arm in ("F", "RF", "C", "RC"))
        cluster = cells["F"]["cluster_id"]
        groups["rules_main"][cluster].append(((rf - f) + (rc - c)) / 2)
        groups["compact_main"][cluster].append(((c - f) + (rc - rf)) / 2)
        groups["interaction"][cluster].append((rc - c) - (rf - f))
        count += 1
    return {
        "metric": metric,
        "complete_factorial_blocks": count,
        "formula": {
            "rules_main": "((RF-F)+(RC-C))/2",
            "compact_main": "((C-F)+(RC-RF))/2",
            "interaction": "(RC-C)-(RF-F)",
        },
        "contrasts": {key: interval(group) for key, group in groups.items()},
        "inference": "exploratory cluster bootstrap; family-wise confirmatory superiority is not inferred",
    }


def noninferiority(rows, frozen):
    """One predeclared E1 endpoint; conservative two-sided 95% lower bound."""
    settings = frozen["config"].get("statistics", {})
    matched = defaultdict(dict)
    for row in rows:
        if row["repeat"] == 0:
            matched[row["case_id"]][row["arm"]] = row
    groups = defaultdict(list)
    complete = True
    for case in frozen["cases"]:
        arms = matched[case["id"]]
        if not all(
            arm in arms and arms[arm]["usable_final"] is not None
            for arm in ("adaptive", "graph_compact")
        ):
            complete = False
            continue
        groups[case["cluster_id"]].append(
            arms["graph_compact"]["usable_final"] - arms["adaptive"]["usable_final"]
        )
    estimate = interval(groups)
    cluster_means = [statistics.mean(values) for values in groups.values()]
    degenerate = len(set(cluster_means)) < 2
    margin = settings.get("quality_noninferiority_margin", 0.05)
    eligible = (
        frozen["live"]
        and frozen["config"]["role"] == "evaluation"
        and complete
        and estimate["clusters"] >= settings.get("minimum_clusters_for_ni", 20)
        and bool(settings.get("protocol_registration"))
        and not degenerate
    )
    return {
        "endpoint": "usable_final",
        "comparison": "graph_compact minus adaptive",
        "primary_repeat": 0,
        "margin": margin,
        **estimate,
        "analysis_eligible": eligible,
        "degenerate_cluster_distribution": degenerate,
        "noninferiority_supported": estimate["ci95"][0] > -margin
        if eligible and estimate["ci95"]
        else None,
        "gate": "Requires live held-out approved references, complete paired quality data, registered protocol, minimum lineage count, and non-degenerate cluster differences. Minimum count is not a power guarantee; degenerate bootstrap requires a separately justified interval method.",
        "method": "Equal-lineage-weighted paired risk difference; two-sided 95% cluster bootstrap lower bound > -margin.",
    }


def agreement(ratings, primary, keys):
    result = {}
    for key in keys:
        pairs = [(row["scores"][key], primary[row["cell_id"]]["scores"][key]) for row in ratings]
        if not pairs:
            result[key] = {"n": 0, "agreement": None, "cohen_kappa": None}
            continue
        observed = statistics.mean(a == b for a, b in pairs)
        pa, pb = statistics.mean(a for a, _ in pairs), statistics.mean(b for _, b in pairs)
        chance = pa * pb + (1 - pa) * (1 - pb)
        result[key] = {
            "n": len(pairs),
            "agreement": observed,
            "cohen_kappa": (observed - chance) / (1 - chance) if chance < 1 else None,
        }
    return result
