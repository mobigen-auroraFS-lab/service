"""관계 묶음의 대상 — 상세의 관련 자료와 같은 이웃(확인 전 포함)을 같은 순서로 담는다."""

from __future__ import annotations

import unittest
from unittest import mock

from service.portal.asset import download

SEED = "018f0000-0000-7000-8000-0000000000a1"


def _n(i: int, tier: str = "weak") -> dict:
    return {"asset_id": f"018f0000-0000-7000-8000-0000000000b{i}", "tier": tier}


class TestCollectBundleAssets(unittest.TestCase):
    def _run(self, neighbors, paths=None, **kw):
        ids = [SEED, *[n["asset_id"] for n in neighbors]]
        path_map = paths if paths is not None else {a: f"/data/{a[-2:]}.bin" for a in ids}
        with mock.patch("service.portal.asset.detail.related_assets", return_value=neighbors) as rel, \
                mock.patch.object(download, "fetch_asset_paths", return_value=path_map):
            out = download.collect_bundle_assets(object(), seed_asset_id=SEED, **kw)
        return out, rel

    def test_기준_자산이_먼저고_이웃은_상세와_같은_순서(self) -> None:
        out, _ = self._run([_n(2, "strong"), _n(1), _n(3)])
        self.assertEqual([SEED, _n(2)["asset_id"], _n(1)["asset_id"], _n(3)["asset_id"]], [t["asset_id"] for t in out])

    def test_확인_전_관계도_담긴다(self) -> None:
        """승인이 0건이어도(확인된 관계 없음) 상세가 보여 주는 이웃이 묶음에 든다 — 종전에는 기준 파일 하나만 들었다."""
        out, rel = self._run([_n(1, "weak"), _n(2, "weak")])
        self.assertEqual(3, len(out))
        rel.assert_called_once()

    def test_자기_자신과_경로를_모르는_이웃은_뺀다(self) -> None:
        me = {"asset_id": SEED, "tier": "strong"}
        known = {SEED: "/data/seed.bin", _n(1)["asset_id"]: "/data/1.bin"}
        out, _ = self._run([me, _n(1), _n(2)], paths=known)
        self.assertEqual([SEED, _n(1)["asset_id"]], [t["asset_id"] for t in out])
        self.assertEqual("1.bin", out[1]["file_name"].split("__")[-1])

    def test_이웃_수_상한은_없다(self) -> None:
        out, _ = self._run([_n(i) for i in range(10, 90)])
        self.assertEqual(81, len(out))

    def test_상한을_줄_때만_자른다(self) -> None:
        out, _ = self._run([_n(i) for i in range(10, 30)], max_neighbors=5)
        self.assertEqual(6, len(out))

    def test_이웃이_없으면_기준_자산만(self) -> None:
        out, _ = self._run([])
        self.assertEqual([SEED], [t["asset_id"] for t in out])


if __name__ == "__main__":
    unittest.main()
