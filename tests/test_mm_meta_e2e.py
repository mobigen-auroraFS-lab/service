"""095 — 개체 화면 라우트 **실 DB e2e**(RUN_DB_E2E=1 일 때만 · 읽기 전용 · 쓰기 0).

무엇을 증명하나 — 단위 테스트가 잡지 못하는 것만 본다:
① **적힌 숫자 = 누르면 나오는 수**(SC-04) — 좁히기 칩의 건수와 그 조건으로 부른 목록의 건수가 같다.
   임계를 목록·칩에서 하나로 통일한 판정(2026-09-08)의 실질이 이것이다.
② **노출 임계가 실제로 걸린다** — 목록에 구성 자산이 임계 미만인 개체가 없다.
③ **갈래로 좁히면 그 갈래를 가진 개체만** 남는다(자산 라벨로 좁히던 옛 화면의 쪼개짐이 없다).
④ **카드·자산 소속이 서로 맞물린다** — 카드가 준 자산을 자산 쪽에서 물어도 그 개체가 나온다.
⑤ **zip 이 실제로 열리고** 헤더가 말한 수와 담긴 수가 맞는다(경로가 사라진 파일은 목록 파일에 남는다).
⑥ **오류 경로가 뜻에 맞는 코드**를 낸다 — 없는 개체 404 · 빈 결과 404 · 용량 초과 413.
⑦ **같은 요청은 같은 응답**(헌법 3조) — 검색까지 포함해 두 번 불러 원문이 같다.

DB 를 쓰지 않는다 — dev 에 이미 적재된 자산·개체를 읽기만 한다. 개체가 없는 환경에서는 각 테스트가
skip 한다(빈 DB 에서 초록으로 지나가 "통과했다"고 오해하지 않게 이유를 남긴다).
"""

from __future__ import annotations

import io
import json
import os
import unittest
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi.testclient import TestClient

_RUN = os.getenv("RUN_DB_E2E") == "1"
_ENV = Path(__file__).resolve().parents[1] / ".env.dev"

_LIST_MAX = 500  # 라우트가 허용하는 limit 상한


