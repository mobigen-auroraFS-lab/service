"""접속 주소 제한(선택) — ``PORTAL_ALLOWED_CLIENT_CIDRS`` 에 적은 네트워크 밖에서 온 요청은 403 봉투로 거절한다.

비어 있으면 아무것도 하지 않고, 루프백은 늘 허용한다. 인증을 끈 개발 서버를 사내망에 열어 둘 때 쓴다. 프록시 뒤에서는 보이는 주소가 프록시 것이라 쓸 수 없다.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Any

_LOG = logging.getLogger("meta_extract.portal_api")
ENV = "PORTAL_ALLOWED_CLIENT_CIDRS"
_LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


def parse_networks(raw: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            _LOG.warning("%s 의 %r 는 주소 형식이 아니다 — 무시한다", ENV, part)
    return out


def configured() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return parse_networks(os.getenv(ENV, ""))


class ClientAllowlistMiddleware:
    """순수 ASGI — 허용 목록이 비어 있으면 그대로 통과한다(요청마다 하는 일이 없다)."""

    def __init__(self, app: Any, networks: list | None = None) -> None:
        self.app = app
        self.networks = configured() if networks is None else networks

    def allowed(self, host: str | None) -> bool:
        if not self.networks:
            return True
        try:
            ip = ipaddress.ip_address((host or "").split("%")[0])
        except ValueError:
            return False
        return any(ip in n for n in (*_LOOPBACK, *self.networks))

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.networks:
            await self.app(scope, receive, send)
            return
        host = (scope.get("client") or (None, 0))[0]
        if self.allowed(host):
            await self.app(scope, receive, send)
            return
        from service.api.errors import envelope
        await envelope(403, "허용되지 않은 접속 주소입니다")(scope, receive, send)
