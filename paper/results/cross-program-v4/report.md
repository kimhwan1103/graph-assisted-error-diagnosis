# Additional cross-program experiment results

The scheduled evaluation comprises 3 author-built Node.js programs, 9 scenarios, and 3 repetitions per condition, for 54 units. This is a development evaluation without recorded human reference approval. Original responses form the primary analysis; limited normalization is a supplementary analysis fixed before execution.

|Responses|Program|Method|Valid responses|Target/location match|Recognition of normal cases|Unnecessary changes|Calls|Input tokens (observed sum)|Output tokens (observed sum)|Mean pipeline ms|
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
|original|settlement-batch|adaptive|3/9|0/6|3/3|0/3|9|64155|10248|33267.444444432236|
|original|settlement-batch|graph_compact|6/9|3/6|3/3|0/3|9|51690|6555|21670.222222222947|
|original|shipping-cli|adaptive|3/9|0/6|3/3|0/3|9|64137|10071|32638.777777798372|
|original|shipping-cli|graph_compact|6/9|3/6|3/3|0/3|9|51540|6114|20515.55555557004|
|original|template-http|adaptive|3/9|0/6|3/3|0/3|9|64122|10953|35317.77777775036|
|original|template-http|graph_compact|6/9|3/6|3/3|0/3|9|51540|7659|25149.22222224737|
|normalized|settlement-batch|adaptive|9/9|3/6|3/3|0/3|9|64155|10248|33267.444444432236|
|normalized|settlement-batch|graph_compact|9/9|3/6|3/3|0/3|9|51690|6555|21670.222222222947|
|normalized|shipping-cli|adaptive|9/9|3/6|3/3|0/3|9|64137|10071|32638.777777798372|
|normalized|shipping-cli|graph_compact|9/9|3/6|3/3|0/3|9|51540|6114|20515.55555557004|
|normalized|template-http|adaptive|9/9|3/6|3/3|0/3|9|64122|10953|35317.77777775036|
|normalized|template-http|graph_compact|9/9|3/6|3/3|0/3|9|51540|7659|25149.22222224737|

Target/location matching checks the mentioned target path and the structural agreement of cited source locations; it is not a semantic diagnosis-accuracy score. Counts of calls with missing token usage and observed values are stored in the JSON/CSV records. Costs in normalized rows refer to the same original execution and must not be added again.

|Responses|Program|Scenario|Method|Scheduled|Applied|Functional pass|Regression pass|Preservation pass|Recovered|Unevaluated|
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
|original|settlement-batch|A|adaptive|3|0|0|0|0|0|0|
|original|settlement-batch|A|graph_compact|3|3|3|3|3|3|0|
|original|settlement-batch|C|adaptive|3|0|0|0|0|0|0|
|original|settlement-batch|C|graph_compact|3|0|0|0|0|0|0|
|original|shipping-cli|A|adaptive|3|0|0|0|0|0|0|
|original|shipping-cli|A|graph_compact|3|3|3|3|3|3|0|
|original|shipping-cli|C|adaptive|3|0|0|0|0|0|0|
|original|shipping-cli|C|graph_compact|3|0|0|0|0|0|0|
|original|template-http|A|adaptive|3|0|0|0|0|0|0|
|original|template-http|A|graph_compact|3|3|3|3|3|3|0|
|original|template-http|C|adaptive|3|0|0|0|0|0|0|
|original|template-http|C|graph_compact|3|0|0|0|0|0|0|
|normalized|settlement-batch|A|adaptive|3|0|0|0|0|0|0|
|normalized|settlement-batch|A|graph_compact|3|3|3|3|3|3|0|
|normalized|settlement-batch|C|adaptive|3|0|0|0|0|0|0|
|normalized|settlement-batch|C|graph_compact|3|0|0|0|0|0|0|
|normalized|shipping-cli|A|adaptive|3|0|0|0|0|0|0|
|normalized|shipping-cli|A|graph_compact|3|3|3|3|3|3|0|
|normalized|shipping-cli|C|adaptive|3|3|3|3|3|3|0|
|normalized|shipping-cli|C|graph_compact|3|3|3|3|3|3|0|
|normalized|template-http|A|adaptive|3|0|0|0|0|0|0|
|normalized|template-http|A|graph_compact|3|3|3|3|3|3|0|
|normalized|template-http|C|adaptive|3|0|0|0|0|0|0|
|normalized|template-http|C|graph_compact|3|0|0|0|0|0|0|

## Failures and route transitions

- settlement-batch / adaptive: Routes {"adaptive": 9}, reasons {}, response errors {"INVALID_SOURCE_RESULT": 6}
- settlement-batch / graph_compact: Routes {"compact_accepted": 3, "early_general": 6}, reasons {"accepted": 3, "no_graph_attempt_recorded": 6}, response errors {"INVALID_SOURCE_RESULT": 3}
- shipping-cli / adaptive: Routes {"adaptive": 9}, reasons {}, response errors {"INVALID_SOURCE_RESULT": 6}
- shipping-cli / graph_compact: Routes {"early_general": 6, "compact_accepted": 3}, reasons {"no_graph_attempt_recorded": 6, "accepted": 3}, response errors {"INVALID_SOURCE_RESULT": 3}
- template-http / adaptive: Routes {"adaptive": 9}, reasons {}, response errors {"INVALID_SOURCE_RESULT": 6}
- template-http / graph_compact: Routes {"compact_accepted": 3, "early_general": 6}, reasons {"accepted": 3, "no_graph_attempt_recorded": 6}, response errors {"INVALID_SOURCE_RESULT": 3}
- original / adaptive recovery evaluation outcomes: {"contract_failed": 18, "no_change_correct": 9}
- original / graph_compact recovery evaluation outcomes: {"restored": 9, "no_change_correct": 9, "contract_failed": 9}
- normalized / adaptive recovery evaluation outcomes: {"ambiguous_plan": 12, "no_change_correct": 9, "restored": 3, "unsupported_plan": 3}
- normalized / graph_compact recovery evaluation outcomes: {"restored": 12, "no_change_correct": 9, "ambiguous_plan": 6}

## Interpretation scope

Successful recovery counts require all functional, regression, and preservation checks to pass with the specified COPY adapter and fixed container environment. The overall recovery rate is not established for conditions with unevaluated units. Cases where both methods succeed demonstrate execution feasibility, not the superiority of the graph method.

The new programs do not represent naturally occurring failures in external public projects. Repetitions and syntactic variants of 3 base incidents must not be interpreted as 54 independent incidents. General-analysis fallbacks remain part of the graph method's original condition. Automatic evidence checks do not replace assessment of semantic sufficiency or human review.

Raw responses, calls, actual routes, patches, check results, and hashes are linked through cells.json/csv and run folders in the preserved original archive. The original deviations.md and reproduction.md contain preparation and verification history and the commands used for that run. This public release provides aggregate tables and excludes those original run records. See the [reproducibility notes](../../../docs/REPRODUCIBILITY.md) for the checks supported by this release.
