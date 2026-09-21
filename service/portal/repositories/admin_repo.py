"""운영·관리자 화면이 읽는 표들 — 접근 이력 · 계보 · 자산 통계 · 관계(읽기 전용)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from service.portal.asset.detail import fetch_asset_detail
from service.portal.asset.stats import (
    asset_stats,
    asset_timeline,
    build_modality_overview,
    modality_detail,
    query_assets,
)
from service.portal.common.repository import Repository
from service.portal.dashboard import build_dashboard_summary
from service.portal.history.access_log import (
    access_log_overview,
    access_log_stats,
    access_log_timeline,
    query_access_logs,
    record_access,
)
from service.portal.history.lineage import (
    lineage_timeline,
    query_asset_lineage,
    query_lineage_feed,
    relation_proposed_summary,
)
from src.relations.review import list_edges_for_review, list_relation_kinds
from src.topic.asset_topic_query import fetch_asset_topic, find_same_topic_groups


class AdminRepository(Repository):
    """관리자 화면의 조회."""

    def access_logs(self, **kw: Any) -> dict[str, Any]:
        return query_access_logs(self._conn, **kw)

    def access_log_stats(self, **kw: Any) -> dict[str, Any]:
        return access_log_stats(self._conn, **kw)

    def access_log_timeline(self, **kw: Any) -> dict[str, Any]:
        return access_log_timeline(self._conn, **kw)

    def access_log_overview(self, **kw: Any) -> dict[str, Any]:
        return access_log_overview(self._conn, **kw)

    def asset_lineage(self, asset_id: str) -> list[dict[str, Any]]:
        return query_asset_lineage(self._conn, asset_id)

    def lineage_feed(self, **kw: Any) -> dict[str, Any]:
        return query_lineage_feed(self._conn, **kw)

    def lineage_timeline(self, **kw: Any) -> dict[str, Any]:
        return lineage_timeline(self._conn, **kw)

    def relation_proposed_summary(self, **kw: Any) -> dict[str, Any]:
        return relation_proposed_summary(self._conn, **kw)

    def asset_stats(self, **kw: Any) -> dict[str, Any]:
        return asset_stats(self._conn, **kw)

    def assets(self, **kw: Any) -> dict[str, Any]:
        return query_assets(self._conn, **kw)

    def modality_detail(self, modality: str, **kw: Any) -> dict[str, Any]:
        return modality_detail(self._conn, modality, **kw)

    def modality_overview(self, modality: str, **kw: Any) -> dict[str, Any]:
        return build_modality_overview(self._conn, modality, **kw)

    def asset_timeline(self, **kw: Any) -> dict[str, Any]:
        return asset_timeline(self._conn, **kw)

    def asset_detail(self, *, asset_id: str, clearance: str | None) -> dict[str, Any] | None:
        """관리자 자산 상세 — 주제까지 한 트랜잭션에서 붙인다. 노출 대상이 아니면 ``None``."""
        detail = fetch_asset_detail(self._conn, asset_id=asset_id, clearance=clearance)
        if detail is None:
            return None
        detail["topics"] = fetch_asset_topic(self._conn, asset_id=asset_id)
        detail["same_topic_groups"] = find_same_topic_groups(self._conn, asset_id=asset_id)
        return detail

    def dashboard_summary(self, *, now: datetime, **kw: Any) -> dict[str, Any]:
        return build_dashboard_summary(self._conn, now=now, **kw)

    def edges_for_review(self, **kw: Any) -> dict[str, Any]:
        return list_edges_for_review(self._conn, **kw)

    def relation_kinds(self, *, status: str | None) -> dict[str, Any]:
        return list_relation_kinds(self._conn, status=status)

    def record_access(self, *, action: str, user_id: str, asset_id: str | None = None,
                      detail: dict | None = None) -> str:
        """접근 이력 한 행을 남긴다(이 저장소의 **유일한 쓰기** — 추가만)."""
        return record_access(self._conn, action=action, user_id=user_id,
                             asset_id=asset_id, detail=detail)
