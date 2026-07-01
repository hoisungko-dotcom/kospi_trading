# 한국봇 SaaS 정리안

## 1. 목표

한국봇을 개인 실행형 자동매매 코드베이스에서, 운영 가능한 SaaS 제품 구조로 재정리한다.

핵심 목표는 4가지다.

1. 사용자별 계정, 설정, 실행 상태를 분리한다.
2. 실거래 엔진과 SaaS 웹 서비스 책임을 분리한다.
3. 전략 개발, 섀도우 검증, 실전 실행을 서로 다른 운영 단계로 나눈다.
4. 현재 `legacy(main.py/core)`와 `kospi_bot_v2`가 혼재된 상태를 제품 기준으로 정리한다.

---

## 2. 제품 정의

### 제품명

- 대외 제품명: `한국봇`
- 내부 엔진명: `KR Bot Engine`

### SaaS로 제공할 가치

- 한국 주식 시장 대상 전략 기반 자동매매
- 사용자별 종목 유니버스/리스크 설정 관리
- 섀도우 운용 결과 리포트 제공
- 실전 전환 전 검증 기록 축적
- 알림, 리포트, 계좌 스냅샷, 실행 이력 관리

### 초기에 하지 말 것

- 멀티 브로커 동시 지원
- 초고빈도/초단타 지향 구조
- 사용자가 임의 파이썬 전략 코드를 업로드하는 구조
- 모바일 앱 우선 개발

초기 SaaS는 "단일 브로커 중심의 관리형 자동매매 서비스"로 좁히는 편이 맞다.

---

## 3. 사용자 관점의 SaaS 기능

### 필수 기능

1. 회원가입/로그인
2. 브로커 API 자격 증명 등록
3. 모의/실전 모드 전환
4. 전략 선택
5. 리스크 설정
6. 당일 후보 종목/보유 종목/주문 내역 조회
7. 일별 리포트 조회
8. 텔레그램 알림 연결
9. 봇 시작/중지

### 운영자 기능

1. 사용자 상태 조회
2. 봇 실행 상태 모니터링
3. 강제 중지 킬스위치
4. 전략 버전 배포 관리
5. 장애 로그 확인
6. 일별 거래/주문/실패 집계

---

## 4. 권장 제품 구조

SaaS는 아래 4계층으로 나누는 것이 좋다.

### A. Web App

- 사용자 대시보드
- 설정 화면
- 실행 상태/리포트 화면
- 운영자 콘솔

추천 기술:

- Frontend: Next.js
- Backend API: FastAPI
- Auth: Supabase Auth 또는 Auth.js

### B. Application API

책임:

- 사용자/플랜/설정 CRUD
- 브로커 자격 증명 저장
- 실행 요청 생성
- 봇 상태 조회
- 리포트/주문/포지션 조회

이 레이어는 거래 판단을 직접 하지 않고, "제어면(control plane)" 역할을 맡는다.

### C. Bot Runner

책임:

- 실제 장중 루프 실행
- 브로커 시세 조회
- 전략 계산
- 주문 실행
- 섀도우/실전 결과 저장

이 레이어는 "데이터면(data plane)"이다. 웹 API와 분리된 별도 워커/프로세스로 두는 편이 안전하다.

### D. Storage

- PostgreSQL: 사용자, 설정, 주문 메타데이터, 리포트 인덱스
- Redis: 실행 상태, 락, 짧은 TTL 캐시
- Object Storage: 리포트 원본, 로그 아카이브

---

## 5. 현재 코드 기준 권장 분리

현재 레포는 크게 두 덩어리다.

1. `main.py` + `core/`
2. `kospi_bot_v2/`

전략 정체성 기준으로는 `main.py` 계열 Box Bot을 유일한 본 전략으로 보고,
`kospi_bot_v2`는 shadow/support 엔진으로 두는 편이 맞다.

### 이유

- 운영자가 인식하는 실제 전략 철학은 Box Bot이다.
- `main.py` 쪽이 현재 집중형 박스봇 실전 흐름에 더 가깝다.
- `kospi_bot_v2`는 구조적으로 유용하지만, 별도 전략 정체성으로 유지하면 혼선이 생긴다.
- 따라서 SaaS화는 `kospi_bot_v2`의 구조를 참고하되, 전략 정체성은 Box Bot 하나로 수렴해야 한다.

### 정리 원칙

1. Box Bot은 유일한 실전 전략 정체성으로 유지
2. `kospi_bot_v2`의 좋은 구조는 Box Bot으로 흡수
3. shadow/support 엔진은 실전 본체와 경쟁하지 않도록 역할을 제한
4. 실전 주문 호출부는 명확한 인터페이스 뒤로 숨김
5. 저장 포맷은 파일 중심에서 DB 중심으로 이동

---

## 6. SaaS 아키텍처 초안

### 제어 흐름

1. 사용자가 웹에서 봇 시작
2. API 서버가 `bot_run` 레코드 생성
3. 워커가 실행 대기열에서 작업 수신
4. 워커가 사용자 설정과 브로커 자격 증명 로드
5. 워커가 `LiveRunner` 또는 `ShadowRunner` 실행
6. 이벤트/주문/포지션/리포트를 DB와 스토리지에 기록
7. API 서버가 대시보드에 상태 노출

### 권장 프로세스 분리

- `api-server`
- `scheduler`
- `bot-worker`
- `report-worker`
- `monitoring/alerting`

