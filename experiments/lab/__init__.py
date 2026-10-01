"""파일 전송 방식 실험 서버(2026-10-01) — ⚠️ 측정 전용. 인증이 없고 ``EXP_ROOT`` 아래 파일을 그대로 내준다.

배포 패키지(``service``) 밖에 둔 까닭이 그것이다 — 운영 이미지에 실려도 import 경로에 들어가지 않는다.
실행: ``EXP_ROOT=/경로 uvicorn experiments.lab.app:app`` (저장소 루트에서) · 측정: ``experiments/bench.py`` · 결과: ``experiments/RESULTS.md``.
"""
