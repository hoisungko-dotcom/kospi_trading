# Claude To Codex Handoff — 한국봇 WebSocket 재연결 폭주 버그 수정

## Status

- Date: 2026-07-03 (금)
- Owner after handoff: Codex
- Urgency: high (실거래 라이브 봇, 실시간 데이터 신뢰성 이슈)
- 작업 방식: **직접 작업** (bounded worker 아님) — 서버 SSH 접속, 라이브 파일 수정, 서비스 재시작, 로컬 git 커밋/push까지 모두 완료된 상태로 핸드오프함

---

## 요약

사용자 요청으로 한국봇 실서버(`100.27.228.229:/home/ubuntu/kospi_box_bot`, `kospi_box_bot.service`)를 점검하던 중, 실시간 시세 WebSocket이 하루종일 재연결을 반복하는 심각한 버그를 발견하고 수정함.

### 원인

`realtime/kis_realtime.py`에서 `_on_error`와 `_write_tick_audit` 두 메서드 사이 코드가 잘못 섞여 있었음:

```python
def _on_error(self, ws, err) -> None:
    self._last_error = str(err)          # 원래 있어야 할 상태갱신/로그 코드가 사라짐

def _write_tick_audit(self, tick, state) -> None:
    ...
    except Exception as exc:
        logger.debug(...)
    self._status = "reconnecting"        # _on_error에 있어야 할 코드가 여기 잘못 붙음
    logger.warning("KIS WebSocket 에러: %s", err)   # err는 이 함수 스코프에 없는 변수
```

`_write_tick_audit`은 실시간 체결(tick)이 들어올 때마다 호출되는데, 마지막 두 줄에서 정의되지 않은 `err`를 참조해 매번 `NameError` 발생 → on_message 콜백 안에서 예외가 터지며 `websocket-client` 라이브러리가 연결이 끊긴 것으로 처리 → 재연결 → 다음 tick에서 또 죽음, 이 반복.

### 증거

- 당일(2026-07-03) `logs/runner.nohup.log`에서 "Connection to remote host was lost" **9,918회**
- 같은날 AI 리뷰 로그(`journal/20260703_ai_review.md`)에 "stale 발생: 848" 기록 — 실시간 데이터가 사실상 거의 항상 끊긴 상태로 브레이크아웃 감지(BOX_RT_*)가 정상 작동하지 못했을 가능성이 높음

---

## 수정 내용

두 줄을 원래 자리인 `_on_error`로 되돌림 (로직 변경 없음, 순수 버그 수정):

```python
def _on_error(self, ws, err) -> None:
    self._last_error = str(err)
    self._status = "reconnecting"
    logger.warning("KIS WebSocket 에러: %s", err)

def _write_tick_audit(self, tick, state) -> None:
    ...
    except Exception as exc:
        logger.debug("KIS tick audit write failed: %s", exc)
```

---

## 실행한 작업 (서버, 직접)

1. `/home/ubuntu/kospi_box_bot/realtime/kis_realtime.py` → 백업 `kis_realtime.py.bak_20260703_ws_fix` 생성
2. 위 수정 적용, `py_compile`로 문법 검증
3. `diff`로 정확히 2줄만 이동했는지 확인
4. 22:51 KST (장마감 후, 안전한 시점 확인) `sudo systemctl restart kospi_box_bot.service`
5. `journalctl -u kospi_box_bot.service --since <재시작시각>` 에서 에러 없음 확인

## 실행한 작업 (로컬 git, 직접)

1. 서버의 수정된 `kis_realtime.py`를 `scp`로 받아 `.server_work/realtime/kis_realtime.py`에 반영
2. 이 파일 1개만 스테이징 (다른 untracked 파일은 건드리지 않음 — 사용자가 "이번 fix만 최소 커밋"으로 범위 지정)
3. 커밋 `5c5b2b8` 생성, `origin/main`에 push 완료

```
5c5b2b8 fix(kr-box-bot): restore misplaced on_error status/log lines in kis_realtime.py
```

---

## 검증 안 된 부분 (중요)

- 수정 적용 시점(22:51 KST)이 **장마감 후**라서, 실제 실시간 체결(tick)이 들어오는 조건에서 재연결 폭주가 사라졌는지는 **아직 확인 못함**
- 다음 정규장은 **2026-07-06(월) 09:00 KST** 예상 (공휴일 여부 별도 확인 필요)
- **다음 정규장 이후 반드시 확인**: `logs/runner.nohup.log`에서 "Connection to remote host was lost" 빈도가 급감했는지, `BOX_RT` 관련 stale/near_breakout 지표가 정상화됐는지

---

## Codex에게 요청하는 다음 단계

1. 2026-07-06(월) 장 마감 후 서버 로그로 재연결 폭주가 실제로 해소됐는지 확인
2. `.server_snap/`, `.server_work/`의 나머지 파일들(box_checker.py, daily_runner.py, paper_engine.py 등)은 여전히 git에 커밋 안 된 상태 — 서버 코드 전체를 버전관리로 가져올지 여부는 아직 미결정 (사용자에게 다시 물어볼 것, 이번엔 최소 범위만 반영하기로 함)
3. `.server_work/.env.ai_overrides*` 파일은 시크릿은 아니지만(API 키 없음, 전략 튜닝값만 있음) 현재 `.gitignore`에 안 걸려 있음 — 커밋 대상으로 볼지, 계속 로컬 전용으로 둘지 정책 결정 필요
4. `/home/ubuntu/kospi_box_bot` 서버 자체는 git 저장소가 아님(`.git` 없음) — 서버에 직접 git을 붙일지, 지금처럼 로컬 미러(`.server_snap`/`.server_work`) → SSH 배포 방식을 유지할지는 미결정

## 이번 작업에서 안 건드린 것

- `.env`, `.env.ai_overrides*`의 실제 시크릿 (모두 `.gitignore`로 보호됨, 확인 완료)
- `data/`, `journal/`, 계좌 상태 파일 — 서버 원본 그대로
- `kis_realtime.py` 외 다른 서버 코드 파일 — 이번 커밋에 포함 안 함
