"""화면이 고를 값의 **목록**(카탈로그) — 태그·추천 검색어.

검색 결과에서 세는 칩(주제·하위주제·태그·모달리티)과는 쓰임이 다르다. 칩은 "지금 결과 안에서
몇 건"이고, 여기는 "고를 수 있는 값이 무엇인가"다. 화면이 태그 목록을 주제로 좁혀 보여 주려면
결과와 무관한 목록이 필요해서 따로 둔다.

출처는 **DB**다 — 태그(키워드)는 ``asset_metadata.ext_meta->'keywords'`` 에, 주제 배정은
``asset_topic`` 에 있다. 검색 색인에도 같은 값이 있지만, 목록은 검색 엔진이 죽어도 떠야 한다.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from service.portal.common.repository import Repository

# ── 태그 목록 캐시(2026-09-28) ────────────────────────────────────────────────────
# 조건 없는 태그 목록은 등록 자산 전부의 키워드를 펴서 묶는 질의라 **2~3초** 걸린다(공용 DB 실측
# 2.1~2.9초). 화면은 태그 모달을 열 때마다, 추천어는 글자를 칠 때마다 이 질의를 부른다. 키워드는 적재 때만
# 바뀌므로 같은 조건의 답을 잠시 들고 있다. 🔴 대가: 적재 직후 최대 TTL 만큼 **건수가 늦다**.
# ``PORTAL_TAGS_CACHE_SECONDS`` 로 바꾼다(기본 300초 · 0 이면 끈다). 프로세스(워커)마다 따로 든다.
TAGS_CACHE_ENV = "PORTAL_TAGS_CACHE_SECONDS"
TAGS_CACHE_DEFAULT = 300
_TAGS_CACHE_MAX = 256          # 들고 있을 조건 수 — 넘으면 가장 오래된 것부터 버린다
_TAGS_CACHE: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
_TAGS_LOCK = threading.Lock()


def tags_cache_seconds() -> int:
    """캐시 수명(초). 없거나 잘못되면 기본값, 0 이하면 끔(0)."""
    raw = os.getenv(TAGS_CACHE_ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else TAGS_CACHE_DEFAULT
    except ValueError:
        return TAGS_CACHE_DEFAULT


def clear_tags_cache() -> None:
    """캐시를 비운다(시험 · 적재 직후 즉시 반영이 필요할 때)."""
    with _TAGS_LOCK:
        _TAGS_CACHE.clear()

# 등록 자산의 키워드를 낱개로 펴서 센다. 주제·하위주제로 좁힐 수 있게 ``asset_topic`` 을 선택적으로 건다.
#   ⚠️ LEFT JOIN 이 아니라 조건이 있을 때만 INNER JOIN 하도록 SQL 을 나눈다 — LEFT JOIN 으로 두면
#      주제가 여럿인 자산이 키워드마다 여러 번 세어져 건수가 부풀어 오른다.
# 🔴 **검색 필터와 같은 열쇠로 묶는다**(2026-09-28). 검색은 태그를 NFKC → 공백 제거 → 소문자로 맞춘
#    열쇠(코어 ``normalize_text_key``)로 거르므로 ``역사기록``·``역사 기록`` 이 한 태그다. 원문으로 묶으면
#    목록에는 둘이 따로(23 · 9) 나오는데 어느 쪽을 눌러도 32건이 나와 "적힌 숫자 = 누르면 나오는 수"가
#    깨졌다(공용 DB 읽기 전용 대조: 전통건축 원문 2,424 · 열쇠 2,644 = 검색 칩 2,644).
#    보일 이름은 그 열쇠에서 가장 많이 쓰인 표기다(같으면 정렬상 앞선 것).
#    ⚠️ 코어는 ``casefold`` 를 쓰고 여기는 ``lower`` 다 — 한글·영문에서는 같고, ß 같은 드문 글자만 갈린다.
_TAGS_BASE = """
WITH kw AS (
  SELECT a.asset_id, k.kw,
         lower(regexp_replace(normalize(k.kw, NFKC), '\\s', '', 'g')) AS key
  FROM asset a
  JOIN asset_metadata m ON m.asset_id = a.asset_id
  JOIN LATERAL jsonb_array_elements_text(COALESCE(m.ext_meta->'keywords', '[]'::jsonb)) k(kw) ON TRUE
  {topic_join}
  WHERE a.status = 'registered'{topic_where}{like_where}
)
SELECT mode() WITHIN GROUP (ORDER BY kw) AS tag, COUNT(DISTINCT asset_id) AS count
FROM kw
WHERE key <> ''
GROUP BY key COLLATE "C"
ORDER BY count DESC, tag ASC
LIMIT %s
"""
# 🔴 [2026-10-01] 묶는 열쇠(``key``)만 ``"C"`` 콜레이션으로 정렬한다 — 조건 없는 목록이 **약 3초 → 0.36초**(공용 DB 실측 ·
#    사내 k8s 에서는 7.7초 → 1.2초). DB 기본 콜레이션(``en_US.utf8``)으로 한글 8.4만 개를 정렬하는 비용이 시간의 대부분이었다
#    (문자 정규화는 0.4초뿐 · JIT 도 원인이 아니었다). ``"C"`` 는 바이트를 그대로 비교한다.
#    같다/다르다 판정은 콜레이션과 무관하게 바이트 비교(결정적 콜레이션)라 **묶음이 바뀌지 않는다.** 대표 표기(``mode()`` 의 동점 규칙)와
#    최종 이름순 정렬은 기본 콜레이션 그대로다 — ⚠️ ``kw`` 나 최종 ``tag`` 정렬까지 ``"C"`` 로 바꾸면 동점 표기 · 이름순이 달라질 수 있다.
#    **열쇠에만** 붙인다. 근거: 조건 26가지(전체 · 주제 · 하위주제 · 검색어) 결과 대조 동일(작업 지시서 2026-10-01 작업 C).

_TOPIC_JOIN = "JOIN asset_topic t ON t.asset_id = a.asset_id"

# 주제·하위주제 이름 목록(추천 검색어용). 자산이 배정된 것만 — 눌러도 0건인 값을 권하지 않는다.
_TOPIC_SUGGEST_SQL = """
SELECT t.topic_ko AS value, 'topic' AS kind, COUNT(DISTINCT t.asset_id) AS count
FROM asset_topic t
JOIN asset a ON a.asset_id = t.asset_id AND a.status = 'registered'
WHERE t.topic_ko ILIKE %s
GROUP BY t.topic_ko
UNION ALL
SELECT t.subtopic_ko, 'subtopic', COUNT(DISTINCT t.asset_id)
FROM asset_topic t
JOIN asset a ON a.asset_id = t.asset_id AND a.status = 'registered'
WHERE t.subtopic_ko ILIKE %s
GROUP BY t.subtopic_ko
ORDER BY count DESC, value ASC
LIMIT %s
"""


def like_pattern(text: str) -> str:
    """부분 일치용 LIKE 무늬 — 사용자가 넣은 ``%``·``_``·``\\`` 는 글자로 다룬다.

    이스케이프하지 않으면 ``%`` 하나로 전체 표를 긁는 무늬가 되어, 느린 조회가 아무나 만들어진다.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class CatalogRepository(Repository):
    """고를 수 있는 값의 목록을 읽는다(조회 전용)."""

    def tags(self, *, topics: list[str], subtopics: list[str], q: str | None,
             limit: int) -> list[dict[str, Any]]:
        """태그(키워드) 목록을 건수와 함께 돌려준다.

        Args:
            topics: 주제로 좁힐 값들(여럿이면 「또는」). 빈 목록이면 안 좁힌다.
            subtopics: 하위주제로 좁힐 값들.
            q: 태그 이름 부분 일치. 빈 값이면 전체.
            limit: 최대 개수(건수 내림차순 → 이름 오름차순).

        Returns:
            ``[{tag, count}]`` — 건수는 **자산 수**다(한 자산이 같은 태그를 여러 번 가져도 1).
            같은 조건이면 캐시 수명(기본 300초) 동안 **같은 답**을 준다(적재 직후 건수가 늦을 수 있다).
        """
        ttl = tags_cache_seconds()
        key = (tuple(sorted(topics)), tuple(sorted(subtopics)), q or "", int(limit))
        if ttl:
            now = time.monotonic()
            with _TAGS_LOCK:
                hit = _TAGS_CACHE.get(key)
            if hit and now - hit[0] < ttl:
                return [dict(r) for r in hit[1]]      # 복사해 준다 — 호출부가 고쳐도 캐시가 안 바뀐다
        rows = self._tags_uncached(topics=topics, subtopics=subtopics, q=q, limit=limit)
        if ttl:
            with _TAGS_LOCK:
                if len(_TAGS_CACHE) >= _TAGS_CACHE_MAX:
                    _TAGS_CACHE.pop(min(_TAGS_CACHE, key=lambda k: _TAGS_CACHE[k][0]))
                _TAGS_CACHE[key] = (time.monotonic(), [dict(r) for r in rows])
        return rows

    def _tags_uncached(self, *, topics: list[str], subtopics: list[str], q: str | None,
                       limit: int) -> list[dict[str, Any]]:
        """캐시 없이 DB 에서 센다(``tags`` 참조)."""
        params: list[Any] = []
        topic_where = ""
        if topics:
            topic_where += "\n  AND t.topic_ko = ANY(%s)"
            params.append(list(topics))
        if subtopics:
            topic_where += "\n  AND t.subtopic_ko = ANY(%s)"
            params.append(list(subtopics))
        like_where = ""
        if q:
            like_where = "\n  AND k.kw ILIKE %s"
            params.append(like_pattern(q))
        params.append(int(limit))
        sql = _TAGS_BASE.format(
            topic_join=_TOPIC_JOIN if (topics or subtopics) else "",
            topic_where=topic_where, like_where=like_where)
        return [{"tag": r["tag"], "count": int(r["count"])} for r in self.rows(sql, tuple(params))]

    def suggest(self, *, q: str, limit: int) -> list[dict[str, Any]]:
        """검색창 추천 — 주제·하위주제·태그에서 부분 일치하는 값을 건수 많은 순으로.

        파일 이름은 넣지 않는다 — 이름은 검색어로 넣으면 그대로 걸리고, 목록에 섞으면
        "고르는 값"과 "검색 결과"가 한 목록에서 뒤섞인다.

        Args:
            q: 사용자가 친 글자(부분 일치 · 대소문자 무시).
            limit: 종류별이 아니라 **전체** 상한.

        Returns:
            ``[{value, kind, count}]`` — kind 는 ``topic``·``subtopic``·``tag``.
        """
        pattern = like_pattern(q)
        rows = self.rows(_TOPIC_SUGGEST_SQL, (pattern, pattern, int(limit)))
        out = [{"value": r["value"], "kind": r["kind"], "count": int(r["count"])} for r in rows]
        # 🔴 두 출처를 **합친 뒤** 건수순으로 자른다. 종전에는 주제·하위주제로 상한을 먼저 채우고 남는
        #    자리에만 태그를 붙여, 주제가 상한만큼 걸리면 건수가 훨씬 많은 태그도 빠졌다(2026-09-23 대조).
        #    각 출처를 상한만큼 받아 합치면 합친 목록의 상위 N 이 정확하다.
        out.extend({"value": t["tag"], "kind": "tag", "count": int(t["count"])}
                   for t in self.tags(topics=[], subtopics=[], q=q, limit=int(limit)))
        # 같은 건수는 값 → 종류 순으로 못 박는다(같은 요청이 늘 같은 순서를 내게).
        out.sort(key=lambda r: (-r["count"], r["value"], r["kind"]))
        return out[:int(limit)]
