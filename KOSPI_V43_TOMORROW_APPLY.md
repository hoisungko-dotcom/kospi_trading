# KospiBot v4.3 내일 적용 작업 지시서

작성일: 2026-05-31

## 목표

한국주식 봇에 v4.3 신호 로직을 운영 후보로 반영한다.

이번 작업의 목표는 새 전략을 더 만들거나 추가 백테스트를 반복하는 것이 아니라, 이미 검증한 v4.3 결론을 운영 코드에 조심스럽게 옮기는 것이다.

## 최종 결정

- v4.3 per-stock 신호 로직은 채택 후보로 둔다.
- 포트폴리오 운용은 8슬롯 고정 우선으로 한다.
- B안 신호밀도별 4~10슬롯 가변은 폐기한다.
- C안 가변+우선순위 교체는 현 버전 폐기한다.
- 향후 교체를 다시 검토한다면 C→B 교체 금지, A-MOMENTUM 진입 시에만 제한적으로 테스트한다.

## 적용 범위

우선 적용 대상:

- `kospi_bot_v2/strategy/signal_engine.py`
- `kospi_bot_v2/strategy/gates.py`
- `kospi_bot_v2/risk/position_sizer.py`
- 필요 시 `kospi_bot_v2/config/settings.py`
- 필요 시 `kospi_bot_v2/domain/models.py`

참고용 파일:

- `backtest_v3.py`
- `BACKTEST_DECISIONS.md`

## 구현 지시

### 1. 진입 유형 단순화

운영 신호는 아래 3개만 허용한다.

- `BREAKOUT`
- `MOMENTUM`
- `PULLBACK`

운영 매수 사유에서 아래 유형은 독립 진입으로 쓰지 않는다.

- `REVERSAL`
- `LONG_MA_BREAK`

`LONG_MA_BREAK`는 필요하면 보조 점수나 설명용 메타데이터로만 남긴다.

### 2. v4.3 신호 방향 반영

백테스트 v4.3의 핵심 방향을 운영 코드에 맞게 반영한다.

- `BREAKOUT`: 유지
- `MOMENTUM`: 유지
- `PULLBACK`: A/B급만 허용, C급 PULLBACK 차단
- `B-MOMENTUM`: 0.5x 축소 또는 점수/비중 감산
- `B-BREAKOUT`: 0.5x 축소 또는 점수/비중 감산
- `C-BREAKOUT`, `C-MOMENTUM`: 백테스트상 건강했으므로 무조건 차단하지 않는다.

주의: 기존 점수 체계를 무리하게 새로 만들지 말고, 현재 운영 코드의 `SignalEngine._strategy_for`, `_score`, `CandidateGates.reject_reason`, `PositionSizer.quantity` 구조 안에서 최소 변경으로 반영한다.

### 3. 포트폴리오 슬롯

운영 기준:

- 최대 동시 보유: 8슬롯
- 종목당 기준 비중: 12.5%
- 기존 C안 우선순위 교체는 적용하지 않는다.
- 슬롯이 가득 찼을 때 새 신호가 와도 기존 포지션을 자동 교체하지 않는다.

예외 후보는 이번 적용에서 제외한다.

- A-MOMENTUM 교체 허용
- C→B 교체 금지

위 예외는 향후 별도 백테스트 후 적용한다.

### 4. 금지 사항

이번 작업에서 하지 말 것:

- 새 필터 추가 반복
- C안 우선순위 교체 적용
- 신호밀도별 4~10슬롯 가변 적용
- REVERSAL 재도입
- LONG_MA_BREAK 독립 진입 재도입
- 백테스트 숫자에 맞춘 과최적화
- 기존 운영 봇을 바로 완전 교체

## 검증 절차

### 로컬 검증

```bash
cd /Users/hoisung/Downloads/kospi_trading_system
python -m py_compile \
  kospi_bot_v2/strategy/signal_engine.py \
  kospi_bot_v2/strategy/gates.py \
  kospi_bot_v2/risk/position_sizer.py \
  kospi_bot_v2/config/settings.py \
  kospi_bot_v2/domain/models.py
```

가능하면 shadow 1회 실행 또는 dry-run을 먼저 한다.

```bash
cd /Users/hoisung/Downloads/kospi_trading_system
python -m kospi_bot_v2.main --once
```

실패하면 운영 반영하지 않는다.

### 서버 반영

서버 경로:

```text
/home/ubuntu/kospi_trading_system
```

동기화 전 현재 서버 파일 백업:

```bash
ssh aws-trading 'cd /home/ubuntu/kospi_trading_system && mkdir -p backups/v43_$(date +%Y%m%d_%H%M%S) && cp -a kospi_bot_v2 backups/v43_$(date +%Y%m%d_%H%M%S)/'
```

동기화:

```bash
rsync -av kospi_bot_v2/ aws-trading:/home/ubuntu/kospi_trading_system/kospi_bot_v2/
rsync -av BACKTEST_DECISIONS.md KOSPI_V43_TOMORROW_APPLY.md aws-trading:/home/ubuntu/kospi_trading_system/
```

서버 컴파일:

```bash
ssh aws-trading 'cd /home/ubuntu/kospi_trading_system && ./venv/bin/python -m py_compile kospi_bot_v2/strategy/signal_engine.py kospi_bot_v2/strategy/gates.py kospi_bot_v2/risk/position_sizer.py'
```

### 재시작 전 확인

현재 실행 중인 한국 봇 확인:

```bash
ssh aws-trading 'pgrep -af "kospi_bot_v2.main|kospi_trading_system/venv/bin/python3 main.py"'
```

중복 실행이 있으면 바로 재시작하지 말고 어떤 프로세스가 실운영인지 먼저 구분한다.

### 재시작

서비스 이름이 명확하면 systemd로 재시작한다.

```bash
ssh aws-trading 'systemctl --type=service --state=running | grep -i kospi || true'
```

서비스가 없다면 기존 실행 방식에 맞춰 재시작한다. 중복 실행 금지.

## 완료 기준

작업 완료로 인정하는 조건:

- 로컬 py_compile 통과
- 서버 py_compile 통과
- 서버에 `KOSPI_V43_TOMORROW_APPLY.md`와 `BACKTEST_DECISIONS.md` 존재
- 운영 봇 프로세스 중복 없음
- 시작 후 로그에 import error, enum error, attribute error 없음
- 첫 shadow/report 메시지에서 REVERSAL/LONG_MA_BREAK 독립 진입이 나오지 않음
- 포트폴리오 교체 로직이 자동 실행되지 않음

## 내일 판단 기준

내일 적용 후 바로 실매매 확대하지 않는다.

권장 순서:

1. shadow/dry-run 1회
2. 로그 확인
3. 알림 포맷 확인
4. 기존 v3와 신호 비교
5. 이상 없을 때만 운영 비중 반영 검토

## 최종 한 줄

v4.3은 신호 로직만 운영 후보로 옮기고, 포트폴리오는 8슬롯 고정으로 단순하게 시작한다. 교체 시스템은 이번 배포에 넣지 않는다.
