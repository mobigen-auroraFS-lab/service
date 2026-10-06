"""API 접근 이력 — 기록·조회·집계와 동작 이름 도출.

**흐름에서의 위치**: 미들웨어가 매 요청 끝에 여기로 한 행을 남기고, 관리자 화면이 그것을 읽는다.
기록은 **추가만** 한다 — 감사 자료라 수정·삭제 경로를 두지 않는다.
자산 데이터에는 손대지 않는다(헌법 6조) — 접근 이력 테이블에만 한 행씩 덧붙인다.
무엇을 어떤 동작으로 기록할지는 미들웨어가 경로를 보고 정한다.
"""
from __future__ import annotations

import json
import re
from typing import Any

from service.portal.common.timeline import TIMELINE_INTERVALS, pivot_series
from src.database.ids import uuid7

# /assets/{seg} 의 seg 가 자산 단건인지 판정하는 UUID 형식(대소문자 무관). 비-UUID(예약/컬렉션
# 세그먼트·오타)는 감사 대상에서 제외한다 — ``derive_access_action`` 설명 참조.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"  # \Z: $ 는 말미 개행 허용
)

_INSERT = (
    "INSERT INTO access_log (access_id, asset_id, user_id, action, detail) "
    "VALUES (%s, %s, %s, %s, %s::jsonb)"
)
_COLS = "access_id, action, user_id, asset_id, occurred_at"


def record_access(conn: Any, *, action: str, user_id: str,
                  asset_id: str | None = None, detail: dict | None = None) -> str:
    """접근 이력 한 행을 남긴다(추가만 · 커밋은 호출자 몫). 발생 시각은 DB 시계로 찍힌다. ``asset_id`` 는 UUID 여야 한다(아니면 INSERT 실패). 새 ``access_id`` 를 돌려준다."""
    access_id = str(uuid7())
    with conn.cursor() as cur:
        cur.execute(_INSERT, (access_id, asset_id, user_id, action,
                              json.dumps(detail or {}, ensure_ascii=False)))
    return access_id


def record_access_many(conn: Any, *, action: str, user_id: str, asset_ids: list[str], detail: dict | None = None) -> list[str]:
    """같은 동작의 접근 이력을 한 번에 여러 행 남긴다(``executemany`` — 행마다 DB 왕복이 생기지 않는다). 새 ``access_id`` 들을 돌려준다."""
    if not asset_ids:
        return []
    body = json.dumps(detail or {}, ensure_ascii=False)
    ids = [str(uuid7()) for _ in asset_ids]
    with conn.cursor() as cur:
        cur.executemany(_INSERT, [(i, a, user_id, action, body) for i, a in zip(ids, asset_ids, strict=True)])
    return ids


_RANGE_START = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-", re.IGNORECASE)


def is_range_continuation(range_header: str | None) -> bool:
    """이어받기의 뒷 조각인가 — ``Range`` 의 시작이 0 이 아니거나 끝에서부터(``bytes=-N``)인 요청. 한 번의 내려받기가 조각 수만큼 접근 기록을 쌓지 않게 첫 요청만 기록한다."""
    if not range_header:
        return False
    m = _RANGE_START.match(range_header)
    if not m:
        return False
    start = m.group(1)
    return start == "" or int(start) > 0


def derive_access_action(method: str, path: str) -> tuple[str, str | None] | None:
    """요청 경로를 보고 감사에 남길 동작 이름과 대상 자산 ``(action, asset_id)`` 을 정한다(순수). GET 이 아니거나 감사 대상이 아니면 ``None``.

    ⚠ 데이터 라우트를 추가하면 이 함수도 같이 고친다(누락은 조용히 미기록). ``/assets/`` 뒤 첫 세그먼트는 UUID 일 때만 자산 단건으로 본다(컬렉션 경로를 자산 id 로 오인하면 INSERT 가 매번 실패한다).
    고른 자산 묶음은 POST 라 라우트가 담긴 자산마다 ``bundle`` 로 남기고, 개체 묶음 · 썸네일은 기록하지 않는다.
    """
    if method.upper() != "GET":
        return None
    p = path.rstrip("/")
    if p == "/search":
        return ("search", None)
    if p.startswith("/assets/"):
        parts = p[len("/assets/"):].split("/")
        asset_id = parts[0]
        if not asset_id or not _UUID_RE.match(asset_id):
            return None  # 비-UUID(unclassified 등 컬렉션/예약 세그먼트) — 단건 감사 아님(B3)
        if len(parts) == 1:
            return ("asset_view", asset_id)
        if len(parts) == 2 and parts[1] == "download":
            return ("download", asset_id)
        if len(parts) == 2 and parts[1] == "bundle":
            return ("bundle", asset_id)
        if len(parts) == 2 and parts[1] == "content":
            # 원문 열람은 상세(asset_view)와 구분한다 — 본문 글자를 가져가는 접근이다.
            return ("content", asset_id)
    return None


def _filter_clause(conds: list[str]) -> str:
    """조건 목록을 WHERE 절로 조립한다(조건이 없으면 빈 문자열)."""
    return (" WHERE " + " AND ".join(conds)) if conds else ""


