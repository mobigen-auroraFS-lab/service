"""HTTP 라우터 모음 — 경로 공간별로 한 모듈씩.

    search · file_search   /search · /file-search
    assets                 /assets · /topics
    mm_meta                /mm-meta
    admin                  /admin/* (조회 전용)
    review                 /admin/* (유일한 쓰기)

⚠️ 앱에 붙이는 **순서**는 ``service/api/__init__.py`` 가 정한다 — 여기서 재수출하지 않는다.
"""
