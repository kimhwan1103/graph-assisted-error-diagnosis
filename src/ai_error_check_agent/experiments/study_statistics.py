"""Preregisterable paired binary bounds; simulation is not empirical model evidence."""

import math

import numpy as np
from scipy.stats import beta

METHOD = "paired-discordance-cp-bonferroni.v1"


def cp_bounds(k, n, alpha):
    if type(n) is not int or type(k) is not int or not 0 <= k <= n or n < 1:
        raise ValueError("Require integer 0 <= k <= n, n >= 1")
    if not 0 < alpha < 0.5:
        raise ValueError("Require 0 < alpha < .5")
    return (
        0.0 if k == 0 else float(beta.ppf(alpha, k, n - k + 1)),
        1.0 if k == n else float(beta.isf(alpha, k + 1, n - k)),
    )


def paired_bound(cells, alpha=0.025, family_alpha=0.05):
    if len(cells) != 4 or any(type(k) is not int or k < 0 for k in cells):
        raise ValueError("Expected integer n00/n01/n10/n11")
    if not 0 < alpha <= family_alpha < 0.5:
        raise ValueError("Invalid alpha allocation")
    n00, gain, loss, n11 = cells
    n = sum(cells)
    lower = None
    if n:
        lower = max(-1.0, cp_bounds(gain, n, alpha / 2)[0] - cp_bounds(loss, n, alpha / 2)[1])
    return {
        "method_id": METHOD,
        "n": n,
        "n00": n00,
        "n01": gain,
        "n10": loss,
        "n11": n11,
        "delta_hat": (gain - loss) / n if n else None,
        "one_sided_lower": lower,
        "comparison_alpha": alpha,
        "family_alpha": family_alpha,
        "component_alpha": alpha / 2,
        "assumption": "independent identically distributed sampled bugs; project dependence unmodeled",
    }


def paired_binary(pairs, **kwargs):
    """One pair per canonical independent bug, never one pair per repeat/test."""
    cells = [0, 0, 0, 0]
    for before, after in pairs.values():
        if (
            type(before) is not int
            or type(after) is not int
            or before not in (0, 1)
            or after not in (0, 1)
        ):
            raise ValueError("Binary, completely reviewed pairs required")
        cells[2 * before + after] += 1
    return paired_bound(cells, **kwargs)


def simulate_plan(plan):
    """Power and boundary-error sensitivity at fixed candidate sizes, including quality gate."""
    probabilities = plan["probabilities"]
    if (
        len(probabilities) != 4
        or any(not 0 <= p <= 1 for p in probabilities)
        or not math.isclose(sum(probabilities), 1)
    ):
        raise ValueError("Four probabilities must sum to one")
    if not plan.get("probability_source"):
        raise ValueError("Document the source/scenario of probabilities")
    margin, alpha = plan["margin"], plan["comparison_alpha"]
    if not 0 < margin <= 0.2 or not 0 < alpha < 0.5:
        raise ValueError("Invalid margin/alpha")
    count = plan["simulations"]
    target = plan.get("target_power", 0.8)
    if not 0 < target < 1 or not plan["candidate_n"]:
        raise ValueError("Nonempty candidate sizes and target power in (0,1) required")
    if (
        type(count) is not int
        or count < 100
        or any(type(n) is not int or n < 1 for n in plan["candidate_n"])
    ):
        raise ValueError("Invalid simulation count or candidate n")
    rng = np.random.default_rng(plan["seed"])
    # Preserve total discordance if feasible. Delta boundaries are fixed, never tuned to results.
    discordance = max(margin, probabilities[1] + probabilities[2])
    ties = 1 - discordance
    tie_fraction = (
        probabilities[3] / (probabilities[0] + probabilities[3])
        if probabilities[0] + probabilities[3]
        else 0.5
    )
    scenarios = {
        "assumed_alternative": probabilities,
        "structure_null_boundary": [
            ties * (1 - tie_fraction),
            discordance / 2,
            discordance / 2,
            ties * tie_fraction,
        ],
        "compact_null_boundary": [
            ties * (1 - tie_fraction),
            (discordance - margin) / 2,
            (discordance + margin) / 2,
            ties * tie_fraction,
        ],
    }
    results = []
    for n in plan["candidate_n"]:
        lo = np.array([cp_bounds(k, n, alpha / 2)[0] for k in range(n + 1)])
        hi = np.array([cp_bounds(k, n, alpha / 2)[1] for k in range(n + 1)])
        for name, probs in scenarios.items():
            cells = rng.multinomial(n, probs, size=count)
            bounds = lo[cells[:, 1]] - hi[cells[:, 2]]
            quality = (cells[:, 2] + cells[:, 3]) / n
            rates = {
                "structure": float(np.mean(bounds > 0)),
                "compact": float(
                    np.mean((bounds > -margin) & (quality >= plan["minimum_reference_quality"]))
                ),
            }
            planned = n * 2 * plan["arm_count"] * plan["repeats"]
            results.append(
                {
                    "n": n,
                    "scenario": name,
                    "probabilities": probs,
                    "rejection_rate": rates,
                    "target_power_met": {k: p >= target for k, p in rates.items()}
                    if name == "assumed_alternative"
                    else None,
                    "monte_carlo_se": {k: math.sqrt(p * (1 - p) / count) for k, p in rates.items()},
                    "planned_generation_slots": planned,
                    "within_generation_budget": planned <= plan["generation_cap"],
                }
            )
    return {
        "kind": "offline_simulation_not_model_results",
        "method_id": METHOD,
        "plan": plan,
        "results": results,
        "minimum_feasible_candidate_meeting_target": {
            k: min(
                (
                    r["n"]
                    for r in results
                    if r["scenario"] == "assumed_alternative"
                    and r["within_generation_budget"]
                    and r["target_power_met"][k]
                ),
                default=None,
            )
            for k in ("structure", "compact")
        },
    }