def query_access_logs(conn: Any, *, user_id: str | None = None, action: str | None = None,
                      since: Any = None, until: Any = None,
                      limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """접근 이력을 필터 · 페이징해 조회한다(조회 전용). ``until`` 은 미포함. ``{rows, total, limit, offset}`` — 최신순, 같은 시각이면 id 순."""
    conds: list[str] = []
    params: list[Any] = []
    if user_id:
        conds.append("user_id = %s")
        params.append(user_id)
    if action:
        conds.append("action = %s")
        params.append(action)
    if since is not None:
        conds.append("occurred_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("occurred_at < %s")
        params.append(until)
    clause = _filter_clause(conds)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM access_log" + clause, params)
        total = int(cur.fetchone()[0])
        cur.execute(
            f"SELECT {_COLS} FROM access_log{clause} "
            "ORDER BY occurred_at DESC, access_id DESC LIMIT %s OFFSET %s",
            [*params, limit, offset])
        rows = [
            {"access_id": str(a), "action": act, "user_id": u,
             "asset_id": str(aid) if aid is not None else None,
             "occurred_at": ts.isoformat() if ts is not None else None}
            for a, act, u, aid, ts in cur.fetchall()]
    # 페이징 응답 모양을 통일한다({rows,total,limit,offset}).
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def access_log_stats(conn: Any, *, since: Any = None, until: Any = None) -> dict[str, Any]:
    """총계와 동작별 · 사용자별 호출 수 ``{total, by_action, by_user}`` (많은 순 → 이름순)."""
    conds: list[str] = []
    params: list[Any] = []
    if since is not None:
        conds.append("occurred_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("occurred_at < %s")
        params.append(until)
    clause = _filter_clause(conds)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM access_log" + clause, params)
        total = int(cur.fetchone()[0])
        cur.execute(f"SELECT action, COUNT(*) FROM access_log{clause} "
                    "GROUP BY action ORDER BY COUNT(*) DESC, action ASC", params)
        by_action = [{"action": a, "count": int(c)} for a, c in cur.fetchall()]
        cur.execute(f"SELECT user_id, COUNT(*) FROM access_log{clause} "
                    "GROUP BY user_id ORDER BY COUNT(*) DESC, user_id ASC", params)
        by_user = [{"user_id": u, "count": int(c)} for u, c in cur.fetchall()]
    return {"total": total, "by_action": by_action, "by_user": by_user}


# group_by 멀티시리즈 화이트리스트 → 컬럼식(고정 매핑·인젝션 안전). action/user_id 만 허용.
_TIMELINE_GROUP_COLS = {"action": "action", "user_id": "user_id"}


def access_log_timeline(conn: Any, *, since: Any = None, until: Any = None, action: str | None = None,
                        interval: str = "day", group_by: str | None = None) -> dict[str, Any]:
    """시간 버킷(일 · 시)별 호출 수(시간순). ``interval`` 은 허용 목록 밖이면 일 단위로 접는다(SQL 에 박히는 값).
    ``group_by``(``action`` | ``user_id``)를 주면 여러 시리즈 ``{interval, group_by, series}``, 아니면 단일 ``{interval, buckets}`` 이다.
    """
    # ⚠️ 버킷 단위는 아래 SQL 에 **문자열로 직접 박힌다** — 허용 목록을 통과한 값만 쓰고,
    # 그 밖이면 일 단위로 접는다(요청 값을 그대로 넣으면 안 된다).
    trunc = interval if interval in TIMELINE_INTERVALS else "day"
    conds: list[str] = []
    params: list[Any] = []
    if since is not None:
        conds.append("occurred_at >= %s")
        params.append(since)
    if until is not None:
        conds.append("occurred_at < %s")
        params.append(until)
    if action:
        conds.append("action = %s")
        params.append(action)
    clause = (" WHERE " + " AND ".join(conds)) if conds else ""
    with conn.cursor() as cur:
        # 시리즈를 가르면 응답 모양 자체가 달라진다(단일 buckets → series 배열).
        if group_by in _TIMELINE_GROUP_COLS:
            # 컬럼명도 SQL 에 직접 박히므로 매핑을 통과한 값만 쓴다.
            gcol = _TIMELINE_GROUP_COLS[group_by]
            cur.execute(
                f"SELECT {gcol} AS key, date_trunc('{trunc}', occurred_at) AS bkt, COUNT(*) "
                f"FROM access_log{clause} GROUP BY key, bkt ORDER BY key ASC, bkt ASC", params)
            return {"interval": trunc, "group_by": group_by, "series": pivot_series(cur.fetchall())}
        cur.execute(
            f"SELECT date_trunc('{trunc}', occurred_at) AS bkt, COUNT(*) FROM access_log{clause} "
            "GROUP BY bkt ORDER BY bkt ASC", params)
        buckets = [
            {"bucket": b.isoformat() if b is not None else None, "count": int(c)}
            for b, c in cur.fetchall()]
    return {"interval": trunc, "buckets": buckets}


def access_log_overview(conn: Any, *, since: Any = None, until: Any = None,
                        action: str | None = None, interval: str = "day") -> dict[str, Any]:
    """접근 이력 화면이 필요한 기간 지표 + 추이를 한 트랜잭션에서 만든다 ``{total, by_action, timeline}``. ``action`` 은 추이만 좁히고 지표는 기간 전체를 센다."""
    stats = access_log_stats(conn, since=since, until=until)
    timeline = access_log_timeline(
        conn, since=since, until=until, action=action, interval=interval, group_by="action")
    return {"total": stats["total"], "by_action": stats["by_action"], "timeline": timeline}
