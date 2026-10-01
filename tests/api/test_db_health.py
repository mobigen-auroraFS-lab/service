"""DB 장애 차단기 — DB 가 죽었을 때 요청이 매달리지 않고 곧바로 503 으로 끝나는가."""

from __future__ import annotations

import logging
import os
import sys
import time
import unittest
from unittest import mock

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from service.api import db, db_health, errors, search_health
from service.api.breaker import Breaker
from service.api.db_health import DatabaseUnavailable, guard, is_connection_failure
from service.api.logging_config import LibraryNoiseFilter


class TestClassify(unittest.TestCase):
    def test_연결_실패로_보는_것(self) -> None:
        self.assertTrue(is_connection_failure(PoolTimeout("x")))
        self.assertTrue(is_connection_failure(psycopg.OperationalError("connection refused")))      # sqlstate 없음 = 서버에 닿기 전
        self.assertTrue(is_connection_failure(psycopg.errors.AdminShutdown("x")))
        self.assertTrue(is_connection_failure(psycopg.errors.ConnectionFailure("x")))

    def test_원인_사슬을_따라간다(self) -> None:
        try:
            try:
                raise PoolTimeout("t")
            except PoolTimeout as e:
                raise RuntimeError("감싼 예외") from e
        except RuntimeError as outer:
            self.assertTrue(is_connection_failure(outer))

    def test_그_밖의_DB_오류는_연결_실패가_아니다(self) -> None:
        self.assertFalse(is_connection_failure(psycopg.errors.QueryCanceled("statement timeout")))     # OperationalError 지만 연결 문제가 아니다
        self.assertFalse(is_connection_failure(psycopg.errors.UniqueViolation("dup")))
        self.assertFalse(is_connection_failure(psycopg.errors.DataError("nul")))
        self.assertFalse(is_connection_failure(ValueError("x")))
        self.assertFalse(is_connection_failure(None))


class TestGuard(unittest.TestCase):
    def setUp(self) -> None:
        db_health.reset()
        self.addCleanup(db_health.reset)

    def test_정상이면_그대로_돌려준다(self) -> None:
        self.assertEqual(7, guard(lambda: 7))

    def test_연결_실패이고_접속도_안_되면_차단기를_연다(self) -> None:
        with mock.patch.object(db_health, "_probe", return_value=False), mock.patch.object(db_health.BREAKER, "_loop"):
            with self.assertRaises(DatabaseUnavailable):
                guard(lambda: (_ for _ in ()).throw(PoolTimeout("t")))
            self.assertTrue(db_health.is_down())

    def test_차단_중에는_DB_를_부르지_않는다(self) -> None:
        with mock.patch.object(db_health, "_probe", return_value=False), mock.patch.object(db_health.BREAKER, "_loop"):
            with self.assertRaises(DatabaseUnavailable):
                guard(lambda: (_ for _ in ()).throw(PoolTimeout("t")))
        called = []
        t0 = time.perf_counter()
        with self.assertRaises(DatabaseUnavailable):
            guard(lambda: called.append(1))
        self.assertEqual([], called)
        self.assertLess(time.perf_counter() - t0, 0.1)         # 기다리지 않는다

    def test_접속은_되는데_풀만_꽉_찼으면_차단기는_열지_않는다(self) -> None:
        with mock.patch.object(db_health, "_probe", return_value=True):
            with self.assertRaises(DatabaseUnavailable):
                guard(lambda: (_ for _ in ()).throw(PoolTimeout("t")))
        self.assertFalse(db_health.is_down())                  # 그 요청만 503 · 다음 요청은 평소대로 시도

    def test_연결_실패가_아닌_오류는_그대로_올라간다(self) -> None:
        with self.assertRaises(psycopg.errors.UniqueViolation):
            guard(lambda: (_ for _ in ()).throw(psycopg.errors.UniqueViolation("dup")))
        self.assertFalse(db_health.is_down())

    def test_살아나면_뒤에서_차단기를_닫는다(self) -> None:
        with mock.patch.object(db_health, "_probe", return_value=False), mock.patch.object(db_health.BREAKER, "_loop"):
            with self.assertRaises(DatabaseUnavailable):
                guard(lambda: (_ for _ in ()).throw(PoolTimeout("t")))
        self.assertTrue(db_health.is_down())
        with mock.patch.object(db_health, "_probe", return_value=True), mock.patch.object(db_health.BREAKER, "interval", 0.01):
            db_health.BREAKER._loop()
        self.assertFalse(db_health.is_down())

    def test_안전장치_시간이_지나면_차단기가_저절로_닫힌다(self) -> None:
        with mock.patch.object(db_health, "_probe", return_value=False), mock.patch.object(db_health.BREAKER, "_loop"):
            with self.assertRaises(DatabaseUnavailable):
                guard(lambda: (_ for _ in ()).throw(PoolTimeout("t")))
        with mock.patch.object(db_health.BREAKER, "max_open", 0.0):
            time.sleep(0.01)
            self.assertFalse(db_health.is_down())