### 중요한 운영 원칙

- 사용자별 실행은 격리
- 계정별 브로커 rate limit 고려
- 시장 시간 외 실행 제한
- 강제 종료 가능한 kill switch 보유
- 실전 주문은 idempotency key 기반 기록 필요

---

## 7. 데이터 모델 초안

### users

- id
- email
- plan
- status
- created_at

### broker_credentials

- id
- user_id
- broker_name
- app_key_encrypted
- app_secret_encrypted
- account_no_encrypted
- mock_trading

### bot_configs

- id
- user_id
- strategy_key
- universe_mode
- max_positions
- max_position_pct
- daily_loss_limit_pct
- active_start_hhmm
- active_end_hhmm
- notify_telegram

### bot_runs

- id
- user_id
- mode (`shadow`, `live`)
- status (`queued`, `running`, `stopped`, `failed`, `completed`)
- started_at
- ended_at
- strategy_version

### orders

- id
- bot_run_id
- user_id
- symbol
- side
- qty
- requested_price
- filled_price
- status
- broker_order_id
- created_at

### positions

- id
- user_id
- symbol
- qty
- avg_price
- unrealized_pnl
- strategy_tag
- updated_at

### reports

- id
- user_id
- bot_run_id
- report_type
- storage_path
- trade_date
- created_at

---

## 8. 보안/규제/신뢰성 체크포인트

한국봇 SaaS에서는 기능보다 이 부분이 더 중요하다.

### 필수

1. API 키 암호화 저장
2. 실전 모드 다중 확인 절차
3. 주문 직전 최종 가드
4. 전체 서비스 킬스위치
5. 사용자별 일 손실 제한
6. 모든 주문/판단 근거 감사 로그
7. 운영자 액션 로그

### 문구/정책

- 투자자문 아님 고지
- 자동매매 리스크 고지
- 브로커 장애 시 책임 범위 명시
- 실전 전환 사전 동의
- 개인정보/민감정보 저장 정책

이 부분은 제품 출시 전에 법률/컴플라이언스 검토가 필요하다.

---

## 9. 가격 정책 초안

### Free

- 섀도우 운용만
- 일 리포트 제공
- 실전 주문 불가

### Basic

- 실전 운용 가능
- 텔레그램 알림
- 기본 전략 1~2개

### Pro

- 전략 다중 선택
- 고급 리스크 설정
- 상세 리포트
- 우선 지원

초기에는 복잡한 과금보다 `무료 섀도우 + 유료 실전` 구조가 가장 명확하다.

---

## 10. 개발 우선순위

### Phase 1. 엔진 정리

목표:

- `kospi_bot_v2`를 단일 코어 엔진으로 확정
- legacy 의존 경계 파악
- 파일 저장 구조를 서비스 친화적으로 바꾸기

할 일:

1. 실행 엔트리 통합
2. 브로커 인터페이스 표준화
3. 설정 로딩 구조 정리
4. 결과 저장 추상화

### Phase 2. SaaS 제어면

목표:

- 웹/API에서 사용자별 봇 실행 제어 가능하게 만들기

할 일:

1. 사용자 인증
2. 봇 설정 CRUD
3. 실행 요청/중지 API
4. 실행 상태 조회 API

### Phase 3. 운영 가시성

목표:

- 장애와 손실을 운영자가 즉시 볼 수 있게 만들기

할 일:

1. 중앙 로그 수집
2. 주문 실패 경보
3. 사용자별 실행 대시보드
4. 리포트 저장소 정리

### Phase 4. 상용화 준비

목표:

- 유료 서비스로 운영 가능한 최소 기준 확보

할 일:

1. 결제
2. 약관/고지
3. 관리자 킬스위치
4. 감사 로그/보안 점검

---

## 11. 지금 레포에서 바로 할 정리

### 코드 구조

1. `legacy/` 또는 `archive/` 개념 도입 검토
2. `main.py`의 역할을 축소하고 `kospi_bot_v2` 중심으로 이동
3. `backtest_*.py` 스크립트는 `scripts/` 또는 `research/`로 정리
4. 런타임 산출물 경로를 표준화

### 문서 구조

1. 루트 README는 제품 소개용으로 축소
2. `docs/` 아래에 운영 문서, 아키텍처 문서, 전략 문서 분리
3. handoff 문서는 `docs/handoff/` 유지하되 제품 기준 문서와 분리

### 운영 구조

1. `.env` 중심 설정을 장기적으로 DB 설정으로 이전
2. 텔레그램 알림을 Notification 인터페이스 뒤로 이동
3. 파일 로그 외에 DB 이벤트 저장 추가

---

## 12. 추천 다음 액션

가장 현실적인 다음 순서는 이렇다.

1. `kospi_bot_v2`를 한국봇 코어 엔진으로 확정
2. legacy와 v2 사이의 남은 의존관계 목록화
3. SaaS용 API/DB 스키마 초안 작성
4. 사용자 대시보드 와이어프레임 작성
5. 실전 주문 안전장치 요구사항 문서화

---

## 13. 한 줄 결론

한국봇 SaaS는 "자동매매 전략"보다 먼저 "사용자별 실행 제어, 안전장치, 기록, 운영 가시성"을 제품화해야 한다.  
현재 코드베이스에서는 `kospi_bot_v2`를 코어로 삼고, legacy는 축소하는 방향이 가장 자연스럽다.
