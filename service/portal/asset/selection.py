"""화면에서 고른 자산 여러 건을 묶을 때의 상한(건수 · 용량). 노출 게이트 · 부분 zip 규칙은 다른 묶음과 같다."""

from __future__ import annotations

# 한 번에 고를 수 있는 자산 수. 화면의 '결과 전체 선택'이 수천 건을 한 번에 보내는 것을 막는다.
MAX_SELECTION = 200
# 묶음 용량 상한 — 개체 묶음(ENTITIES_BUNDLE_MAX_BYTES)과 같은 값.
MAX_SELECTION_BYTES = 500 * 1024 * 1024