@unittest.skipUnless(_RUN, "RUN_DB_E2E=1 일 때만")
class TestMmMetaE2E(unittest.TestCase):
    """실 dev DB · 실 OpenSearch 로 개체 화면 라우트 계약을 확인한다."""

    @classmethod
    def setUpClass(cls) -> None:
        load_dotenv(_ENV, override=False)
        # 인증은 이 테스트의 대상이 아니다 — dev bypass 로 열고 라우트 계약만 본다.
        os.environ.setdefault("PORTAL_AUTH_DISABLED", "1")
        os.environ.setdefault("PORTAL_JWT_SECRET", "e2e-mm-meta")
        # ``with`` 로 열어야 lifespan(설정 초기화·DB 풀)이 돈다 — 없이 쓰면 설정 미초기화로 죽는다.
        cls._ctx = TestClient(__import__("service.api", fromlist=["app"]).app)
        cls.client = cls._ctx.__enter__()
        cls.min_members = cls.client.get("/mm-meta/facets").json()["min_members"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._ctx.__exit__(None, None, None)

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────
    def _list(self, **params) -> list[dict]:
        """목록을 부른다(기본 limit 은 상한 — 칩 건수와 견주려면 잘리지 않아야 한다)."""
        params.setdefault("limit", _LIST_MAX)
        resp = self.client.get("/mm-meta", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["items"]

    def _facets(self, **params) -> dict:
        resp = self.client.get("/mm-meta/facets", params=params)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _card(self, item: dict) -> dict:
        resp = self.client.get(f"/mm-meta/{item['entity_type']}/{item['entity_uid']}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    # ── ① 적힌 숫자 = 누르면 나오는 수 ─────────────────────────────────────────
    def test_종류_칩_건수가_그_종류_목록_건수와_같다(self) -> None:
        types = [t for t in self._facets()["types"] if t["entities"] > 0]
        if not types:
            self.skipTest("노출 개체가 있는 종류가 없다 — dev DB 에 개체가 없다")
        for t in types:
            with self.subTest(type=t["name"]):
                self.assertLess(t["entities"], _LIST_MAX, "칩 건수가 limit 상한 이상이면 비교가 무효다")
                self.assertEqual(len(self._list(entity_type=t["name"])), t["entities"])

    def test_갈래_칩_건수가_그_갈래_목록_건수와_같다(self) -> None:
        areas = [a for a in self._facets()["areas"] if a["entities"] > 0]
        if not areas:
            self.skipTest("노출 개체가 있는 갈래가 없다")
        for a in areas[:6]:  # 상위 몇 개만 — 갈래마다 목록을 부르면 느리다
            with self.subTest(area=a["name"]):
                self.assertEqual(len(self._list(areas=a["name"])), a["entities"])

    def test_갈래를_더_고르면_건수가_줄어들거나_같다(self) -> None:
        """갈래는 AND 라 더 누를수록 좁아진다 — 칩 건수가 "누르면 나올 수"인 근거."""
        areas = [a for a in self._facets()["areas"] if a["entities"] > 0]
        if len(areas) < 2:
            self.skipTest("갈래가 둘 미만이다")
        first = areas[0]["name"]
        after = {a["name"]: a["entities"] for a in self._facets(areas=first)["areas"]}
        for a in areas[1:6]:
            with self.subTest(area=a["name"]):
                self.assertLessEqual(after.get(a["name"], 0), a["entities"])
                self.assertEqual(len(self._list(areas=f"{first},{a['name']}")),
                                 after.get(a["name"], 0))

    def test_종류_칩은_갈래를_골라도_변하지_않는다(self) -> None:
        """종류는 **갈아타는 축**이라 조건을 적용하지 않는다 — 안 그러면 갈아탈 칩이 사라진다."""
        areas = [a for a in self._facets()["areas"] if a["entities"] > 0]
        if not areas:
            self.skipTest("노출 개체가 있는 갈래가 없다")
        base = {t["name"]: t["entities"] for t in self._facets()["types"]}
        scoped = {t["name"]: t["entities"]
                  for t in self._facets(areas=areas[0]["name"], entity_type="장소")["types"]}
        self.assertEqual(base, scoped)

    # ── ② 노출 임계 ───────────────────────────────────────────────────────────
    def test_목록에_임계_미만_개체가_없다(self) -> None:
        items = self._list()
        if not items:
            self.skipTest("노출 개체가 없다")
        self.assertTrue(all(i["confirmed_count"] >= self.min_members for i in items))
        # 필터가 없으면 "이 조건의 수"와 "전량"이 같아야 한다.
        self.assertTrue(all(i["confirmed_count"] == i["total_count"] for i in items))

    def test_목록이_구성_자산_수_내림차순이다(self) -> None:
        items = self._list()
        if len(items) < 2:
            self.skipTest("개체가 둘 미만이다")
        counts = [i["confirmed_count"] for i in items]
        self.assertEqual(counts, sorted(counts, reverse=True))

    # ── ③ 갈래로 좁히기 ───────────────────────────────────────────────────────
    def test_갈래로_좁히면_그_갈래를_가진_개체만_남는다(self) -> None:
        areas = [a for a in self._facets()["areas"] if a["entities"] > 0]
        if not areas:
            self.skipTest("노출 개체가 있는 갈래가 없다")
        name = areas[0]["name"]
        items = self._list(areas=name)
        self.assertTrue(items)
        for i in items:
            self.assertIn(name, i["areas"], f"{i['entity_uid']} 에 갈래 {name} 가 없다")
        # 개체가 갈래마다 쪼개지지 않는다 — 좁힌 결과의 건수가 전체 기준 건수와 같다.
        whole = {(i["entity_type"], i["entity_uid"]): i["total_count"] for i in self._list()}
        for i in items:
            key = (i["entity_type"], i["entity_uid"])
            if key in whole:
                self.assertEqual(i["confirmed_count"], whole[key])

    # ── ④ 카드 · 자산 소속 ────────────────────────────────────────────────────
    def test_카드가_목록_개체마다_열리고_건수가_맞는다(self) -> None:
        items = self._list()[:20]
        if not items:
            self.skipTest("노출 개체가 없다")
        for item in items:
            with self.subTest(uid=item["entity_uid"]):
                card = self._card(item)
                self.assertEqual(card["total"], item["total_count"])
                self.assertEqual(card["confirmed_count"], card["total"])
                counted = sum(g["count"] for g in card["modalities"])
                self.assertEqual(counted, card["total"])
                self.assertEqual(sorted({g["modality"] for g in card["modalities"]}),
                                 sorted(item["modalities"]))

    def test_카드가_준_자산을_자산_쪽에_물어도_그_개체가_나온다(self) -> None:
        items = self._list()[:5]
        if not items:
            self.skipTest("노출 개체가 없다")
        checked = 0
        for item in items:
            card = self._card(item)
            for group in card["modalities"]:
                for asset in group["assets"][:3]:
                    resp = self.client.get(f"/assets/{asset['asset_id']}/mm-meta")
                    self.assertEqual(resp.status_code, 200, resp.text)
                    owned = {(x["entity_type"], x["entity_uid"]) for x in resp.json()["items"]}
                    self.assertIn((item["entity_type"], item["entity_uid"]), owned)
                    checked += 1
        self.assertGreater(checked, 0, "확인한 자산이 0건 — 개체에 구성 자산이 없다")

    def test_자산_소속은_묶음_1_도_감추지_않는다(self) -> None:
        """자산 쪽에서 보면 "이 파일이 그 개체에 속한다"는 사실이라 감추지 않는다(목록과 다른 점)."""
        items = self._list()[:5]
        if not items:
            self.skipTest("노출 개체가 없다")
        card = self._card(items[0])
        aid = card["modalities"][0]["assets"][0]["asset_id"]
        rows = self.client.get(f"/assets/{aid}/mm-meta").json()["items"]
        self.assertTrue(rows)
        self.assertTrue(all(r["bundle_size"] >= 1 for r in rows))

    # ── ⑤ zip ────────────────────────────────────────────────────────────────
    def test_카드_zip_이_열리고_헤더_수와_맞는다(self) -> None:
        items = self._list()[:3]
        if not items:
            self.skipTest("노출 개체가 없다")
        for item in items:
            with self.subTest(uid=item["entity_uid"]):
                resp = self.client.get(
                    f"/mm-meta/{item['entity_type']}/{item['entity_uid']}/bundle")
                if resp.status_code == 409:
                    continue  # 경로를 아는 파일이 하나도 없는 개체 — 뜻에 맞는 코드다
                self.assertEqual(resp.status_code, 200, resp.text)
                count = int(resp.headers["X-Bundle-Count"])
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    names = zf.namelist()
                    missing = 0
                    if "_manifest.json" in names:
                        missing = len(json.loads(zf.read("_manifest.json"))["missing"])
                    entries = [n for n in names if n != "_manifest.json"]
                    self.assertEqual(len(entries) + missing, count)
                    self.assertIsNone(zf.testzip(), "zip 이 손상됐다")

    def test_묶음_zip_은_좁히면_받아지고_안_좁히면_상한을_알린다(self) -> None:
        whole = self.client.get("/mm-meta/bundle")
        # 전체는 용량 상한을 넘거나(413) 받아지거나(200) 자료가 없다(404) — 500 이면 안 된다.
        self.assertIn(whole.status_code, (200, 404, 413), whole.text[:200])
        areas = [a for a in self._facets()["areas"] if a["entities"] > 0]
        if not areas:
            self.skipTest("노출 개체가 있는 갈래가 없다")
        narrowed = self.client.get("/mm-meta/bundle",
                                   params={"areas": areas[-1]["name"], "exclude_video": "true"})
        self.assertIn(narrowed.status_code, (200, 404, 409, 413), narrowed.text[:200])
        if narrowed.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(narrowed.content)) as zf:
                self.assertIsNone(zf.testzip())

    def test_옛_파라미터_이름도_같은_결과를_준다(self) -> None:
        """프론트가 옛 이름을 계속 보내도 **좁혀지지 않은 전량**을 받지 않게 별칭을 둔다."""
        areas = [a for a in self._facets()["areas"] if a["entities"] > 0]
        if not areas:
            self.skipTest("노출 개체가 있는 갈래가 없다")
        name = areas[-1]["name"]
        new = self.client.get("/mm-meta/bundle", params={"areas": name})
        old = self.client.get("/mm-meta/bundle", params={"labels": name})
        self.assertEqual(new.status_code, old.status_code)
        self.assertEqual(new.headers.get("X-Bundle-Count"), old.headers.get("X-Bundle-Count"))

    # ── ⑥ 오류 경로 ───────────────────────────────────────────────────────────
    def test_없는_개체는_404_없는_갈래_묶음은_404(self) -> None:
        self.assertEqual(self.client.get("/mm-meta/장소/__없는개체__").status_code, 404)
        self.assertEqual(
            self.client.get("/mm-meta/bundle", params={"areas": "__없는갈래__"}).status_code, 404)
        self.assertEqual(
            self.client.get("/mm-meta/장소/__없는개체__/bundle").status_code, 404)

    def test_없는_갈래로_좁히면_빈_목록이고_오류가_아니다(self) -> None:
        self.assertEqual(self._list(areas="__없는갈래__"), [])

    def test_limit_범위_밖은_422(self) -> None:
        for bad in (0, 501):
            self.assertEqual(self.client.get("/mm-meta", params={"limit": bad}).status_code, 422)

    # ── ⑦ 결정성 · 별칭 ───────────────────────────────────────────────────────
    def test_같은_요청은_같은_응답이다(self) -> None:
        for params in ({"limit": 50}, {"q": "제주", "limit": 50},
                       {"q": "제주", "limit": 50, "refine": "제주"}):
            with self.subTest(params=params):
                a = self.client.get("/mm-meta", params=params)
                b = self.client.get("/mm-meta", params=params)
                self.assertEqual(a.status_code, b.status_code)
                self.assertEqual(a.text, b.text)

    def test_좁히기는_결과를_줄이고_지우면_돌아온다(self) -> None:
        items = self._list(limit=50)
        if not items:
            self.skipTest("노출 개체가 없다")
        token = items[0]["name"][:2]
        body = self.client.get("/mm-meta", params={"limit": 50, "refine": token}).json()
        self.assertLessEqual(body["total"], body["scope_total"])
        self.assertEqual(body["scope_total"], len(items))
        self.assertEqual(body["refine"], token)
        for i in body["items"]:
            haystack = " ".join([i["name"], i["description"] or "", *i["keywords"]])
            self.assertIn(token, haystack)

    def test_타입_별칭이_칩_응답의_부분과_같다(self) -> None:
        alias = self.client.get("/mm-meta/types")
        self.assertEqual(alias.status_code, 200, alias.text)
        facets = self._facets()
        self.assertEqual(alias.json(), {"vocab": facets["vocab"], "types": facets["types"]})


if __name__ == "__main__":
    unittest.main()
