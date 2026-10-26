# Final results analysis — contract-v2 / analysis-v1

Status: **All scheduled units processed; experiment concluded**. All 120 scheduled rows were processed; one API timeout is retained as a failed observation, without another call.

Analysis version: `review-controls.contract-v2.analysis-v1`. Original run_id: `ef4abb15ff513f461a564910`. This analysis made 0 model calls and 0 Docker executions. It did not alter the bytes of the original reports, responses, execution records, or frozen code. Preservation and aggregation were checked using `source-manifest.json`, `preservation-after.json`, and `verification.json` in the original analysis archive.

## 1. Completion-status correction and analysis denominators

The original report displayed `Technical recovery required` whenever any execution-eligible row had `provider_unavailable`. The 31st cell, `1d693d3212e91829a4c7`, was template-http / P_supported / G_eq / repeat 2 and ended with MODEL_TIMEOUT after 180.156 seconds. The request had already been dispatched and was preserved as a failed observation. The remaining schedule was subsequently processed: 120 scheduled, 120 completed, 0 pending, and 0 excluded rows. The failed call was not repeated to obtain a new response under the frozen conditions.

This separate analysis distinguishes **schedule completion** from **availability of individual outcomes**. The diagnosis and potential recovery outcome for that one failed call remain unknown; it is neither converted to a success nor removed from the denominator. Existing scores and rates in `summary.json` were retained, with only the completion status and explanatory metadata amended. This correction does not mean that the API problem was resolved or that every response succeeded. The planned experiment is concluded using the available records, without another full experiment.

- Actual generation attempts: 128 = 108 primary + 20 fallback calls. The timeout is included as 1 attempt. The run remained within the total ceiling of 216.
- Valid final original responses: 117/120 (97.5%); normalized responses were also 117/120. Excluding the 12 automatic rows, LLM conditions had 105/108 (97.2%) valid responses. These are format/evidence validation rates, not diagnostic accuracy.
- Observed recovery: 27 of 30 execution-eligible rows (90.0%). All 27 executed rows recovered. The remaining rows had 2 final-validation failures and 1 API failure; these are not counted as actual Docker recovery failures.
- The 27 reused original/normalized executions with identical patches are not additional independent recovery successes. Normalized evaluation required 0 new Docker executions.

## 2. Experimental scope and conditions

The study comprises 12 contexts obtained by applying four authored context variants to 3 base incidents in shipping-cli, template-http, and settlement-batch. The references require acceptance for P_supported and P_unknown and deferral for N_external_supply and N_snapshot_conflict. P_unknown evaluates acceptance of a conditional plan under limited assumptions, not actual recovery. The inputs combine captured observations with authored controlled context, and there is no recorded human reference approval. The evaluation is therefore described as a development-stage proof of concept.

|Comparison|Condition|Configuration|Rows|
|---|---|---|---|
|E1|R_auto|Automatically accept rule candidates without LLM review|12 (1 per context)|
|E1|R_llm|LLM review of the same candidates with context|36 (3 per context)|
|E2|B_eq|Common raw inputs and output contract, without added rule candidates|36|
|E2|G_eq|Same conditions as B_eq, with added rule-candidate fields|36|

E1 and E2 are separate comparisons. Across E2's 36 matched pairs, the actual raw inputs excluding candidate fields, prompts, schemas, and generation settings all match. The 3 repetitions are repeated model outputs for the same case; they do not add 3 independent incidents. R_auto is not duplicated into 36 observations. No significance test or confidence interval assuming independent samples is presented.

Generation used the Gemma API model `gemma-4-31b-it`, with temperature=0, max_tokens=4096, seed=42·43·44, warmup=0, request spacing of 60 seconds, and timeout=180 seconds. These are fixed study settings, not a claim of complete provider determinism from seeds. Model inputs, raw responses, and transmitted settings are preserved in the original call files.

## 3. Primary-stage decisions and final validity

|Condition|Scheduled|Appropriate positive acceptance|Inappropriate negative acceptance|Explicit negative deferral|Contract blockages|API failures|Final valid|
|---|---|---|---|---|---|---|---|
|R_auto|12|6/6 (100.0%)|6/6 (100.0%)|0/6 (0.0%)|0|0|12/12 (100.0%)|
|R_llm|36|18/18 (100.0%)|15/18 (83.3%)|3/18 (16.7%)|0|0|36/36 (100.0%)|
|B_eq|36|18/18 (100.0%)|18/18 (100.0%)|0/18 (0.0%)|0|0|36/36 (100.0%)|
|G_eq|36|9/18 (50.0%)|9/18 (50.0%)|0/18 (0.0%)|17|1|33/36 (91.7%)|

