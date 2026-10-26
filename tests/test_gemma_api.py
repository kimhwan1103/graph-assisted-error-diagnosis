"""Hosted Gemma contract tests: MockTransport only, never keys or live API access."""

import copy
import json

import httpx
import pytest

from ai_error_check_agent.experiments import backends, run
from ai_error_check_agent.experiments.backends import (
    BackendFailure,
    GeminiBackend,
    MockBackend,
    VLLMBackend,
    gemini_api_key,
)
from ai_error_check_agent.experiments.design import (
    ARMS,
    GEMINI_BASE_URL,
    GEMINI_MODEL_ID,
    PUBLIC_PILOT,
    ROOT,
    Decision,
    ExperimentConfig,
    digest,
    load_cases,
)

KEY = "test-only-never-a-real-key"


@pytest.fixture
def config():
    data = json.loads((ROOT / "config/experiment.gemma4-api.json").read_text("utf-8"))
    data["request_interval_seconds"] = 0
    return ExperimentConfig.model_validate(data)


def metadata():
    return {
        "name": "models/" + GEMINI_MODEL_ID,
        "version": "test-version",
        "supportedGenerationMethods": ["generateContent"],
    }


def generated(config, finish="STOP"):
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "text": "not-a-decision"},
                        {"text": MockBackend(config).generate("", {}, 42)["text"]},
                    ]
                },
                "finishReason": finish,
            }
        ],
        "modelVersion": "gemma-4-31b-it-test-revision",
        "responseId": "test-response",
        "usageMetadata": {
            "promptTokenCount": 110,
            "candidatesTokenCount": 80,
            "thoughtsTokenCount": 2,
            "totalTokenCount": 192,
            "cachedContentTokenCount": 10,
        },
    }


def adapter(config, handler):
    return GeminiBackend(config, api_key=KEY, transport=httpx.MockTransport(handler))


def test_four_arms_preserve_shared_evidence_and_record_api_provenance(config, tmp_path):
    seen = []

    def handler(request):
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == KEY
        assert not request.url.query
        if request.method == "GET":
            assert request.url.path == "/v1beta/models/" + GEMINI_MODEL_ID
            return httpx.Response(200, json=metadata())
        assert request.url.path.endswith(GEMINI_MODEL_ID + ":generateContent")
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=generated(config))

    cases, datasets = load_cases([PUBLIC_PILOT])
    cases = copy.deepcopy(cases[:1])
    cases[0]["gold"] = {"secret": "GOLD_NOT_FOR_MODEL"}
    output = tmp_path / "fixture-not-real-inference"
    backend = adapter(config, handler)
    assert (
        run.run_experiment(cases, datasets, config, output, backend_name="gemini", backend=backend)
        == 0
    )
    assert backend.client.is_closed
    assert len(seen) == 4
    assert all(set(body) == {"contents", "systemInstruction", "generationConfig"} for body in seen)
    assert len({digest(body["generationConfig"]) for body in seen}) == 1
    assert seen[0]["generationConfig"] == {
        "maxOutputTokens": 4096,
        "temperature": 0.0,
        "seed": 42,
        "candidateCount": 1,
        "thinkingConfig": {"thinkingLevel": "minimal"},
    }
    assert "GOLD_NOT_FOR_MODEL" not in json.dumps(seen)
    schema = json.dumps(Decision.model_json_schema(), ensure_ascii=False)
    assert all(schema in body["systemInstruction"]["parts"][0]["text"] for body in seen)
    inputs = [
        json.loads(body["contents"][0]["parts"][0]["text"])["untrusted_evidence"] for body in seen
    ]
    graph = [data for data in inputs if "graph_hints" in data]
    raw = [data for data in inputs if "graph_hints" not in data]
    assert len(raw) == len(graph) == 2
    assert raw[0] == raw[1] == {k: v for k, v in graph[0].items() if k != "graph_hints"}
    assert graph[0] == graph[1]
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    assert manifest["provider_calls"] == 4 and manifest["state"] == "completed"
    assert manifest["config"]["model_id"] == GEMINI_MODEL_ID
    assert "not established" in manifest["server"]["identity_note"]
    for file in (output / "calls").glob("*.json"):
        call = json.loads(file.read_text("utf-8"))
        assert call["row"]["model_version"] == "gemma-4-31b-it-test-revision"
        assert call["row"]["output_tokens"] == 80
        assert call["row"]["thinking_tokens"] == 2
        assert call["row"]["total_tokens"] == 192
        assert call["row"]["cached_input_tokens"] == 10
        assert call["response"]["response_id"] == "test-response"
        assert "not-a-decision" not in call["response"]["text"]
    summary = json.loads((output / "summary.json").read_text("utf-8"))
    assert summary["model_identity"]["requested_model"] == GEMINI_MODEL_ID
    assert summary["groups"]["all"]["raw_long"]["semantic_accuracy"] is None
    assert KEY not in "".join(p.read_text("utf-8") for p in output.rglob("*") if p.is_file())


