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

# 기동 시 접속 시험(``lifespan._start_dependency_checks``)도 건너뛴다 — 시험이 실제 DB · 검색 엔진에 붙지 않게.
os.environ.setdefault("PORTAL_STARTUP_CHECK", "0")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_breakers():
    """연결 차단기(DB · 검색 엔진)는 프로세스 전역 상태다 — 시험끼리 넘겨주지 않게 매번 닫고, 실제 네트워크 접속 시험(probe)은 「살아 있음」으로 대신한다.

    (실제 probe 를 두면 개발 장비에서 DB · 엔진이 죽어 있을 때 연결 실패를 흉내 내는 시험이 진짜 차단기를 열어 뒤 시험들이 503 이 된다.)
    전용 시험(``test_db_health``)은 필요한 probe 를 직접 바꿔 끼운다.
    """
    from unittest import mock

    from service.api import db_health, search_health

    db_health.reset()
    search_health.BREAKER.reset()
    with mock.patch.object(db_health, "_probe", return_value=True), mock.patch.object(search_health, "_probe", return_value=True):
        yield
    db_health.reset()
    search_health.BREAKER.reset()
