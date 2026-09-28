"""DB 접근 입구 — 연결·트랜잭션을 잡고 표별 저장소(``service.portal.repositories``)를 건네준다.

    rows = DbManager.read(lambda repo: repo.catalog.tags(topics=[], subtopics=[], q=None, limit=50))

🔴 읽기(``read``)와 쓰기(``write``)를 이름으로 가른다 — 읽기 트랜잭션은 **재시도 가능**으로 표시돼
   있어 쓰기가 섞이면 중복 반영될 수 있다.

⚠️ 저장소를 요청 사이에 들고 있지 말 것 — 커넥션이 그 안에 잡혀 풀로 돌아가지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from service.api import db
from service.portal.repositories.account_repo import AccountRepository
from service.portal.repositories.admin_repo import AdminRepository
from service.portal.repositories.asset_repo import AssetRepository
from service.portal.repositories.catalog_repo import CatalogRepository
from service.portal.repositories.entity_repo import EntityRepository
from service.portal.repositories.review_repo import ReviewRepository
from service.portal.repositories.search_repo import SearchRepository

T = TypeVar("T")


class Repositories:
    """한 커넥션(=한 트랜잭션) 위에 올린 표별 저장소 묶음 — 여러 표를 읽어도 시점이 어긋나지 않는다."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.account = AccountRepository(conn)
        self.admin = AdminRepository(conn)
        self.asset = AssetRepository(conn)
        self.catalog = CatalogRepository(conn)
        self.entity = EntityRepository(conn)
        self.review = ReviewRepository(conn)
        self.search = SearchRepository(conn)


class DbManager:
    """DB 작업의 입구(상태 없음)."""

    @staticmethod
    def read(work: Callable[[Repositories], T]) -> T:
        """조회 트랜잭션에서 ``work(저장소)`` 를 실행한다(쓰기 금지)."""
        return db.run_in_db(lambda conn: work(Repositories(conn)))  # type: ignore[return-value]

    @staticmethod
    def write(work: Callable[[Repositories], T]) -> T:
        """쓰기 트랜잭션에서 ``work(저장소)`` 를 실행한다(끝나면 커밋)."""
        return db.run_in_db_write(lambda conn: work(Repositories(conn)))  # type: ignore[return-value]
