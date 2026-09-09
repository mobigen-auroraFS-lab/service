# dataplatform-service

멀티모달 데이터 통합 플랫폼의 **HTTP API**입니다. 하이브리드 검색·자산 상세·다운로드·썸네일·
관계 검토 서빙을 제공합니다.

> 국책과제 **RS-2025-02215256** 산출물.

## 세 레포의 관계

| 레포 | 파이썬 패키지 | 역할 |
|---|---|---|
| dataplatform-core | `src.*` | 규약·계약·순수 로직 + DB 스키마 정본 |
| dataplatform-pipeline | `processing.*` | 처리 파이프라인(Airflow) |
| **dataplatform-service**(이 레포) | `service.*` | HTTP API |

**이 레포는 코어를 필요로 합니다.** 코어가 없으면 `service.*` 이 import 되지 않습니다.

## 설계 — 2계층

| 계층 | 위치 | 책임 |
|---|---|---|
| 전송 | `service/api/` | FastAPI 라우팅·요청/응답 모델·미들웨어·예외 처리 |
| 로직 | `service/portal/` | 검색 조립·자산 조회·다운로드·썸네일·인증·권한 |

검색·관계 조회·임베딩 같은 도메인 로직은 코어(`src.*`)에 위임합니다. 이 레포는 **서빙**만 합니다.

## 요구사항

| 항목 | 버전 |
|---|---|
| Python | **3.13 이상** |
| PostgreSQL | 17 + `pgvector` (코어 스키마) |
| OpenSearch | `analysis-nori` 플러그인 |

## 설치

**코어를 먼저 설치합니다.**

```bash
# ① 코어 — 나란히 clone 해서 참조형으로 설치(개발) 또는 태그로 설치형
git clone <이 레포와 같은 계정>/dataplatform-core.git   # 예: gh repo clone <owner>/dataplatform-core
pip install -e ./dataplatform-core

# ② 이 레포
pip install -e .
```

코어를 설치하면 psycopg·opensearch-py 등이 전이로 따라옵니다. 이 레포의 `pyproject.toml` 에는
코어가 제공하지 않는 것(fastapi·uvicorn·PyJWT·pydantic·opencv)만 있습니다.

## 환경변수

템플릿이 있습니다 — 복사해서 값만 채우면 됩니다:

```bash
cp .env.example .env.dev      # .env.dev 는 커밋되지 않습니다(.gitignore)
```

### 설정을 주는 두 가지 방법

| 방법 | 어디에 | 우선순위 |
|---|---|---|
| **A. `.env.<환경>` 파일** | **실행하는 디렉터리** → 없으면 레포 루트 순으로 찾습니다 | 낮음 |
| **B. 환경변수 직접 주입** | 배포·컨테이너·CI(`export` · `env_file:` · `env:`) | **높음**(A 를 덮어씁니다) |

방법 B 로 파일 값을 그대로 올리려면:

```bash
set -a; . ./.env.dev; set +a
```

### 🔴 필수 — 없으면 기동 시점에 실패합니다

백엔드는 코어 설정을 **서빙 역할**로 초기화합니다(`service/bootstrap.py` → 코어 `bootstrap_env(env, repo_root=…, role="serving")`).
그래서 코어의 필수 11개 중 **적재 전용 5개**(`ENCODING`·`CHUNK_SIZE`·`OVERLAP_SIZE`·`SUMMARY_MAX_CHARS`·`TOP_K_KEYWORDS` —
요약·청킹이 읽는 값)는 요구하지 않고, 다음 **6개**만 필수입니다(미설정 시 `ValueError: 필수 환경변수 누락: <이름>`
으로 즉시 중단 — 잘못된 설정으로 조용히 도는 것을 막는 fail-fast). `/health` 만 확인할 때는 값이 형식만 맞으면 되고
DB·LLM 에 접속하지 않습니다.

```dotenv
META_MODEL=              # 온프레미스 LLM 모델 이름(검색 결과 검증·질의 정규화 LLM 방식·개체 판정이 켜져 있을 때 사용)
OPENAI_BASE_URL=         # OpenAI 호환 엔드포인트(= 온프레미스 LLM 서버)
OPENAI_API_KEY=
TEXT_EMBED_MODEL=        # 질의를 벡터로 바꾸는 임베딩 모델 — 문서 색인과 같아야 합니다
TEXT_EMBED_CHUNK_SIZE=512
TEXT_EMBED_NORMALIZE=true
```

> 적재 전용 5개를 그래도 넣어 두면 그 값이 읽힙니다(형식 검증 포함). 없으면 코어가 정한 자리값이 들어가며 백엔드는
> 그 값을 어디에서도 읽지 않습니다.

### 그 외

| 구분 | 변수 |
|---|---|
| 프로파일 | `PORTAL_API_ENV`(기본 `dev`) |
| DB | `POSTGRES_HOST` · `POSTGRES_PORT` · `POSTGRES_DB` · `POSTGRES_USER` · `POSTGRES_PASSWORD` |
| 검색 | `OPENSEARCH_HOST` · `OPENSEARCH_PORT` |
| 인증 | `PORTAL_AUTH_DISABLED` · `PORTAL_JWT_SECRET` · `PORTAL_JWT_ISSUER` · `PORTAL_JWT_TTL_SECONDS` · `PORTAL_AUTH_BACKEND` |
| 썸네일 | `THUMBNAIL_CACHE_DIR`(선택 · 기본 시스템 임시 디렉터리) |

> 원본 파일 위치는 별도 설정이 없습니다 — 다운로드·썸네일은 **DB 에 기록된 경로(`fs_path`)** 를 읽습니다.
> 그 경로가 이 서버에서 접근 가능해야 합니다(적재한 기계와 다른 기계면 같은 마운트가 필요합니다).

