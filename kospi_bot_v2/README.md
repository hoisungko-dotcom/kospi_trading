# KR Bot

한국주식 자동매매 봇입니다. 운영 중에는 **KR live bot**으로 부르고,
버전 표기는 전략 기록에서만 다룹니다.

> 참고: 디렉터리명은 마이그레이션 비용을 줄이기 위해 아직 `kospi_bot_v2`를 유지합니다.

## 설계 원칙

- 실전 모드는 `--live`, 섀도우 모드는 기본 실행으로 분리합니다.
- 시장 레짐 우선: `BULL`, `NEUTRAL`, `WEAK`, `CRASH`를 먼저 판단합니다.
- 전략 분리: 돌파, 눌림목, 약세장 상대강도, 인버스 ETF 신호를 분리합니다.
- 리스크 예산: 장이 좋을 때는 열고, 나쁠 때는 포지션 수와 크기를 줄입니다.
- 비교 가능성: 모든 가상 매매를 JSONL ledger와 Markdown report로 남깁니다.

## 실행

샘플 데이터로 뼈대 실행:

```bash
python -m kospi_bot_v2.main --sample
```

KIS 시세만 사용해서 1회 실행:

```bash
python -m kospi_bot_v2.main --kis
```

KIS 시세만 사용해서 반복 실행:

```bash
python -m kospi_bot_v2.main --kis --loop
```

백그라운드 실행:

```bash
bash kospi_bot_v2/start_shadow_loop.sh
bash kospi_bot_v2/status_shadow_loop.sh
bash kospi_bot_v2/stop_shadow_loop.sh
```

텔레그램이 `.env`에 설정되어 있으면 요약 알림:

```bash
python -m kospi_bot_v2.main --kis --loop --notify
```

직접 만든 OHLCV CSV로 실행:

```bash
python -m kospi_bot_v2.main --csv /path/to/prices.csv
```

CSV 필수 컬럼:

```text
symbol,name,timestamp,open,high,low,close,volume
```

## 산출물

- `kospi_bot_v2/data/shadow_ledger.jsonl`
- `kospi_bot_v2/reports/shadow_report_YYYYMMDD_HHMMSS.md`

리포트 하단에는 기존 봇 로그와 섀도우 신호/가상매매 비교 섹션이 붙습니다.

## 환경 변수

모두 선택값입니다.

```text
V2_INITIAL_CASH=10000000
V2_UNIVERSE_SYMBOLS=005930,000660,005380,252670
V2_LOOP_INTERVAL_SEC=60
V2_ACTIVE_START_HHMM=0830
V2_ACTIVE_END_HHMM=1530
V2_ACTIVE_TIMEZONE=Asia/Seoul
V2_INCLUDE_ACCOUNT_SNAPSHOT=true
V2_COMPARE_LOG_PATH=/path/to/legacy/log
V2_MAX_POSITION_PCT=0.35
V2_DAILY_LOSS_LIMIT_PCT=-0.025
```

잔고 조회를 끄고 싶을 때:

```bash
python -m kospi_bot_v2.main --kis --no-account
```

장외에도 강제로 실행하고 싶을 때:

```bash
python -m kospi_bot_v2.main --kis --loop --ignore-hours
```

## 다음 단계

1. 실제 장중 10거래일 섀도우 운용
2. 기존 봇 vs v2 리포트의 매매 결과 정밀 파싱
3. 약세장 인버스 ETF 엔진 정교화
4. 전략별 파라미터 튜닝
5. 실전 전환 여부 판단
