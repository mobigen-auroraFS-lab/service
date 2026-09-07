"""검색 튜닝 **프리셋** — 닫힌 이름을 코어 ``SearchTuning`` 값으로 바꾼다(093 2단계 · ADR 2026-09-02 §6).

무엇을 하는 모듈인가: 프론트는 "정확하게·보통·넓게" 같은 **이름**만 보내고, 이 모듈이 그 이름을 코어의
튜닝 묶음(가중치·컷 기준선·연산자·리랭크 등 13개 값)으로 바꿔 검색 함수에 넘긴다. 자판기 버튼이
"진하게·보통·연하게" 셋뿐이고 원두 g 수는 기계 안에 적혀 있는 것과 같다.

왜 숫자를 직접 받지 않는가: ① 남용 방지 — 임계값 0 같은 값을 보내 서버를 무겁게 만들 수 없다.
② 재현성(헌법 3조) — "같은 질의 + 같은 프리셋 = 같은 결과"가 성립하고, 적용된 값은 응답
``meta.tuning`` 에 남는다.

지금 프리셋은 ``default`` 하나다(2026-09-07 사용자 결정): 서버 설정값 그대로. "정확하게 보기" 같은
화면 요구가 생기면 **이 파일에만** ``dataclasses.replace(base, cutoff_floor=…)`` 식으로 눈금을
추가한다 — 코어를 고치지 않는다. 값은 골든 질의로 실측해 정한다(근거 없는 숫자를 계약으로 굳히지
않기 위해).

규칙 위치(093 세 질문): 손잡이(``tuning=`` 인자·``SearchTuning``)는 코어, 어느 눈금에 놓을지(이 파일)는
백엔드 — 화면 요구가 바뀌면 함께 바뀌는 것은 백엔드 몫이다(규칙 ②).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from src.search.search_tuning import SearchTuning

DEFAULT_PRESET = "default"
# 닫힌 어휘 — 여기 없는 이름은 라우트가 400 으로 거절한다(오타를 기본으로 흡수하면 사용자는 다른
# 눈금으로 검색된 줄 안다).
PRESETS: tuple[str, ...] = (DEFAULT_PRESET,)


def resolve_tuning(preset: str, base: SearchTuning) -> SearchTuning:
    """프리셋 이름을 튜닝 묶음으로 바꾼다.

    Args:
        preset: 프리셋 이름(``PRESETS`` 중 하나).
        base: 서버 설정에서 해소한 기본 튜닝(``SearchTuning.from_settings``). ``default`` 는 이것을
            그대로 돌려준다 — 그래서 프리셋을 안 준 요청은 종전과 완전히 같다.

    Returns:
        적용할 ``SearchTuning``.

    Raises:
        ValueError: 모르는 프리셋 이름.
    """
    if preset not in PRESETS:
        raise ValueError(f"알 수 없는 검색 프리셋: {preset!r} (허용: {', '.join(PRESETS)})")
    # default = 설정값 그대로. 새 눈금이 생기면 여기서 ``dataclasses.replace(base, …)`` 로 만든다.
    return base


def tuning_meta(preset: str, tuning: SearchTuning) -> dict[str, Any]:
    """응답 ``meta.tuning`` 내용 — 프리셋 이름 + 실제 적용된 값 전부.

    이름만 남기면 나중에 서버 설정이 바뀌었을 때 옛 응답을 재현할 수 없다. 영수증에 "보통"이 아니라
    "보통 = 원두 18g·물 200ml"까지 찍는 쪽이다(2026-09-07 사용자 결정).

    Args:
        preset: 적용한 프리셋 이름.
        tuning: 실제로 검색 함수에 넘긴 튜닝 묶음.

    Returns:
        JSON 으로 바로 실을 수 있는 dict(``weights`` 튜플은 목록으로).
    """
    body = asdict(tuning)
    body["weights"] = list(tuning.weights)
    return {"preset": preset, **body}
