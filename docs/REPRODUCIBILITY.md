# 공개본 재현 안내

## 공개본만으로 가능한 확인

1. README의 설치 명령으로 이 저장소 안에 `.venv`를 만듭니다.
2. `run_paper.cmd verify`로 배포 파일의 SHA-256과 파일 목록을 검사합니다.
3. `run_paper.cmd plan`으로 최신 실험의 12개 문맥·120행·최대 216회 생성 예산을 확인합니다.
4. `run_paper.cmd example --program template-http`로 저장된 P_supported 입력을 읽어 사실 추출, RDF/SHACL/SPARQL 및 자동 조립 경로를 확인합니다. `shipping-cli`, `settlement-batch`도 선택할 수 있습니다. 이 결과는 LLM 성능이나 Docker 복구 점수가 아닙니다.
5. `run_paper.cmd test`로 코어와 공개 입력에 대한 단위 테스트를 실행합니다. 원본의 이전 v2/v3 데이터·노트북 전용 테스트는 공개 기본 테스트에 포함하지 않았습니다. 제외 목록은 `publication-provenance.json`에 있습니다.

Windows 이외의 환경에서는 설치 후 다음 명령을 사용합니다.

```sh
.venv/bin/python scripts/verify_release.py
.venv/bin/python scripts/replay_example.py --program shipping-cli
.venv/bin/python scripts/run_tests.py
PYTHONPATH=src .venv/bin/python tools/run_review_controls_v4_contract_v2.py plan
```

Node.js가 없으면 이를 요구하는 단위 테스트는 건너뛸 수 있습니다. API 키, Docker 엔진은 기본 검증에 필요하지 않습니다. 단위 테스트의 fake 모델 응답은 실험 성공 수나 실제 API 호출 수에 포함하지 않습니다.

테스트 실행기는 시스템 임시 폴더 권한 문제를 피하도록 저장소 안의 `.test-tmp/` 아래에 매번 새 경로를 만듭니다. 이 폴더는 Git에서 제외됩니다. 비동기 처리 테스트는 로컬 루프백 소켓을 사용할 수 있는 환경에서 실행해야 합니다.

## 원본 실행 아카이브가 필요한 작업

`tools/`의 실험 코드는 원본과 같습니다. 원래의 감사·동결·live 재개·Docker 평가·사후 분석 도구는 다음 원자료의 해시와 환경을 검사합니다.

- 이전 review-controls 실행과 보고서, 원본 모델 응답·call/state 파일
- Docker 사전 검사·실행 로그와 증명 파일
- freeze-manifest, protocol, 준비 단계의 전체 파일 집합
- 원래의 연구 문서·설정·환경 버전

사용자가 선택한 공개 범위에 따라 이 아카이브는 포함하지 않았습니다. 따라서 `tools/prepare_review_controls_v4_contract_v2.py clone/freeze`, `live`, `tools/tests/`의 아카이브 의존 테스트, `analysis/review_controls_contract_v2_final_v1/analyze.py`를 이 공개본만으로 실행할 수 있다고 가정하면 안 됩니다. 원본 경로나 freeze 파일을 임의로 고쳐 검증을 우회하지 마세요.

원본 기록은 작성자의 보존된 실험 폴더에 남아 있습니다. 이를 별도 연구 아카이브로 제공하면 전체 수치의 원시 응답 기반 재집계가 가능합니다. 새 환경의 재실행은 새 연구 실행으로 준비·사전 검증·동결해야 하며, 논문에 사용한 기존 실행과 합산하지 않습니다.

## 버전과 해시

`publication-provenance.json`은 원본 상대 경로, 원본 SHA-256, 공개 파일 SHA-256, 경로 비식별화 여부를 기록합니다. `release-manifest.json`은 공개 배포 파일 전체의 SHA-256을 기록합니다. 새 `.git`, `.venv`, 캐시와 결과 폴더는 배포 검증 대상이 아닙니다.

Git 줄바꿈 변환으로 기록이 달라지지 않도록 `.gitattributes`에서 원래 파일 바이트를 유지합니다. 공개 문서·런처·검증 스크립트만 새로 작성했고 진단·프롬프트·온톨로지·SHACL·SPARQL·채점 코드는 변경하지 않았습니다.
