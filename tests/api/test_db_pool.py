"""DB 커넥션 풀 크기 환경변수 — 미설정 시 코어 기본값 유지, 설정 시 그 항목만 덮어쓴다. 실DB 0.

배경: 라우트가 전부 동기라 스레드풀(기본 40)에서 도는데 코어 풀 기본값은 max 10 이라
동시 요청이 10을 넘으면 커넥션 대기가 쌓인다. 풀을 배포에서 맞출 수 있게 연 손잡이를 봉인한다.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from service.api import db as papi


class TestPoolSizeOverride(unittest.TestCase):
    """환경변수 파싱 — 잘못된 값이 기동을 막지 않고 코어 기본값으로 접힌다."""

    def test_unset_yields_none(self) -> None:
        # 미설정 = "코어 기본값을 그대로 쓴다". 0 이나 10 같은 숫자로 채우지 않는다 —
        # 그러면 코어가 기본값을 바꿔도 이쪽이 낡은 값으로 덮어쓴다.
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(papi.POOL_MIN_ENV, None)
            os.environ.pop(papi.POOL_MAX_ENV, None)
            self.assertEqual(papi.pool_size_override(), (None, None))

    def test_valid_values_parsed(self) -> None:
        with patch.dict("os.environ", {papi.POOL_MIN_ENV: "4", papi.POOL_MAX_ENV: "40"}):
            self.assertEqual(papi.pool_size_override(), (4, 40))

    def test_non_integer_falls_back(self) -> None:
        # 오타에 기동을 막지 않는다(풀 크기는 성능 손잡이지 정확성 조건이 아니다).
        with patch.dict("os.environ", {papi.POOL_MAX_ENV: "많이"}):
            self.assertEqual(papi.pool_size_override()[1], None)

    def test_below_one_rejected(self) -> None:
        # 0·음수는 풀을 만들 수 없는 값이라 무시한다(코어 검증에 닿기 전에 접는다).
        with patch.dict("os.environ", {papi.POOL_MIN_ENV: "0", papi.POOL_MAX_ENV: "-3"}):
            self.assertEqual(papi.pool_size_override(), (None, None))


class TestNewDb(unittest.TestCase):
    """``_new_db`` — 미설정이면 코어에 그대로 맡기고, 설정 시에만 config 를 조립한다."""

    def test_unset_delegates_to_core(self) -> None:
        # 접속 정보 해석(DSN 우선 → 개별 환경변수)을 코어가 소유한다 — 인자 없이 만들어야
        # 그 규칙이 이쪽에 복제되지 않는다.
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(papi.POOL_MIN_ENV, None)
            os.environ.pop(papi.POOL_MAX_ENV, None)
            with patch("src.database.postgres_util.PostgresUtil") as mk:
                papi.new_db()
        mk.assert_called_once_with()

    def test_override_sets_pool_and_keeps_dsn_precedence(self) -> None:
        # DSN 이 있으면 접속은 DSN 이 정하고(``_build_conninfo``), 풀 크기는 config 가 정한다
        # (``open_pool``). 둘을 함께 넘기는 것이 그 조합을 얻는 유일한 방법이다.
        env = {
            papi.POOL_MAX_ENV: "40",
            "DATABASE_URL": "postgresql://u@h/db",
        }
        with patch.dict("os.environ", env):
            with patch("src.database.postgres_util.PostgresUtil") as mk:
                papi.new_db()
        kwargs = mk.call_args.kwargs
        self.assertEqual(kwargs["dsn"], "postgresql://u@h/db")
        self.assertEqual(kwargs["config"].max_pool_size, 40)

    def test_override_without_dsn_passes_none(self) -> None:
        with patch.dict("os.environ", {papi.POOL_MAX_ENV: "25"}):
            import os

            os.environ.pop("DATABASE_URL", None)
            os.environ.pop("POSTGRES_DSN", None)
            with patch("src.database.postgres_util.PostgresUtil") as mk:
                papi.new_db()
        kwargs = mk.call_args.kwargs
        self.assertIsNone(kwargs["dsn"])
        self.assertEqual(kwargs["config"].max_pool_size, 25)


class TestThreadLimitAlignment(unittest.TestCase):
    """스레드 상한을 풀 상한에 맞춘다(2026-09-28) — 어떤 경우에도 기동을 막지 않는다."""

    def _run(self, env: dict[str, str], tokens: int = 40) -> MagicMock:
        limiter = MagicMock()
        limiter.total_tokens = tokens
        with patch("anyio.to_thread.current_default_thread_limiter", return_value=limiter), \
             patch.dict("os.environ", env):
            papi.align_thread_limit_to_pool()
        return limiter

    def test_스레드가_풀보다_많으면_풀에_맞춘다(self) -> None:
        self.assertEqual(10, self._run({papi.POOL_MAX_ENV: "10"}).total_tokens)

    def test_이미_작으면_그대로(self) -> None:
        self.assertEqual(8, self._run({papi.POOL_MAX_ENV: "10"}, tokens=8).total_tokens)

    def test_배포가_정한_값을_쓴다_풀보다_크면_경고(self) -> None:
        with self.assertLogs(papi._LOG, level="WARNING") as cm:
            lim = self._run({papi.POOL_MAX_ENV: "10", papi.THREAD_LIMIT_ENV: "30"})
        self.assertEqual(30, lim.total_tokens)
        self.assertIn("커넥션 풀 상한", "".join(cm.output))

    def test_never_raises(self) -> None:
        # async 컨텍스트 밖·anyio 미가용 등에서 예외가 새면 기동이 죽는다.
        with patch("anyio.to_thread.current_default_thread_limiter", side_effect=RuntimeError):
            papi.align_thread_limit_to_pool()  # 예외 없이 반환하면 통과

if __name__ == "__main__":
    unittest.main()
