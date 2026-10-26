# Index of paper experiment results

| Experiment | Artifacts | Units and scope |
| --- | --- | --- |
| System v4 | [Quantitative tables](results/system-v4/tables.md), [original-response remediation evaluation](results/system-v4/remediation-original.md), [normalized-response remediation evaluation](results/system-v4/remediation-normalized.md) | Initial 11 cases × 10 conditions; development-stage system evaluation |
| Cross-program v4 | [Report](results/cross-program-v4/report.md), `program-summary.csv`, `remediation-summary.csv` | Three programs × three scenarios × two methods × three repetitions; 54 rows |
| Review ablation and matched baseline, contract-v2 | [Latest summary](results/contract-v2/paper-results.md), [detailed analysis](results/contract-v2/report.md), [claim boundaries](results/contract-v2/paper-claims-handoff.md), `tables/*.csv` | Three base incidents, 12 authored contexts, 120 rows; analysis-v1 |

Do not combine experiments with different samples and settings to construct an overall accuracy or independent sample count. The latest comparison has 117/120 valid final responses and recovery in 27/30 scheduled execution-eligible rows. Conditional on actual execution, recovery was 27/27. One API timeout and two validation failures remain in the scheduled denominator.

This directory contains public copies of saved aggregate results. Personal paths were redacted and the documentation was translated into English; observed values were not changed. Original model responses, execution logs, and the complete preservation manifest are not included. Reading these tables is distinct from recomputing them from raw records.

`contract-v2/tables/copy-groups.csv` summarizes a post hoc COPY audit. The frozen detector can miss directory copies whose destination lacks a trailing slash. This limitation must accompany the results; a COPY non-detection must not be interpreted as a safety success.
