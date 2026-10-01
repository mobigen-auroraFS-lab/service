"""시험 전체 공통 — 프로세스 안 캐시가 시험끼리 답을 넘겨주지 않게 한다."""

import os

# 태그 목록 캐시(``catalog_repo``)는 끈다 — 대역 DB 가 시험마다 다른 행을 주는데, 캐시가 켜져 있으면
#   앞 시험의 답이 뒤 시험에 나온다. 캐시 동작은 전용 시험이 직접 켜서 본다.
os.environ.setdefault("PORTAL_TAGS_CACHE_SECONDS", "0")

# 로그 설정(``logging_config``)은 시험에서 건너뛴다 — pytest 의 로그 포획(caplog)과 겹치지 않게. 전용 시험이 직접 세운다.
os.environ.setdefault("PORTAL_LOG_CONFIGURE", "0")

# 등급표 캐시(``access_tiers``)도 끈다 — 시험마다 다른 등급표를 대역으로 주는데 캐시가 켜져 있으면 앞 시험의 표가 나온다.
os.environ.setdefault("PORTAL_ACCESS_TIER_CACHE_SECONDS", "0")
# 기동 예열(``warmup``)은 건너뛴다 — 시험이 DB 를 부르지 않게.
os.environ.setdefault("PORTAL_WARMUP", "0")
