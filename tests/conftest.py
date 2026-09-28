"""시험 전체 공통 — 프로세스 안 캐시가 시험끼리 답을 넘겨주지 않게 한다."""

import os

# 태그 목록 캐시(``catalog_repo``)는 끈다 — 대역 DB 가 시험마다 다른 행을 주는데, 캐시가 켜져 있으면
#   앞 시험의 답이 뒤 시험에 나온다. 캐시 동작은 전용 시험이 직접 켜서 본다.
os.environ.setdefault("PORTAL_TAGS_CACHE_SECONDS", "0")
