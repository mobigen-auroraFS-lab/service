"""자산의 글자 내용을 읽기 위한 조회 — 노출 게이트는 상세·다운로드와 같다."""

from __future__ import annotations

from typing import Any

from service.portal.common.repository import Repository

_ASSET_CONTENT_SQL = """
SELECT a.asset_id, a.modality, a.status, a.fs_path, m.ext_meta
FROM asset a
LEFT JOIN asset_metadata m ON m.asset_id = a.asset_id
WHERE a.asset_id = %s
LIMIT 1
"""


class AssetContentRepository(Repository):
    """원문을 어디서 가져올지 판정할 재료를 읽는다."""

    def source_of(self, asset_id: str) -> dict[str, Any] | None:
        """``{asset_id, modality, fs_path, stt}`` — 없거나 등록 완료가 아니면 ``None``."""
        row = self.one(_ASSET_CONTENT_SQL, (asset_id,))
        if row is None or row["status"] != "registered":
            return None
        ext_meta = row["ext_meta"] if isinstance(row["ext_meta"], dict) else {}
        stt = ext_meta.get("stt")
        return {
            "asset_id": str(row["asset_id"]),
            "modality": row["modality"],
            "fs_path": row["fs_path"],
            "stt": stt if isinstance(stt, str) and stt.strip() else None,
        }
