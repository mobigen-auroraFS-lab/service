"""자산의 **글자 내용**을 읽어 준다 — 상세 화면의 원문 영역용.

어디서 오는가(모달리티마다 다르다)
  · 문서(text)  — 원본 파일을 그대로 읽는다. 추출본을 따로 보관하지 않으므로 이것이 원문이다.
  · 소리·영상   — 받아쓰기(``ext_meta.stt``)가 있으면 그것을 준다. 원본은 글자가 아니다.
                  🔴 [2026-09-22 실데이터] 받아쓰기는 **소리에만** 있다(675건 중 580건). 영상은
                  파이프라인이 받아쓰기를 만들지 않아(장면별 설명 ``keyframes`` 만 있다) 지금은
                  전부 404 다. 영상에 ``stt`` 가 생기면 코드를 고치지 않아도 그대로 나온다.
  · 그림        — 글자가 없다(요약·라벨은 상세 응답의 ``ext_meta`` 에 이미 있다).

원본은 DB 에 적힌 경로에 있다고 본다(2026-10-01 · 없으면 ``OSError`` → 호출부가 410).

🔴 **상한을 둔다**(기본 1MiB). 원본이 수백 MB 일 수 있고, 그것을 그대로 JSON 에 실으면 응답이
   메모리와 대역을 통째로 먹는다. 자른 경우 ``truncated`` 로 알린다 — 조용히 자르면 화면은
   원문이 거기서 끝난 줄 안다.
"""

from __future__ import annotations

import os
from typing import Any

# 한 번에 실어 보낼 최대 글자 바이트(1MiB). 넘으면 잘라서 ``truncated=True``.
TEXT_BYTE_CAP = 1024 * 1024

# 글자로 읽을 수 있는 모달리티와 그 출처.
TEXT_SOURCE = {"text": "file", "audio": "stt", "video": "stt"}


def read_text_file(fs_path: str, *, cap: int = TEXT_BYTE_CAP) -> tuple[str, bool]:
    """파일 앞부분을 글자로 읽는다(순수 IO · 상한까지만).

    Args:
        fs_path: 읽을 파일 경로.
        cap: 읽을 최대 바이트.

    Returns:
        ``(글자, 잘렸는지)``. 해독할 수 없는 바이트는 ``\\ufffd`` 로 바꾼다 — 인코딩 하나 때문에
        원문 전체를 못 보여 주는 것보다 낫다.

    Raises:
        OSError: 파일을 열 수 없을 때(호출부가 410 으로 바꾼다).
    """
    with open(fs_path, "rb") as fp:
        raw = fp.read(cap + 1)
    truncated = len(raw) > cap
    return raw[:cap].decode("utf-8", errors="replace"), truncated


def build_content(source: dict[str, Any], *, cap: int = TEXT_BYTE_CAP) -> dict[str, Any] | None:
    """조회 결과를 응답 모양(``{asset_id, modality, source, text, truncated, byte_cap}``)으로 만든다.

    글자가 없으면 ``None``(호출부가 404).

    Raises:
        OSError: 문서인데 원본 파일을 읽을 수 없을 때(호출부가 410).
    """
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
