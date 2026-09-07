"""코어 밑줄 이름 import 금지 가드 — 093 책무 경계 규칙의 백엔드 쪽 안전장치. DB·네트워크 없음.

**무엇을 막나**: ``from src.<모듈> import _이름`` 처럼 코어의 **내부 이름**(밑줄로 시작)을 가져다 쓰는 것.
밑줄 이름은 코어가 예고 없이 바꾸는 내부 구현이라, 백엔드가 기대면 코어 릴리스마다 조용히 깨질 수 있다.
실제로 ``_REVIEW_STATUSES`` 를 두 라우트가 쓰고 있었다(2026-09-02 감사 A1 → ``service/portal/review_vocab.py`` 로 정리).

**왜 grep 이 아니라 AST 인가**: 여러 줄 괄호 import(``from x import (\n    _a,\n    b,\n)``)는 한 줄 grep 이
놓친다 — 바로 그 형태가 ``routes_review.py`` 에 있었다. 파이썬이 읽는 방식 그대로 읽어야 빠지지 않는다.

코어 쪽 짝은 코어 레포 ``tests/test_public_api.py``(공개 API 표의 이름이 존재하고 밑줄이 없음)다.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1] / "service"


def _core_underscore_imports(py: Path) -> list[str]:
    """파일 하나에서 코어(``src.*``) 밑줄 이름 import 를 찾아 ``"파일:줄 이름"`` 목록으로 돌려준다.

    Args:
        py: 검사할 파이썬 파일.

    Returns:
        위반 목록. 없으면 빈 목록.
    """
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "src" or node.module.startswith("src."):
                for alias in node.names:
                    if alias.name.startswith("_"):
                        found.append(f"{py.relative_to(SERVICE_ROOT.parent)}:{node.lineno} {node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "src" and any(p.startswith("_") for p in parts[1:]):
                    found.append(f"{py.relative_to(SERVICE_ROOT.parent)}:{node.lineno} {alias.name}")
    return found


class TestCoreImportGuard(unittest.TestCase):
    """``service/`` 아래 어느 파일도 코어의 밑줄 이름을 import 하지 않는다."""

    def test_코어_밑줄_이름_import_가_없다(self) -> None:
        violations: list[str] = []
        for py in sorted(SERVICE_ROOT.rglob("*.py")):
            violations.extend(_core_underscore_imports(py))
        self.assertEqual(
            violations, [],
            "코어 내부 이름을 import 하고 있다 — 공개 API 표(코어 README)의 이름을 쓰거나 코어에 공개를 요청할 것:\n  "
            + "\n  ".join(violations),
        )

    def test_가드_자체가_위반을_잡는다(self) -> None:
        """가드가 실제로 동작하는지 — 여러 줄 괄호 import 도 잡아야 한다(routes_review 의 옛 형태)."""
        sample = SERVICE_ROOT.parent / "tests" / "_guard_probe.py"
        sample.write_text(
            "from src.relations.review import (\n    _REVIEW_STATUSES,\n    bulk_review,\n)\n"
            "import src.search._hidden\n",
            encoding="utf-8",
        )
        try:
            found = _core_underscore_imports(sample)
        finally:
            sample.unlink()
        self.assertEqual(len(found), 2)
        self.assertIn("src.relations.review._REVIEW_STATUSES", found[0])


if __name__ == "__main__":
    unittest.main()
