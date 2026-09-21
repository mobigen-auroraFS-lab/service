"""IDD 계약 대조 도구 — 책임별로 나눠 둔다.

    contract   IDD 계약을 읽어 온다(엑셀 → JSON 고정본)
    simulator  DB 를 모의해 실제 조회 코드를 태운다
    probes     예외 상황을 유발하는 레시피

단언은 ``tests/contract/test_idd_conformance.py`` 가 한다 — 도구와 판정을 섞지 않는다.
"""
