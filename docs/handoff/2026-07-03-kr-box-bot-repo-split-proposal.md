# 한국 박스봇 전용 저장소 분리안

## Status

- Date: 2026-07-03 (금)
- Owner after handoff: Codex / 사용자 결정 대기
- Urgency: medium (서버 git init의 선행 작업, 즉시 서버에 영향 없음)
- 작업 방식: 로컬 파일 정리만 수행. **서버는 전혀 건드리지 않음**

---

## 배경

서버 `/home/ubuntu/kospi_box_bot`(`kospi_box_bot.service`, 실전투자)를 git으로 관리하려 했으나,
현재 로컬 `kospi_trading_system` 저장소는 아래 두 가지가 한 저장소에 섞여 있어 바로 연결할 수 없음:

1. `kospi_bot_v2`, `core/`, `brokers/`, `strategy/` 등 메인 트리 — **다른 봇** (v2 샤도우 리그, 실거래 박스봇과 무관)
2. `.server_work/realtime/...` — 박스봇 서버 코드, 저장소 **루트가 아니라 하위 폴더**에 위치

이 상태로 서버에서 `git init` 후 이 origin에 연결하면 무관한 코드가 섞여 들어오고,
실제 필요한 파일은 실행 경로(`realtime/daily_runner.py`)와 다른 위치에 놓여 서비스가 깨짐.

**결론**: 박스봇 전용 새 저장소로 분리 (사용자 확정, 2026-07-03).

---

## 의존성 추적 결과

`realtime/daily_runner.py`(서비스가 실제로 실행하는 모듈)부터 import를 역추적해서
"진짜 라이브 운영에 필요한 파일"만 골라냄.

### 포함 (새 저장소 루트 구조)

```
kospi_box_bot/
├── __init__.py
├── ai_reviewer.py            # realtime/daily_runner.py:37 "from ai_reviewer import ProfitReviewAgent" — 서버 루트에 있어야 함
├── requirements.txt
├── kospi_box_bot.service
├── .env.ai_overrides         # 전략 튜닝값만 있음, 시크릿 없음 (확인 완료)
├── .gitignore
├── realtime/                 # 18개 .py — 서비스가 직접 실행하는 활성 런타임
├── collector/                # 5개 .py — realtime/*.py 다수가 import
├── review/                   # 빈 디렉터리, box_checker.py 등이 출력 경로로 사용 (.gitkeep으로 보존)
├── tests/                    # 5개 pytest 파일
└── docs/                     # server-system-map.md, history-index.md/json
```

### 제외 (다음 이유로 새 저장소에 안 넣음)

| 파일/디렉터리 | 제외 이유 |
|---|---|
| 루트 `daily_runner.py`, `paper_engine.py`, `kis_mock_broker.py` | `realtime/` 버전과 이름 중복. 어디서도 import 안 됨 (grep 확인) — 구버전 잔재. `server-system-map.md`의 "Active runtime files" 목록에도 `realtime/` 버전만 명시됨 |
| `analysis/` | `test_connection.py`(루트, 수동 스모크 테스트)에서만 참조. 라이브 경로 미사용 |
| `legacy/` | v1 참고 자료, 현재 미사용 |
| `bt_arm_sweep.py`, `bt_new_config.py`, `patch_daily_runner.py`, `build_history_index.py` | 백테스트/패치 유틸리티, 서비스 실행에 불필요 |
| 루트 `test_connection.py`, `test_kis_live.py`, `test_kis_ws.py`, `test_ws_tick.py` | 수동 스모크 스크립트, pytest 스위트(`tests/`)와 별개 |
| `run_kr_ai_review.sh` | **서버에서 이미 죽은 스크립트로 확인됨** — 내용이 `/Users/hoisung/Downloads/turtle_trader_kis/.tmp_kospi_pattern_bot` 등 로컬 Mac 경로를 참조하고 있어 서버에서 실행 불가. 실제 AI 리뷰는 `realtime/daily_runner.py`가 `ai_reviewer.py`를 직접 import해서 인프로세스로 수행함 |
| `systemd/kospi_box_bot.service` | 루트 `kospi_box_bot.service`와 중복 파일, 하나만 유지 |

### 참고: service 파일 설명 불일치 발견

저장소에 있던 `kospi_box_bot.service`의 `Description=KOSPI BOX Bot - 모의투자`는
실제 서버에 설치된 유닛 파일(`systemctl status` 결과 `KOSPI BOX Bot - 실전투자`)과 다름.
서버가 진짜 소스이고, 저장소 사본이 갱신 안 된 상태 — 새 저장소를 서버와 연결할 때 서버 버전으로 덮어써야 함.

### 시크릿/라이브 상태 (항상 제외, `.gitignore`로 처리)

`.env`, `data/`, `logs/`, `journal/`, `venv/`, `__pycache__/`, `*.bak_*`, 토큰 캐시

---

## 현재 상태

- 새 디렉터리 `/Users/hoisung/Downloads/kospi_box_bot/`에 위 "포함" 목록 파일 33개 + `.gitignore` 생성 완료
- **아직 git 저장소 아님** (git init 안 함)
- **GitHub 원격 저장소 없음** (새로 만들어야 함)
- **서버는 전혀 안 건드림** — `/home/ubuntu/kospi_box_bot`은 여전히 `.git` 없는 상태 그대로

---

## 다음 단계 (사용자 확인 후 진행)

1. GitHub에 새 저장소 생성 (예: `hoisungko-dotcom/kospi_box_bot`) — **사용자가 직접 만들거나 `gh repo create` 승인 필요**
2. `/Users/hoisung/Downloads/kospi_box_bot`에서 `git init` → 위 원격 추가 → 최초 커밋 → push
3. 서버 `kospi_box_bot.service`를 실제 설치본 기준으로 저장소에 반영 (설명 불일치 수정)
4. 서버에서 `git init` → 위 원격 `remote add` → `git fetch` → 기존 라이브 파일(`data/`, `logs/`, `.env` 등)과 충돌 없이 병합하는 절차 설계 (이 부분은 실거래 중단 없이 해야 하므로 별도 신중한 계획 필요 — 지금 단계에서 실행 안 함)
5. `kospi_trading_system`의 `.server_work/`, `.server_snap/`은 새 저장소로 대체된 뒤 정리(삭제 또는 archive) 검토
6. `docs/handoff/2026-06-30-repo-cleanup-claude-to-codex.md`의 봇별 경로 표에 한국봇 로컬 경로를 `kospi_box_bot/`로 확정 기입

## 이번에 하지 않은 것 (명시적 보류)

- GitHub 원격 저장소 생성
- 로컬 `git init` / 커밋
- 서버 접속 또는 수정
- `.server_work` / `.server_snap` 삭제
