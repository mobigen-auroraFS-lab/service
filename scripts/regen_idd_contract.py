#!/usr/bin/env python3
"""IDD.xlsx → ``tests/fixtures/idd_contract.json`` 재생성(단일 책임: 엑셀 읽기·직렬화).

왜 JSON 으로 굳히나 — 대조 테스트가 엑셀 바이너리에 매달리면 그 파일이 없는 환경(CI·공개 사본)에서
통째로 건너뛰어 아무것도 지키지 못한다. 계약을 텍스트로 굳혀 두면 **코드가 계약에서 멀어지는 것**을
어디서나 잡을 수 있고, 엑셀이 있는 환경에서는 굳힌 값이 최신인지도 함께 확인한다.

    python scripts/regen_idd_contract.py [IDD.xlsx 경로]

엑셀을 고쳤으면 이 스크립트를 돌려 JSON 을 갱신하고 함께 커밋한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.conformance.contract import DEFAULT_XLSX, contract_from_xlsx  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "idd_contract.json"


def main() -> int:
    xlsx = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XLSX
    if not xlsx.exists():
        print(f"IDD.xlsx 를 찾을 수 없습니다: {xlsx}", file=sys.stderr)
        return 1
    data = contract_from_xlsx(xlsx)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"{OUT.relative_to(Path.cwd())} 갱신 — 인터페이스 {len(data['interfaces'])}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
