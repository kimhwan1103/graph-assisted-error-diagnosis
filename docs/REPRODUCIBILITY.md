# Reproducing checks from the public release

## Checks supported by this release alone

1. Follow the installation commands in the README to create a local `.venv`.
2. Run `run_paper.cmd verify` to check the listed publication files against their SHA-256 hashes.
3. Run `run_paper.cmd plan` to inspect the latest experiment's 12 contexts, 120 rows, and ceiling of 216 generation attempts.
4. Run `run_paper.cmd example --program template-http` to reconstruct fact extraction, RDF/SHACL/SPARQL processing, and automatic assembly from a saved P_supported input. The other choices are `shipping-cli` and `settlement-batch`. This output is not an LLM-performance or Docker-recovery score.
5. Run `run_paper.cmd test` to execute unit tests for the core and public inputs. Tests dedicated to earlier v2/v3 datasets and notebooks are not part of the public default suite. The omitted modules are listed in `publication-provenance.json`.

On non-Windows systems, use these commands after installation:

```sh
.venv/bin/python scripts/verify_release.py
.venv/bin/python scripts/replay_example.py --program shipping-cli
.venv/bin/python scripts/run_tests.py
PYTHONPATH=src .venv/bin/python tools/run_review_controls_v4_contract_v2.py plan
```

Tests requiring Node.js may be skipped when it is unavailable. Basic checks require neither an API key nor a Docker engine. Fake model responses in unit tests do not contribute to experimental success counts or actual API-call counts.

The test runner creates a new directory under the repository's `.test-tmp/` for each run to avoid system temporary-directory permission problems. This directory is excluded from Git. Tests of asynchronous processing require an environment that permits local loopback sockets.

## Operations requiring the original run archive

The experiment code in `tools/` is unchanged. The original audit, freeze, live-resume, Docker evaluation, and post hoc analysis tools verify the hashes and environment associated with:

- Earlier review-controls runs and reports, original model responses, and call/state files.
- Docker preflight and execution logs, together with verification artifacts.
- The freeze-manifest, protocol, and complete prepared file set.
- Original research documents, settings, and environment versions.

This archive is outside the selected public release scope. Do not assume that `tools/prepare_review_controls_v4_contract_v2.py clone/freeze`, `live`, archive-dependent tests in `tools/tests/`, or `analysis/review_controls_contract_v2_final_v1/analyze.py` can run from this release alone. Do not bypass validation by editing original paths or freeze files.

The original records remain in the author's preserved experiment folder. Providing that folder as a separate research archive would support recomputation of the complete results from raw responses. A rerun in a new environment must be prepared, checked, and frozen as a new experiment; its observations must not be pooled with the run used in the paper.

## Versions and hashes

`publication-provenance.json` records original relative paths, original SHA-256 hashes, exported SHA-256 hashes, and transformations such as path redaction or documentation translation. `release-manifest.json` records hashes for the published files. New `.git`, `.venv`, cache, and result directories are outside the publication-verification scope.

The `.gitattributes` file preserves file bytes rather than allowing Git newline conversion. Publication documentation, launchers, and verification scripts were added separately. Diagnosis code, runtime prompts, ontology, SHACL, SPARQL, and scoring code were not changed.

The original Korean runtime prompts are retained to preserve the experiment's language conditions. Their [English reading guides](prompts/README.md) are documentation only and are not loaded by the runtime.
