"""자산의 **파일 메타**(크기·수정일)를 화면에 보이는 행에만 붙이는 조회(095 파일 검색 화면).

**왜 백엔드인가**(093 책무 경계 · 규칙 ④): 그래프를 읽지 않는 화면용 단순 조회다. 어디서 읽어도 같은
답이어야 하는 규칙도, 잘못 짜면 조용히 틀리는 읽기도 아니다. 파이프라인은 이 값을 쓰지 않는다.

**왜 색인에 넣지 않는가**: 크기·수정일은 검색 조건이 아니라 **표에 찍는 값**이다. 색인에 넣으면 자산이
바뀔 때마다 재색인해야 하고, 지금 코퍼스만 해도 전량 재색인이 필요하다. 화면에 보이는 행은 한 페이지
분량(수십 건)이라 그때 한 번 묻는 것이 싸다.

⚠️ **보이는 행만** 묻는다 — 검색이 데려온 후보 전부를 물으면 페이지를 넘길 때마다 수백 행을 조회한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from src.domain.status_vocab import AssetStatus

# 노출 기준은 다운로드·묶음과 같다(``status='registered'``) — 비노출 자산의 크기가 표에 새지 않게.
_FILE_META_SQL = f"""
SELECT asset_id::text AS asset_id,
       COALESCE(file_size, 0) AS file_size,
       updated_at,
       created_at
  FROM asset
 WHERE asset_id::text = ANY(%(ids)s)
   AND status = '{AssetStatus.REGISTERED}'
"""


def _iso(value: Any) -> str | None:
    """날짜 값을 ISO 문자열로. ``None`` 이면 ``None``(화면이 빈칸으로 그린다).

    Args:
        value: DB 가 준 timestamp 또는 ``None``.

    Returns:
        ISO 8601 문자열, 또는 ``None``.
    """
    return value.isoformat() if isinstance(value, datetime) else None


def fetch_file_meta(
    conn: Connection[Any], asset_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """자산들의 크기·수정일을 **한 번에** 읽는다(조회 전용).

    Args:
        conn: DB 커넥션.
        asset_ids: 화면에 보일 자산 id 목록. 빈 목록이면 DB 를 건드리지 않는다.

    Returns:
        ``{asset_id: {"file_size": int, "updated_at": str|None, "created_at": str|None}}``.
        등록 상태가 아니거나 없는 자산은 키가 없다 — 호출부는 그 행을 빈칸으로 그린다.
    """
    ids = [str(a) for a in asset_ids if str(a)]
    if not ids:
        return {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FILE_META_SQL, {"ids": ids})
        rows = cur.fetchall()
    return {
        str(r["asset_id"]): {
            "file_size": int(r["file_size"] or 0),
            "updated_at": _iso(r["updated_at"]),
            "created_at": _iso(r["created_at"]),
        }
        for r in rows
    }
