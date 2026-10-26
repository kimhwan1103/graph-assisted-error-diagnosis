# 독립 프로그램 추가 실험 결과

저자 제작 Node.js 프로그램 3개, 상황 9개, 조건별 3회 반복으로 예정 평가 단위는 54개다. 실제 사람 참조 승인이 없는 development 평가다. 원본이 주 분석이고 제한적 정규화는 실행 전 고정한 보조 분석이다.

|응답|프로그램|방식|유효 응답|대상·위치 일치|정상 인식|불필요 수정|호출|입력 토큰(관측 합)|출력 토큰(관측 합)|평균 pipeline ms|
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

대상·위치 일치는 응답의 대상 경로 언급과 소스 인용 위치의 구조적 일치이며, 진단 의미 정확성 점수가 아니다. 토큰 누락 호출 수와 관측 수는 JSON/CSV에 함께 저장한다. 정규화 행의 비용은 동일한 기존 실행 비용이며 합산하지 않는다.

|응답|프로그램|상황|방식|예정|적용|기능|회귀|보존|복구|미평가|
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

## 실패 및 전환

- settlement-batch / adaptive: 경로 {"adaptive": 9}, 사유 {}, 응답 오류 {"INVALID_SOURCE_RESULT": 6}
- settlement-batch / graph_compact: 경로 {"compact_accepted": 3, "early_general": 6}, 사유 {"accepted": 3, "no_graph_attempt_recorded": 6}, 응답 오류 {"INVALID_SOURCE_RESULT": 3}
- shipping-cli / adaptive: 경로 {"adaptive": 9}, 사유 {}, 응답 오류 {"INVALID_SOURCE_RESULT": 6}
- shipping-cli / graph_compact: 경로 {"early_general": 6, "compact_accepted": 3}, 사유 {"no_graph_attempt_recorded": 6, "accepted": 3}, 응답 오류 {"INVALID_SOURCE_RESULT": 3}
- template-http / adaptive: 경로 {"adaptive": 9}, 사유 {}, 응답 오류 {"INVALID_SOURCE_RESULT": 6}
- template-http / graph_compact: 경로 {"compact_accepted": 3, "early_general": 6}, 사유 {"accepted": 3, "no_graph_attempt_recorded": 6}, 응답 오류 {"INVALID_SOURCE_RESULT": 3}
- original / adaptive 복구 평가 판정: {"contract_failed": 18, "no_change_correct": 9}
- original / graph_compact 복구 평가 판정: {"restored": 9, "no_change_correct": 9, "contract_failed": 9}
- normalized / adaptive 복구 평가 판정: {"ambiguous_plan": 12, "no_change_correct": 9, "restored": 3, "unsupported_plan": 3}
- normalized / graph_compact 복구 평가 판정: {"restored": 12, "no_change_correct": 9, "ambiguous_plan": 6}

## 해석 범위

실제 복구 성공 수는 지정된 COPY 어댑터와 고정 컨테이너에서 기능·회귀·보존 조건을 모두 통과한 수다. 미평가가 있는 조건의 전체 복구율은 확정하지 않는다. 양쪽이 성공한 사례는 실행 가능성의 근거이며 그래프 방식의 우월성을 뜻하지 않는다.

새 프로그램은 외부 공개 프로젝트의 자연 발생 장애가 아니다. 기본 장애 3개의 반복과 구문 변형을 독립 장애 54개로 해석하지 않는다. 그래프 방식의 일반 분석 전환도 원래 조건에 포함한다. 자동 근거 검사는 의미적 충분성이나 사람 채점을 대체하지 않는다.

원시 응답·호출·실제 경로·패치·검사 결과와 해시는 보존된 원본 아카이브의 cells.json/csv 및 각 실행 폴더에 연결돼 있다. 원본의 deviations.md와 reproduction.md는 자료 준비·검사 이력과 당시 실행 명령을 담는다. 이 공개본에는 해당 원본 실행 기록을 포함하지 않았으며 집계 결과표만 제공한다. 공개본에서 가능한 재현 범위는 [재현 안내](../../../docs/REPRODUCIBILITY.md)를 참고한다.
