"""비밀번호 해시 — argon2id 만 쓴다.

bcrypt 는 입력 72바이트를 넘는 부분을 **조용히 버린다**. 비밀번호 길이에 제한을 두지 않기로 했으므로
그 절단이 결정과 충돌한다(코어 초안 스키마 주석과 같은 판단).

🔴 평문·복호 가능한 암호화는 저장하지 않는다. 대조는 해시로만 한다.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_HASHER = PasswordHasher()


def hash_password(password: str) -> str:
    """argon2id 인코딩 문자열을 만든다(솔트는 라이브러리가 만든다)."""
    return _HASHER.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    """비밀번호가 맞는지 본다 — 틀렸거나 해시가 깨졌으면 ``False``.

    🔴 "해시가 깨졌다"와 "비밀번호가 틀렸다"를 응답에서 가르지 않는다 — 가르면 어떤 계정이
    존재하는지 알려 주는 셈이다.
    """
    try:
        return _HASHER.verify(encoded, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(encoded: str) -> bool:
    """저장된 해시가 지금 설정보다 약한지 — 로그인 성공 시 다시 저장할지 판단한다."""
    try:
        return _HASHER.check_needs_rehash(encoded)
    except (InvalidHashError, ValueError):
        return False
