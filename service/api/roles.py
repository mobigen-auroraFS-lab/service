"""프로세스 역할(단일 책임: 이 프로세스가 어떤 창구를 열지 정한다) — ⚠️ 실험 브랜치(2026-10-01).

같은 코드베이스를 **둘로 나눠 띄울 수 있게** 한다. 파일을 내주는 창구(원본 · 원문 · 묶음 zip · 썸네일)는 요청 하나가 오래 걸리고 CPU(압축)와 대역폭을
많이 쓴다. 검색 · 상세 · 계정과 같은 프로세스 · 같은 스레드풀에서 돌면 묶음 몇 개가 화면 응답을 같이 늦춘다.

  ``PORTAL_ROLE``  all(기본 · 지금과 같다) | api(파일 창구를 뺀 나머지) | files(파일 창구만 + 헬스 · 내 정보)

실행: ``PORTAL_ROLE=files uvicorn service.api:app --port 8100`` (역할은 기동할 때 환경변수로만 정한다 — 모듈을 불러오는 것만으로 환경을 바꾸는 진입점은 두지 않는다. 테스트가 모든 모듈을 불러올 때 다른 시험을 오염시킨다).

경로 규칙은 한 곳(``is_file_path``)에만 둔다 — 앞단(인그레스)도 같은 규칙으로 나눠 보낸다(``docs`` 아님 · 배포 매니페스트 몫).
라우트 코드는 옮기지 않았다(테스트 · 구현 그대로) — 등록할 때 거른다.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter

ROLE_ENV = "PORTAL_ROLE"
ROLES = ("all", "api", "files")

# 파일을 내주는 경로 — 단건 원본 · 원문 · 관계 묶음 · 고른 자산 묶음 · 개체 묶음 · 카드 묶음 · 썸네일
_FILE_PATH = re.compile(r"^/(assets/(\{[^/]+\}/(download|content|bundle|thumbnail)|bundle)|mm-meta/(bundle|\{[^/]+\}/\{[^/]+\}/bundle))$")


def role() -> str:
    raw = os.getenv(ROLE_ENV, "all").strip().lower() or "all"
    if raw not in ROLES:
        raise ValueError(f"{ROLE_ENV}={raw!r} — {', '.join(ROLES)} 중 하나여야 한다")
    return raw


def is_file_path(path: str) -> bool:
    return bool(_FILE_PATH.match(path))


def filtered(router: APIRouter, keep_files: bool) -> APIRouter:
    """라우터에서 파일 창구만(``keep_files=True``) 또는 파일 창구를 뺀 나머지만 남긴 새 라우터.

    라우터에 건 의존성(인증)은 라우트를 정의할 때 이미 라우트마다 합쳐져 있어 복사해도 그대로 따라온다.
    """
    out = APIRouter(tags=list(router.tags or []))
    for r in router.routes:
        if is_file_path(getattr(r, "path", "")) == keep_files:
            out.routes.append(r)
    return out
