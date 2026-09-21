"""IDD 계약 로더 — 엑셀 상세 시트를 기계가 읽을 계약으로 바꾼다(단일 책임: 읽기·정규화).

판정은 하지 않는다. 여기는 "IDD 가 뭐라고 적었나"만 답한다.

IDD 표기 규약 셋을 여기서 흡수한다 — 표기가 바뀌면 이 파일만 고친다:
  · ``a / b / c``      한 칸에 여러 필드명
  · ``meta(고정)``      괄호로 성격을 덧붙인 필드명 → 괄호를 뗀다
  · ``(단일)`` + 비고   필드명 칸이 설명이고 형상이 비고에 있다 → 비고에서 최상위 키를 뽑는다
  · ``(IF-X 과 같은 형태)`` 교차참조 → 가리키는 쪽 계약을 빌린다
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests import _paths

# 엑셀은 이 레포 밖(설계 산출물)에 있다 — 없으면 굳힌 JSON 만으로 돈다.
# 설계 산출물(엑셀)은 이 레포 **밖**에 있다 — 없으면 굳힌 JSON 만으로 돈다.
DEFAULT_XLSX = _paths.ROOT.parent / "db설계" / "IDD.xlsx"
FROZEN = _paths.TESTS_ROOT / "fixtures" / "idd_contract.json"

_LOC = re.compile(r"^\s*(Query|Path|Body|Header)\b", re.I)
_XREF = re.compile(r"(IF-[A-Z]+-\d+)\s*과 같은")
_COND = re.compile(r"(일 때|경우에만|조건부|준 요청에만)")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def split_names(cell: object) -> list[str]:
    """필드명 칸을 개별 이름으로 편다(``a / b`` 분해 · ``meta(고정)`` 괄호 제거)."""
    text = str(cell or "").strip()
    if not text or text == "-":
        return []
    out = []
    for part in text.split("/"):
        name = re.sub(r"\(.*?\)", "", part).strip()
        if name and name != "-":
            out.append(name)
    return out


def top_keys(shape: str) -> list[str]:
    """``{interval, buckets:[{bucket, count}]}`` 에서 **최상위 키만** 뽑는다.

    중첩 안쪽은 세지 않는다 — 대조 대상은 응답 봉투이기 때문이다.
    """
    t = shape.strip()
    if not (t.startswith("{") and "}" in t):
        return []
    t = t[1:t.rfind("}")]
    parts, depth, buf = [], 0, ""
    for ch in t:
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return [n for n in (p.split(":")[0].strip() for p in parts) if IDENT.match(n)]


# 비고의 자식 키 목록 표기 — ``a · b · c`` 또는 ``a·b·c``. 앞에 설명이 붙으면 ``:`` 뒤만 본다.
_KEYLIST = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\s*·\s*[A-Za-z_][A-Za-z0-9_]*)+")


def child_keys(note: str) -> list[str]:
    """비고에서 **자식 키 목록**을 뽑는다 — 없으면 빈 목록.

    ``엣지 원본 상세 9키: a · b · c`` 처럼 설명이 앞에 붙으면 ``:`` 뒤를 본다. 각 키의 괄호
    한정(``llm_verify(현재 off)``)은 떼어 이름만 남긴다.
    """
    text = note.split(":", 1)[1] if ":" in note.split("·")[0] else note
    # ⚠️ 괄호를 **먼저** 뗀다 — 괄호 안에 ``·`` 가 들어간 표기가 있어(``topic_pairs('a>b' 짝·059)``)
    #    나중에 떼면 구분자가 하나 더 생겨 키가 쪼개진다(실제로 그렇게 깨졌다).
    text = re.sub(r"\([^)]*\)", "", text)
    m = _KEYLIST.search(text)
    if not m:
        return []
    return [n for n in (part.strip() for part in m.group(0).split("·")) if n]


def nested_target(field_cell: str) -> tuple[str, str] | None:
    """필드명 칸을 ``(대상 필드, 종류)`` 로 읽는다.

    - ``results[].(행 키)`` · ``rows[]``  → ``(results, "element")``  각 원소의 키
    - ``meta(고정)`` · ``meta(조건부)``   → ``(meta, "child")``       그 객체의 자식 키
    - ``relations[].edges``              → ``(relations.edges, "element")``
    """
    raw = str(field_cell or "").strip()
    if "[]" in raw:
        head, _, tail = raw.partition("[]")
        tail = re.sub(r"^\.", "", tail).strip()
        tail = "" if tail.startswith("(") else tail
        return ((f"{head}.{tail}" if tail else head), "element")
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\((고정|조건부)\)$", raw)
    if m:
        return (m.group(1), "child" if m.group(2) == "고정" else "child_opt")
    return None


def contract_from_xlsx(xlsx: Path) -> dict[str, Any]:
    """IDD.xlsx 를 계약 dict 로 바꾼다(직렬화 가능한 순수 자료구조)."""
    import openpyxl  # 엑셀이 있을 때만 필요한 의존

    wb = openpyxl.load_workbook(xlsx, data_only=True)
    lst = wb["인터페이스목록"]
    ifaces: dict[str, Any] = {}
    for r in range(3, lst.max_row + 1):
        api = lst.cell(row=r, column=2).value
        if not api:
            continue
        ifaces[str(api).strip()] = {
            "name": str(lst.cell(row=r, column=3).value or ""),
            "method": str(lst.cell(row=r, column=4).value or "").upper(),
            "url": str(lst.cell(row=r, column=5).value or ""),
            "gubun": str(lst.cell(row=r, column=9).value or ""),
            "req": {}, "resp": {}, "resp_opt": {}, "resp_alt": {}, "errors": {}, "same_as": "",
            # 이름만이 아니라 **타입·필수**까지 담는다 — 이름만 맞고 타입이 어긋나면
            # 화면은 "있는데 못 쓰는" 필드를 받는다.
            "req_meta": {}, "resp_meta": {},
            # 필드명이 없는 응답(``(zip 스트림)``·``(두 축의 건수)``)도 **타입 칸은 계약**이다.
            # 이걸 버리면 그 엔드포인트는 대조에서 통째로 빠진다(실제로 5개가 빠져 있었다).
            "resp_kind": {},
            # 중첩 계약 — ``results[].(행 키)``·``meta(고정)`` 처럼 **자식 키 목록**을 비고에 적는
            # 행들. 최상위만 보면 행 하나하나의 모양이 바뀌어도 안 잡힌다(화면이 그걸 그린다).
            "resp_nested": {},
        }

    for api, spec in ifaces.items():
        if api not in wb.sheetnames:
            continue
        for row in wb[api].iter_rows(min_row=3, values_only=True):
            kind = str(row[0] or "").strip()
            if not kind:
                continue
            note, raw = str(row[4] or ""), str(row[1] or "").strip()
            names = split_names(row[1])
            if kind.startswith("요청"):
                m = _LOC.match(note)
                loc = m.group(1).capitalize() if m else "Query"
                spec["req"].setdefault(loc, []).extend(names)
                # ⚠️ 범위·기본값은 **한 줄에 이름이 하나일 때만** 쓴다. IDD 는 ``limit / offset`` 을
                #    한 줄에 묶고 ``기본 50 / 0`` 처럼 값도 묶어 적는다 — 어느 값이 어느 이름 것인지
                #    기계가 가를 수 없다. 묶인 줄에 한 값을 양쪽에 붙이면 **거짓 불일치**가 쏟아진다.
                single = len(names) == 1
                rng = re.search(r"(\d+)\s*~\s*(\d+)", note) if single else None
                dflt = re.search(r"기본\s*([A-Za-z0-9_.]+)", note) if single else None
                for n in names:
                    spec["req_meta"][n] = {
                        "loc": loc,
                        "type": str(row[2] or "").strip(),
                        "required": str(row[3] or "").strip().upper() == "Y",
                        # 비고에 적힌 허용 범위·기본값. 숫자·참거짓만 대조한다(산문 기본값은 제외).
                        "min": int(rng.group(1)) if rng else None,
                        "max": int(rng.group(2)) if rng else None,
                        "default": dflt.group(1).rstrip(".") if dflt else None,
                    }
            elif kind.startswith("응답"):
                code = re.search(r"(\d{3})", kind).group(1)
                xref = _XREF.search(raw)
                if xref:
                    spec["same_as"] = xref.group(1)
                elif raw.startswith("(") and top_keys(note):
                    spec["resp_alt"].setdefault(code, []).append(top_keys(note))
                tgt = nested_target(raw)
                keys = child_keys(note) if tgt else []
                if tgt and keys:
                    spec["resp_nested"].setdefault(code, []).append(
                        {"field": tgt[0], "kind": tgt[1], "keys": keys})
                if tgt and keys and tgt[1] == "element":
                    continue          # 원소 키 선언 행은 최상위 키 목록에 넣지 않는다
                if not names and str(row[2] or "").strip():
                    spec["resp_kind"][code] = str(row[2]).strip()
                else:
                    tgt = "resp_opt" if _COND.search(note) else "resp"
                    spec[tgt].setdefault(code, []).extend(names)
                    for n in names:
                        spec["resp_meta"].setdefault(code, {})[n] = str(row[2] or "").strip()
            elif kind.startswith("에러"):
                spec["errors"][re.search(r"(\d{3})", kind).group(1)] = note

    for spec in ifaces.values():                      # 교차참조 해소
        src = ifaces.get(spec["same_as"])
        if src:
            spec["resp"].setdefault("200", []).extend(src["resp"].get("200", []))
            spec["resp_opt"].setdefault("200", []).extend(src["resp_opt"].get("200", []))
    return {"interfaces": ifaces}


def load() -> dict[str, Any]:
    """굳힌 계약(JSON)을 읽는다 — 어디서나 돈다."""
    return json.loads(FROZEN.read_text(encoding="utf-8"))["interfaces"]


# IDD 타입 표기 → OpenAPI 스키마 타입. ``string(UUID)``·``string(date)`` 처럼 괄호로 좁힌 표기는
# 괄호를 떼고 본다(형식 제약은 IDD 가 비고로 설명하고, 여기서는 **자료형**만 대조한다).
_TYPE_MAP = {
    "string": "string", "integer": "integer", "number": "number", "boolean": "boolean",
    "string[]": "array", "array": "array", "object": "object",
}


def openapi_type(idd_type: str) -> str | None:
    """IDD 타입 칸을 OpenAPI 자료형 이름으로 바꾼다 — 모르는 표기는 ``None``(대조 제외)."""
    t = re.sub(r"\(.*?\)", "", str(idd_type or "")).strip().lower()
    return _TYPE_MAP.get(t)


# 응답 값 판정용(파이썬 실값 → 자료형 이름)
def value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null" if value is None else "unknown"


def declared_params(spec: dict[str, Any]) -> set[str]:
    """IDD 가 선언한 Query·Path 파라미터 이름."""
    return {n for loc in ("Query", "Path") for n in spec["req"].get(loc, [])}


def declared_resp(spec: dict[str, Any], code: str = "200") -> tuple[set[str], set[str]]:
    """``(필수, 조건부)`` 응답 최상위 필드 — 식별자 형태만 추린다."""
    need = {n for n in spec["resp"].get(code, []) if IDENT.match(n)}
    opt = {n for n in spec["resp_opt"].get(code, []) if IDENT.match(n)}
    return need, opt