Appropriate positive acceptance requires both an effective accept decision and matching bindings in a reference-positive context. Inappropriate negative acceptance is an effective accept decision in a reference-negative context. Explicit deferral, contract blockage, and API error are distinct outcomes. Excessive deferral on positive contexts was 0 in every condition. Reduced acceptance caused by failures must not be interpreted as a deferral effect.

### E1: Limited primary-stage deferral

R_auto accepted all positive units (6/6) and all negative units (6/6). R_llm accepted 18/18 positive units, inappropriately accepted 15/18 negative units, and explicitly deferred 3/18. The inappropriate negative-acceptance rate decreased from 100.0% to 83.3%, a difference of 16.7 percentage points. All three deferrals were concentrated in **the three repetitions of settlement-batch's N_external_supply context**. The other five negative contexts were accepted in every repetition. This cannot be generalized to consistent safety improvement across error types.

Structured counter_evidence_ids were empty, including in the three deferred responses. Negative evidence-group coverage was therefore 0/6 for R_auto and 0/18 for R_llm. With no citations, precision is N/A (0/0), not 0%. Free-text discussion or references to logs in other evidence_ids fields were not substituted for structured counter-evidence citations. The final-plan limitations in Section 6 must accompany this result.

### E2: No established advantage from additional candidate information

B_eq passed the primary contract in all 36 rows but also accepted 18/18 negative units. G_eq had 18/36 effective accepts, 17/36 contract blockages, 1/36 API failures, and 0 explicit deferrals. Its 9/18 inappropriate negative accepts reflect contract blockage in the other 9 units. **They do not reflect semantic rejection of negative contexts.** Appropriate positive acceptance also decreased from 18/18 for B_eq to 9/18 for G_eq.

Of G_eq's 17 fallback analyses, 15 produced valid final responses, bringing final validity to 33/36. B_eq made no fallback calls and had 36/36 valid final responses. These data do not establish a comparative advantage from additional candidates in accuracy, contract compliance, or call cost. The comparison concerns the provision of candidate fields; it does not independently establish advantages of RDF representation, SHACL, or SPARQL.

## 4. Contract blockages and binding analysis

|Condition|Field|Match / scheduled|Mismatch|No response|
|---|---|---|---|---|
|B_eq|target|36/36 (100.0%)|0|0|
|B_eq|source_path|36/36 (100.0%)|0|0|
|B_eq|source_line|36/36 (100.0%)|0|0|
|B_eq|repository_path|36/36 (100.0%)|0|0|
|B_eq|insert_after_line|36/36 (100.0%)|0|0|
|B_eq|plan|36/36 (100.0%)|0|0|
|G_eq|target|35/36 (97.2%)|0|1|
|G_eq|source_path|35/36 (97.2%)|0|1|
|G_eq|source_line|35/36 (97.2%)|0|1|
|G_eq|repository_path|35/36 (97.2%)|0|1|
|G_eq|insert_after_line|18/36 (50.0%)|17|1|
|G_eq|plan|35/36 (97.2%)|0|1|

All 17 G_eq contract blockages were associated with incorrect line numbers in packaging.insert_after_line. The 11 template-http outputs returned 11 instead of the actual last COPY line, 6; the 6 settlement-batch outputs returned 12 instead of the actual line, 7. These values match the numeric portions of the corresponding last COPY evidence identifiers, SC000011 and SC000012. This is **a pattern consistent with confusion between SC identifier numbers and actual line numbers**. It does not establish the model's internal reasoning or a causal mechanism by which candidate information produced the confusion.

Two fallback responses for template-http / P_supported, repeats 0 and 1, were also blocked with INVALID_SOURCE_LOCATION because the SC citations in source_findings did not match the declared line range. Repeat 2 of the same context timed out at the API. Earlier fallback failures involving the language enum or empty observations.evidence_ids did not recur in these saved results. Raw responses were not edited and line numbers were not automatically corrected.

## 5. Executed recovery and results by program

|Condition|Execution-eligible|Executed|Recovery / scheduled|Recovery / executed|Not executed: validation failure|Not executed: API failure|
|---|---|---|---|---|---|---|
|R_auto|3|3|3/3 (100.0%)|3/3 (100.0%)|0|0|
|R_llm|9|9|9/9 (100.0%)|9/9 (100.0%)|0|0|
|B_eq|9|9|9/9 (100.0%)|9/9 (100.0%)|0|0|
|G_eq|9|6|6/9 (66.7%)|6/6 (100.0%)|2|1|

