# Computed tables for the paper

## final_checks

| Metric | Original | Normalized |
| --- | --- | --- |
| Final-response validation passed | 26/110 (23.6%) | 75/110 (68.2%) |
| Status-label agreement / all cells | 26/110 (23.6%) | 75/110 (68.2%) |
| Status-label agreement / valid responses | 26/26 (100.0%) | 75/75 (100.0%) |
| Code-location hit / all eligible cells | 26/80 (32.5%) | 50/80 (62.5%) |
| Code-location hit / eligible valid responses | 26/26 (100.0%) | 50/60 (83.3%) |
| EV identifier exists / citations in valid responses | 64/64 (100.0%) | 237/237 (100.0%) |
| SC evidence-binding agreement / citations in valid responses | 86/86 (100.0%) | 139/139 (100.0%) |

## graph_scores

| Aggregation scope | Dimension | TP | FP | FN | P | R | F1 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| All 11 cells | Facts | 100 | 0 | 33 | 1.000 | 0.752 | 0.858 |
| All 11 cells | Relations | 76 | 0 | 18 | 1.000 | 0.809 | 0.894 |
| All 11 cells | Candidates | 19 | 0 | 4 | 1.000 | 0.826 | 0.905 |
| 9 cells reaching the source stage | Facts | 100 | 0 | 11 | 1.000 | 0.901 | 0.948 |
| 9 cells reaching the source stage | Relations | 76 | 0 | 4 | 1.000 | 0.950 | 0.974 |
| 9 cells reaching the source stage | Candidates | 19 | 0 | 0 | 1.000 | 1.000 | 1.000 |

## arms

| Condition | Original valid | Normalized valid | Mean input tokens | Mean output tokens | Mean processing time (s) |
| --- | --- | --- | --- | --- | --- |
| adaptive | 1/11 (9.1%) | 9/11 (81.8%) | 7056.9 | 1224.8 | 35.96 |
| graph_compact | 6/11 (54.5%) | 8/11 (72.7%) | 5285.7 | 766.1 | 22.97 |
| F | 1/11 (9.1%) | 7/11 (63.6%) | 7423.0 | 1332.2 | 38.78 |
| RF | 1/11 (9.1%) | 6/11 (54.5%) | 7545.7 | 1361.2 | 39.55 |
| C | 1/11 (9.1%) | 7/11 (63.6%) | 9865.8 | 1601.2 | 47.39 |
| RC | 2/11 (18.2%) | 7/11 (63.6%) | 9477.6 | 1485.4 | 44.05 |
| procedural_RC | 2/11 (18.2%) | 7/11 (63.6%) | 9477.6 | 1485.3 | 44.02 |
| no_shacl | 6/11 (54.5%) | 8/11 (72.7%) | 5285.7 | 766.1 | 22.94 |
| no_rules | 3/11 (27.3%) | 8/11 (72.7%) | 7260.6 | 1282.6 | 37.10 |
| no_shacl_no_rules | 3/11 (27.3%) | 8/11 (72.7%) | 7260.6 | 1282.6 | 37.27 |

## audit

| Condition | Candidates from corrupted inputs | SHACL rejection / corrupted inputs | Candidates from uncorrupted inputs | SHACL rejection / checked uncorrupted inputs |
| --- | --- | --- | --- | --- |
| graph_compact | 0/4 (0.0%) | 2/4 (50.0%) | 8/11 (72.7%) | 0/8 (0.0%) |
| no_rules | 0/4 (0.0%) | 2/4 (50.0%) | 0/11 (0.0%) | 0/8 (0.0%) |
| no_shacl | 1/4 (25.0%) | 0/4 (0.0%) | 8/11 (72.7%) | N/A |
| no_shacl_no_rules | 0/4 (0.0%) | 0/4 (0.0%) | 0/11 (0.0%) | N/A |

## sensitivity

| Analysis | Total cells | Original validation passed | Normalized validation passed | Normalized code-location hit | graph_compact candidate F1 |
| --- | --- | --- | --- | --- | --- |
| All cases | 110 | 26/110 (23.6%) | 75/110 (68.2%) | 50/80 (62.5%) | 0.905 |
| Excluding direct-literal | 100 | 22/100 (22.0%) | 66/100 (66.0%) | 41/70 (58.6%) | 0.895 |