class TestBreakerStartup(unittest.TestCase):
    def test_기동_시_접속이_안_되면_바로_연다(self) -> None:
        b = Breaker("시험", lambda: False)
        with mock.patch.object(b, "_loop"):
            b.startup_check()
        self.assertTrue(b.is_down())

    def test_기동_시_접속이_되면_열지_않는다(self) -> None:
        b = Breaker("시험", lambda: True)
        b.startup_check()
        self.assertFalse(b.is_down())


class TestSearchBreaker(unittest.TestCase):
    def test_차단_중이면_엔진을_부르기_전에_SearchUnavailable(self) -> None:
        with mock.patch.object(search_health.BREAKER, "_loop"), mock.patch.object(search_health, "_probe", return_value=False):
            search_health.BREAKER.note_failure(ConnectionError("x"))
        called = []
        with mock.patch("src.search.opensearch_sync.get_client", side_effect=lambda: called.append(1)):
            with self.assertRaises(search_health.SearchUnavailable):
                search_health.get_client()
        self.assertEqual([], called)

    def test_엔진_호출이_연결_실패하면_차단기에_알리고_같은_예외를_올린다(self) -> None:
        from opensearchpy.exceptions import ConnectionError as OSConn

        class Client:
            def search(self, **_k):
                raise OSConn("N/A", "refused", Exception("x"))

            name = "plain"

        with mock.patch("src.search.opensearch_sync.get_client", return_value=Client()), \
                mock.patch.object(search_health, "_probe", return_value=False), mock.patch.object(search_health.BREAKER, "_loop"):
            client = search_health.get_client()
            self.assertEqual("plain", client.name)            # 호출 가능이 아닌 속성은 그대로
            with self.assertRaises(OSConn):                   # 기존 503 처리가 받도록 원래 예외
                client.search(index="x")
            self.assertTrue(search_health.BREAKER.is_down())

    def test_핸들러는_503_과_Retry_After(self) -> None:
        app = FastAPI()
        app.add_exception_handler(search_health.SearchUnavailable, errors.search_unavailable_handler)

        @app.get("/x")
        def x() -> None:
            raise search_health.SearchUnavailable("차단")

        r = TestClient(app, raise_server_exceptions=False).get("/x")
        self.assertEqual((503, "5"), (r.status_code, r.headers["retry-after"]))


class TestHealthIsAsync(unittest.TestCase):
    def test_헬스는_스레드풀을_쓰지_않는다(self) -> None:
        import inspect

        from service.api import health, health_head
        self.assertTrue(inspect.iscoroutinefunction(health))
        self.assertTrue(inspect.iscoroutinefunction(health_head))


class TestWait(unittest.TestCase):
    def test_환경변수(self) -> None:
        for raw, want in (("", 5.0), ("2.5", 2.5), ("x", 5.0), ("0", 5.0), ("-3", 5.0)):
            with mock.patch.dict(os.environ, {db_health.WAIT_ENV: raw}):
                self.assertEqual(want, db_health.wait_seconds(), raw)