Recovery was evaluated only for P_supported. The entire first final plan was applied using the same image and checker that had passed preflight checks with reference and deliberately incorrect repairs. No Docker recovery rate was assigned to P_unknown or negative contexts. G_eq's 6/9 is observed recovery over scheduled units, not knowledge of the failed API call's potential outcome. Excluding the API failure yields 6/8=75.0% as an available-data reference value; it does not replace the primary 6/9. Even if that one missing unit had recovered, scheduled recovery for G_eq would be at most 7/9=77.8%, below B_eq's 9/9. This bounds the effect of one missing observation; it is neither a statistical estimate nor a rerun result.

|Program|Condition|Appropriate positive acceptance|Inappropriate negative acceptance|Negative deferral|Contract / API failures|Final valid|Recovery / scheduled|
|---|---|---|---|---|---|---|---|
|settlement-batch|R_auto|2/2 (100.0%)|2/2 (100.0%)|0/2 (0.0%)|0/0|4/4 (100.0%)|1/1 (100.0%)|
|settlement-batch|R_llm|6/6 (100.0%)|3/6 (50.0%)|3/6 (50.0%)|0/0|12/12 (100.0%)|3/3 (100.0%)|
|settlement-batch|B_eq|6/6 (100.0%)|6/6 (100.0%)|0/6 (0.0%)|0/0|12/12 (100.0%)|3/3 (100.0%)|
|settlement-batch|G_eq|3/6 (50.0%)|3/6 (50.0%)|0/6 (0.0%)|6/0|12/12 (100.0%)|3/3 (100.0%)|
|shipping-cli|R_auto|2/2 (100.0%)|2/2 (100.0%)|0/2 (0.0%)|0/0|4/4 (100.0%)|1/1 (100.0%)|
|shipping-cli|R_llm|6/6 (100.0%)|6/6 (100.0%)|0/6 (0.0%)|0/0|12/12 (100.0%)|3/3 (100.0%)|
|shipping-cli|B_eq|6/6 (100.0%)|6/6 (100.0%)|0/6 (0.0%)|0/0|12/12 (100.0%)|3/3 (100.0%)|
|shipping-cli|G_eq|6/6 (100.0%)|6/6 (100.0%)|0/6 (0.0%)|0/0|12/12 (100.0%)|3/3 (100.0%)|
|template-http|R_auto|2/2 (100.0%)|2/2 (100.0%)|0/2 (0.0%)|0/0|4/4 (100.0%)|1/1 (100.0%)|
|template-http|R_llm|6/6 (100.0%)|6/6 (100.0%)|0/6 (0.0%)|0/0|12/12 (100.0%)|3/3 (100.0%)|
|template-http|B_eq|6/6 (100.0%)|6/6 (100.0%)|0/6 (0.0%)|0/0|12/12 (100.0%)|3/3 (100.0%)|
|template-http|G_eq|0/6 (0.0%)|0/6 (0.0%)|0/6 (0.0%)|11/1|9/12 (75.0%)|0/3 (0.0%)|

All 12 shipping-cli rows in G_eq were accepted at the primary stage. template-http had 11 contract blockages and 1 timeout. In settlement-batch, all three repetitions of P_unknown and all three of N_external_supply were contract-blocked. This concentration by program and context should be reported alongside aggregate averages.

## 6. Final COPY indicators in negative contexts and supplementary audit

|Condition|Frozen target-COPY indicator|COPY in first plan|COPY in any plan|Archive mapping includes target (any plan)|
|---|---|---|---|---|
|R_auto|6/6 (100.0%)|6/6 (100.0%)|6/6 (100.0%)|6/6 (100.0%)|
|R_llm|15/18 (83.3%)|18/18 (100.0%)|18/18 (100.0%)|18/18 (100.0%)|
|B_eq|18/18 (100.0%)|18/18 (100.0%)|18/18 (100.0%)|18/18 (100.0%)|
|G_eq|9/18 (50.0%)|15/18 (83.3%)|18/18 (100.0%)|18/18 (100.0%)|

The frozen COPY indicator detects explicit target-file destinations or parent-directory destinations ending in `/`. It can miss copying an entire directory to a destination without a trailing slash, such as `COPY --chown=node:node config ./config`. All three R_llm deferrals were followed by this COPY in the first fallback plan. The 9 negative G_eq units marked false by the original indicator also contained directory COPY in the first or another plan.

The added columns are a **post hoc supplementary audit**. They separately assess whether the first or any plan's Dockerfile snippet contains COPY and whether literal path mapping against files/directories in the provided archive includes the target. Only supported single literal COPY instructions are mapped; unsupported syntax remains indeterminate. This is not a new scorer validating `.dockerignore`, application conditions, actual deployment context, interactions with other path-changing edits, or safety in full. No additional Docker execution or human semantic scoring was performed.

