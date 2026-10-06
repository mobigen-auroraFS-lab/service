"""사용자 화면이 읽는 자산 표 — 상세 · 주제 · 소속 개체.

🔴 노출 게이트(``status='registered'``)를 지키는 조회를 한곳에 모은다 — 게이트가 두 벌로 갈리면
   "어떤 창구로는 보이고 어떤 창구로는 안 보이는" 자산이 생긴다.
"""

from __future__ import annotations

from typing import Any

from service.portal.asset.detail import fetch_asset_detail
from service.portal.asset.download import collect_bundle_assets, resolve_download_target
from service.portal.common.repository import Repository
from src.relations.graph_query import mm_meta_of_asset
from src.topic.asset_topic_query import (
    assets_in_topic,
    assets_unclassified,
    fetch_asset_topic,
    find_same_topic_groups,
    list_topics,
)


class AssetRepository(Repository):
    """자산·주제 조회."""

    def detail(self, *, asset_id: str, clearance: str | None) -> dict[str, Any] | None:
        """상세 + 자기주제 + 같은 주제 묶음을 한 트랜잭션에서 모은다.

        없는 자산과 노출 대상이 아닌 자산을 **같게** ``None`` 으로 다룬다(존재 여부를 흘리지 않는다).
        """
        detail = fetch_asset_detail(self._conn, asset_id=asset_id, clearance=clearance)
        if detail is None:
            return None
        detail["topics"] = fetch_asset_topic(self._conn, asset_id=asset_id)
        detail["same_topic_groups"] = find_same_topic_groups(self._conn, asset_id=asset_id)
        return detail

    def download_target(self, *, asset_id: str) -> dict[str, Any] | None:
        """내려받을 원본의 경로 · 이름 · 종류. 없는 자산과 노출 대상이 아닌 자산은 같게 ``None``(404)."""
        return resolve_download_target(self._conn, asset_id=asset_id)

    def sample_fs_paths(self, limit: int) -> list[str]:
        """원본 경로 표본(무작위) — 기동 시 원본 저장소가 보이는지 점검할 때만 쓴다."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT fs_path FROM asset WHERE fs_path IS NOT NULL ORDER BY random() LIMIT %s", (limit,))
            return [r[0] for r in cur.fetchall()]

    def bundle_targets(self, *, seed_asset_id: str) -> list[dict[str, Any]] | None:
        """관계 묶음 대상. 기준 자산의 노출을 먼저 확인한다(순서를 바꾸면 볼 수 없는 자산을 통해 파일이 샌다)."""
        if resolve_download_target(self._conn, asset_id=seed_asset_id) is None:
            return None
        return collect_bundle_assets(self._conn, seed_asset_id=seed_asset_id)

    def unclassified(self, **kw: Any) -> dict[str, Any]:
        return assets_unclassified(self._conn, **kw)

    def topics(self) -> list[dict[str, Any]]:
        return list_topics(self._conn)

    def topic_assets(self, **kw: Any) -> dict[str, Any]:
        return assets_in_topic(self._conn, **kw)

    def entities_of(self, *, asset_id: str) -> list[dict[str, Any]]:
        return mm_meta_of_asset(self._conn, asset_id=asset_id)
