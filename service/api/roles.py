"""프로세스 역할 — ``PORTAL_ROLE`` 이 이 프로세스가 열 창구를 정한다.

  all(기본) 전부 · api 파일 창구를 뺀 나머지 · files 파일 창구(원본 · 원문 · 묶음 · 링크 · 썸네일) + 헬스 · 내 정보.
파일 창구는 CPU(압축)와 대역폭을 많이 써서 별도 프로세스로 띄울 수 있다. 앞단(인그레스)도 ``is_file_path`` 와 같은 규칙으로 나눠 보낸다.
라우트 코드는 옮기지 않고 등록할 때 거른다. 역할은 기동 시 환경변수로만 정한다.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter

ROLE_ENV = "PORTAL_ROLE"
ROLES = ("all", "api", "files")

# 파일을 내주는 경로 — 단건 원본 · 원문 · 관계 묶음 · 고른 자산 묶음 · 개체 묶음 · 카드 묶음 · 썸네일
_FILE_PATH = re.compile(r"^/(assets/(\{[^/]+\}/(download|download-link|content|bundle|thumbnail)|bundle)|mm-meta/(bundle|\{[^/]+\}/\{[^/]+\}/bundle))$")


def role() -> str:
    raw = os.getenv(ROLE_ENV, "all").strip().lower() or "all"
    if raw not in ROLES:
        raise ValueError(f"{ROLE_ENV}={raw!r} — {', '.join(ROLES)} 중 하나여야 한다")
    return raw


def is_file_path(path: str) -> bool:
    return bool(_FILE_PATH.match(path))


def filtered(router: APIRouter, keep_files: bool) -> APIRouter:
    """라우터에서 파일 창구만(``keep_files=True``) 또는 그것을 뺀 나머지만 남긴 새 라우터. 라우터에 건 의존성(인증)은 라우트에 이미 합쳐져 있어 따라온다."""
    out = APIRouter(tags=list(router.tags or []))
    for r in router.routes:
        if is_file_path(getattr(r, "path", "")) == keep_files:
            out.routes.append(r)
    return out
