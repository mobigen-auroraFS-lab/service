"""091 — 결과 내 재검색 **배선** 단위 테스트(백엔드 몫). DB·네트워크 없음.

무엇을 봉인하나: 두 가지다.
① 좁히기 **판단**(토큰 AND·순서 유지)은 서비스가 자체 구현하지 않고 코어 ``refine_rows`` 를
   그대로 참조한다 — 083 이 정해 둔 규율("서비스는 코어 순수 함수를 호출하는 배선만").
② 좁힐 **대상 필드**를 고르는 것은 서비스 몫이다 — 읽는 키(``file_name``·``summary``·``tags``)가
   서비스의 응답 모양이라서다(093 책무 경계 규칙 ②). 그래서 ``asset_refine_fields`` 는
   ``service.portal.search_group`` 에 있고, 라우트는 그것을 참조한다.

라우트 전체 호출은 DB·OpenSearch 가 필요하므로 여기서 하지 않는다.
"""
from __future__ import annotations

import inspect
import unittest

from service.api import routes_search
from service.portal import search_group
from src.search.refine import refine_rows as core_refine_rows


class TestRefineWiring(unittest.TestCase):
    """판단은 코어 함수를 **그대로**, 필드 선택은 서비스 함수를 **그대로** 참조한다."""

    def test_좁히기_판단은_코어_함수를_쓴다(self) -> None:
        self.assertIs(routes_search.refine_rows, core_refine_rows)

    def test_좁힐_필드_선택은_서비스_함수를_쓴다(self) -> None:
        # 응답 키를 아는 쪽(응답 모양을 만든 search_group)이 재료 추출기를 갖는다.
        self.assertIs(routes_search.asset_refine_fields, search_group.asset_refine_fields)


class TestRefineParameter(unittest.TestCase):
    """``refine`` 파라미터 계약 — 선택 인자이고 기본은 '좁히지 않음'."""

    def test_search_라우트가_refine_을_받는다(self) -> None:
        params = inspect.signature(routes_search.search).parameters
        self.assertIn("refine", params)

    def test_기본값이_없음이라_기존_호출은_영향이_없다(self) -> None:
        """되돌림의 실질 — 파라미터를 주지 않으면 091 이전과 같은 응답이다."""
        default = inspect.signature(routes_search.search).parameters["refine"].default
        # FastAPI Query 객체 — 그 안의 기본값이 None 이어야 한다.
        self.assertIsNone(getattr(default, "default", default))


class TestAssetRefineFields(unittest.TestCase):
    """재료 추출기 계약 — 파일명 → 요약 → 태그 순, 빈 값·타입 불일치는 뺀다."""

    def test_세_필드를_순서대로_뽑는다(self) -> None:
        row = {"file_name": "김치.txt", "summary": "배추김치", "tags": ["전통음식", "발효"]}
        self.assertEqual(
            search_group.asset_refine_fields(row), ["김치.txt", "배추김치", "전통음식", "발효"]
        )

    def test_없거나_타입이_다른_축은_뺀다(self) -> None:
        # tags 가 문자열 하나면 글자 단위로 쪼개져 쓰레기 값이 되므로 배열만 받는다.
        row = {"file_name": "", "summary": None, "tags": "전통음식"}
        self.assertEqual(search_group.asset_refine_fields(row), [])
        self.assertEqual(search_group.asset_refine_fields({}), [])

    def test_요약_원문을_주면_행의_잘린_요약_대신_쓴다(self) -> None:
        # 간략 보기는 요약을 자른다 — 잘린 글자로 거르면 "화면엔 보이는데 안 걸림"이 생긴다.
        row = {"file_name": "a.txt", "summary": "배추…", "tags": []}
        self.assertEqual(
            search_group.asset_refine_fields(row, summary="배추김치 담그는 법"),
            ["a.txt", "배추김치 담그는 법"],
        )

    def test_내부용_키는_보지_않는다(self) -> None:
        row = {"file_name": "a.txt", "_kwtext": "김치", "_about": "김치"}
        self.assertEqual(search_group.asset_refine_fields(row), ["a.txt"])


