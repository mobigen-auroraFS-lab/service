"""백엔드 부트스트랩 — 코어 ``bootstrap_env`` 를 **자기 레포 루트 · 서빙 역할**로 부른다(093 5단계).

무엇을 하는가: 서버가 뜰 때 ``dataplatform-service/.env.{env}`` 를 읽고 코어 설정을 초기화한다. 종전에는
코어 함수가 코어 레포 위치 기준으로 ``.env`` 를 찾아서(A8) 백엔드가 같은 5줄을 복사해 자기 루트를 계산했다.
코어가 ``repo_root=`` 를 받게 되면서 복사본은 사라지고 이 모듈은 **호출 한 줄**만 남긴다 — 설정 스키마·필수
env 검증·``.env`` 탐색 규칙은 코어 단일 출처다.

``role="serving"``: 백엔드는 텍스트 청킹·요약 길이·키워드 수·파일 인코딩 같은 **적재 전용 값을 읽지 않는다**.
그래서 코어 설정을 서빙 역할로 초기화해 그 5개(``ENCODING``·``CHUNK_SIZE``·``OVERLAP_SIZE``·``SUMMARY_MAX_CHARS``·
``TOP_K_KEYWORDS``)가 없어도 뜬다. 질의 임베딩 모델·LLM 접속·DB·OpenSearch 는 서빙도 쓰므로 그대로 필수다.

탐색 순서는 코어 규칙 그대로 "작업 디렉터리 → 이 레포 루트"(두 곳에 있으면 앞선 것 하나만). 보통 서버는
레포 루트에서 띄우므로 두 후보가 같은 파일이다.
"""
from __future__ import annotations

from pathlib import Path

from src.config.bootstrap import bootstrap_env as core_bootstrap_env
from src.config.settings import PipelineSettings

# service/bootstrap.py → parents[1] = 백엔드 레포 루트(dataplatform-service). 코어가 아니라 **여기** 기준.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def bootstrap_env(env: str) -> PipelineSettings:
    """``dataplatform-service/.env.{env}`` 로드 후 코어 설정을 **서빙 역할**로 초기화한다.

    Args:
        env: 설정 프로파일(``dev``·``prod``). ``PORTAL_API_ENV`` 에서 온다.

    Returns:
        코어 ``init_settings`` 가 만든 frozen 설정(이후 ``get_current_settings`` 활성).
    """
    return core_bootstrap_env(env, repo_root=_REPO_ROOT, role="serving")
