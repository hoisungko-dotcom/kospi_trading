# KR Trading System

국내 주식 자동매매 시스템입니다. 현재 저장소는 단일 봇 스크립트에서
브로커 교체형 SaaS 구조로 정리하는 중이며, 공통 계층과 브로커 계층을 분리하는 방향으로 운영합니다.

## 현재 구조

- `brokers/`: KIS, Kiwoom 등 브로커별 구현
- `runtime/`: 실행 엔트리포인트, 장시간, 오케스트레이션
- `services/`: 잔고, 섹터, 운영 보조 서비스
- `state/`: 계좌 스냅샷, 포지션 상태
- `strategy/`: 신호, 리스크, 전략 로직
- `docs/`: 시스템 지도, 운영 문서, 이력 인덱스

전체 구조와 수정 지점은 [docs/README.md](/Users/hoisung/Downloads/kospi_trading_system/docs/README.md)부터 보면 됩니다.

## 실행 진입점

기존 로컬 실거래 엔진:

```bash
python main.py
```

모듈형 KR bot 엔진:

```bash
python -m kospi_bot_v2.main --sample
python -m kospi_bot_v2.main --broker-quote
python -m kospi_bot_v2.main --broker-quote --loop
python -m kospi_bot_v2.main --live --broker-quote --loop
```

## 브로커 설정 원칙

- 공통 코드에서는 브로커 브랜드명을 직접 쓰지 않습니다.
- 브로커별 키와 계좌 설정은 브로커 어댑터 레이어에서만 처리합니다.
- 새 설정은 표준 키를 우선 사용하고, 과거 키는 호환 fallback만 유지합니다.

대표 표준 키 예시:

```text
BROKER_PROFILE=kiwoom_full
BROKER_SYNC_INTERVAL_SEC=20
BOX_BOT_UNIVERSE_BROKER_MARKET=KOSPI
```

명칭 기준은 [docs/naming-convention.md](/Users/hoisung/Downloads/kospi_trading_system/docs/naming-convention.md)에 정리되어 있습니다.

## 운영 문서

- 시스템 지도: [docs/system-map.md](/Users/hoisung/Downloads/kospi_trading_system/docs/system-map.md)
- 운영 런북: [docs/operations-runbook.md](/Users/hoisung/Downloads/kospi_trading_system/docs/operations-runbook.md)
- 이력 인덱스: [docs/history-index.md](/Users/hoisung/Downloads/kospi_trading_system/docs/history-index.md)
- 구조 로드맵: [docs/repo-structure-roadmap.md](/Users/hoisung/Downloads/kospi_trading_system/docs/repo-structure-roadmap.md)

## 주의

- 이 저장소는 리팩터링 중이라 로컬 워크스페이스와 운영 서버가 동일하지 않을 수 있습니다.
- 실제 운영 서버 변경은 로컬 수정과 별도로 배포해야 합니다.
- 실거래 전환 전에는 반드시 모의투자 기준으로 계좌, 주문, 체결, 잔고 동기화를 검증해야 합니다.