class TestParityWithCoreOriginal(unittest.TestCase):
    """🔴 책무 분리 후 동작은 **전과 같아야 한다** — 옮긴 함수가 코어 원본과 모든 입력에서 같은 결과.

    코어 ``src.search.refine.asset_refine_fields`` 는 095 에서 deprecate 될 때까지 남아 있으므로,
    그동안 두 구현을 직접 대조해 "옮기면서 달라진 것이 없다"를 봉인한다. 코어 함수가 사라지면 이
    테스트는 함께 지운다(그때는 아래 ``TestAssetRefineFields`` 가 계약을 대신 지킨다).
    """

    _CASES: tuple[tuple[dict, str | None], ...] = (
        ({"file_name": "김치.txt", "summary": "배추김치", "tags": ["전통음식", "발효"]}, None),
        ({"file_name": "김치.txt", "summary": "배추…", "tags": ["전통음식"]}, "배추김치 담그는 법"),
        ({"file_name": "", "summary": None, "tags": "전통음식"}, None),        # 빈 파일명·None·문자열 tags
        ({"file_name": 123, "summary": ["x"], "tags": None}, None),           # 타입 불일치 전부
        ({"tags": ("튜플", "도", 7, "", None)}, None),                          # 튜플·비문자·빈값 섞임
        ({}, None),                                                            # 키 전무
        ({"file_name": "a.txt", "_kwtext": "내부", "_about": "내부"}, None),   # 내부용 키는 무시
        ({"file_name": "a.txt", "summary": "요약"}, ""),                       # summary="" 는 명시 빈 원문
        ({"file_name": " ", "summary": " ", "tags": [" "]}, None),             # 공백만 — 비어 있지 않은 문자열로 취급
    )

    def test_모든_입력에서_코어_원본과_같다(self) -> None:
        from src.search.refine import asset_refine_fields as core_original

        for row, summary in self._CASES:
            with self.subTest(row=row, summary=summary):
                self.assertEqual(
                    search_group.asset_refine_fields(row, summary=summary),
                    core_original(row, summary=summary),
                )

    def test_입력_조합_전수에서_코어_원본과_같다(self) -> None:
        """세 키 × 값 종류 × 요약 원문 지정을 **전부 조합**(450건)해 한 건도 다르지 않음을 본다."""
        from itertools import product

        from src.search.refine import asset_refine_fields as core_original

        absent = object()
        file_names = (absent, None, "", "a.txt", 7)
        summaries = (absent, None, "", "요약 문장", ["x"])
        tags_options = (absent, None, "", "문자열태그", ["a", "", None, 3, "b"], ("튜플",))
        overrides = (None, "", "클립 전 원문")
        n = 0
        for fn, sm, tg, ov in product(file_names, summaries, tags_options, overrides):
            row: dict = {}
            if fn is not absent:
                row["file_name"] = fn
            if sm is not absent:
                row["summary"] = sm
            if tg is not absent:
                row["tags"] = tg
            with self.subTest(row=row, summary=ov):
                self.assertEqual(
                    search_group.asset_refine_fields(row, summary=ov),
                    core_original(row, summary=ov),
                )
            n += 1
        self.assertEqual(n, 450)

    def test_시그니처가_같다(self) -> None:
        from src.search.refine import asset_refine_fields as core_original

        self.assertEqual(
            str(inspect.signature(search_group.asset_refine_fields)),
            str(inspect.signature(core_original)),
        )

    def test_좁히기_결과도_같다(self) -> None:
        """추출기를 바꿔 끼워도 ``refine_rows`` 의 결과 행·순서가 같다(라우트가 실제로 쓰는 조합)."""
        from src.search.refine import asset_refine_fields as core_original

        rows = [
            {"file_name": "김치_담그기.txt", "summary": "배추김치 담그는 법", "tags": ["전통음식"]},
            {"file_name": "라면.txt", "summary": "라면 끓이기", "tags": ["간편식"]},
            {"file_name": "김치찌개.mp4", "summary": "김치찌개 끓이기", "tags": ["전통음식"]},
        ]
        for q in ("김치", "김치 담그기", "전통음식 배추", "없는말", "", None):
            with self.subTest(q=q):
                self.assertEqual(
                    core_refine_rows(rows, q, fields_of=search_group.asset_refine_fields),
                    core_refine_rows(rows, q, fields_of=core_original),
                )


class TestScopeCountContract(unittest.TestCase):
    """표시 건수 = 실제 남은 행 수. 라우트가 세는 방식 그대로 재현한다."""

    _GROUPED = {
        "text": [
            {"file_name": "김치_담그기.txt", "summary": "배추김치 담그는 법", "tags": ["전통음식"]},
            {"file_name": "라면.txt", "summary": "라면 끓이기", "tags": ["간편식"]},
        ],
        "video": [
            {"file_name": "김치찌개.mp4", "summary": "김치찌개 끓이기", "tags": ["전통음식"]},
        ],
    }

    def test_좁힌_뒤_건수가_실제_행수와_같다(self) -> None:
        scope_counts = {m: len(rows) for m, rows in self._GROUPED.items()}
        narrowed = {
            m: core_refine_rows(rows, "김치", fields_of=search_group.asset_refine_fields)
            for m, rows in self._GROUPED.items()
        }
        counts = {m: len(rows) for m, rows in narrowed.items()}
        self.assertEqual(scope_counts, {"text": 2, "video": 1})
        self.assertEqual(counts, {"text": 1, "video": 1})
        self.assertEqual(sum(counts.values()), sum(len(r) for r in narrowed.values()))

    def test_아무것도_안_걸리면_빈_버킷이_남는다(self) -> None:
        # 버킷 키를 지우지 않는다 — 화면이 "이 모달리티는 0건"을 그릴 수 있어야 한다.
        narrowed = {
            m: core_refine_rows(rows, "존재하지않는말", fields_of=search_group.asset_refine_fields)
            for m, rows in self._GROUPED.items()
        }
        self.assertEqual(set(narrowed), {"text", "video"})
        self.assertEqual(sum(len(r) for r in narrowed.values()), 0)

    def test_다어절은_토큰_AND_로_좁힌다(self) -> None:
        # `김치 담그기` 는 한 필드에 연속으로 없어도 두 낱말이 각각 어느 필드엔가 있으면 남는다(091 D2).
        narrowed = core_refine_rows(
            self._GROUPED["text"], "김치 담그기", fields_of=search_group.asset_refine_fields
        )
        self.assertEqual([r["file_name"] for r in narrowed], ["김치_담그기.txt"])


if __name__ == "__main__":
    unittest.main()
