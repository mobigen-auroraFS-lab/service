"""로그 — 요청 ID · 요청 한 줄 · 설정(수준 · 형식) · 민감값이 줄에 남지 않는가."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import unittest
import warnings
from unittest import mock

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from service.api import errors, logging_config
from service.api.logging_config import JsonFormatter, RequestIdFilter, request_id_var
from service.api.request_log import (
    REQUEST_ID_HEADER,
    RequestLogMiddleware,
    printable_path,
    slow_request_ms,
)

ACCESS = "meta_extract.portal_api.access"


class _Capture:
    """운영과 같은 줄 — 핸들러에 ``RequestIdFilter`` 를 달아 문맥의 요청 ID 가 실리는지까지 본다."""

    def __init__(self, name: str, level: int = logging.DEBUG) -> None:
        self.records: list[logging.LogRecord] = []
        cap = self

        class _H(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                cap.records.append(record)

        self._h = _H()
        self._h.addFilter(RequestIdFilter())
        self._lg = logging.getLogger(name)
        self._old = (self._lg.level, self._lg.propagate)
        self._level = level

    def __enter__(self) -> _Capture:
        self._lg.addHandler(self._h)
        self._lg.setLevel(self._level)
        self._lg.propagate = False
        return self

    def __exit__(self, *exc) -> None:
        self._lg.removeHandler(self._h)
        self._lg.level, self._lg.propagate = self._old


def _app(slow_ms: int = 0) -> TestClient:
    def ok(_r):
        return PlainTextResponse("ok")

    def boom(_r):
        raise RuntimeError("비밀 값이 든 메시지")

    def echo_id(_r):
        return PlainTextResponse(request_id_var.get())

    def not_ready(_r):
        return PlainTextResponse("아직 제공하지 않음", status_code=501)

    app = Starlette(routes=[Route("/x", ok), Route("/health", ok), Route("/boom", boom), Route("/id", echo_id), Route("/soon", not_ready)])
    app.add_middleware(RequestLogMiddleware, slow_ms=slow_ms)
    return TestClient(app, raise_server_exceptions=False)


class TestRequestId(unittest.TestCase):
    def test_없으면_새로_만들어_응답_머리에_돌려준다(self) -> None:
        r = _app().get("/x")
        rid = r.headers[REQUEST_ID_HEADER]
        self.assertRegex(rid, r"^[0-9a-f]{12}$")

    def test_앞단이_보낸_ID_는_모양이_맞으면_그대로(self) -> None:
        r = _app().get("/x", headers={"X-Request-ID": "gw-abc_123.4"})
        self.assertEqual("gw-abc_123.4", r.headers[REQUEST_ID_HEADER])

    def test_모양이_틀린_ID_는_버리고_새로_만든다(self) -> None:
        for bad in ("a b", "x" * 65, "a;b", "a/b", "a,b"):
            with self.subTest(bad=bad):
                got = _app().get("/x", headers={"X-Request-ID": bad}).headers[REQUEST_ID_HEADER]
                self.assertNotEqual(bad, got)
                self.assertRegex(got, r"^[0-9a-f]{12}$")

    def test_핸들러_안에서_같은_ID_가_보인다(self) -> None:
        r = _app().get("/id", headers={"X-Request-ID": "rid-1"})
        self.assertEqual("rid-1", r.text)

    def test_요청이_끝나면_문맥을_돌려놓는다(self) -> None:
        _app().get("/x")
        self.assertEqual(logging_config.NO_REQUEST, request_id_var.get())

    def test_요청마다_다른_ID(self) -> None:
        c = _app()
        self.assertNotEqual(c.get("/x").headers[REQUEST_ID_HEADER], c.get("/x").headers[REQUEST_ID_HEADER])


class TestAccessLine(unittest.TestCase):
    def test_경로까지만_남기고_쿼리_문자열은_남기지_않는다(self) -> None:
        with self.assertLogs(ACCESS, "INFO") as cm:
            _app().get("/x?q=비밀검색어&cursor=eyJzZWNyZXQiOjF9")
        line = cm.records[0]
        method, path, status, ms, client = line.getMessage().split(" ")
        self.assertEqual(("GET", "/x", "200"), (method, path, status))
        self.assertTrue(ms.endswith("ms"))
        self.assertEqual(line.client, client)
        self.assertNotIn("비밀검색어", line.getMessage())
        self.assertNotIn("eyJ", line.getMessage())
        self.assertEqual(("GET", "/x", 200), (line.method, line.path, line.status))
        self.assertGreaterEqual(line.duration_ms, 0)

    def test_요청_ID_가_줄에_붙는다(self) -> None:
        with _Capture(ACCESS) as cap:
            _app().get("/x", headers={"X-Request-ID": "rid-2"})
        self.assertEqual("rid-2", cap.records[0].request_id)

    def test_토큰과_헤더는_남기지_않는다(self) -> None:
        with self.assertLogs(ACCESS, "INFO") as cm:
            _app().get("/x", headers={"Authorization": "Bearer SECRET-TOKEN", "Cookie": "sid=SECRET-COOKIE"})
        text = json.dumps(cm.records[0].__dict__, default=str)
        self.assertNotIn("SECRET", text)

    def test_5xx_는_ERROR(self) -> None:
        with self.assertLogs(ACCESS, "INFO") as cm:
            r = _app().get("/boom")
        self.assertEqual(500, r.status_code)
        self.assertEqual("ERROR", cm.records[0].levelname)
        self.assertEqual(500, cm.records[0].status)

    def test_501_은_고장이_아니라_WARNING(self) -> None:
        # 자리만 있는 창구(썸네일)를 부를 때마다 오류 알람이 울리면 진짜 5xx 가 묻힌다.
        with self.assertLogs(ACCESS, "INFO") as cm:
            r = _app().get("/soon")
        self.assertEqual(501, r.status_code)
        self.assertEqual("WARNING", cm.records[0].levelname)
        self.assertEqual(501, cm.records[0].status)

    def test_헬스_체크는_DEBUG(self) -> None:
        with self.assertLogs(ACCESS, "DEBUG") as cm:
            _app().get("/health")
        self.assertEqual("DEBUG", cm.records[0].levelname)

    def test_헬스_체크는_INFO_에서는_안_보인다(self) -> None:
        with self.assertNoLogs(ACCESS, "INFO"):
            _app().get("/health")

    def test_기준을_넘긴_요청은_WARNING(self) -> None:
        # 기준 1ms — 실제 처리 시간을 늘리지 않고 시계를 돌린다.
        with mock.patch("service.api.request_log.time") as clock:
            clock.perf_counter.side_effect = [0.0, 5.0]
            with self.assertLogs(ACCESS, "INFO") as cm:
                _app(slow_ms=1000).get("/x")
        rec = cm.records[0]
        self.assertEqual("WARNING", rec.levelname)
        self.assertTrue(rec.getMessage().startswith("느린 요청 GET /x 200"))

    def test_기준이_0_이면_느린_요청을_따로_치지_않는다(self) -> None:
        with mock.patch("service.api.request_log.time") as clock:
            clock.perf_counter.side_effect = [0.0, 99.0]
            with self.assertLogs(ACCESS, "INFO") as cm:
                _app(slow_ms=0).get("/x")
        self.assertEqual("INFO", cm.records[0].levelname)

    def test_경로의_제어문자는_줄을_위조하지_못한다(self) -> None:
        self.assertEqual("/a\\x0ab\\x1b", printable_path("/a\nb\x1b"))
        self.assertTrue(printable_path("/" + "a" * 500).endswith("…"))
        self.assertLessEqual(len(printable_path("/" + "a" * 500)), 201)


class TestSlowEnv(unittest.TestCase):
    def test_기본_3000(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORTAL_SLOW_REQUEST_MS", None)
            self.assertEqual(3000, slow_request_ms())

    def test_값과_오류(self) -> None:
        for raw, want in (("500", 500), ("0", 0), ("-5", 0), ("abc", 3000)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"PORTAL_SLOW_REQUEST_MS": raw}):
                self.assertEqual(want, slow_request_ms())


class TestServerErrorHandler(unittest.TestCase):
    """미처리 예외 처리기는 요청 로그 층 바깥이라 ID 를 scope 로 받는다."""

    def _call(self, scope_extra: dict):
        scope = {"type": "http", "method": "GET", "path": "/x", "query_string": b"", "headers": [], **scope_extra}
        return asyncio.run(errors.unhandled_exception_handler(Request(scope), RuntimeError("비밀 값이 든 메시지")))

    def test_응답_머리와_로그_줄에_요청_ID(self) -> None:
        with self.assertLogs("meta_extract.portal_api", "ERROR") as cm:
            r = self._call({"request_id": "rid-9"})
        self.assertEqual("rid-9", r.headers[REQUEST_ID_HEADER])
        self.assertEqual("rid-9", cm.records[0].request_id)

    def test_예외_종류만_남기고_메시지와_스택은_남기지_않는다(self) -> None:
        with self.assertLogs("meta_extract.portal_api", "ERROR") as cm:
            self._call({"request_id": "rid-9"})
        rec = cm.records[0]
        self.assertIn("RuntimeError", rec.getMessage())
        self.assertNotIn("비밀", rec.getMessage())
        self.assertIsNone(rec.exc_info)

    def test_ID_가_없어도_500_봉투는_나간다(self) -> None:
        with self.assertLogs("meta_extract.portal_api", "ERROR"):
            r = self._call({})
        self.assertEqual(500, r.status_code)
        self.assertNotIn(REQUEST_ID_HEADER, r.headers)


def _record(msg: str = "안녕", **extra) -> logging.LogRecord:
    rec = logging.LogRecord("meta_extract.x", logging.INFO, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


class TestFormatters(unittest.TestCase):
    def test_text_는_읽기_쉽게_줄인다(self) -> None:
        for name, want in (("meta_extract.portal_api", "api"), ("meta_extract.portal_api.access", "access"),
                           ("uvicorn.error", "uvicorn"), ("uvicorn", "uvicorn"), ("py.warnings", "warn"),
                           ("src.database.postgres_util", "postgres_util"), ("plain", "plain")):
            self.assertEqual(want, logging_config.short_logger_name(name), name)
        rec = _record()
        rec.levelname = "WARNING"
        RequestIdFilter().filter(rec)
        self.assertEqual(("WARN", ""), (rec.lvl, rec.rid))      # 요청 밖의 줄에는 ID 칸이 없다
        rec2 = _record(request_id="abc")
        RequestIdFilter().filter(rec2)
        self.assertEqual(" [abc]", rec2.rid)

    def test_JSON_은_줄이지_않고_text_전용_칸은_싣지_않는다(self) -> None:
        rec = _record(request_id="r1")
        RequestIdFilter().filter(rec)
        data = json.loads(JsonFormatter().format(rec))
        self.assertEqual("meta_extract.x", data["logger"])
        for key in ("lvl", "short", "rid"):
            self.assertNotIn(key, data)

    def test_필터는_문맥_ID_를_싣고_줄에_준_값을_우선한다(self) -> None:
        token = request_id_var.set("ctx-1")
        try:
            a, b = _record(), _record(request_id="given")
            RequestIdFilter().filter(a)
            RequestIdFilter().filter(b)
        finally:
            request_id_var.reset(token)
        self.assertEqual(("ctx-1", "given"), (a.request_id, b.request_id))

    def test_JSON_한_줄이_JSON_한_건(self) -> None:
        rec = _record("한글 메시지 7", request_id="r1", status=200, path="/x")
        line = JsonFormatter().format(rec)
        self.assertNotIn("\n", line)
        data = json.loads(line)
        self.assertEqual(("INFO", "meta_extract.x", "한글 메시지 7", "r1"),
                         (data["level"], data["logger"], data["msg"], data["request_id"]))
        self.assertEqual((200, "/x"), (data["status"], data["path"]))
        self.assertTrue(data["ts"].endswith("Z"))

    def test_JSON_예외는_exc_칸에(self) -> None:
        try:
            raise ValueError("x")
        except ValueError:
            import sys
            rec = logging.LogRecord("n", logging.ERROR, __file__, 1, "m", (), sys.exc_info())
        data = json.loads(JsonFormatter().format(rec))
        self.assertIn("ValueError", data["exc"])
        self.assertNotIn("\n", JsonFormatter().format(rec))


class TestConfigure(unittest.TestCase):
    def setUp(self) -> None:
        self._showwarning = warnings.showwarning
        self._root = (logging.getLogger().handlers[:], logging.getLogger().level)
        self._names = ("uvicorn", "uvicorn.error", "uvicorn.access", "opensearch", "urllib3", "httpx", "httpcore")
        self._lg = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).level, logging.getLogger(n).propagate)
                    for n in self._names}

    def tearDown(self) -> None:
        warnings.showwarning = self._showwarning
        root = logging.getLogger()
        root.handlers[:], root.level = self._root
        for n, (h, lv, pr) in self._lg.items():
            lg = logging.getLogger(n)
            lg.handlers[:], lg.level, lg.propagate = h, lv, pr

    def test_수준과_형식_해석(self) -> None:
        self.assertEqual(("INFO", None), logging_config.resolve_level(None))
        self.assertEqual(("DEBUG", None), logging_config.resolve_level(" debug "))
        self.assertEqual(("WARNING", None), logging_config.resolve_level("warn"))
        level, warn = logging_config.resolve_level("loud")
        self.assertEqual("INFO", level)
        self.assertIn("loud", warn)
        self.assertEqual(("text", None), logging_config.resolve_format(""))
        self.assertEqual(("json", None), logging_config.resolve_format("JSON"))
        self.assertEqual("text", logging_config.resolve_format("xml")[0])

    def test_표준출력_하나로_모으고_uvicorn_접근_로그는_끈다(self) -> None:
        logging_config.configure_logging("WARNING", "json")
        root = logging.getLogger()
        self.assertEqual(logging.WARNING, root.level)
        self.assertEqual(1, len(root.handlers))
        self.assertIsInstance(root.handlers[0].formatter, JsonFormatter)
        # uvicorn 은 자기 핸들러를 비우고 루트로 흘린다 — 접근 로그는 INFO 가 나가지 않는다
        self.assertEqual([], logging.getLogger("uvicorn.error").handlers)
        self.assertTrue(logging.getLogger("uvicorn.error").propagate)
        access = logging.getLogger("uvicorn.access")
        self.assertFalse(access.isEnabledFor(logging.INFO))
        self.assertFalse(access.propagate)
        # 잡음이 큰 라이브러리
        self.assertFalse(logging.getLogger("httpx").isEnabledFor(logging.INFO))

    def test_여러_번_불러도_핸들러가_쌓이지_않는다(self) -> None:
        for _ in range(3):
            logging_config.configure_logging("INFO", "text")
        self.assertEqual(1, len(logging.getLogger().handlers))

    def test_실제_줄이_표준출력에_JSON_으로_나간다(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            logging_config.configure_logging("INFO", "json")
            token = request_id_var.set("rid-7")
            try:
                logging.getLogger("meta_extract.portal_api").info("본문 검증 통과")
            finally:
                request_id_var.reset(token)
        lines = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
        last = lines[-1]
        self.assertEqual(("본문 검증 통과", "rid-7"), (last["msg"], last["request_id"]))

    def test_텍스트_형식에_요청_ID_가_보인다(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            logging_config.configure_logging("INFO", "text")
            token = request_id_var.set("rid-8")
            try:
                logging.getLogger("meta_extract.portal_api").info("한 줄")
            finally:
                request_id_var.reset(token)
        line = [x for x in buf.getvalue().splitlines() if "한 줄" in x][0]
        self.assertRegex(line, r"^\d\d-\d\d \d\d:\d\d:\d\d INFO  api: 한 줄 \[rid-8\]$")

    def test_잘못된_값은_기본값과_경고(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.assertEqual(("INFO", "text"), logging_config.configure_logging("loud", "xml"))
        self.assertIn("loud", buf.getvalue())
        self.assertIn("xml", buf.getvalue())

    def test_파이썬_경고도_같은_형식_한_줄로_모은다(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            logging_config.configure_logging("INFO", "text")
            with warnings.catch_warnings():
                warnings.simplefilter("always")
                warnings.warn("키가 짧다", UserWarning, stacklevel=1)
        lines = [x for x in buf.getvalue().splitlines() if "키가 짧다" in x]
        self.assertEqual(1, len(lines))
        self.assertIn(" WARN  warn: UserWarning: 키가 짧다 (test_logging.py:", lines[0])

    def test_환경변수로_건너뛴다(self) -> None:
        with mock.patch.dict(os.environ, {"PORTAL_LOG_CONFIGURE": "0"}), \
                mock.patch.object(logging_config, "configure_logging") as cfg:
            self.assertFalse(logging_config.configure_from_env())
        cfg.assert_not_called()
        with mock.patch.dict(os.environ, {"PORTAL_LOG_CONFIGURE": "1"}), \
                mock.patch.object(logging_config, "configure_logging") as cfg:
            self.assertTrue(logging_config.configure_from_env())
        cfg.assert_called_once()


class TestAppWiring(unittest.TestCase):
    """실제 앱에서 — 응답마다 ID 가 붙고, 가장 바깥이라 거절 응답에도 붙는다."""

    def test_모든_응답에_요청_ID(self) -> None:
        from service.api import app
        c = TestClient(app, raise_server_exceptions=False)
        for path in ("/health", "/nope", "/file-search?sort=bad"):
            with self.subTest(path=path):
                self.assertRegex(c.get(path).headers[REQUEST_ID_HEADER], r"^[0-9a-f]{12}$")

    def test_NUL_400_도_ID_를_단다(self) -> None:
        from service.api import app
        r = TestClient(app).get("/file-search", params={"q": "a\x00b"})
        self.assertEqual(400, r.status_code)
        self.assertIn(REQUEST_ID_HEADER, r.headers)

    def test_본문_상한_413_도_ID_를_단다(self) -> None:
        from service.api import app
        r = TestClient(app).post("/auth/login", content=b"x" * (2 * 1024 * 1024),
                                 headers={"Content-Type": "application/json"})
        self.assertEqual(413, r.status_code)
        self.assertIn(REQUEST_ID_HEADER, r.headers)

    def test_요청마다_접근_한_줄(self) -> None:
        from service.api import app
        with self.assertLogs(ACCESS, "INFO") as cm:
            TestClient(app).get("/nope?q=비밀")
        self.assertEqual(1, len(cm.records))
        self.assertEqual(404, cm.records[0].status)
        self.assertNotIn("비밀", cm.records[0].getMessage())


if __name__ == "__main__":
    unittest.main()
