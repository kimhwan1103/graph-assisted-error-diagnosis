# v4 automated remediation execution evaluation

Status: **complete** · Responses: **normalized** · Execution requested: **True**

The evaluation covers supported COPY plan representations in a fixed reproduction environment. Passing structural validation is not counted as successful recovery.
4 cases, 4 incidents, 1 lineage, and 8 cells. Classification timing: `post_hoc_development_before_replay_not_before_generation`.

## Repair-eligible cases

| Condition | Scheduled | Completed | Unresolved | Response valid | Application rate | Full recovery rate | Recovery after application | Regressions / checks completed | Applied cells without regression checks |
|---|---:|---:|---:|---|---|---|---|---|---:|
| adaptive | 1 | 1 | 0 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) | 0 |
| graph_compact | 1 | 1 | 0 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) | 0 |

## No-change and insufficient-evidence cases

| Condition | No-change cells | Unnecessary proposals / all | Unnecessary proposals / valid | Appropriate no-change | No-change response failures | Insufficient-evidence cells | Structural deferral | Insufficient-evidence response failures |
|---|---:|---|---|---|---:|---:|---|---:|
| adaptive | 2 | 0/2 (0.0%) | 0/1 (0.0%) | 1/2 (50.0%) | 1 | 1 | 0/1 (0.0%) | 1 |
| graph_compact | 2 | 0/2 (0.0%) | 0/1 (0.0%) | 1/2 (50.0%) | 1 | 1 | 0/1 (0.0%) | 1 |

## Matched comparison and execution scope

```json
{
  "both_restored": 1,
  "adaptive_only": 0,
  "graph_compact_only": 0,
  "neither_restored": 0,
  "unresolved": 0,
  "missing_comparator": 0
}
```

Actual agent-plan replay attempts: 2; shared cached executions: 0; new model calls: 0.
Reference repairs checked before replay and evaluator regression tests are not counted as agent recovery successes. Multiple conditions for the same incident do not increase the number of independent cases.
Checks that were not executed during a dry run or because of infrastructure errors are null. When unresolved cells remain, the full recovery rate is not established; confirmed successes over the scheduled denominator are reported only as a lower bound.
Original and normalized responses are evaluated separately. These execution timings are not the model's original response timings or new measurements of system tokens or latency after normalization.
The source scope visible to the model may differ across conditions. Inspect each cell's model_visible_inputs and fixture conditions. The semantic validity of natural-language application conditions is not automatically scored.

## Per-cell outcomes

| Cell | Condition | Case | Outcome | Reason |
|---|---|---|---|---|
| f894e1251331da2e67fe | graph_compact | expected-test-error | contract_failed | INVALID_STATE |
| db948e9d9762db92dd16 | adaptive | expected-test-error | contract_failed | INVALID_STATE |
| 178fdccea9b853641c18 | adaptive | docker-copy-omission | restored |  |
| 787f41c4608f3dcf48f8 | graph_compact | docker-copy-omission | restored |  |
| 4609d4dc1fcad0868f7b | adaptive | source-target-mismatch | contract_failed | INVALID_SOURCE_RESULT |
| 4251891de94fdebb7a14 | graph_compact | source-target-mismatch | contract_failed | INVALID_SOURCE_RESULT |
| b2026dfa6ca1089a2e4f | adaptive | recovered-same-stream | no_change_correct |  |
| b96190f8a8db7afb6c96 | graph_compact | recovered-same-stream | no_change_correct |  |

## Excluded cases

- missing-task-data: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- renamed-file: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- repeated-stack: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- direct-literal: Known source/log execution inconsistency
- other-stream-healthy: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- already-copied: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- ignored-file: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
