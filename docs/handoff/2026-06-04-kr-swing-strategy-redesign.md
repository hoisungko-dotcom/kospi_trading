# KR 봇 스윙 전략 재설계 핸드오프

Date: 2026-06-04

## 사용자 의도 (변경 불가 원칙)

```text
상승세가 강한 종목을 고른다.
숨고르기 또는 약간의 눌림이 있을 때 진입한다.
짧게 수익을 보고 나온다.
우량주 장기 보유가 목적이 아니다.
시장 레짐보다 개별 종목의 강도를 우선한다.
약세장에서도 오르는 종목이 있다. 그걸 아침에 골라야 한다.
```

이것이 스윙 트레이딩 전략이다. 현재 봇은 스캘핑/단타 파라미터로 설정되어 있어 구조적으로 맞지 않는다.

---

## 현재 문제 진단

### 1. 후보 종목 선정 문제

- `top_10_daily.json` — 시총 상위 대형주 중심으로 선정
- 장기 하락 추세 종목(기아 등)도 통과
- 아침에 "강한 종목"이 아니라 "큰 종목"을 뽑는 구조

### 2. 진입 전략 문제 — DEFENSE_LONG

현재 `signal_engine.py`의 DEFENSE_LONG 조건:

```python
close > sma20
and close >= sma5 * 0.995
and sma20 >= sma60 * 0.98   ← 최대 60일선까지만 봄
and -0.08 <= return5 <= 0.06
and -0.05 <= return20 <= 0.35
and 42 <= rsi <= 64          ← RSI 42도 통과 (하락 중)
and atr_pct <= 0.08
and volume_ratio >= 0.65
```

- sma224(약 1년 이평선)을 전혀 보지 않음
- 장기 하락 추세 종목이 단기 반등만 해도 통과
- WEAK/CRASH 레짐에서만 발동 → 약세장에 롱 진입하는 구조

백테스트 결과 (KOSPI 상위 50, 2년):
- 승률 33.3%, 평균 손실 -0.16%, 손절 비율 64%
- 구조적 손실 전략임이 확인됨

### 3. 청산 파라미터 문제

```python
stop_loss_pct:      -0.020   # -2%  ← KOSPI ATR 6~8% 대비 너무 타이트
take_profit_pct:    +0.050   # +5%
trailing_start_pct: +0.025   # +2.5%
trailing_gap_pct:   +0.010   # 1%
time_stop_minutes:  45       # 45분 → 스윙에 무의미
```

- -2% 손절은 KOSPI 일중 변동(ATR 6~8%)에서 하루 만에 터짐
- 손절 64%는 파라미터 문제임이 명확
- 45분 time stop은 스윙이 아닌 스캘핑 세팅

---

## 요구 변경 사항

### 변경 1: 청산 파라미터 (서버 `.env`)

```bash
STOP_LOSS_PCT=-0.05          # -2% → -5%
TAKE_PROFIT_PCT=0.10         # +5% → +10%
TRAILING_START_PCT=0.05      # +2.5% → +5%
TRAILING_GAP_PCT=0.03        # 1% → 3%
# TIME_STOP_MINUTES는 entry_time=None이므로 실질적으로 비활성
```

### 변경 2: 진입 전략 — signal_engine.py

#### 2-1. DEFENSE_LONG 비활성화

WEAK 레짐에서 롱을 잡는 구조 자체가 스윙 철학과 맞지 않음.
DEFENSE_LONG 전략 자체를 제거하거나, sma224 조건으로 사실상 차단.

```python
# _strategy_for() 에서 DEFENSE_LONG 반환 조건 삭제 또는
# 아래 조건 추가로 사실상 비활성화:
and close > sma224           # 장기 하락 종목 차단
and sma60 > sma224           # 60일선도 장기선 위
```

#### 2-2. PULLBACK 전략 강화 (핵심 전략)

사용자 전략과 가장 일치하는 전략. 조건 강화:

