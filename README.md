# service

멀티모달 데이터 통합 플랫폼의 HTTP API 서버입니다. FastAPI 로 만들었습니다.

## 이 레포지토리는 무엇인가

웹 화면이 호출하는 API 를 제공합니다. 검색, 자산 상세 조회, 개체 조회, 계정과 로그인을
담당합니다.

**파일을 내주는 API 는 자리만 있습니다.** 원본 내려받기, 묶음 zip, 미리보기 이미지, 본문은 경로와
파라미터만 등록돼 있고 부르면 501 입니다. 파일 제공 방식을 다른 쪽과 협의한 뒤 구현합니다(2026-09-28 · 지우기 직전 구현은
git `9d11d29`). 다시 만들 때 따를 원본 파일 전제는 `service/portal/asset/__init__.py` 에 적어 두었습니다.

검색 기능 자체는 core 라이브러리에 있습니다. 이 레포는 그 함수를 호출하고, 결과를 화면이
쓰기 쉬운 JSON 으로 만들어 돌려줍니다.

데이터베이스와 검색 색인은 읽기만 합니다. 자산을 등록하거나 색인을 바꾸는 일은
pipeline 레포가 합니다.

| 저장소 | 하는 일 |
|---|---|
| [core](https://github.com/mobigen-auroraFS-lab/core) | 공통 코드 · 데이터베이스 스키마 |
| [pipeline](https://github.com/mobigen-auroraFS-lab/pipeline) | 파일 수집 · 분류 · 메타데이터 추출 · 색인 · 자산 간 관계 생성 |
| **service** (이 레포) | 웹 화면이 사용하는 HTTP API |

## 디렉터리 구조

```
service/
  api/                # FastAPI 앱
    __init__.py       # 앱 조립. 상태 확인·로그인 토큰 경로도 여기 있습니다
    routes/           # 경로별 라우터
      search.py         # 종류별로 묶어 보여주는 검색
      file_search.py    # 파일 목록 형태의 검색
      assets.py         # 자산 상세, 주제, 자산의 개체
      mm_meta.py        # 개체 목록과 상세
      files.py          # 파일 제공 경로 — 자리만(모두 501)
      catalog.py        # 태그·관계 종류 목록
      account.py        # 회원가입·로그인
      admin.py          # 운영 통계 — 등재 보류
      review.py         # 관계 검토 — 등재 보류
    db.py             # DB 연결 풀과 트랜잭션 통로
    audit.py          # 접근 기록. 응답과 분리해 뒤에서 남깁니다
    errors.py         # 예외를 HTTP 응답으로 옮김
    lifespan.py       # 기동·종료 순서
    params.py         # 여러 경로가 함께 쓰는 요청 검증

  portal/             # 실제 조회와 가공
    asset/            # 자산 상세, 집계, 파일 메타
    auth/             # 로그인, 토큰, 비밀번호
    search/           # 검색 결과 묶기, 정렬 기준, 칩 집계
    history/          # 접근 기록, 처리 이력
    repositories/     # 데이터베이스 접근
    common/           # 여러 곳이 함께 쓰는 조각

  bootstrap.py        # 시작할 때 core 설정을 읽어 들입니다

tests/                # 단위 테스트
```

`api/` 는 경로와 파라미터만 다루고 실제 일은 `portal/` 에서 합니다. 화면이 바뀌어 응답
모양을 고칠 때는 `portal/` 을 봅니다.

`admin.py` 와 `review.py` 는 코드에 있지만 앱에 등재하지 않았습니다. 해당 경로로 요청하면
404 가 돌아옵니다.

## 주요 API

| 경로 | 용도 |
|---|---|
| `GET /health` | 서버 상태 확인 |
| `POST /auth/signup` | 회원가입 |
| `POST /auth/login` | 로그인 |
| `GET /auth/login-id/availability` | 아이디 사용 가능 여부 |
| `GET /me` | 내 정보 |
| `POST /auth/token` | 개발용 토큰 발급 |
| `GET /search` | 종류별로 묶어 보여주는 검색 |
| `GET /file-search` | 파일 목록 형태의 검색. 주제·종류·태그로 좁히고 정렬 |
| `GET /file-search/suggest` | 검색어 제안 |
| `GET /file-search/facet-extra` | 추가 칩 집계 |
| `GET /assets/{id}` | 자산 상세 |
| `GET /assets/{id}/mm-meta` | 이 자산에서 나온 개체 |
| `GET /assets/unclassified` | 분류되지 않은 자산 |
| `GET /topics` | 주제 목록. 대주제와 세부주제를 자산 수와 함께 |
| `GET /topics/{topic}` | 그 주제에 속한 자산 |
| `GET /mm-meta` | 개체 목록 |
| `GET /mm-meta/{type}/{uid}` | 개체 상세 |
| `GET /mm-meta/facets` | 개체 칩 집계 |
| `GET /tags` · `/relation-kinds` | 태그·관계 종류 목록 |

파일 제공 경로는 자리만 있습니다(부르면 501). 화면이 이 이름으로 미리 코딩할 수 있고 `/docs` 에 파라미터가 보입니다.

| 경로 | 용도 |
|---|---|
| `GET /assets/{id}/download` | 원본 파일 내려받기 |
| `GET /assets/{id}/thumbnail` | 미리보기 이미지 |
| `GET /assets/{id}/content` | 본문(문서 글자 · 받아쓰기) |
| `GET /assets/{id}/bundle` | 이 자산과 관계된 자산을 zip 하나로 |
| `POST /assets/bundle` | 고른 자산 여러 개를 zip 하나로 |
| `GET /mm-meta/bundle` · `/mm-meta/{type}/{uid}/bundle` | 개체들 · 개체 카드의 구성 자산을 zip 하나로 |

서버를 띄운 뒤 `/docs` 로 접속하면 전체 목록과 파라미터를 확인할 수 있습니다.

## 사용 환경

### 하드웨어

**세 레포 중 가장 가볍습니다.** 무거운 처리는 pipeline 이 미리 끝내 두었고, 이 서버는 그
결과를 읽어 내보내기 때문입니다.

| 구분 | 최소 | 권장 |
|---|---|---|
| CPU | 1 코어 | 2 코어 |
| 메모리 | 2 GB | 4 GB |
| 디스크 | 10 GB | — |
| GPU | 불필요 | 불필요 |

운영 클러스터에서 최대 2코어·4GB 까지 허용해 두었는데, 자산 2만 건에 검색이 붙은 상태에서
실제 사용량은 0.43GB 였습니다. 동시 접속자가 늘면 CPU 를 먼저 올리십시오. 디스크는 로그로만
씁니다.

GPU 는 필요 없습니다. 검색어를 벡터로 바꾸는 일은 임베딩 서버에 요청합니다. 이때 **문서를
색인할 때 쓴 것과 같은 모델**이어야 합니다. 다르면 오류 없이 엉뚱한 결과가 나옵니다.

### 소프트웨어

| 항목 | 요구 버전 | 개발 확인 |
|---|---|---|
| Python | 3.13 이상 | 3.13.13 |
| core 라이브러리 | v0.7.0 이상 | — |
| FastAPI | 0.115 이상 | 0.136.0 |
| uvicorn | 0.30 이상 | 0.44.0 |
| pydantic | 2.7 이상 | 2.13.1 |
| PyJWT | 2.8 이상 | 2.12.1 |
| argon2-cffi | 23.1 이상 | 25.1.0 |
| PostgreSQL | 17 + pgvector 확장 | 17.9 |
| OpenSearch | 3.x | 3.6.0 |

## 설치 방법

```bash
pip install "meta-extract @ git+https://github.com/mobigen-auroraFS-lab/core.git@v0.7.0"
pip install -e .
```

검색 결과가 나오려면 데이터베이스에 스키마가 만들어져 있고 pipeline 이 자산을 적재해 둔
상태여야 합니다. `/health` 는 그 전에도 응답합니다.

## 실행 및 운영 방법

### 설정

core 가 요구하는 값 중 파일 적재에만 쓰는 다섯 개(`ENCODING`·`CHUNK_SIZE`·
`OVERLAP_SIZE`·`SUMMARY_MAX_CHARS`·`TOP_K_KEYWORDS`)는 이 레포에서 요구하지 않습니다.
나머지 여섯 개가 필요합니다.

```dotenv
META_MODEL=                 # LLM 모델 이름
OPENAI_BASE_URL=            # LLM 서버 주소
OPENAI_API_KEY=             # LLM 서버 인증 키
TEXT_EMBED_MODEL=           # 검색어 임베딩 모델. 색인할 때 쓴 것과 같아야 합니다
TEXT_EMBED_CHUNK_SIZE=512
TEXT_EMBED_NORMALIZE=true
```

이 레포 고유 설정입니다.

| 변수 | 용도 |
|---|---|
| `PORTAL_API_ENV` | `dev` 또는 `prod`. 기본값 `dev` |
| `PORTAL_AUTH_DISABLED` | `1` 이면 토큰 없이 호출할 수 있습니다 |
| `PORTAL_AUTH_BACKEND` | 계정 확인 방식 |
| `PORTAL_JWT_SECRET` | 토큰 서명 키 |
| `PORTAL_JWT_ISSUER` | 토큰 발급자 |
| `PORTAL_JWT_TTL_SECONDS` | 토큰 유효 시간 |
| `PORTAL_CORS_ORIGINS` | 다른 주소에서 호출을 허용할 목록. 쉼표로 나열 |
| `PORTAL_DB_POOL_MIN` · `PORTAL_DB_POOL_MAX` | DB 연결 풀 크기 |
| `PORTAL_MM_META_FORM_SKILLS` | 개체 화면에 쓸 분류 기준 |
| `PORTAL_THREAD_LIMIT` | 동시에 처리할 요청 수. 없으면 기동할 때 DB 연결 풀 크기에 맞춥니다 |
| `PORTAL_MAX_BODY_BYTES` | 요청 본문 상한. 기본 1MiB, 넘으면 413 |
| `PORTAL_TAGS_CACHE_SECONDS` | `/tags` 와 검색어 제안이 쓰는 태그 목록을 들고 있을 시간. 기본 300초, `0` 이면 끕니다 |

### 다른 주소에서 호출할 때

`PORTAL_CORS_ORIGINS` 를 비워 두면 **다른 주소에서 오는 요청을 하나도 받지 않습니다.**
개발용 프런트엔드 서버에서 직접 부르려면 그 주소를 적습니다.

```dotenv
PORTAL_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

프런트엔드 개발 서버의 프록시 기능을 쓰면 이 설정이 필요 없습니다.

### DB 연결 풀

요청 처리는 원래 최대 40개까지 동시에 돌아가는데 데이터베이스 연결은 기본 10개뿐이라,
11번째부터는 연결을 기다리다 시간 초과로 떨어졌습니다. 그래서 기동할 때 동시 처리 수를 연결
풀 크기에 맞춥니다(2026-09-28). 넘치는 요청은 실패하지 않고 줄을 서서 기다립니다.

동시 접속이 늘면 `PORTAL_DB_POOL_MAX` 로 풀을 키웁니다. 동시 처리 수도 함께 따라 올라갑니다.
아래를 넘지 않게 맞춥니다.

```
uvicorn 워커 수 × PORTAL_DB_POOL_MAX ≤ PostgreSQL 최대 연결 수
```

같은 데이터베이스를 pipeline 과 함께 쓴다면 그쪽 연결까지 더해 계산해야 합니다.

### 실행

```bash
set -a; . ./.env.dev; set +a

uvicorn service.api:app --host 127.0.0.1 --port 8001                # 개발
uvicorn service.api:app --host 0.0.0.0 --port 8001 --workers 2      # 운영
```

계속 띄워 두려면 systemd 나 supervisor 같은 프로세스 관리자에 등록합니다.

`PORTAL_AUTH_DISABLED` 가 `0` 인데 `PORTAL_JWT_SECRET` 이 비어 있으면 **서버가 뜨지
않습니다.** 설정이 빠진 채로 도는 것보다 낫기 때문입니다.

### 운영 시 확인할 것

| 상황 | 할 일 |
|---|---|
| core 를 새 버전으로 올렸을 때 | core 재설치 → 테스트 → 재시작. 임베딩 모델이 바뀌었으면 pipeline 재색인이 먼저입니다 |
| 검색 결과가 비어 있을 때 | pipeline 의 색인 상태, 임베딩 서버 응답, OpenSearch 연결을 확인하십시오 |
| 동시 접속이 늘 때 | `PORTAL_DB_POOL_MAX` 와 PostgreSQL 최대 연결 수를 함께 확인하십시오 |

## 실행 예제

```bash
$ curl -s http://127.0.0.1:8001/health
{"status":"ok"}
```

파일 검색입니다. 주제를 두 개 지정하면 둘 중 하나에 해당하는 자산을 찾습니다.

```bash
$ curl -s "http://127.0.0.1:8001/file-search?q=김치&topic=음식&topic=전통문화&modality=video&sort=updated_desc&offset=20&limit=20"
{
  "query": "김치",
  "total": 57,
  "offset": 20,
  "limit": 20,
  "sort": "updated_desc",
  "items": [
    {"asset_id": "...", "modality": "video", "file_name": "...", "score": 0.71,
     "topics": ["음식"], "tags": ["..."]}
  ],
  "facets": {
    "topic": [{"value": "음식", "count": 41}],
    "modality": [{"value": "video", "count": 57}]
  },
  "filters": {"topics": ["음식", "전통문화"], "modalities": ["video"]}
}
```

`facets` 는 화면에서 결과를 더 좁힐 때 쓰는 목록입니다. 각 항목의 개수는 검색 엔진이
계산한 값입니다. 다음 쪽을 받을 때처럼 칩이 필요 없으면 `with_facets=false` 를 붙여 집계를
건너뜁니다(`facets` 가 비어서 옵니다).

응답의 건수와 점수는 데이터에 따라 달라집니다. 자산 식별자는 UUID v7 입니다.

## 기타

- **테스트** — `python -m unittest discover -s tests` 와 `ruff check service tests`.
- **core 와의 경계** — 검색 순위를 정하는 방식처럼 pipeline 과 이 레포가 같은 답을 내야
  하는 것은 core 에 둡니다. 화면이 바뀌면 함께 바뀌는 것은 이 레포에 둡니다.

## 자주 겪는 문제

| 증상 | 원인 |
|---|---|
| `필수 환경변수 누락` | 위 여섯 개 중 빠진 것이 있습니다 |
| `No module named 'src'` | core 라이브러리가 설치되지 않았습니다 |
| 기동하다 멈춤 | `PORTAL_JWT_SECRET` 이 비어 있습니다 |
| 화면에서 부르면 막힘 | `PORTAL_CORS_ORIGINS` 에 그 주소가 없습니다 |
| 부하가 걸리면 응답이 느려짐 | DB 연결 풀이 모자라 요청이 줄을 서고 있습니다 |
| 404 가 나옴 | `/admin` 과 `/review` 는 등재하지 않았습니다 |
| 501 이 나옴 | 파일 제공 경로(내려받기·미리보기·본문·묶음)는 자리만 있습니다. `size_bucket` 으로 거르기도 아직 없습니다 |
| 검색 결과가 비어 있음 | 색인이 없거나, 검색어 임베딩 모델이 색인할 때와 다릅니다 |
| 503 이 나옴 | 임베딩 서버나 OpenSearch 에 연결하지 못했습니다 |
| 413 이 나옴 | 요청 본문이 `PORTAL_MAX_BODY_BYTES` 를 넘었습니다 |

## 제3자 오픈소스

전체 목록과 라이선스 전문은 `NOTICE` 파일에 있습니다.

| 구성요소 | 라이선스 |
|---|---|
| FastAPI · pydantic · PyJWT | MIT |
| uvicorn | BSD 3-Clause |
| OpenSearch 클라이언트 | Apache License 2.0 |
| argon2-cffi | MIT |
| psycopg | LGPL 3.0 |

psycopg 는 LGPL 입니다. 파이썬에서 불러 쓰는 것은 이 소프트웨어의 라이선스에 영향을 주지
않지만, 사용 사실을 `NOTICE` 에 밝혀야 합니다.
