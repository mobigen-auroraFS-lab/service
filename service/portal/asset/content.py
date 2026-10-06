"""자산의 글자 내용(원문)을 읽어 준다 — 상세 화면의 원문 영역용.

문서는 원본 파일을 그대로 읽고, 소리는 받아쓰기(``ext_meta.stt``)를, 영상은 받아쓰기가 있을 때만 준다. 그림은 글자가 없다.
원본은 DB 경로에 있다고 보고 읽는다(없으면 OSError → 호출부가 410). 응답이 커지지 않게 기본 1MiB 에서 자르고 ``truncated`` 로 알린다.
"""

from __future__ import annotations

import os
from typing import Any

# 한 번에 실어 보낼 최대 글자 바이트(1MiB). 넘으면 잘라서 ``truncated=True``.
TEXT_BYTE_CAP = 1024 * 1024

# 글자로 읽을 수 있는 모달리티와 그 출처.
TEXT_SOURCE = {"text": "file", "audio": "stt", "video": "stt"}


def read_text_file(fs_path: str, *, cap: int = TEXT_BYTE_CAP) -> tuple[str, bool]:
    """파일 앞부분을 ``cap`` 바이트까지 글자로 읽는다. ``(글자, 잘렸는지)`` — 해독할 수 없는 바이트는 대체 문자로 바꾼다. 열 수 없으면 OSError."""
    with open(fs_path, "rb") as fp:
        raw = fp.read(cap + 1)
    truncated = len(raw) > cap
    return raw[:cap].decode("utf-8", errors="replace"), truncated


def build_content(source: dict[str, Any], *, cap: int = TEXT_BYTE_CAP) -> dict[str, Any] | None:
    """조회 결과를 ``{asset_id, modality, source, text, truncated, byte_cap}`` 로 만든다. 글자가 없으면 ``None``(404). 원본을 못 읽으면 OSError(410)."""
    modality = str(source["modality"])
    kind = TEXT_SOURCE.get(modality)
    if kind == "file":
        fs_path = str(source["fs_path"] or "")
        if not fs_path or not os.path.isfile(fs_path):
            raise OSError(f"원본 파일 없음: {source['asset_id']}")
        text, truncated = read_text_file(fs_path, cap=cap)
    elif kind == "stt" and source["stt"]:
        text = str(source["stt"])
        truncated = len(text.encode("utf-8")) > cap
        if truncated:
            text = text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
    else:
        return None
    return {
        "asset_id": source["asset_id"],
        "modality": modality,
        "source": kind,
        "text": text,
        "truncated": truncated,
        "byte_cap": cap,
    }
