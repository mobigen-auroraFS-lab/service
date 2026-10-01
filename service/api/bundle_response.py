"""묶음(zip) 응답 만들기 — 네 묶음 창구가 함께 쓴다(고른 자산 · 관계 · 개체 목록 · 개체 카드).

대상 확정(DB)과 상한 판정은 창구마다 다르고, 여기서는 **응답 모양**만 맞춘다: 시작 전에 계획(``plan_bundle``)을 세워 건수 · 용량 헤더를
먼저 정하고, ZIP 은 임시 파일 없이 만들면서 흘려 보낸다(``bundle_stream`` — 방식 선택의 배경과 대안은 그 모듈 설명).

헤더(``X-Bundle-*`` — CORS 노출 목록에 있다)
  · ``X-Bundle-Count``(또는 고른 자산 묶음의 ``X-Bundle-Files``) — ZIP 에 **담길** 자산 수. 시작 전에 파일을 확인해 센다(DB 기준이 아니다).
  · ``X-Bundle-Missing`` — 빠진 수(노출 대상이 아니라 뺀 것 + 원본이 없는 것). 빠진 이름은 ZIP 끝의 ``_manifest.json`` 에 있다.
  · ``X-Bundle-Bytes`` — 담길 원본의 **합계 크기**. ZIP 은 크기를 미리 알 수 없어 ``Content-Length`` 가 없다 — 화면이 진행률을 어림할 때 쓴다.
  · ``X-Bundle-Truncated`` — 개체 카드가 상한(200건)에서 잘렸을 때만.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from service.portal.asset.bundle_stream import BundlePlan, plan_bundle, stream_zip

_LOG = logging.getLogger("meta_extract.portal_api")


def make_plan(targets: list[dict[str, Any]]) -> BundlePlan:
    """대상의 존재 · 크기를 확인한다(응답을 시작하기 **전에** 부른다 — 상한 · 오류 판정과 헤더가 이 값을 쓴다)."""
    return plan_bundle(targets)


def zip_response(plan: BundlePlan, *, content_disposition: str, headers: dict[str, str]) -> StreamingResponse:
    """계획대로 ZIP 을 만들면서 흘려 보내는 응답.

    Args:
        plan: ``make_plan`` 이 만든 계획.
        content_disposition: 완성된 ``Content-Disposition`` 헤더 값.
        headers: 함께 실을 ``X-Bundle-*`` 헤더.

    Returns:
        ``application/zip`` 스트리밍 응답(``Content-Length`` 없음 — 청크 전송).
    """
    _LOG.info("묶음 시작: %d건 · 원본 %d바이트 · 빠짐 %d건", len(plan.packed), plan.total_bytes, len(plan.unreadable))
    stream = stream_zip(plan)
    return StreamingResponse(
        stream,
        media_type="application/zip",
        headers={"Content-Disposition": content_disposition, "Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff", **headers},
        # 받다가 끊기면 스트리밍이 취소되지만 제너레이터는 멈춘 채 남는다 — 열어 둔 원본 핸들은 가비지 수집 때까지 안 닫힌다
        # (2026-10-01 끝에서 끝까지 실측으로 확인). 응답이 어떻게 끝나든 여기서 닫는다(닫으면 ``with`` 가 핸들을 닫는다).
        background=BackgroundTask(stream.close),
    )