class TestRunInDb(unittest.TestCase):
    def setUp(self) -> None:
        db_health.reset()
        self.addCleanup(db_health.reset)

    def test_조회_통로도_쓰기_통로도_연결_실패를_503_용_예외로_바꾼다(self) -> None:
        class Dead:
            def execute_in_transaction(self, _cb, idempotent):
                raise PoolTimeout("couldn't get a connection after 5.00 sec")

        with mock.patch.object(db, "get_db", return_value=Dead()), mock.patch.object(db_health, "_probe", return_value=True):
            with self.assertRaises(DatabaseUnavailable):
                db.run_in_db(lambda conn: 1)
            with self.assertRaises(DatabaseUnavailable):
                db.run_in_db_write(lambda conn: 1)


class TestHandler(unittest.TestCase):
    def test_503_봉투와_Retry_After(self) -> None:
        app = FastAPI()
        app.add_exception_handler(DatabaseUnavailable, errors.db_unavailable_handler)

        @app.get("/x")
        def x() -> None:
            raise DatabaseUnavailable("DB 연결 불가")

        r = TestClient(app, raise_server_exceptions=False).get("/x")
        self.assertEqual(503, r.status_code)
        self.assertEqual("5", r.headers["retry-after"])
        self.assertIsInstance(r.json()["detail"], str)
        self.assertIn("데이터베이스", r.json()["detail"])


class TestNoiseFilter(unittest.TestCase):
    @staticmethod
    def rec(name: str, msg: str, exc: bool = False) -> logging.LogRecord:
        info = None
        if exc:
            try:
                raise ConnectionRefusedError(61, "Connection refused")
            except ConnectionRefusedError:
                info = sys.exc_info()
        return logging.LogRecord(name, logging.WARNING, __file__, 1, msg, (), info)

    def test_라이브러리_로그의_트레이스백을_뗀다(self) -> None:
        f = LibraryNoiseFilter()
        r = self.rec("opensearch", "POST /assets/_search [status:N/A request:1.067s]", exc=True)
        self.assertTrue(f.filter(r))
        self.assertIsNone(r.exc_info)
        self.assertIn("ConnectionRefusedError", r.getMessage())
        self.assertNotIn("Traceback", r.getMessage())

    def test_숫자만_다른_같은_경고는_한_번만_남기고_다음에_생략_건수를_붙인다(self) -> None:
        f = LibraryNoiseFilter()
        with mock.patch("service.api.logging_config.time") as clock:
            clock.monotonic.side_effect = [0.0, 1.0, 2.0, 40.0]
            first = self.rec("psycopg.pool", "error connecting in 'pool-1': connection refused 172")
            self.assertTrue(f.filter(first))
            self.assertFalse(f.filter(self.rec("psycopg.pool", "error connecting in 'pool-1': connection refused 99")))
            self.assertFalse(f.filter(self.rec("psycopg.pool", "error connecting in 'pool-1': connection refused 5")))
            again = self.rec("psycopg.pool", "error connecting in 'pool-1': connection refused 3")
            self.assertTrue(f.filter(again))              # 30초가 지났다
            self.assertIn("같은 경고 2건 생략", again.getMessage())

    def test_코어_DB_도구의_실제_로거_이름도_잡는다(self) -> None:
        # 서버 로그에는 짧은 이름(postgres_util)으로 보이지만 실제 이름은 src.database.postgres_util 이다 — 이름 **앞**만 보면 놓친다.
        f = LibraryNoiseFilter()
        r = self.rec("src.database.postgres_util", "Operation execute_in_transaction failed (attempt 3/3, idempotent=True).", exc=True)
        self.assertTrue(f.filter(r))
        self.assertIsNone(r.exc_info)
        self.assertNotIn("Traceback", r.getMessage())

    def test_우리_로그는_건드리지_않는다(self) -> None:
        f = LibraryNoiseFilter()
        for _ in range(3):
            r = self.rec("meta_extract.portal_api", "같은 문장", exc=True)
            self.assertTrue(f.filter(r))
            self.assertIsNotNone(r.exc_info)


if __name__ == "__main__":
    unittest.main()
