#!/bin/bash
# 한국주식 KOSPI/KOSDAQ 관리 메뉴

PROJECT="/Users/hoisung/Downloads/kospi_trading_system"
LOG="$PROJECT/logs/kospi_trading.log"
STDERR_LOG="$PROJECT/logs/stderr.log"
SCREENING_LOG="$PROJECT/logs/screening.log"
PYTHON="/Users/hoisung/.pyenv/shims/python3"
VENV_PYTHON="$PROJECT/venv/bin/python"   # 백테스트용 (numpy/pandas/FDR 설치됨)
ENV_FILE="$PROJECT/.env"

cd "$PROJECT" || exit 1

LOCK_FILE="$PROJECT/data/kospi_bot.lock"

# 살아있는 봇 PID 목록 (락 파일 PID + pgrep 교차 검증)
bot_pid() {
    {
        # pgrep: 커맨드라인에 main.py 포함된 python/caffeinate 프로세스
        pgrep -f "kospi_trading_system.*main\.py" 2>/dev/null
        # 락 파일 PID (프로세스명이 바뀌었을 경우 대비)
        local lp
        lp=$(cat "$LOCK_FILE" 2>/dev/null | tr -d '[:space:]')
        [ -n "$lp" ] && kill -0 "$lp" 2>/dev/null && echo "$lp"
    } | sort -u | while IFS= read -r pid; do
        # 실제 살아있는 PID만 출력
        kill -0 "$pid" 2>/dev/null && echo "$pid"
    done
}

# 봇 관련 프로세스 완전 종료 (최대 10초 대기 후 SIGKILL)
kill_bot_all() {
    local pids
    pids=$(bot_pid | tr '\n' ' ')

    # 락 파일 PID 추가 (bot_pid에 없을 수도 있음)
    local lp
    lp=$(cat "$LOCK_FILE" 2>/dev/null | tr -d '[:space:]')
    [ -n "$lp" ] && kill -0 "$lp" 2>/dev/null && pids="$pids $lp"

    # caffeinate가 감싸고 있는 자식 프로세스도 포함
    for pid in $pids; do
        local children
        children=$(pgrep -P "$pid" 2>/dev/null | tr '\n' ' ')
        [ -n "$children" ] && pids="$pids $children"
    done

    pids=$(echo "$pids" | tr ' ' '\n' | grep -v '^$' | sort -u | tr '\n' ' ')
    [ -z "$(echo "$pids" | tr -d '[:space:]')" ] && return 0

    kill $pids 2>/dev/null

    # 최대 8초 폴링하며 대기
    local i=0
    while [ $i -lt 8 ]; do
        sleep 1; i=$((i+1))
        [ -z "$(bot_pid)" ] && return 0
    done

    # 아직 살아있으면 강제 종료
    local rem
    rem=$(bot_pid | tr '\n' ' ')
    [ -n "$(echo "$rem" | tr -d '[:space:]')" ] && kill -9 $rem 2>/dev/null && sleep 1
    return 0
}

bot_status() {
    local pids
    pids=$(bot_pid | tr '\n' ' ')
    if [ -n "$pids" ]; then
        local uptime_info=""
        local first_pid
        first_pid=$(bot_pid | head -1)
        if [ -n "$first_pid" ]; then
            uptime_info=$(ps -p "$first_pid" -o etime= 2>/dev/null | tr -d ' ')
        fi
        echo "  ✅ 봇 실행 중 (PID: $pids | 실행시간: ${uptime_info:-?})"
    else
        echo "  ⭕ 봇 정지 중"
    fi
}

