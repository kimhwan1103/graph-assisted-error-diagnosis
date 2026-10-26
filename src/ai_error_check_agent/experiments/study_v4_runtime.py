"""Durable, paced model calls for the real diagnosis Runtime protocol."""

import asyncio
import json
import time
from types import SimpleNamespace

from ..errors import DiagnosisError
from ..runtime import ModelResponse
from .backends import BackendFailure


def mock_answer(schema):
    if "decision" in schema.get("properties", {}):
        response = {
            "decision": "defer",
            "plan": "defer",
            "summary": "모의 실행입니다.",
            "uncertainty": "실제 모델 판단이 필요합니다.",
            "counter_evidence_ids": [],
        }
        if "candidate_id" in schema["properties"]:
            response["candidate_id"] = "C1"
        else:
            response.update(
                target="",
                code_path="",
                code_line=1,
                evidence_ids=[],
                source_evidence_ids=[],
                packaging=None,
            )
        return response
    response = {
        "analysis_status": "insufficient_evidence",
        "summary": "실행 흐름을 확인하는 모의 응답입니다.",
        "observations": [],
        "hypotheses": [],
        "next_checks": [
            {
                "id": "C1",
                "target": "실제 모델 검토",
                "method": "실제 모델로 같은 입력을 검토합니다.",
                "purpose": "모의 실행 이후 진단 품질을 평가합니다.",
                "hypothesis_ids": [],
            }
        ],
        "missing_information": [
            {
                "requested_data": "실제 모델 검토",
                "reason": "모의 응답은 진단 품질을 평가하지 않습니다.",
            }
        ],
        "limitations": ["실제 모델을 호출하지 않았습니다."],
        "remediation": {
            "status": "needs_more_evidence",
            "reason": "실제 모델 검토가 필요합니다.",
            "plans": [],
        },
    }
    if "source_findings" in schema.get("properties", {}):
        response["source_findings"] = {"findings": []}
    if "source_request" in schema.get("properties", {}):
        response["source_request"] = {
            "needed": False,
            "reason": "모의 실행을 완료했습니다.",
            "files": [],
        }
    return response


class Session:
    def __init__(self, output, backend, config, save, read, *, live):
        self.output, self.backend, self.config = output, backend, config
        self.save, self.read, self.live = save, read, live
        self.last_dispatch = 0.0
        self.calls = 0
        for path in (output / "cells").glob("*/calls/*.json"):
            call = read(path)
            if call.get("dispatched"):
                self.calls += 1
                self.last_dispatch = max(self.last_dispatch, call.get("dispatch_time", 0))

    def factory(self, cell, seed):
        return lambda: StudyRuntime(self, cell, seed)


class StudyRuntime:
    runtime_version = "study-v4/native-gemini.v1"
    cleanup_status = "not_needed"
    abort_confirmed = None
    reusable = True

    def __init__(self, session, cell, seed):
        self.session, self.cell, self.seed = session, cell, seed
        c = session.config
        self.profile = SimpleNamespace(
            profile_id="profile-demo-a",
            provider_id="google" if session.live else "mock",
            model_id=c.model_id,
            timeout_seconds=c.timeout_seconds + c.request_interval_seconds + 5,
            max_evidence_bytes=c.max_evidence_bytes,
            max_prompt_bytes=c.max_prompt_bytes,
        )
        self.message_submissions = self.provider_call_count = 0
        self.task = None

    async def run(self, prompt, data, schema):
        s = self.session
        calls_dir = self.cell / "calls"
        calls_dir.mkdir(parents=True, exist_ok=True)
        path = calls_dir / f"{len(list(calls_dir.glob('*.json'))):02d}.json"
        record = {
            "prompt": prompt,
            "input": data,
            "schema": schema,
            "seed": self.seed,
            "status": "prepared",
            "dispatched": False,
            "real_model": s.live,
        }
        s.save(path, record)
        self.message_submissions = 1
        if not s.live:
            raw = {
                "text": json.dumps(mock_answer(schema), ensure_ascii=False),
                "input_tokens": None,
                "output_tokens": None,
                "thinking_tokens": None,
                "finish_reason": "mock",
                "provider_calls": 0,
            }
            record.update(status="completed", raw=raw, network_ms=None, queue_ms=None)
            s.save(path, record)
        else:
            if s.calls >= s.config.max_calls:
                raise DiagnosisError("STUDY_CALL_BUDGET", "연구 호출 한도에 도달했습니다.")
            queued = time.monotonic()
            wait = max(0, s.config.request_interval_seconds - (time.time() - s.last_dispatch))
            if wait:
                await asyncio.sleep(wait)
            record.update(
                status="in_flight",
                dispatched=True,
                dispatch_time=time.time(),
                queue_ms=(time.monotonic() - queued) * 1000,
            )
            s.save(
                path, record
            )  # Before dispatch: an ambiguous crash is NEVER automatically retried.
            s.calls += 1
            s.last_dispatch = record["dispatch_time"]
            self.provider_call_count = 1

            def generate():
                started = time.monotonic()
                try:
                    raw = s.backend.generate(prompt, data, self.seed, schema=schema)
                    record.update(status="completed", raw=raw)
                    return raw
                except BackendFailure as exc:
                    record.update(status="failed", error=str(exc))
                    raise
                finally:
                    record["network_ms"] = (time.monotonic() - started) * 1000
                    s.save(
                        path, record
                    )  # Persist raw response before downstream parsing/validation.

            self.task = asyncio.create_task(asyncio.to_thread(generate))
            try:
                raw = await asyncio.shield(self.task)
            except BackendFailure as exc:
                raise DiagnosisError(
                    str(exc).split(":", 1)[0],
                    "모델 API 호출이 실패했습니다. 호출 기록을 확인하세요.",
                ) from None
        text = raw["text"]
        # Only exact fenced JSON is normalized; the original response remains on disk.
        normalized = text.strip()
        if normalized.startswith("```json\n") and normalized.endswith("\n```"):
            text = normalized[8:-4]
        record["normalization"] = "exact_json_fence" if text != raw["text"] else "none"
        s.save(path, record)
        if s.live and raw.get("finish_reason") != "stop":
            raise DiagnosisError("MODEL_NON_STOP", "모델 응답이 정상 종료되지 않았습니다.")
        return ModelResponse(
            text,
            {
                "tokens": {
                    "input": raw.get("input_tokens"),
                    "output": raw.get("output_tokens"),
                    "reasoning": raw.get("thinking_tokens"),
                },
                "reported_model": {
                    "provider_id": "google",
                    "model_id": raw.get("model_version") or s.config.model_id,
                }
                if s.live
                else None,
                "verification": "reported" if s.live else "not_dispatched",
                "cost": None,
            },
        )

    async def close(self):
        if self.task is not None:
            # A cancelled caller must not release its worker while a provider request is still active.
            try:
                await asyncio.shield(self.task)
            except BackendFailure:
                pass
