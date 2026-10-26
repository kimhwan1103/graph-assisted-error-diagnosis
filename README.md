# Evidence Binding and Deferred Decisions in Graph-Assisted Error Diagnosis

**A Controlled Case Study of a Gemma-Based Developer Workflow**

Companion research code, reproducible input examples, and aggregate results for the paper by **Hwan Kim**. The manuscript is included alongside the code for readers following the repository link from Kaggle.

**[Read the paper (PDF)](Evidence%20Binding%20and%20Deferred%20Decisions%20in%20Graph-Assisted%20Error%20Diagnosis.pdf)** · [Results](#results) · [Quick start](#quick-start) · [Reproducibility](#reproducibility-and-release-scope)

## Overview

The workflow connects deployment logs, source locations, and deployment configuration to produce evidence-linked diagnoses and conditional remediation plans. It combines:

1. Rule-based extraction of observations from logs and related source files.
2. An RDF knowledge graph with explicit links between observations and evidence.
3. SHACL constraint validation and SPARQL rules for generating diagnostic candidates.
4. LLM review of candidates alongside the original evidence.
5. Output-contract validation and server-side plan assembly, with a general-analysis fallback for deferral or supported validation failures.

Log evidence uses **EV identifiers**; source evidence uses **SC identifiers**. An SC identifier is an evidence index, not a source-file line number. This distinction is central to the paper's analysis of evidence-binding errors.

The implementation supports a restricted set of **Node.js file-read failures and Docker COPY contexts**. Diagnosis produces proposals; Docker-based execution is a separate evaluation step. SHACL conformance does not establish causal correctness, and the SPARQL rules are not general-purpose OWL reasoning.

## Paper and research questions

The paper, **[Evidence Binding and Deferred Decisions in Graph-Assisted Error Diagnosis](Evidence%20Binding%20and%20Deferred%20Decisions%20in%20Graph-Assisted%20Error%20Diagnosis.pdf)**, is included in the repository root.

It examines three questions:

- **E1 — Review:** Does LLM review change acceptance of rule-generated candidates compared with automatic acceptance?
- **E2 — Candidate information:** Does providing additional rule-generated candidates change outcomes when raw inputs and output contracts are matched?
- **Execution:** Do eligible final plans restore supported failures under the fixed evaluation checks?

The contribution is a controlled case study of **evidence binding, contextual decisions, and final plans**. It does not establish that adding graph candidates improves diagnosis in general.

## Results

The latest experiment is **contract-v2**, with the corrected **analysis-v1** report. It uses three author-built programs:

| Program | Workload |
| --- | --- |
| shipping-cli | Command-line application |
| template-http | HTTP application |
| settlement-batch | Batch-processing application |

Each program has four authored contexts: supported COPY omission, unknown supply conditions, external supply, and snapshot conflict. These form **12 contexts from three base incidents**.

R_auto is evaluated once per context. Each LLM condition is evaluated three times per context, giving **120 evaluation units** in total.

| Condition | Comparison role | Valid final responses | Recovery / scheduled execution-eligible units |
| --- | --- | ---: | ---: |
| R_auto | Automatic candidate acceptance | 12/12 | 3/3 |
| R_llm | LLM review of candidates | 36/36 | 9/9 |
| B_eq | Matched baseline without candidate fields | 36/36 | 9/9 |
| G_eq | Matched condition with candidate fields | 33/36 | 6/9 |
| **Total** | | **117/120** | **27/30** |

Final validity includes successful fallback responses and checks the output contract and evidence/location bindings. It is not a measure of semantic accuracy or safety.

Key observations:

- R_llm explicitly deferred **3/18 negative-context units**, all repetitions of one external-supply context. Each subsequent first plan again contained conditional COPY, so primary-stage deferral did not demonstrate safe final-plan behavior.
- G_eq had **17 primary contract blockages and one API timeout**. The 17 blockages involved insertion-line values matching SC identifier numbers rather than actual Dockerfile line numbers. The pattern is consistent with identifier/line confusion, but the causal mechanism was not established.
- Of 30 scheduled execution-eligible units, **27 were executed and recovered**. Two invalid final outputs and one API timeout remained in the scheduled denominator. Recovery was therefore **27/30 overall**, or **27/27 conditional on execution**.
- There were **128 generation attempts**: 108 primary and 20 fallback attempts. The timeout was retained without replacement. The configured upper bound was 216 attempts.

The API model was gemma-4-31b-it, with temperature 0, a 4,096-token output limit, requested seeds 42/43/44, a 180-second timeout, and 60-second call spacing. Seeds do not guarantee provider determinism.

### Result artifacts

| Artifact | Contents |
| --- | --- |
| [Paper PDF](Evidence%20Binding%20and%20Deferred%20Decisions%20in%20Graph-Assisted%20Error%20Diagnosis.pdf) | English manuscript, experimental design, results, and limitations |
| [Final result tables](paper/results/contract-v2/tables/) | CSV tables by condition, program, context, binding, cost, and failure |
| [Latest result summary](paper/results/contract-v2/paper-results.md) | Paper-oriented result summary |
| [Detailed analysis](paper/results/contract-v2/report.md) | Denominators, failures, normalization, and supplementary COPY audit |
| [Claim boundaries](paper/results/contract-v2/paper-claims-handoff.md) | Supported interpretations and limitations |
| [Experiment index](paper/README.md) | Earlier system-v4 and cross-program evaluations |

Earlier experiments have different designs and should not be pooled with the latest comparison. Original and normalized results are separate views of the same responses; reused executions are not independent successes.

## Quick start

The reference environment is **Windows with Python 3.12**. The pinned dependency file records that environment. GPU access, an API key, and Docker are not required for the offline checks below. Some optional unit tests use Node.js.

### Windows CMD

Run from the repository root:

```cmd
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-research-win-py312.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .

run_paper.cmd verify
run_paper.cmd plan
run_paper.cmd example --program template-http
run_paper.cmd test
```

### Linux / macOS

The equivalent commands are provided below. Installation and execution in Kaggle's hosted environment have not been validated; this release does not include a Kaggle notebook.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-research-win-py312.lock
.venv/bin/python -m pip install --no-deps -e .

.venv/bin/python scripts/verify_release.py
PYTHONPATH=src .venv/bin/python tools/run_review_controls_v4_contract_v2.py plan
.venv/bin/python scripts/replay_example.py --program template-http
.venv/bin/python scripts/run_tests.py
```

| Command | What it checks |
| --- | --- |
| verify | SHA-256 hashes of the publication files and their recorded provenance |
| plan | The 12-context, 120-unit schedule and 216-attempt generation ceiling |
| example | Reconstruction of a saved input through fact extraction, RDF/SHACL/SPARQL processing, and automatic plan assembly |
| test | The portable unit-test suite; model responses are mocked |

The example also accepts shipping-cli and settlement-batch. It does not query an LLM or measure Docker recovery. Tests may run isolated local Node.js examples and use loopback sockets.

**Recorded validation:** 578 tests passed and one was skipped because Windows symlink privileges were unavailable. This is an implementation check, not an experimental accuracy result.

A blank configuration template is available in [.env.example](.env.example) for separately configured, user-initiated API experiments. Credentials and generated outputs are excluded from Git.

Public documentation is available in English. The two original Korean runtime prompts are deliberately preserved to keep the experimental model inputs unchanged. [English prompt reading guides](docs/prompts/README.md) are provided for reviewers; the runtime does not load them.

## Reproducibility and release scope

This release includes the manuscript, diagnosis and evaluation code, example programs, prepared input packets, references, and aggregate result tables. The diagnosis core and original experiment tools retain their archived source bytes.

It excludes API keys, original model-response and call archives, Docker execution logs, temporary test outputs, and the complete historical freeze/proof artifacts.

Consequently:

- The public release supports code inspection, offline input reconstruction, unit tests, and inspection of the published tables.
- Resuming the original live run, rerunning archive-dependent audit tests, or recomputing every paper result from raw responses requires the separately preserved original experiment archive.
- The original clone/freeze/live workflow and the full post hoc analysis script cannot be assumed to run from this compact release alone. Existing freeze checks should not be bypassed.
- File-integrity verification checks the distributed artifact; it does not independently validate the underlying experimental observations.

See the [reproducibility notes](docs/REPRODUCIBILITY.md) and [publication inventory](docs/PUBLICATION.md) for additional details.

## Limitations

This is a development-stage case study with one model, three base incidents, authored contexts, and repeated observations. It has no recorded independent human reference approval, and output-contract clarification was informed by earlier results on the same contexts. It is not a held-out evaluation.

The frozen COPY indicator can miss directory copies whose destination lacks a trailing slash. Its non-detections must not be interpreted as safety successes. The supplementary audit is descriptive and does not establish harmful execution or final-plan safety.

The 120 units are not 120 independent bugs. The study does not establish statistical significance, broad generalization, or the superiority of RDF or LLM review. Reported API timings exclude offline candidate preparation and are not end-to-end system latency.

## Repository layout

| Path | Purpose |
| --- | --- |
| Root PDF | English paper accompanying the code |
| src/ai_error_check_agent/ | Diagnosis, API, source analysis, and validation |
| src/ai_error_check_agent/knowledge/ | RDF ontology, SHACL shapes, and SPARQL rules |
| src/ai_error_check_agent/agent/ | Prompts and response schemas |
| tools/ | Experiment, normalization, remediation, and reporting tools |
| fixtures/cross-program-v4/ | Three author-built Node.js example programs |
| evaluation/ | Case definitions, references, prepared packets, and evaluation utilities |
| config/ | Experiment settings without credentials |
| tests/ | Portable unit-test suite |
| scripts/ | Publication verification, example replay, and test entry points |
| paper/results/ | Aggregate tables and interpretation notes |
| analysis/ | Post hoc analysis requiring the original run archive |

Some modules retain earlier version names because the current implementation reuses them. Archive-dependent tests in tools/tests/ are separate from the default public test suite. The small historical BugsJS input retained for API regression tests is documented with its upstream licenses in [its own README](evaluation/public/bugsjs_hessian/README.md).

## License

Copyright 2026 Hwan Kim.

Original source code in this repository is licensed under the
Apache License 2.0. See [LICENSE](LICENSE).

Third-party materials retain their original licenses and
copyright notices. The paper PDF is not covered by this
source-code license.