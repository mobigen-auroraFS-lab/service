"""고른 자산 여러 건의 경로·크기 조회 — 묶음(zip) 대상 만들기."""

from __future__ import annotations

from typing import Any

from service.portal.common.repository import Repository
from src.config.filename_util import display_file_name

_SELECTION_SQL = """
SELECT asset_id, fs_path, file_size
FROM asset
WHERE asset_id = ANY(%s) AND status = 'registered'
ORDER BY created_at DESC, asset_id
"""


class SelectionRepository(Repository):
    """고른 자산들의 경로·크기를 읽는다."""

    def targets(self, asset_ids: list[str]) -> dict[str, Any]:
        """묶음 대상과 빠진 것을 가른다. 노출되지 않는(없거나 등록 완료가 아닌) 자산은 ``missing`` 으로 돌려준다.
        ``{"targets": [{asset_id, fs_path, file_name}], "missing": [asset_id…], "total_bytes": 노출되는 자산의 크기 합}``.
        """
        if not asset_ids:
            return {"targets": [], "missing": [], "total_bytes": 0}
        rows = self.rows(_SELECTION_SQL, (list(asset_ids),))
        found = {str(r["asset_id"]): r for r in rows}
        targets = []
        total = 0
        for row in rows:
            fs_path = str(row["fs_path"] or "")
            targets.append({
                "asset_id": str(row["asset_id"]),
                "fs_path": fs_path,
                "file_name": display_file_name(fs_path),
            })
            total += int(row["file_size"] or 0)
        # 요청 순서가 아니라 **DB 순서**로 담되, 빠진 것은 요청 순서를 지켜 알려 준다(화면이 짝을 맞춘다).
        missing = [a for a in asset_ids if a not in found]
        return {"targets": targets, "missing": missing, "total_bytes": total}