@pytest.mark.parametrize("status", [307, 400, 401, 403, 404, 429, 503])
def test_http_error_stops_without_retry_and_never_logs_body_or_key(config, tmp_path, status):
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=metadata())
        posts.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": KEY}},
            headers={"Location": "https://example.com/leak"},
        )

    cases, datasets = load_cases([PUBLIC_PILOT])
    output = tmp_path / "failed"
    assert (
        run.run_experiment(
            cases[:1],
            datasets,
            config,
            output,
            backend_name="gemini",
            backend=adapter(config, handler),
        )
        == 2
    )
    assert len(posts) == 1
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    assert manifest["state"] == "stopped_on_backend_error"
    assert manifest["completed_rows"] == manifest["provider_calls"] == 1
    call = json.loads((output / "calls/00000.json").read_text("utf-8"))
    assert call["row"]["error"] == f"MODEL_HTTP_{status}"
    assert KEY not in json.dumps([manifest, call])


@pytest.mark.parametrize(
    "data, error",
    [
        (
            {"name": "models/other", "supportedGenerationMethods": ["generateContent"]},
            "MODEL_ID_MISMATCH",
        ),
        ({"name": "models/" + GEMINI_MODEL_ID}, "MODEL_GENERATION_UNSUPPORTED"),
    ],
)
def test_preflight_failure_is_recorded_without_generation(config, tmp_path, data, error):
    seen = []

    def handler(request):
        seen.append(request.method)
        return httpx.Response(200, json=data)

    cases, datasets = load_cases([PUBLIC_PILOT])
    output = tmp_path / "preflight-failed"
    with pytest.raises(BackendFailure, match=error):
        run.run_experiment(
            cases[:1],
            datasets,
            config,
            output,
            backend_name="gemini",
            backend=adapter(config, handler),
        )
    assert seen == ["GET"]
    manifest = json.loads((output / "manifest.json").read_text("utf-8"))
    assert manifest["state"] == "stopped_on_backend_error" and manifest["error"] == error
    assert manifest["provider_calls"] == 0


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY"])
def test_truncation_and_blocking_are_invalid_even_with_parseable_json(config, tmp_path, finish):
    def handler(request):
        return httpx.Response(
            200, json=metadata() if request.method == "GET" else generated(config, finish)
        )

    cases, datasets = load_cases([PUBLIC_PILOT])
    output = tmp_path / "truncated"
    assert (
        run.run_experiment(
            cases[:1],
            datasets,
            config,
            output,
            backend_name="gemini",
            backend=adapter(config, handler),
        )
        == 2
    )
    calls = [json.loads(p.read_text("utf-8")) for p in (output / "calls").glob("*.json")]
    assert len(calls) == 4
    assert all(not c["row"]["contract_valid"] and c["row"]["output_tokens"] == 80 for c in calls)
    assert all(c["response"]["provider_finish_reason"] == finish for c in calls)
    assert all(c["row"]["validation_error"] == "NON_STOP_FINISH" for c in calls)


def test_fence_normalization_preserves_raw_output_and_is_reported(config, tmp_path):
    response = generated(config)
    original = response["candidates"][0]["content"]["parts"][1]["text"]
    wrapped = "```json\n" + original + "\n```"
    response["candidates"][0]["content"]["parts"][1]["text"] = wrapped

    def handler(request):
        return httpx.Response(200, json=metadata() if request.method == "GET" else response)

    cases, datasets = load_cases([PUBLIC_PILOT])
    output = tmp_path / "fenced"
    assert (
        run.run_experiment(
            cases[:1],
            datasets,
            config,
            output,
            backend_name="gemini",
            backend=adapter(config, handler),
        )
        == 0
    )
    calls = [json.loads(p.read_text("utf-8")) for p in (output / "calls").glob("*.json")]
    assert len(calls) == 4
    assert all(c["row"]["validation_error"] is None for c in calls)
    assert all(c["row"]["format_normalized"] and not c["row"]["raw_json_valid"] for c in calls)
    assert all(c["response"]["text"] == wrapped for c in calls)
    assert all(c["validation_text"] == original for c in calls)
    assert all(c["row"]["provider_calls"] == 1 for c in calls)
    summary = json.loads((output / "summary.json").read_text("utf-8"))
    assert all(g["raw_contract_valid_n"] == 0 for g in summary["groups"]["all"].values())
    assert all(g["format_normalized_n"] == 1 for g in summary["groups"]["all"].values())


def test_prompt_block_preserves_usage_and_missing_token_counts_stay_null(config):
    backend = adapter(
        config,
        lambda _: httpx.Response(
            200,
            json={
                "promptFeedback": {"blockReason": "SAFETY"},
                "usageMetadata": {"promptTokenCount": 30},
            },
        ),
    )
    try:
        response = backend.generate("prompt", {}, 42)
    finally:
        backend.close()
    assert response["finish_reason"] == "prompt_blocked"
    assert response["text"] == "" and response["input_tokens"] == 30
    assert response["output_tokens"] is None and response["total_tokens"] is None


