"""093 5단계 — 백엔드 부트스트랩이 코어 ``bootstrap_env`` 를 **자기 루트 · 서빙 역할**로 부르는지 봉인한다.

무엇을 봉인하나: ① 복사본 없이 코어 함수 호출 한 줄 ② ``repo_root`` 가 백엔드 레포 루트(코어가 아님)
③ ``role="serving"`` 전달 ④ 반환값은 코어가 만든 설정 그대로. 실 ``.env``·실 설정은 건드리지 않는다(대역).
"""
from __future__ import annotations

import unittest
from unittest import mock

from service import bootstrap
from src.config import bootstrap as core_bootstrap


class TestServiceBootstrap(unittest.TestCase):
    def test_calls_core_with_own_repo_root_and_serving_role(self) -> None:
        sentinel = object()
        with mock.patch.object(bootstrap, "core_bootstrap_env", return_value=sentinel) as m:
            out = bootstrap.bootstrap_env("dev")
        m.assert_called_once_with("dev", repo_root=bootstrap._REPO_ROOT, role="serving")
        self.assertIs(out, sentinel)

    def test_repo_root_is_the_service_repo_not_core(self) -> None:
        root = bootstrap._REPO_ROOT
        self.assertTrue((root / "service").is_dir(), root)
        self.assertNotEqual(root, core_bootstrap._REPO_ROOT)

    def test_wrapper_is_the_core_function(self) -> None:
        # 복사본이 아니라 코어 함수를 그대로 참조한다(설정 스키마·탐색 규칙 단일 출처).
        self.assertIs(bootstrap.core_bootstrap_env, core_bootstrap.bootstrap_env)


if __name__ == "__main__":
    unittest.main()