### 인증 동작 (중요)

| `PORTAL_AUTH_DISABLED` | 동작 |
|---|---|
| `1` (연구·개발) | 토큰 없이 호출 가능 → `anonymous`(public 권한). Bearer 가 있으면 검증합니다. `POST /auth/token` 으로 dev 토큰 발급 가능 |
| `0` (운영) | Bearer **필수**(없으면 401) · `POST /auth/token` 은 404 · **`PORTAL_JWT_SECRET` 미설정이면 기동 시점에 실패**(fail-fast) |

JWT 는 HS256 이고 `exp`·`sub` 를 필수로 검증합니다. `PORTAL_JWT_ISSUER` 를 설정하면 `iss` 를 고정해
다른 서비스의 토큰 재사용을 막습니다.

## 실행

```bash
set -a; . ./.env.dev; set +a                          # 설정을 환경으로 올린다
uvicorn service.api:app --host 127.0.0.1 --port 8001
```

백그라운드로 돌리고 pid·로그를 관리하려면 프로세스 매니저(systemd·supervisor 등)를 쓰거나
간단히 `nohup … &` 로 띄우십시오.

> ⚠️ `--host 0.0.0.0` 은 모든 네트워크 인터페이스에 노출됩니다. 신뢰된 네트워크가 아니면
> 리버스 프록시 뒤에 두고 인증을 활성화(`PORTAL_AUTH_DISABLED=0`)하십시오.

전제: 코어에서 **DB 스키마 생성 + 닫힌 taxonomy 시드**가 끝나 있어야 하고, 파이프라인이 자산을
적재·색인해 둔 상태여야 검색 결과가 나옵니다.

## 확인

```bash
curl "http://127.0.0.1:8001/health"
curl "http://127.0.0.1:8001/search?q=김치%20담그기&modalities=video,text&size=10"
```

> ⚠️ **새 화면은 `GET /file-search` 를 쓴다**(대체 창구 — 같은 단어 절·점수식 · 개수·칩·페이징·정렬 · 12배 빠름). `/search` 는 기존 화면과 과제 산출물 근거로 남는다.

`GET /search` 는 결과를 **모달리티별 그룹**(text·image·video·audio)으로 반환하며 섹션마다 독립
랭킹입니다. 주제·기간·확장자·출처 필터와 주제 facet 을 함께 제공합니다.

## 테스트

```bash
python -m unittest discover -s tests
```

## 구조

```
service/
  api/          FastAPI 앱·라우터·요청/응답 모델·미들웨어
  portal/       검색 조립·자산 조회·다운로드·썸네일·인증/권한
  bootstrap.py  코어 bootstrap_env 호출(자기 레포 루트의 .env · 서빙 역할)
tests/          단위 테스트
```

## 설계 제약

- **학습 기반 방식을 쓰지 않습니다** — 사전학습 모델은 추론 전용입니다.
- 배포는 네이티브입니다(Dockerfile·compose 를 두지 않습니다). 공유 스토리지는 호스트 파일시스템 경로로 접근합니다.
- 코드·주석·로그는 한국어로 작성합니다.

## 트러블슈팅

### `ValueError: 필수 환경변수 누락: META_MODEL`

설정이 **하나도** 로드되지 않았다는 뜻입니다. 값이 틀린 게 아니라 대개 `.env` 파일을 못 찾은 것입니다.

1. `.env.dev` 가 **실행하는 디렉터리** 또는 레포 루트에 있는지 확인하십시오(`cp .env.example .env.dev`).
2. `--env dev` 로 실행했는지 확인하십시오 — `--env prod` 는 `.env.prod` 를 찾습니다.
3. 그래도 안 되면 환경변수를 직접 주입하십시오: `set -a; . ./.env.dev; set +a`
   (§환경변수 › 방법 B — 설치 방식과 무관하게 항상 동작합니다).

### 코어를 못 찾습니다 (`ModuleNotFoundError: No module named 'src'`)

이 레포는 코어(`dataplatform-core`)를 필요로 합니다. §설치 순서대로 **코어를 먼저** 설치하십시오.

### 401 이 반환됩니다

`PORTAL_AUTH_DISABLED=0`(운영)이면 Bearer 토큰이 필수입니다. 연구·개발이면 `1` 로 두십시오.
`0` 인데 `PORTAL_JWT_SECRET` 이 없으면 **기동 시점에** 실패합니다(fail-fast).

### 검색 결과가 비어 있습니다

적재·색인이 끝나 있어야 합니다. 파이프라인 레포에서 수집을 돌리고 `OPENSEARCH_SYNC_ENABLED=true`
인지 확인하십시오. 다운로드·썸네일이 404 면 DB 에 기록된 원본 경로(`fs_path`)가 **이 서버에서 접근 가능한지** 보십시오
(적재한 기계와 다르면 같은 경로로 마운트돼 있어야 합니다).

## 이 레포에 대해

이 레포는 이 프로젝트의 **공개 개발 레포**입니다 — 소스는 여기서 직접 개발합니다(2026-08-06 이후). 코드·테스트와
"어떻게 돌리나"(이 README)만 담고, **왜 이렇게 설계했나**(기획·설계 문서·설계 변경 이력·결정 기록)는 별도 비공개
문서 레포에 있습니다. 그래서 커밋 메시지는 짧고, 근거는 `근거: 설계이력 YYYY-MM-DD` 한 줄로 그 문서를 가리킵니다.

- 코어는 git 태그(`vMAJOR.MINOR.PATCH`)를 기준으로 설치합니다. 코어 공개 API 변경은 코어 `CHANGELOG.md` 에 있습니다.
- 문의는 과제 담당자에게 해주십시오.
