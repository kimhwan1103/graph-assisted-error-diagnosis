# Manuscript results summary — separate analysis of saved contract-v2 results

All scheduled units processed; experiment concluded. All 120 scheduled rows were processed; one API timeout is retained as a failed observation, without another call. The following text is a results draft based on existing records; no new experiment was performed.

Four authored context variants were applied to 3 base incidents in three Node.js programs, producing 12 contexts. Automatic acceptance was evaluated once per context, while each of the three LLM conditions was evaluated three times per context, giving 120 evaluation units. Actual generation comprised 128 calls: 108 primary and 20 fallback attempts. One API timeout was retained as a failed observation. References were author-controlled, with no recorded human approval, so the evaluation is limited to a small development-stage proof of concept.

The model was `gemma-4-31b-it`, with temperature=0, a maximum of 4096 output tokens, repetition seeds 42·43·44, call spacing of 60 seconds, and timeout=180 seconds. E2's 36 matched pairs used identical inputs except for candidate fields; prompts, output schemas, and generation settings also matched.

|Condition|Scheduled|Appropriate positive acceptance|Inappropriate negative acceptance|Explicit negative deferral|Contract blockages|API failures|Final valid|
|---|---|---|---|---|---|---|---|
|R_auto|12|6/6 (100.0%)|6/6 (100.0%)|0/6 (0.0%)|0|0|12/12 (100.0%)|
|R_llm|36|18/18 (100.0%)|15/18 (83.3%)|3/18 (16.7%)|0|0|36/36 (100.0%)|
|B_eq|36|18/18 (100.0%)|18/18 (100.0%)|0/18 (0.0%)|0|0|36/36 (100.0%)|
|G_eq|36|9/18 (50.0%)|9/18 (50.0%)|0/18 (0.0%)|17|1|33/36 (91.7%)|

In E1, automatic acceptance accepted both positive units (6/6) and negative units (6/6). LLM review retained appropriate acceptance for 18/18 positive units and explicitly deferred 3 of 18 negative units. The inappropriate negative-acceptance rate therefore decreased from 100.0% to 83.3%, a difference of 16.7 percentage points. However, all three deferrals were repetitions of one external-supply context in settlement-batch, and none included structured counter-evidence citations. Directory COPY was again proposed in the final plans after deferral. The result therefore concerns a limited primary-stage deferral effect and cannot be extended to improved safety of the complete agent.

In E2, B_eq, which received no additional candidates, passed the primary contract in 36/36 units but also accepted 18/18 negative units. G_eq, which received candidates, produced 18 effective accepts, 17 contract blockages, and 1 API failure, with no explicit deferrals. All 17 blockages involved Dockerfile insertion-line numbers, with returned numbers matching the numeric portions of the last COPY's SC identifiers. Including fallback analysis, final validity was 36/36 for B_eq and 33/36 for G_eq. No comparative advantage from providing additional candidates was established under these conditions.

|Condition|Execution-eligible|Executed|Recovery / scheduled|Recovery / executed|Not executed: validation failure|Not executed: API failure|
|---|---|---|---|---|---|---|
|R_auto|3|3|3/3 (100.0%)|3/3 (100.0%)|0|0|
|R_llm|9|9|9/9 (100.0%)|9/9 (100.0%)|0|0|
|B_eq|9|9|9/9 (100.0%)|9/9 (100.0%)|0|0|
|G_eq|9|6|6/9 (66.7%)|6/6 (100.0%)|2|1|

Execution evaluation was limited to 30 P_supported rows. Plans were applied and recovery confirmed in 27 rows; 2 final-validation failures and 1 API timeout prevented execution in the remaining rows. Recovery was therefore 27/30 (90.0%) over scheduled eligible rows and 27/27 (100.0%) conditional on execution. Final validity was 117/120 (97.5%) for both original responses and limited normalization, and recovery outcomes were identical. Normalized evaluation reused original executions for identical patches, so these were not counted as independent successes.

G_eq increased total calls from B_eq's 36 to 53. Observed input and output tokens were respectively 2.07 and 3.66 times as high, with usage for the timed-out call retained as missing. These costs combine candidate-input volume and fallback calls; they are not end-to-end system latency or monetary cost.

The frozen COPY indicator for negative contexts could miss directory copies whose destination lacked a trailing slash. A post hoc supplementary audit found COPY code in at least one final plan for every negative response in every condition. This observation does not evaluate actual application or harm; it shows why non-detection by the original indicator cannot be interpreted as safety success. Results are confined to one model, 3 base incidents, synthetic contexts, non-independent repetitions, post hoc contract clarification, and no recorded human reference approval. No claim of statistical significance or generalization to other projects or error types is made.

