"""Dry runs, local QAT inference, and hosted Gemma through Google's Gemini API."""

import json
import os
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

from .design import GEMINI_BASE_URL, GEMINI_MODEL_ID, MODEL_ID, ROOT, Decision


class BackendFailure(Exception):
    pass


class MockBackend:
    is_mock = True

    def __init__(self, config):
        self.config = config
        self.calls_started = 0

    def preflight(self):
        return {"backend": "mock", "real_model": False, "tokens": "not measured"}

    def generate(self, prompt, payload, seed):
        # No reference answers, fabricated token counts, or accuracy claim.
        answer = {
            "analysis_status": "insufficient_evidence",
            "statement": "모의 응답으로 실행 흐름만 확인합니다.",
            "evidence_ids": [],
            "source_evidence_ids": [],
            "counter_evidence_ids": [],
            "target": None,
            "source_path": None,
            "source_line": None,
            "summary": "실제 모델을 호출하지 않은 모의 실행입니다.",
            "uncertainty": "진단 정확도와 모델 지연은 측정되지 않았습니다.",
            "next_check": "GPU 환경에서 같은 네 조건을 실제 모델로 평가해야 합니다.",
        }
        return {
            "text": json.dumps(answer, ensure_ascii=False),
            "input_tokens": None,
            "output_tokens": None,
            "finish_reason": "mock",
            "provider_calls": 0,
        }

    def close(self):
        pass


class _HTTPBackend:
    def _request(self, method, path, **kwargs):
        try:
            with self.client.stream(method, path, **kwargs) as response:
                if response.status_code >= 300 or response.status_code < 200:
                    raise BackendFailure(f"MODEL_HTTP_{response.status_code}")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise BackendFailure("MODEL_RESPONSE_TOO_LARGE")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise BackendFailure("MODEL_INVALID_RESPONSE")
            return data
        except httpx.TimeoutException:
            raise BackendFailure("MODEL_TIMEOUT") from None
        except (httpx.HTTPError, ValueError):
            raise BackendFailure("MODEL_CONNECTION_OR_JSON_ERROR") from None

    def close(self):
        self.client.close()


