"""관계 검토 — 결정(쓰기)과 그 감사 기록.

🔴 결정과 감사는 **같은 트랜잭션**에 있어야 한다. 감사 실패가 결정을 되돌려서도 안 되므로
   감사는 중첩 트랜잭션(SAVEPOINT)으로 격리한다 — 그 규칙을 이 클래스 안에 가둔다.
"""

from __future__ import annotations

import logging
from typing import Any

from service.portal.common.repository import Repository
from service.portal.history.access_log import record_access
from src.relations.review import bulk_review, promote_relation_kind, revise_edge

_LOG = logging.getLogger(__name__)


class ReviewRepository(Repository):
    """관계 검토 쓰기(커밋은 ``DbManager.write``)."""

    def bulk_decide(self, *, action: str, edge_ids: list[str], reviewer: str) -> Any:
        return bulk_review(self._conn, action=action, edge_ids=edge_ids, reviewer=reviewer)

    def revise(self, **kw: Any) -> Any:
        return revise_edge(self._conn, **kw)

    def promote_kind(self, **kw: Any) -> Any:
        return promote_relation_kind(self._conn, **kw)

    def audit(self, *, action: str, reviewer: str, detail: dict[str, Any]) -> None:
        """결정과 같은 트랜잭션에 감사 한 행 — 실패해도 결정은 살린다(최선 노력)."""
        try:
            with self._conn.transaction():
                record_access(self._conn, action=action, user_id=reviewer, detail=detail)
        except Exception:  # noqa: BLE001 — 감사 실패가 결정을 되돌리면 안 된다
            _LOG.warning("relation 감사 기록 실패(무시): %s %s", action, detail)