@pytest.mark.parametrize(
    "body",
    [[], {"candidates": "bad"}, {"candidates": [None]}, {"candidates": [{"content": "bad"}]}],
)
def test_malformed_response_fails_cleanly(config, body):
    backend = adapter(config, lambda _: httpx.Response(200, json=body))
    try:
        with pytest.raises(BackendFailure, match="MODEL_"):
            backend.generate("prompt", {}, 42)
    finally:
        backend.close()
    assert backend.calls_started == 1


def test_timeout_is_sanitized_and_prompt_budget_prevents_transmission(config):
    def handler(request):
        raise httpx.ReadTimeout(KEY, request=request)

    backend = adapter(config, handler)
    try:
        with pytest.raises(BackendFailure, match="^MODEL_TIMEOUT$"):
            backend.generate("prompt", {}, 42)
        with pytest.raises(BackendFailure, match="PROMPT_BUDGET_EXCEEDED"):
            backend.generate("x" * config.max_prompt_bytes, {}, 42)
    finally:
        backend.close()
    assert backend.calls_started == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://generativelanguage.googleapis.com/v1beta",
        GEMINI_BASE_URL + "?key=x",
        "https://example.com/v1beta",
    ],
)
def test_key_cannot_be_sent_to_non_google_or_overridden_url(config, url):
    with pytest.raises(ValueError, match="official Google HTTPS"):
        GeminiBackend(config.model_copy(update={"base_url": url}), api_key=KEY)


def test_qat_and_hosted_model_ids_cannot_be_mislabeled(config):
    with pytest.raises(ValueError, match="exact QAT"):
        VLLMBackend(config)
    with pytest.raises(ValueError, match="model_id=gemma-4-31b-it"):
        GeminiBackend(ExperimentConfig(base_url=GEMINI_BASE_URL), api_key=KEY)


def test_keys_are_loaded_without_mutating_environment_or_interpolating(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "unrelated-production-key")
    path = tmp_path / ".env"
    with pytest.raises(BackendFailure, match="GEMINI_API_KEY_MISSING"):
        gemini_api_key(path)
    path.write_text("GEMINI_API_KEY=${LITERAL_NOT_EXPANDED}\n", encoding="utf-8-sig")
    assert gemini_api_key(path) == "${LITERAL_NOT_EXPANDED}"
    path.write_text("GOOGLE_API_KEY=file-google\nGEMINI_API_KEY=file-gemini\n", encoding="utf-8")
    assert gemini_api_key(path) == "file-google"
    monkeypatch.setenv("GEMINI_API_KEY", "process-gemini")
    assert gemini_api_key(path) == "process-gemini"
    monkeypatch.setenv("GOOGLE_API_KEY", "process-google")
    assert gemini_api_key(path) == "process-google"


def test_missing_key_creates_no_run_and_sends_no_request(config, tmp_path, monkeypatch):
    def missing():
        raise BackendFailure("GEMINI_API_KEY_MISSING")

    monkeypatch.setattr(backends, "gemini_api_key", missing)
    cases, datasets = load_cases([PUBLIC_PILOT])
    output = tmp_path / "missing"
    with pytest.raises(BackendFailure, match="GEMINI_API_KEY_MISSING"):
        run.run_experiment(cases[:1], datasets, config, output, backend_name="gemini")
    assert not output.exists()


def test_api_cli_selects_its_config_without_calling_network(tmp_path, monkeypatch):
    seen = []

    def fake_run(cases, datasets, config, output, *, backend_name):
        seen.append((cases, config, backend_name))
        return 0

    monkeypatch.setattr(run, "run_experiment", fake_run)
    assert (
        run.main(["--backend", "gemini", "--limit", "1", "--output", str(tmp_path / "unused")]) == 0
    )
    cases, config, name = seen[0]
    assert len(cases) == 1 and name == "gemini"
    assert config.model_id == GEMINI_MODEL_ID and config.warmups == 0 and config.max_calls == 12


def test_pacing_is_not_counted_as_inference_latency(config, tmp_path, monkeypatch):
    now, waits = [100.0], []

    def clock():
        now[0] += 0.001
        return now[0]

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(run.time, "perf_counter", clock)
    monkeypatch.setattr(run.time, "sleep", sleep)

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=metadata())
        now[0] += 0.040
        return httpx.Response(200, json=generated(config))

    cases, datasets = load_cases([PUBLIC_PILOT])
    config = config.model_copy(update={"request_interval_seconds": 15.0})
    output = tmp_path / "pacing"
    assert (
        run.run_experiment(
            cases[:1],
            datasets,
            config,
            output,
            backend_name="gemini",
            backend=adapter(config, handler),
        )
        == 0
    )
    calls = [json.loads(p.read_text("utf-8")) for p in sorted((output / "calls").glob("*.json"))]
    assert len(waits) == 3 and all(wait > 14 for wait in waits)
    assert calls[0]["row"]["request_wait_ms"] == 0
    assert all(c["row"]["request_wait_ms"] > 14000 for c in calls[1:])
    assert all(c["row"]["request_ms"] < 100 and c["row"]["end_to_end_ms"] < 1000 for c in calls)
    assert {c["row"]["arm"] for c in calls} == {a.name for a in ARMS}
