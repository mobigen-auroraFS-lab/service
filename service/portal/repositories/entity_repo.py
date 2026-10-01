"""개체(멀티모달 메타) 화면이 읽는 표 — 목록 · 모수 · 칩 · 카드."""

from __future__ import annotations

from typing import Any

from service.portal import mm_meta
from service.portal.common.repository import Repository


class EntityRepository(Repository):
    """개체 조회 · 묶음 대상."""

    def page(self, **kw: Any) -> list[dict[str, Any]]:
        return mm_meta.fetch_list(self._conn, **kw)

    def total(self, **kw: Any) -> int:
        return mm_meta.fetch_total(self._conn, **kw)

    def facets(self, **kw: Any) -> dict[str, Any]:
        return mm_meta.fetch_facets(self._conn, **kw)

    def card(self, *, entity_type: str, entity_uid: str) -> dict[str, Any] | None:
        return mm_meta.fetch_card(self._conn, entity_type=entity_type, entity_uid=entity_uid)

    def zip_rows(self, **kw: Any) -> Any:
        """좁힌 개체들의 구성 자산(묶음 대상)."""
        return mm_meta.entities_zip_rows(self._conn, **kw)

    def card_zip_targets(self, **kw: Any) -> Any:
        """카드 한 장의 구성 자산."""
        return mm_meta.card_zip_targets(self._conn, **kw)
