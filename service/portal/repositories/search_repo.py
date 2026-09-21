"""검색 결과에 **DB 로만 알 수 있는 것**을 붙인다 — 등급 미달 항목 제거 · 표에 찍을 크기·시각.

검색 자체는 검색 엔진이 한다.
"""

from __future__ import annotations

from typing import Any

from service.portal.asset.file_meta import fetch_file_meta
from service.portal.common.repository import Repository
from service.portal.search.projection import project_grouped, project_rows


class SearchRepository(Repository):
    """검색 결과 보강."""

    def project_rows(self, rows: list[dict[str, Any]], *, clearance: str | None) -> list[dict[str, Any]]:
        return project_rows(self._conn, rows, clearance=clearance)

    def project_grouped(self, grouped: Any, *, clearance: str | None) -> Any:
        return project_grouped(self._conn, grouped, clearance=clearance)

    def file_meta(self, asset_ids: list[str]) -> dict[str, Any]:
        """표에 찍을 크기·시각(모르는 자산은 빠진다)."""
        return fetch_file_meta(self._conn, asset_ids)
