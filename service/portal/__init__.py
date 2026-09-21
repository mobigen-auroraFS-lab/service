"""포탈 백엔드 조회 계층 — 라우트가 부르는 실제 조회·집계 함수들.

코어의 하이브리드 검색·관계 그래프 위에 검색 → 상세 → 다운로드(단일·관계 묶음)
HTTP 백엔드를 올리는 조회 계층이다. 흐름은 순수 로직(검색 모달리티 그룹화·Range 파싱)과
conn 기반 조회 서비스(상세·묶음 수집/zip)로 나뉜다.

임포트
    이 패키지는 **아무것도 재수출하지 않는다** — 소비처는 필요한 모듈을 직접 가리킨다
    (``from service.portal.search.group import group_ranked``). 패키지 ``__init__`` 이 비어 있으므로
    ``import service.portal`` 이 FastAPI·psycopg 같은 무거운 의존성을 끌어오지 않는다.

테스트
    순수 함수(``group_ranked``/``parse_range_header``)는 DB 없이 각 모듈에서 직접 import 한다.
"""

from __future__ import annotations
