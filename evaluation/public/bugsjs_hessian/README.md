# Historical pilot inputs used by API regression tests

`development.v2.json` contains three fixed cases used by `tests/test_gemma_api.py` to test API errors, retry prevention, and raw-response preservation with a fake HTTP client. They are not added to the programs or results of the latest contract-v2 experiment. These inputs are included to avoid external downloads during testing.

Sources, pinned commits, and selection procedures are recorded in the JSON provenance and `source/PROVENANCE.json`. The original MIT licenses for the included Hessian.js code and BugsJS material are preserved in `source/LICENSE.hessian` and `source/LICENSE.benchmark`. The full original ZIP archives mentioned in the provenance are not included in this compact release. These upstream licenses do not assign a new license to the repository as a whole.
