"""도메인별 노출 등급표(``ext_meta_field_registry``) 조회 — 짧은 시간 들고 있는 캐시를 얹는다.

**왜 필요한가** — 상세 한 건과 검색 결과(``/file-search`` · ``/search``)가 응답마다 이 표를 DB 에서 다시 읽었다
(질의 한 번 = DB 왕복 한 번 · 실측 28ms). 표는 운영자가 항목 등급을 바꿀 때만 달라지는 설정이다.

**대가 — 권한 변경이 늦게 닿는다.** 이 표는 ``summary`` 같은 항목을 누구에게 보일지 정하므로, 등급을 **올려**
숨기는 방향으로 바꿔도 캐시 수명(기본 60초)만큼은 옛 등급으로 응답한다. 그래서 수명을 짧게 두었다
(``PORTAL_ACCESS_TIER_CACHE_SECONDS`` · 기본 60 · 0 이면 끈다). 프로세스(워커)마다 따로 든다.

코어 ``fetch_access_tiers`` 와 **같은 이름 · 같은 서명**이다 — 소비처는 이름만 바꿔 불러 오면 된다.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from src.registry.ext_meta_field_registry import fetch_access_tiers as _fetch_from_db

ACCESS_TIER_CACHE_ENV = "PORTAL_ACCESS_TIER_CACHE_SECONDS"
ACCESS_TIER_CACHE_DEFAULT = 60

_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_LOCK = threading.Lock()


def cache_seconds() -> int:
    """캐시 수명(초). 없거나 잘못되면 기본값, 0 이하면 끔(0)."""
    raw = os.getenv(ACCESS_TIER_CACHE_ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else ACCESS_TIER_CACHE_DEFAULT
    except ValueError:
        return ACCESS_TIER_CACHE_DEFAULT


def clear_access_tier_cache() -> None:
    """캐시를 비운다(시험 · 등급을 바꾼 직후 즉시 반영이 필요할 때)."""
    with _LOCK:
        _CACHE.clear()


def fetch_access_tiers(conn: Any, domain: str) -> dict[str, str]:
    """``domain`` 의 활성 ``ext_meta`` 키 → ``access_tier`` 맵. 같은 도메인은 캐시 수명 동안 DB 를 다시 읽지 않는다.

    Args:
        conn: DB 커넥션(캐시에 없을 때만 쓴다).
        domain: 도메인 라벨.

    Returns:
        키 → 등급 맵. **복사본**을 준다 — 호출부가 고쳐도 캐시는 바뀌지 않는다. 읽기에 실패하면 예외를 그대로
        올리고 아무것도 캐시하지 않는다.
    """
    ttl = cache_seconds()
    if ttl:
        now = time.monotonic()
        with _LOCK:
            hit = _CACHE.get(domain)
        if hit and now - hit[0] < ttl:
            return dict(hit[1])
    tiers = _fetch_from_db(conn, domain)
    if ttl:
        with _LOCK:
            _CACHE[domain] = (time.monotonic(), dict(tiers))
    return tiers
