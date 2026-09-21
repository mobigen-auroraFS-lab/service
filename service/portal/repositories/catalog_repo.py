"""화면이 고를 값의 **목록**(카탈로그) — 태그·추천 검색어.

검색 결과에서 세는 칩(주제·하위주제·태그·모달리티)과는 쓰임이 다르다. 칩은 "지금 결과 안에서
몇 건"이고, 여기는 "고를 수 있는 값이 무엇인가"다. 화면이 태그 목록을 주제로 좁혀 보여 주려면
결과와 무관한 목록이 필요해서 따로 둔다.

출처는 **DB**다 — 태그(키워드)는 ``asset_metadata.ext_meta->'keywords'`` 에, 주제 배정은
``asset_topic`` 에 있다. 검색 색인에도 같은 값이 있지만, 목록은 검색 엔진이 죽어도 떠야 한다.
"""

from __future__ import annotations

from typing import Any

from service.portal.common.repository import Repository

# 등록 자산의 키워드를 낱개로 펴서 센다. 주제·하위주제로 좁힐 수 있게 ``asset_topic`` 을 선택적으로 건다.
#   ⚠️ LEFT JOIN 이 아니라 조건이 있을 때만 INNER JOIN 하도록 SQL 을 나눈다 — LEFT JOIN 으로 두면
#      주제가 여럿인 자산이 키워드마다 여러 번 세어져 건수가 부풀어 오른다.
_TAGS_BASE = """
SELECT k.kw AS tag, COUNT(DISTINCT a.asset_id) AS count
FROM asset a
JOIN asset_metadata m ON m.asset_id = a.asset_id
JOIN LATERAL jsonb_array_elements_text(COALESCE(m.ext_meta->'keywords', '[]'::jsonb)) k(kw) ON TRUE
{topic_join}
WHERE a.status = 'registered'{topic_where}{like_where}
GROUP BY k.kw
ORDER BY count DESC, tag ASC
LIMIT %s
"""

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
        """
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
        remaining = int(limit) - len(out)
        if remaining > 0:
            for tag in self.tags(topics=[], subtopics=[], q=q, limit=remaining):
                out.append({"value": tag["tag"], "kind": "tag", "count": tag["count"]})
        return out[:int(limit)]
