-- =============================================================================
-- portal_user — 로그인에 필요한 계정 표(2026-09-21 · 서비스에서 만든다).
--
-- 🔴 원본은 코어의 초안 마이그레이션(`migrations/sql/302_portal_user.sql`)이다. 그 초안이 아직
--    커밋되지 않았고 번호도 겹쳐(`TODO.md`) 코어가 적용해 주기를 기다릴 수 없어, **같은 정의**를
--    여기에 두고 서비스가 만든다. 코어가 뒤에 자기 마이그레이션을 돌려도 충돌하지 않는다 —
--    양쪽 모두 `CREATE TABLE IF NOT EXISTS` 다.
--
--    적용:  psql "$DATABASE_URL" -f scripts/sql/portal_user.sql
-- =============================================================================

-- =============================================================================
-- 포털 사용자 계정 — portal_user (임시 인증·인가 수단 · 계정_설계.md).
--
-- 배경: 지금까지 계정 표가 없었다. 토큰 주체(`Principal.user_id`)는 dev 발급기가 넣는 임의
--   문자열이었고, 권한은 `access_tier.principal_clearance(authenticated)` 의 2단계(public /
--   authorized)뿐이었다. 소유권을 갖는 기능(303 데이터셋)이 들어오면서 **주체를 검증할 표**가
--   필요해졌다. 설계 근거는 `계정_설계.md`.
--
-- 결정 요약
--   · 실운영 로그인이 아니라 임시 인증·인가 수단이다(계정_설계.md 0절). 승인제·시도 제한·계정
--     복구·약관 동의·이메일·기능별 권한을 전부 두지 않는다 — 두면 그에 딸린 절차(잠금 해제,
--     승인권자 보장 등)가 함께 필요해진다(계정_설계.md 1절).
--   · 역할·상태를 토큰에 담지 않는다 — 클레임은 sub·iat·exp·iss 넷뿐이고, 요청마다 이 표를
--     읽는다(담으면 권한을 회수해도 토큰 만료까지 그대로 유지된다. 강제 로그아웃을 두지 않으므로).
--   · JWT `sub` = user_id(UUIDv7). `access_log.user_id`(VARCHAR·FK 아님)에도 같은 값이 들어가
--     계정을 지워도 감사 기록이 남는다.
--   · 기능별 허용 플래그 컬럼을 두지 않는다 — 조회·다운로드·생성은 전원 허용으로 확정했고(단,
--     나중에 바뀔 수 있다), 인가는 `role` 하나로 판정한다. 데이터셋 수정·삭제만 예외이며 이건
--     `role` 이 아니라 소유권(303 owner_id)으로 판정한다(계정_설계.md 3.1).
--   · 비밀번호 해시는 argon2id 만 쓴다 — bcrypt 는 입력 72바이트를 넘는 부분을 조용히 버리는데,
--     비밀번호 길이에 제한을 두지 않기로 했으므로(계정_설계.md 2.5) 이 절단이 그 결정과
--     충돌한다(계정_설계.md 2.4).
--
-- 적용 순서: v301 이후. 신규 테이블·인덱스만 추가하고 다른 스키마는 무접촉.
-- =============================================================================

CREATE TABLE IF NOT EXISTS portal_user (
    user_id       UUID PRIMARY KEY,                       -- UUIDv7. JWT sub 에 이 값이 들어간다
    login_id      VARCHAR(50)  NOT NULL,                  -- 로그인 아이디. 규칙 없음, 상한은 컬럼 크기
    display_name  VARCHAR(100),                           -- 화면 표시용(선택). NULL 이면 login_id 를 쓴다
    password_hash VARCHAR(255) NOT NULL,                  -- argon2id 인코딩 문자열만. 평문·복호가능 암호화 금지
    status        VARCHAR(20)  NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended')),
    role          VARCHAR(20)  NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user', 'admin')),
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ
);

-- 대소문자만 다른 아이디를 막는다 — 없으면 가입 시 중복 확인이 무의미해진다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_portal_user_login_id
    ON portal_user (lower(login_id));

-- status 인덱스는 두지 않는다 — 값이 두 개뿐이라 선택성이 없다(계정_설계.md 3).

COMMENT ON TABLE portal_user IS
    '포털 사용자 계정. 임시 인증·인가 수단(계정_설계.md 0절). 역할·상태는 토큰이 아니라 요청마다 이 표에서 읽는다.';
COMMENT ON COLUMN portal_user.user_id IS
    'PK(UUIDv7). JWT sub · access_log.user_id 와 같은 값.';
COMMENT ON COLUMN portal_user.login_id IS
    '로그인 아이디. lower(login_id) 유일 — 대소문자 차이만으로 중복 가입할 수 없다. 규칙 없음, 상한은 컬럼 크기(50자).';
COMMENT ON COLUMN portal_user.display_name IS
    '화면 표시 이름(선택). NULL 이면 소비처가 login_id 를 쓴다.';
COMMENT ON COLUMN portal_user.password_hash IS
    'argon2id 인코딩 문자열만 저장한다. bcrypt 는 쓰지 않는다(72바이트 절단이 비밀번호 길이 무제한 결정과 충돌). 평문·복호가능 암호화 금지.';
COMMENT ON COLUMN portal_user.status IS
    '계정 상태: active(사용가능)|suspended(정지). 가입 즉시 active — 승인제를 두지 않는다.';
COMMENT ON COLUMN portal_user.role IS
    '역할: user|admin. 데이터셋 수정·삭제 권한 판정에 쓰인다(303 참조). 조회·다운로드·생성은 role 과 무관하게 전원 허용.';
COMMENT ON COLUMN portal_user.last_login_at IS
    '마지막 로그인 시각. 계정 이벤트 감사는 이 컬럼과 updated_at 근사치 이상은 남기지 않는다(계정_설계.md 2.6).';
