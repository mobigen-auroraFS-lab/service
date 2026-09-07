"""관계 검토 라우트가 받는 **상태 값 목록** — 코어 어휘 ``GraphEdgeStatus`` 에서 파생한다(사본 금지).

왜 이 모듈이 있나: 검토 라우트 둘(`/admin/relations` 조회 · `/admin/relations/revise` 정정)이 상태 값을
검증할 때 코어의 **내부 이름** ``_REVIEW_STATUSES`` 를 import 하고 있었다(2026-09-02 감사 A1). 밑줄
이름은 예고 없이 바뀌는 내부 구현이라 백엔드가 기대면 안 된다. 값의 정본은 코어 ``status_vocab`` 의
``GraphEdgeStatus``(DB CHECK 와 동기)이므로 거기서 **파생**한다 — 상태가 늘면 여기도 자동으로 따라간다.

값과 순서는 종전 ``_REVIEW_STATUSES``(``("proposed", "active", "rejected")``)와 같다. 그래서 400 응답의
"허용: [...]" 문구도 그대로다(`tests/test_review_vocab.py` 가 봉인).
"""
from __future__ import annotations

from src.domain.status_vocab import GraphEdgeStatus

# 검토 큐(proposed) · 승인(active) · 반려(rejected). Enum 선언 순서 = 응답 문구 순서.
REVIEW_STATUSES: tuple[str, ...] = tuple(s.value for s in GraphEdgeStatus)

__all__ = ["REVIEW_STATUSES"]
