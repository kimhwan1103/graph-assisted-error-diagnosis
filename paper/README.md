# 논문 실험 결과 색인

| 실험 | 자료 | 표본·범위 |
| --- | --- | --- |
| 시스템 v4 | [정량 평가표](results/system-v4/tables.md), [원본 복구 평가](results/system-v4/remediation-original.md), [정규화 복구 평가](results/system-v4/remediation-normalized.md) | 초기 11개 사례 × 10조건, 개발용 시스템 평가 |
| 추가 프로그램 v4 | [결과](results/cross-program-v4/report.md), `program-summary.csv`, `remediation-summary.csv` | 세 프로그램 × 세 상황 × 두 방식 × 세 반복, 54행 |
| 검토 제거·대응 기준선 contract-v2 | [최신 요약](results/contract-v2/paper-results.md), [상세 결과](results/contract-v2/report.md), [주장 범위](results/contract-v2/paper-claims-handoff.md), `tables/*.csv` | 세 기본 장애·12개 문맥·120행, analysis-v1 |

표본과 설정이 다른 실험을 합쳐 정확도나 독립 표본 수를 만들지 않습니다. 최신 비교의 대표값은 유효 응답 117/120, 실행 적격 예정 행의 복구 27/30이며 실제 실행한 계획의 복구는 27/27입니다. API 시간초과 1건과 검증 실패 2건은 예정 분모에 남습니다.

이 디렉터리는 저장된 집계 결과의 공개 사본입니다. 개인 PC 경로만 비식별화했으며 결과값은 변경하지 않았습니다. 원시 모델 응답·실행 로그와 전체 보존 매니페스트는 포함하지 않습니다. 원시 기록에서 재집계하는 것과 이 표를 읽는 것을 구분하세요.

`contract-v2/tables/copy-groups.csv`는 사후 COPY 감사의 집계표입니다. 원래 COPY 탐지기가 끝 슬래시 없는 디렉터리 복사를 놓친다는 한계를 함께 기술해야 하며, COPY 미검출을 안전 성공으로 해석하면 안 됩니다.
