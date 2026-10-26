# API 회귀 테스트가 사용하는 이전 파일럿 입력

`development.v2.json`은 기존 `tests/test_gemma_api.py`가 API 오류·재시도 방지·원본 응답 보존을 가짜 HTTP 클라이언트로 검사할 때 사용하는 세 사례의 고정 입력입니다. 최신 contract-v2 실험의 프로그램이나 결과에 합산하지 않습니다. 이 입력은 테스트의 외부 다운로드 의존성을 없애기 위해 포함했습니다.

출처, 고정 커밋, 선택 과정은 JSON의 provenance와 `source/PROVENANCE.json`에 있습니다. 포함된 Hessian.js 코드와 BugsJS 자료의 원래 MIT 라이선스를 `source/LICENSE.hessian`, `source/LICENSE.benchmark`에 보존했습니다. provenance에서 언급하는 전체 원본 ZIP은 이 축약 공개본에 포함하지 않습니다. 저장소 전체에 새 라이선스를 부여한다는 의미가 아닙니다.