```python
# 기존
if sma20 > 0 and sma60 > 0 and close > sma20 > sma60 and return5 < -0.01:
    return StrategyType.PULLBACK

# 변경
sma224 = float(row.get("sma224", 0) or 0)
if (
    sma224 > 0                     # sma224 계산 가능할 때만
    and close > sma224             # 장기 우상향 종목만
    and sma60 > sma224             # 중기 추세도 장기선 위
    and close > sma20 > sma60      # 단기 추세 정렬
    and -0.05 <= return5 <= -0.005 # 의미 있는 눌림 (0.5~5%)
):
    return StrategyType.PULLBACK
```

#### 2-3. sma224 컬럼 확인

`kis_client_kospi.py`의 `_compute_indicators`는 `sma_224`(언더스코어)로 반환.
`signal_engine.py`는 `sma224`(언더스코어 없음)로 읽음.

`live_runner.py`의 DataFrame 컬럼 매핑 확인 필요:
- `sma_224` → `sma224` 변환이 있는지 확인
- 없으면 `signal_engine.py`에서 `row.get("sma_224", row.get("sma224", 0))` 로 양쪽 처리

### 변경 3: 후보 종목 선정 (중기 과제)

현재는 `top_10_daily.json`을 레거시 `KospiTopTenSystem.morning_screening()`이 생성.
이 스캐너가 무엇을 기준으로 뽑는지 별도 검토 필요.

**방향:**
- 시총 상위가 아닌 "강한 상승 추세 중인 종목" 기준
- 필터: `close > sma60 > sma224`, RSI 50~65, return20 > 0, volume_ratio >= 1.0
- 이것은 별도 핸드오프로 관리 권장

---

## 구현 우선순위

| 순서 | 항목 | 효과 | 방법 |
|---|---|---|---|
| 1 | 청산 파라미터 변경 | 즉각 손절 빈도 감소 | 서버 `.env` 수정 |
| 2 | sma224 컬럼 매핑 확인 | 필터 전제조건 | live_runner.py 확인 |
| 3 | PULLBACK 조건 강화 | 장기 하락 종목 차단 | signal_engine.py |
| 4 | DEFENSE_LONG 비활성화 | 약세장 롱 차단 | signal_engine.py |
| 5 | 후보 스캔 로직 교체 | 근본적 종목 품질 개선 | 별도 작업 |

---

## 검증 계획

### 백테스트 (구현 전)
- 스크립트: `backtest_defense_long.py` (기존)
- 파라미터: `STOP_LOSS=-0.05`, `TAKE_PROFIT=0.10`, `MAX_HOLD=5`
- 필터: `close > sma224 and sma60 > sma224 and -0.05 <= return5 <= -0.005`
- 비교: 현재 전략 vs 신규 전략

### 배포 검증
```bash
python -m compileall kospi_bot_v2 -q
python -m pytest tests/ -q
git add ...
git commit -m "..."
git push origin main
ssh ... 'git pull origin main'
python3 -m compileall kospi_bot_v2 -q
sudo systemctl restart kr-bot.service
journalctl -u kr-bot.service -n 30
tail -n 50 .../logs/live.log
```

---

## 금지 사항

- `.env` API 키, 계좌번호, 토큰 수정/커밋 금지
- `data/*.json`, `logs/*` 커밋 금지
- 미국봇(`us_bot_v2`), IRP봇 건드리지 않음
- 지금까지의 엔트리/손절 로직 외 다른 부분 수정 금지

---

## 레포 / 서비스 정보

- 로컬: `/Users/hoisung/Downloads/kospi_trading_system`
- AWS: `/home/ubuntu/kospi_trading_system`
- 서비스: `kr-bot.service`
- SSH 키: `/Users/hoisung/.ssh/crypto_trader_upbit-key.pem`
- GitHub: `https://github.com/hoisungko-dotcom/kospi_trading.git`

---

## 현재 봇 상태 (2026-06-04 작성 시점)

- 포지션: 없음 (000270 손절 후 현금 ₩2,003,017)
- 레짐: WEAK (KOSPI -1.8%)
- 오늘 신호: 없음 (전 종목 volume too weak)
- 내일 장 열리기 전까지 구현 및 배포 목표

---

## 최종 보고 필요 항목

- PULLBACK 조건 변경 전/후 백테스트 비교 결과
- sma224 컬럼 매핑 확인 결과
- 변경된 파일 목록
- 테스트 결과
- 커밋 해시
- 서비스 재시작 확인
