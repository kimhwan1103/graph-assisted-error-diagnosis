# v4 해결안 자동 실행 평가

상태: **complete** · 응답: **original** · 실행 요청: **True**

평가 범위는 지원된 COPY 계획 표현과 고정 재현 환경이다. 구조 검증 통과를 복구 성공으로 세지 않는다.
사례 4개, incident 4개, 계보 1개, 셀 8개. 분류 시점: `post_hoc_development_before_replay_not_before_generation`.

## 수정 가능한 사례

| 조건 | 예정 | 완료 | 미판정 | 응답 유효 | 적용률 | 완전 복구율 | 적용 후 복구율 | 회귀 / 검사 완료 | 회귀 미검사 적용 셀 |
|---|---:|---:|---:|---|---|---|---|---|---:|
| adaptive | 1 | 1 | 0 | 0/1 (0.0%) | 0/1 (0.0%) | 0/1 (0.0%) | N/A (0/0) | N/A (0/0) | 0 |
| graph_compact | 1 | 1 | 0 | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 1/1 (100.0%) | 0/1 (0.0%) | 0 |

## 무변경 및 정보 부족

| 조건 | 무변경 셀 | 불필요한 제안 / 전체 | 불필요한 제안 / 유효 | 적절한 무변경 | 무변경 응답 실패 | 정보 부족 셀 | 구조적 보류 | 정보 부족 응답 실패 |
|---|---:|---|---|---|---:|---:|---|---:|
| adaptive | 2 | 0/2 (0.0%) | N/A (0/0) | 0/2 (0.0%) | 2 | 1 | 0/1 (0.0%) | 1 |
| graph_compact | 2 | 0/2 (0.0%) | N/A (0/0) | 0/2 (0.0%) | 2 | 1 | 0/1 (0.0%) | 1 |

## 대응 비교와 실행 범위

```json
{
  "both_restored": 0,
  "adaptive_only": 0,
  "graph_compact_only": 1,
  "neither_restored": 0,
  "unresolved": 0,
  "missing_comparator": 0
}
```

실제 에이전트 계획 재현 시도: 1회. 캐시 공유: 0회. 신규 모델 호출: 0회.
사전 참조 수정과 평가기 회귀 테스트는 에이전트 복구 성공에 포함하지 않는다. 같은 장애의 여러 조건은 독립 사례 수를 늘리지 않는다.
dry-run과 인프라 오류의 미실행 검사는 null이다. 미판정 셀이 있으면 완전 복구율을 확정하지 않으며, 예정 분모에 대한 확인된 성공만 하한으로 표시한다.
원본과 정규화는 별도 평가다. 이 실행 시간은 모델의 기존 응답 시간이 아니며, 정규화 후 시스템의 새 토큰·지연 측정도 아니다.
실제 모델 입력의 소스 범위가 조건별로 다를 수 있다. 셀별 model_visible_inputs와 fixture 조건을 확인해야 한다. 조건부 적용의 자연어 타당성은 자동 채점하지 않는다.

## 셀별 판정

| 셀 | 조건 | 사례 | 판정 | 이유 |
|---|---|---|---|---|
| f894e1251331da2e67fe | graph_compact | expected-test-error | contract_failed | INVALID_STATE |
| db948e9d9762db92dd16 | adaptive | expected-test-error | contract_failed | INVALID_STATE |
| 178fdccea9b853641c18 | adaptive | docker-copy-omission | contract_failed | INVALID_SOURCE_RESULT |
| 787f41c4608f3dcf48f8 | graph_compact | docker-copy-omission | restored |  |
| 4609d4dc1fcad0868f7b | adaptive | source-target-mismatch | contract_failed | INVALID_SOURCE_RESULT |
| 4251891de94fdebb7a14 | graph_compact | source-target-mismatch | contract_failed | INVALID_SOURCE_RESULT |
| b2026dfa6ca1089a2e4f | adaptive | recovered-same-stream | contract_failed | INVALID_SOURCE_RESULT |
| b96190f8a8db7afb6c96 | graph_compact | recovered-same-stream | contract_failed | INVALID_SOURCE_RESULT |

## 제외 사례

- missing-task-data: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- renamed-file: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- repeated-stack: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- direct-literal: Known source/log execution inconsistency
- other-stream-healthy: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- already-copied: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
- ignored-file: Outside v1 controlled COPY/no-change/context scope; no verified general file-supply repair
