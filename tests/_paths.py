"""테스트가 쓰는 경로 단일 출처(순수·표준 라이브러리만).

**왜 두는가**: 테스트가 ``Path(__file__).parents[N]`` 로 레포 루트를 세면, 파일이 폴더 한 단계
아래로 옮겨질 때마다 N 이 어긋난다. 실제로 테스트를 소스와 같은 구조로 재배치하면서 6곳이
한꺼번에 깨졌다 — 깊이에 무관하게 **표식을 보고 올라가** 찾는다.
"""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """``pyproject.toml`` 이 있는 가장 가까운 상위 디렉터리 = 이 레포 루트."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(f"레포 루트를 찾지 못했습니다: {here}")


ROOT = repo_root()
SERVICE_ROOT = ROOT / "service"
TESTS_ROOT = ROOT / "tests"
ENV_DEV = ROOT / ".env.dev"
