"""Inspect two selected saved case pairs; never calls an API or runs Docker.

Usage: python verify_examples.py
Python 3.9+; standard library only. The JSON must be in ./evidence/.
This is not the experimental contract validator or full-result reanalysis.
"""
from pathlib import Path
import hashlib
import json
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def response(call):
    text = call['raw_output_text'].strip()
    if text.startswith('```'):
        text = '\n'.join(text.splitlines()[1:-1])
    return json.loads(text)


def inspect(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    b = data['e2_pair']['B_eq']['call']
    g = data['e2_pair']['G_eq']['call']
    for field in ['prompt', 'schema', 'seed', 'stage', 'requested_model_id', 'generation_config']:
        require(b[field] == g[field], 'E2 pair differs in ' + field)
    keys = set(b['input']) | set(g['input'])
    differing = sorted(k for k in keys if b['input'].get(k) != g['input'].get(k))
    require(differing == ['rule_candidates'], 'Unexpected paired input difference')
    source = next(x for x in b['input']['source_evidence'] if x['id'] == 'SC000011')
    require(source['path'] == 'Dockerfile' and source['line'] == 6, 'Source mapping differs')
    b_out, g_out = response(b), response(g)
    require(b_out['packaging']['insert_after_line'] == 6, 'Baseline insertion line differs')
    require(g_out['packaging']['insert_after_line'] == 11, 'Candidate insertion line differs')
    review, fallback = data['defer_pair']['review'], data['defer_pair']['fallback']
    r_out, f_out = response(review), response(fallback)
    require(r_out['decision'] == 'defer', 'Review decision differs')
    require(r_out['counter_evidence_ids'] == [], 'Structured counterevidence differs')
    # Check both ID and observation content in the retained raw fallback input.
    raw_input = json.dumps(fallback['input'], ensure_ascii=False)
    require('EV000008' in raw_input and 'external_volume' in raw_input,
            'Expected raw external-supply observation absent')
    require('rule_candidates' not in fallback['input'], 'Unexpected fallback candidates')
    require(r_out['summary'] not in raw_input, 'Prior rationale is present in fallback input')
    require(r_out['uncertainty'] not in raw_input, 'Prior uncertainty is present in fallback input')
    plan = data['defer_pair']['first_final_plan']
    # Verify the stored first-plan excerpt also occurs in the saved raw response.
    def objects(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from objects(child)
        elif isinstance(value, list):
            for child in value:
                yield from objects(child)
    require(plan in list(objects(f_out)), 'Stored plan differs from saved fallback response')
    require(plan['apply_when'], 'Conditional applicability statement absent')
    require(any(c.get('snippet') == 'COPY --chown=node:node config ./config'
                for c in plan['changes']), 'Expected conditional COPY absent')
    return {
        'status': 'selected_examples_checked',
        'evidence_file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'e2_differing_input_fields': differing,
        'SC000011_actual_line': source['line'],
        'baseline_insert_after_line': b_out['packaging']['insert_after_line'],
        'candidate_insert_after_line': g_out['packaging']['insert_after_line'],
        'review_decision': r_out['decision'],
        'fallback_raw_external_supply_observation_retained': True,
        'fallback_prior_review_explanation_absent': True,
        'fallback_conditional_COPY_present': True,
        'scope': 'One selected E2 pair and one selected review/fallback pair only.',
        'not_established': 'Full-120-unit reanalysis, original source-file hashes, causal explanation, semantic correctness, or live reproducibility.',
        'model_calls': 0,
        'docker_runs': 0,
    }


if __name__ == '__main__':
    try:
        result = inspect(Path(__file__).resolve().parent/'evidence/final_control_examples.json')
    except (ValueError, KeyError, StopIteration, OSError) as error:
        print('Selected-example inspection failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2, ensure_ascii=False))