safety_status() {
    local mock live maxpos
    mock=$(grep -E '^MOCK_TRADING=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    live=$(grep -E '^LIVE_TRADING_CONFIRMED=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    maxpos=$(grep -E '^MAX_POSITION_PCT=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')

    [ -z "$mock" ] && mock="true"
    [ -z "$live" ] && live="false"
    [ -z "$maxpos" ] && maxpos="0.15"

    local mode_label
    if [ "$mock" = "true" ]; then
        mode_label="🟡 모의투자"
    else
        mode_label="🔴 실전투자"
    fi
    echo "  $mode_label | MAX_POSITION_PCT=$maxpos | LIVE_CONFIRMED=$live"
}

token_status() {
    local issued
    issued=$(ls -t "$PROJECT/data/.token_"*.json 2>/dev/null | head -1)
    if [ -n "$issued" ]; then
        local ts
        ts=$(python3 -c "
import json, sys
from pathlib import Path
f = Path('$issued')
if f.exists():
    d = json.loads(f.read_text())
    print(d.get('issued_at','?')[:19])
" 2>/dev/null)
        echo "  🔑 토큰 발급: $ts"
    else
        echo "  🔑 토큰: 캐시 없음"
    fi
}

start_bot() {
    echo ""

    # ── 1. 살아있는 프로세스 확인 ───────────────────────────────────────
    local running_pids
    running_pids=$(bot_pid | tr '\n' ' ')
    if [ -n "$(echo "$running_pids" | tr -d '[:space:]')" ]; then
        echo "  ⚠️  이미 실행 중 (PID: $running_pids)"
        echo "  먼저 봇을 중지하세요 (2번)"
        sleep 2
        return
    fi

    # ── 2. 락 파일에 살아있는 PID가 남아있으면 차단 ────────────────────
    local lock_pid
    lock_pid=$(cat "$LOCK_FILE" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$lock_pid" ]; then
        if kill -0 "$lock_pid" 2>/dev/null; then
            echo "  ⚠️  락 파일에 살아있는 PID $lock_pid 감지"
            echo "  봇을 중지 후 다시 시작하세요 (2번 → 1번)"
            sleep 2
            return
        else
            # 죽은 프로세스 잔존 락 파일 → 정리 후 진행
            rm -f "$LOCK_FILE"
            echo "  🧹 잔존 락 파일 정리 (PID $lock_pid 이미 종료됨)"
        fi
    fi

    # ── 3. 봇 시작 ──────────────────────────────────────────────────────
    echo "  🚀 한국주식 봇 시작..."
    find "$PROJECT/data" -maxdepth 1 -name '.token_*.json' -delete 2>/dev/null
    # 락 파일은 Python이 직접 생성·관리 — 여기서 삭제하지 않음

    caffeinate -i "$PYTHON" "$PROJECT/main.py" >> "$STDERR_LOG" 2>&1 &

    # 최대 7초 폴링하며 프로세스 확인
    local i=0
    while [ $i -lt 7 ]; do
        sleep 1; i=$((i+1))
        if [ -n "$(bot_pid)" ]; then
            echo "  ✅ 봇 시작 완료 (PID: $(bot_pid | tr '\n' ' ')) — 로그: logs/kospi_trading.log"
            sleep 1
            return
        fi
    done

    echo "  ❌ 시작 실패 — logs/stderr.log 확인"
    echo ""
    echo "  최근 에러:"
    tail -20 "$STDERR_LOG" 2>/dev/null | grep -E "Error|Traceback|Exception|이중|중복" | tail -5
    sleep 3
}

stop_bot() {
    echo ""
    local pids
    pids=$(bot_pid | tr '\n' ' ')
    local lock_pid
    lock_pid=$(cat "$LOCK_FILE" 2>/dev/null | tr -d '[:space:]')

    local has_proc=false
    [ -n "$(echo "$pids" | tr -d '[:space:]')" ] && has_proc=true
    [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null && has_proc=true

    if ! $has_proc; then
        echo "  ⚠️  실행 중인 봇 없음"
    else
        echo "  🛑 봇 종료 중 (PID: $pids)..."
        kill_bot_all
        if [ -z "$(bot_pid)" ]; then
            echo "  ✅ 봇 종료 완료"
        else
            echo "  ⚠️  일부 프로세스가 남아있습니다: $(bot_pid | tr '\n' ' ')"
        fi
    fi

    # 락 파일 정리 (Python이 정상 종료 시 이미 삭제하지만 보험용)
    sleep 1
    rm -f "$LOCK_FILE" 2>/dev/null
    sleep 1
}

force_refresh_token() {
    echo ""
    echo "  🔄 KIS 토큰 강제 재발급 중..."
    find "$PROJECT/data" -maxdepth 1 -name '.token_*.json' -delete 2>/dev/null
    echo "  ✅ 토큰 캐시 삭제 완료"

    if [ -n "$(bot_pid)" ]; then
        echo "  ℹ️  봇 실행 중 — 다음 주문 시 자동으로 신규 토큰 발급됩니다"
        echo "  ℹ️  즉시 적용하려면 봇을 재시작하세요 (2번 → 1번)"
    else
        echo "  ℹ️  봇 시작 시 새 토큰이 자동 발급됩니다"
    fi
    sleep 3
}

show_portfolio() {
    echo ""
    "$PYTHON" "$PROJECT/check_status.py"
    echo ""
    read -p "  Enter로 돌아가기..."
}

run_backtest() {
    echo ""
    echo "  📊 백테스트 실행 (Strategy 2 — KOSPI/KOSDAQ 상위 150개, 500일)..."
    "$VENV_PYTHON" "$PROJECT/backtest.py"
    echo ""
    read -p "  Enter로 돌아가기..."
}

toggle_mock() {
    echo ""
    local current
    current=$(grep -E '^MOCK_TRADING=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    [ -z "$current" ] && current="true"

    if [ "$current" = "true" ]; then
        echo "  현재: 모의투자 → 실전투자로 전환하시겠습니까?"
        echo "  ⚠️  실전투자는 실제 자금이 사용됩니다!"
        echo ""
        read -p "  확인 (yes 입력): " confirm
        if [ "$confirm" = "yes" ]; then
            # MOCK_TRADING 줄 교체
            if grep -q '^MOCK_TRADING=' "$ENV_FILE"; then
                sed -i '' 's/^MOCK_TRADING=.*/MOCK_TRADING=false/' "$ENV_FILE"
            else
                echo "MOCK_TRADING=false" >> "$ENV_FILE"
            fi
            # LIVE_TRADING_CONFIRMED 줄 교체
            if grep -q '^LIVE_TRADING_CONFIRMED=' "$ENV_FILE"; then
                sed -i '' 's/^LIVE_TRADING_CONFIRMED=.*/LIVE_TRADING_CONFIRMED=true/' "$ENV_FILE"
            else
                echo "LIVE_TRADING_CONFIRMED=true" >> "$ENV_FILE"
            fi
            echo "  ✅ 실전투자로 전환 완료 (봇 재시작 필요)"
        else
            echo "  취소됨"
        fi
    else
        echo "  현재: 실전투자 → 모의투자로 전환합니다"
        if grep -q '^MOCK_TRADING=' "$ENV_FILE"; then
            sed -i '' 's/^MOCK_TRADING=.*/MOCK_TRADING=true/' "$ENV_FILE"
        else
            echo "MOCK_TRADING=true" >> "$ENV_FILE"
        fi
        if grep -q '^LIVE_TRADING_CONFIRMED=' "$ENV_FILE"; then
            sed -i '' 's/^LIVE_TRADING_CONFIRMED=.*/LIVE_TRADING_CONFIRMED=false/' "$ENV_FILE"
        else
            echo "LIVE_TRADING_CONFIRMED=false" >> "$ENV_FILE"
        fi
        echo "  ✅ 모의투자로 전환 완료 (봇 재시작 필요)"
    fi
    sleep 2
}

restart_bot() {
    echo ""
    echo "  🔄 봇 재시작 중..."
    stop_bot
    sleep 1
    start_bot
}

show_errors_only() {
    echo ""
    echo "  📋 최근 에러/경고 (kospi_trading.log 기준)"
    echo "  (Ctrl+C 로 종료)"
    sleep 1
    echo ""
    echo "  --- 최근 에러/경고 40줄 ---"
    tail -n 300 "$LOG" 2>/dev/null \
        | grep -E "ERROR|WARNING|❌|⚠️|Traceback|Exception" \
        | tail -40
    echo ""
    echo "  --- 실시간 에러/경고 추적 시작 ---"
    tail -n 0 -f "$LOG" 2>/dev/null \
        | grep --line-buffered -E "ERROR|WARNING|❌|⚠️|Traceback|Exception" \
        || echo "  ⚠️  로그 없음 — 봇을 먼저 시작해주세요"
}

market_status() {
    local last_line
    last_line=$(grep "시장 상태:" "$LOG" 2>/dev/null | tail -1 | sed 's/.*시장 상태: //')
    if [ -n "$last_line" ]; then
        echo "  📊 $last_line"
    fi
}

show_sector() {
    echo ""
    echo "  📊 최근 섹터 모멘텀 (logs 기준)"
    grep "섹터 모멘텀 갱신" "$LOG" 2>/dev/null | tail -5 | while IFS= read -r line; do
        echo "    $line" | sed 's/.*INFO - //'
    done
    echo ""
    read -p "  Enter로 돌아가기..."
}

while true; do
    clear
    echo "============================================"
    echo "   🇰🇷 한국주식 KOSPI/KOSDAQ 관리 메뉴"
    echo "   v3.6 | 섹터매핑KIS · 동적SCALP · 14:30예외진입 · 폭락반등자동매수"
    echo "============================================"
    echo ""
    bot_status
    safety_status
    token_status
    market_status
    echo ""
    echo "  1) 봇 시작"
    echo "  2) 봇 중지"
    echo "  3) 봇 재시작"
    echo "  4) 트레이딩 로그 (실시간)"
    echo "  5) 에러/경고 로그 (실시간 필터)"
    echo "  6) 스크리닝 로그 (실시간)"
    echo "  7) 포트폴리오 / 잔고 상태"
    echo "  8) 백테스트 실행"
    echo "  9) 에러 로그 전체 보기"
    echo "  s) 섹터 모멘텀 보기"
    echo "  t) 토큰 강제 재발급"
    echo "  m) 모의/실전 전환"
    echo "  0) 종료"
    echo ""
    echo -n "  선택: "
    read choice

    case "$choice" in
        1) start_bot ;;
        2) stop_bot ;;
        3) restart_bot ;;
        4)
            echo "  (Ctrl+C 로 로그 보기 종료)"
            sleep 1
            echo ""
            echo "  --- 최근 트레이딩 로그 80줄 ---"
            tail -n 80 "$LOG" 2>/dev/null || echo "  ⚠️  로그 없음 — 봇을 먼저 시작해주세요"
            echo ""
            echo "  --- 실시간 트레이딩 로그 추적 시작 ---"
            tail -n 0 -f "$LOG" 2>/dev/null || echo "  ⚠️  로그 없음 — 봇을 먼저 시작해주세요"
            ;;
        5) show_errors_only ;;
        6)
            echo "  (Ctrl+C 로 로그 보기 종료)"
            sleep 1
            echo ""
            echo "  --- 최근 스크리닝 로그 80줄 ---"
            tail -n 80 "$SCREENING_LOG" 2>/dev/null || echo "  ⚠️  스크리닝 로그 없음"
            echo ""
            echo "  --- 실시간 스크리닝 로그 추적 시작 ---"
            tail -n 0 -f "$SCREENING_LOG" 2>/dev/null || echo "  ⚠️  스크리닝 로그 없음"
            ;;
        7) show_portfolio ;;
        8) run_backtest ;;
        9)
            echo "  (Ctrl+C 로 로그 보기 종료)"
            sleep 1
            echo "=== 최근 에러 로그 (stderr.log) ==="
            tail -50 "$STDERR_LOG" 2>/dev/null || echo "  ⚠️  stderr 로그 없음"
            echo ""
            read -p "  Enter로 돌아가기..."
            ;;
        s|S) show_sector ;;
        t|T) force_refresh_token ;;
        m|M) toggle_mock ;;
        0) echo "  종료합니다."; exit 0 ;;
        *) echo "  ⚠️  잘못된 입력"; sleep 1 ;;
    esac
done