class VLLMBackend(_HTTPBackend):
    is_mock = False

    def __init__(self, config, *, transport=None):
        if config.model_id != MODEL_ID:
            raise ValueError("vllm experiments require the exact QAT model_id")
        parsed = urlparse(config.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/v1"
        ):
            raise ValueError("Experiments require a dedicated loopback /v1 server")
        self.config = config
        self.calls_started = 0
        self.client = httpx.Client(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=config.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def preflight(self):
        data = self._request("GET", "models")
        models = [item.get("id") for item in data.get("data", [])]
        if self.config.model_id not in models:
            raise BackendFailure("MODEL_ID_MISMATCH")
        return {
            "backend": "vllm_chat_completions",
            "real_model": True,
            "served_models": models,
            "identity_note": "served alias checked; attach server weight provenance separately",
        }

    def generate(self, prompt, payload, seed):
        schema = Decision.model_json_schema()
        system = prompt + "\n[공통 출력 JSON Schema]\n" + json.dumps(schema, ensure_ascii=False)
        user = json.dumps({"untrusted_evidence": payload}, ensure_ascii=False)
        if len((system + user).encode("utf-8")) > self.config.max_prompt_bytes:
            raise BackendFailure("PROMPT_BUDGET_EXCEEDED")
        body = {
            "model": self.config.model_id,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "seed": seed,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": self.config.enable_thinking},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "iris_factorial_decision",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        self.calls_started += 1
        data = self._request("POST", "chat/completions", json=body)
        choices = data.get("choices", [])
        if len(choices) != 1 or not isinstance(choices[0].get("message", {}).get("content"), str):
            raise BackendFailure("MODEL_MISSING_CONTENT")
        usage = data.get("usage") or {}
        return {
            "text": choices[0]["message"]["content"],
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "finish_reason": choices[0].get("finish_reason"),
            "provider_calls": 1,
        }


def gemini_api_key(env_file=None):
    """Read only the research key; never reuse a production LLM/OpenAI credential."""
    # The process environment wins over the project's explicit .env file.
    # Within each source, match Google's GOOGLE_API_KEY precedence.
    names = ("GOOGLE_API_KEY", "GEMINI_API_KEY")
    for name in names:
        if value := os.getenv(name, "").strip():
            return value
    values = dotenv_values(env_file or ROOT / ".env", interpolate=False, encoding="utf-8-sig")
    for name in names:
        if value := (values.get(name) or "").strip():
            return value
    raise BackendFailure("GEMINI_API_KEY_MISSING: set GEMINI_API_KEY in the project .env")


class GeminiBackend(_HTTPBackend):
    """Native generateContent API. API alias is not proof of QAT weight identity."""

    is_mock = False

    def __init__(self, config, *, api_key=None, transport=None):
        if config.model_id != GEMINI_MODEL_ID:
            raise ValueError("gemini experiments require model_id=gemma-4-31b-it")
        if config.base_url.rstrip("/") != GEMINI_BASE_URL:
            raise ValueError("gemini experiments require the official Google HTTPS endpoint")
        key = api_key if api_key is not None else gemini_api_key()
        if not key or not key.strip() or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise BackendFailure("GEMINI_API_KEY_INVALID")
        self.config = config
        self.calls_started = 0
        self.client = httpx.Client(
            base_url=GEMINI_BASE_URL + "/",
            headers={"x-goog-api-key": key},
            timeout=config.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def preflight(self):
        data = self._request("GET", f"models/{self.config.model_id}")
        if data.get("name") != f"models/{self.config.model_id}":
            raise BackendFailure("MODEL_ID_MISMATCH")
        if "generateContent" not in (data.get("supportedGenerationMethods") or []):
            raise BackendFailure("MODEL_GENERATION_UNSUPPORTED")
        return {
            "backend": "google_gemini_generate_content",
            "real_model": True,
            "model_name": data["name"],
            "model_version": data.get("version"),
            "identity_note": "Hosted API alias; exact weights/quantization/hardware not verified. Kaggle QAT identity is not established.",
            "output_enforcement": "schema in prompt; strict local validation; exact JSON code-fence normalization recorded separately",
            "thinking_level": "high" if self.config.enable_thinking else "minimal",
            "seed_note": "seed is requested; deterministic hosted inference is not guaranteed",
        }

    def request_body(self, prompt, payload, seed, *, schema=None):
        schema = Decision.model_json_schema() if schema is None else schema
        system = prompt + "\n[공통 출력 JSON Schema]\n" + json.dumps(schema, ensure_ascii=False)
        user = json.dumps({"untrusted_evidence": payload}, ensure_ascii=False)
        if len((system + user).encode("utf-8")) > self.config.max_prompt_bytes:
            raise BackendFailure("PROMPT_BUDGET_EXCEEDED")
        settings = {
            "temperature": self.config.temperature,
            "maxOutputTokens": self.config.max_tokens,
            "seed": seed,
            "candidateCount": 1,
            "thinkingConfig": {
                "thinkingLevel": "high" if self.config.enable_thinking else "minimal"
            },
        }
        # Native JSON/schema modes passed tiny probes but returned HTTP 500 for
        # the actual experiment request. Use the same prompt schema in all arms.
        # Do not silently retry with different settings if a request is rejected.
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": settings,
        }

    def count_tokens(self, prompt, payload, seed, *, schema=None):
        body = self.request_body(prompt, payload, seed, schema=schema)
        data = self._request(
            "POST",
            f"models/{self.config.model_id}:countTokens",
            json={"generateContentRequest": {"model": f"models/{self.config.model_id}", **body}},
        )
        count = data.get("totalTokens")
        if type(count) is not int or count < 1:
            raise BackendFailure("MODEL_INVALID_TOKEN_COUNT")
        return count

    def generate(self, prompt, payload, seed, *, schema=None):
        body = self.request_body(prompt, payload, seed, schema=schema)
        settings = body["generationConfig"]
        self.calls_started += 1
        data = self._request("POST", f"models/{self.config.model_id}:generateContent", json=body)
        candidates = data.get("candidates") or []
        feedback = data.get("promptFeedback") or {}
        usage = data.get("usageMetadata") or {}
        if (
            not isinstance(candidates, list)
            or not isinstance(feedback, dict)
            or not isinstance(usage, dict)
        ):
            raise BackendFailure("MODEL_INVALID_RESPONSE")
        if not candidates and feedback.get("blockReason"):
            text, finish = "", "PROMPT_BLOCKED"
        else:
            if len(candidates) != 1 or not isinstance(candidates[0], dict):
                raise BackendFailure("MODEL_MISSING_CONTENT")
            candidate = candidates[0]
            content = candidate.get("content") or {}
            if not isinstance(content, dict):
                raise BackendFailure("MODEL_INVALID_RESPONSE")
            parts = content.get("parts") or []
            if not isinstance(parts, list) or any(not isinstance(part, dict) for part in parts):
                raise BackendFailure("MODEL_INVALID_RESPONSE")
            # Thought text/signatures must never be parsed as the decision.
            text = "".join(
                part["text"]
                for part in parts
                if not part.get("thought") and isinstance(part.get("text"), str)
            )
            finish = candidate.get("finishReason", "UNKNOWN")
        if not isinstance(finish, str):
            raise BackendFailure("MODEL_INVALID_RESPONSE")

        def tokens(name):
            value = usage.get(name)
            return value if type(value) is int and value >= 0 else None

        return {
            "text": text,
            "input_tokens": tokens("promptTokenCount"),
            "output_tokens": tokens("candidatesTokenCount"),
            "thinking_tokens": tokens("thoughtsTokenCount"),
            "total_tokens": tokens("totalTokenCount"),
            "cached_input_tokens": tokens("cachedContentTokenCount"),
            "finish_reason": "stop" if finish == "STOP" else finish.lower(),
            "provider_finish_reason": finish,
            "provider_calls": 1,
            "requested_model": self.config.model_id,
            "model_version": data.get("modelVersion"),
            "response_id": data.get("responseId"),
            "generation_config": settings,
            "usage_metadata": usage,
            "prompt_feedback": feedback,
        }