The original 15/18 and 9/18 values are preserved but **must not be presented as exact rates of harmful final proposals or safety success**. In particular, R_llm's primary-stage deferral effect does not establish that the complete agent prevented inappropriate remediation proposals. The metric does not demonstrate that deferral survives fallback analysis. Snippets and audit reasoning for the 60 negative evaluation units are retained in `tables/supplementary-copy-audit.csv` in the original analysis archive. This public release provides the aggregate `tables/copy-groups.csv`, without response snippets.

## 7. Calls, tokens, and time

|Condition|Primary / fallback calls|Observed input tokens|Observed output tokens|Missing usage|API waiting/processing sum (s)|Pacing sum (s)|
|---|---|---|---|---|---|---|
|R_auto|0/0|0|0|0|0.00|0.00|
|R_llm|36/3|137934|11292|0|368.75|1728.00|
|B_eq|36/0|134634|10239|0|337.14|1394.92|
|G_eq|36/17|279128|37510|1|1418.52|2543.39|

Relative to B_eq, G_eq made 53/36=1.47 times as many calls. Observed input tokens were 279,128/134,634=2.07 times as high, and observed output tokens were 37,510/10,239=3.66 times as high. Usage was unavailable for G_eq's timed-out call, so its token totals are not exact complete totals. Input-token differences reflect both candidate fields and additional fallback calls. Monetary cost was not calculated.

API timing includes the 180-second timeout and is not the same as model computation time. Pacing is waiting under the 60-second call-spacing policy. Graphs and candidates were generated once during offline preparation; the live run replayed fixed packets. These timings therefore do not measure end-to-end latency from graph construction through execution-based recovery. User waiting time between process restarts is also absent from summed cell timings.

## 8. Development comparison with the previous contract version

|Condition|Primary failures: previous → current|Final valid: previous → current|Recovery: previous → current|
|---|---|---|---|
|R_auto|0/12 → 0/12|12/12 → 12/12|3/3 → 3/3|
|R_llm|0/36 → 0/36|33/36 → 36/36|9/9 → 9/9|
|B_eq|36/36 → 0/36|0/36 → 36/36|0/9 → 9/9|
|G_eq|36/36 → 18/36|0/36 → 33/36|0/9 → 6/9|

Generation attempts decreased by 55, from 183 (108 primary + 75 fallback) to 128 (108 primary + 20 fallback). This is a development observation of improved response validity and assembly feasibility after contract clarification. The revision followed inspection of earlier results and reused the same contexts. It is not an independent holdout, a preregistered confirmatory experiment, or evidence of improved graph-based diagnostic accuracy. Rows from both versions must not be pooled to enlarge the sample.

## 9. Interpretations supported in the manuscript

- In restricted Node.js file-read and Docker COPY contexts, the implementation connected evidence binding, validation, rule-based candidate generation, LLM review, and execution-based recovery. Recovery was confirmed for 27 executable positive plans.
- In E1, LLM review explicitly deferred three repetitions of one negative context, while inappropriate acceptance persisted in other negative contexts. Final-plan safety was not established.
- In E2, no additional benefit from candidate fields was established. G_eq exhibited location-contract violations and increased fallback-call cost.
- Contract clarification improved response validity, but validity is not a proxy for semantic correctness or safety.

Limitations include 3 base incidents, authored contexts, non-independent repetitions, a single model with fixed settings, post hoc development changes, no recorded human reference approval, 1 missing API outcome, a restricted output contract and applicator, and the COPY detector's scope. The study makes no claim of generalization to other projects, languages, or unsupported errors; necessity of an LLM; universal superiority of RDF/OWL; or statistical significance/equivalence. SHACL structural validation must not be described as causal proof, nor SPARQL rule execution as general OWL reasoning.

## 10. Reproducibility and source records

The original separate analysis verified 14,364 files. In that archive, `summary.json` preserves the original numerical results with corrected completion status, `analysis.json` contains reaggregation and the supplementary audit, and `status-amendment.json` records the rationale and scope of the change. The original report, manuscript TeX/Bib files, experiment code, prompts, schemas, references, plans, and Docker outcomes were not modified.

This GitHub release provides result tables and interpretation documents, not the complete original archive. Paths and release-scope explanations were clarified, and the documentation was translated into English; observed values were not changed. See the [reproducibility notes](../../../docs/REPRODUCIBILITY.md) for the distinction between public input examples and full reaggregation. Public-file provenance and hashes are recorded in the repository-root `publication-provenance.json`.

