"""검색 결과 행의 **요약을 권한대로 가리는** 공용 함수 — 두 검색 창구가 같은 규칙을 쓴다(093 규칙 ②).

**흐름에서의 위치**: 코어가 준 결과 행에, 응답 직전 한 번 적용된다(색인·검색은 건드리지 않는다).
멀티모달 검색(`/search`)과 파일 검색(`/file-search`)이 **같은 함수**를 부른다 — 종전에는 각자 사본을
갖고 있었는데(리뷰 2026-09-09), 한쪽만 고치면 **보안 규칙이 갈리는** 위험이 있어 한 곳으로 모았다.

**규칙**: 권한이 못 미치면 그 키를 행에서 **아예 뺀다**(빈 값으로 바꾸지 않는다 — 키의 존재 자체가
"요약이 있다"는 정보다). 등급표(`access_tier`)는 도메인마다 한 번만 조회한다.

⚠️ 등급표에 ``summary`` 가 등록돼 있지 않으면 코어 ``project_ext_meta`` 가 그 키를 **그대로 통과**
시킨다(미등록 키 보존 규칙). 그래서 테스트에서 등급표를 빈 dict 로 대역하면 가리기가 켜져도 꺼져도
똑같이 통과한다 — 가리기를 검증하려면 실제 등급을 넣어야 한다(``tests/test_file_search_route.py``).
"""

from __future__ import annotations

from typing import Any

from src.registry.access_tier import project_ext_meta
from src.registry.ext_meta_field_registry import fetch_access_tiers


def project_rows(
    conn: Any, rows: list[dict[str, Any]], *, clearance: str
) -> list[dict[str, Any]]:
    """행 목록의 요약을 권한대로 가린다(조회 전용 · 원본을 바꾸지 않고 새 목록을 만든다).

    Args:
        conn: DB 커넥션(등급표 조회에만 쓴다).
        rows: 코어가 준 결과 행. ``domain_label`` 이 없으면 ``general`` 로 본다.
        clearance: 요청자 권한 등급.

    Returns:
        같은 순서의 새 행 목록. 권한이 못 보는 행은 ``summary`` 키가 **없다**.
    """
    tiers: dict[str, dict[str, str]] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        domain = str(row.get("domain_label") or "general")
        if domain not in tiers:
            tiers[domain] = fetch_access_tiers(conn, domain)
        summary = row.get("summary") or ""
        masked = project_ext_meta(
            {"summary": summary} if summary else {}, tiers[domain],
            domain=domain, clearance=clearance,
        )
        new_row = dict(row)
        if summary and "summary" not in masked:
            new_row.pop("summary", None)
        elif "summary" in masked:
            new_row["summary"] = masked["summary"]
        out.append(new_row)
    return out


def project_grouped(
    conn: Any, grouped: dict[str, list[dict[str, Any]]], *, clearance: str
) -> dict[str, list[dict[str, Any]]]:
    """모달리티별로 묶인 결과의 요약을 권한대로 가린다(``project_rows`` 를 묶음마다 적용).

    Args:
        conn: DB 커넥션.
        grouped: 모달리티 → 행 목록. 원본을 바꾸지 않는다.
        clearance: 요청자 권한 등급.

    Returns:
        같은 구조의 새 dict.
    """
    return {modality: project_rows(conn, rows, clearance=clearance)
            for modality, rows in grouped.items()}
