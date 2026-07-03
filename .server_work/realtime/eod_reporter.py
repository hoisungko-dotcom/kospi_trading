"""
EOD 텔레그램 리포트 — 당일 전략 상태 기준 거래 결과 + 시간대별 분석.

자동: daily_runner.py 에서 15:30 호출
수동: python -m realtime.eod_reporter [--date YYYYMMDD]
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[1] / ".env")
load_dotenv(Path(__file__).parents[1] / ".env.ai_overrides", override=True)

from realtime.strategy_state_engine import StrategyStateEngine, INITIAL_CASH, STATE_PATH

KST = ZoneInfo("Asia/Seoul")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIME_WINDOWS = [
    ((9, 0),  (9, 30),  "09:00-09:30"),
    ((9, 30), (10, 30), "09:30-10:30"),
    ((10, 30),(13, 0),  "10:30-13:00"),
    ((13, 0), (15, 15), "13:00-15:15"),
]


def _send(msg: str) -> None:
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"텔레그램 전송 실패: {e}")
        print(msg)


def _fmt_krw(v: int) -> str:
    if v >= 0:
        return f"+{v:,}원"
    return f"{v:,}원"


def _window_label(entry_ts: str) -> str:
    if len(entry_ts) < 12:
        return "??"
    h = int(entry_ts[8:10])
    m = int(entry_ts[10:12])
    hm = (h, m)
    for start, end, label in TIME_WINDOWS:
        if start <= hm < end:
            return label
    return f"{h:02d}:{m:02d}"


def _runtime_mode(mode: str | None = None) -> str:
    if mode:
        return mode.strip().lower()
    return (os.getenv("KIS_TRADING_MODE", "mock").strip().lower() or "mock")


def send_eod_report(engine: StrategyStateEngine, date_str: str | None = None, *, mode: str | None = None) -> None:
    if date_str is None:
        date_str = datetime.now(KST).strftime("%Y%m%d")
    runtime_mode = _runtime_mode(mode)
    live_mode = runtime_mode == "live"

    today = [t for t in engine.trades if t.entry_ts[:8] == date_str]

    # ── 날짜 헤더 ─────────────────────────────────────────────────────────────
    ymd = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    if live_mode:
        lines = [f"📊 *박스봇 LIVE 전략 상태 리포트 — {ymd}*"]
        lines.append(f"_실계좌 실현손익이 아니라 전략 상태 파일(`{STATE_PATH.name}`) 기준 결과입니다._")
    else:
        lines = [f"📊 *박스봇 PAPER 일일 결과 — {ymd}*"]

    if not today:
        lines.append("\n거래 없음")
        nav = int(engine.cash)
        profit_pct = (nav - INITIAL_CASH) / INITIAL_CASH * 100
        lines.append(f"\n잔고 {nav:,}원  (누적 {profit_pct:+.2f}%)")
        _send("\n".join(lines))
        return

    # ── 요약 ─────────────────────────────────────────────────────────────────
    wins      = [t for t in today if t.pnl_krw > 0]
    losses    = [t for t in today if t.pnl_krw <= 0]
    total_pnl = sum(t.pnl_krw for t in today)
    win_rate  = len(wins) / len(today) * 100
    avg_win   = sum(t.pnl_krw for t in wins)   / len(wins)   if wins   else 0
    avg_loss  = sum(t.pnl_krw for t in losses) / len(losses) if losses else 0

    nav = int(engine.cash + sum(
        p.entry_price * p.qty for p in engine.positions.values()
    ))
    cumulative_pct = (nav - INITIAL_CASH) / INITIAL_CASH * 100

    lines += [
        "",
        f"거래: {len(today)}건  승률: {win_rate:.0f}%  ({len(wins)}승/{len(losses)}패)",
        f"총손익: {_fmt_krw(total_pnl)}",
        f"평균 이익: {_fmt_krw(int(avg_win))}  /  평균 손실: {_fmt_krw(int(avg_loss))}",
        f"잔고: {nav:,}원  (누적 {cumulative_pct:+.2f}%)",
    ]

    # ── 시간대별 결과 ─────────────────────────────────────────────────────────
    window_data: dict[str, list] = {}
    for t in today:
        label = _window_label(t.entry_ts)
        window_data.setdefault(label, []).append(t)

    lines.append("\n*[시간대별]*")
    for label, ts in sorted(window_data.items()):
        wpnl = sum(t.pnl_krw for t in ts)
        wwin = len([t for t in ts if t.pnl_krw > 0])
        lines.append(
            f"  {label}: {len(ts)}건 {wwin}승 {_fmt_krw(wpnl)}"
        )

    # ── 청산 사유별 ───────────────────────────────────────────────────────────
    reason_data: dict[str, list] = {}
    for t in today:
        reason_data.setdefault(t.exit_reason, []).append(t)

    lines.append("\n*[청산 사유]*")
    for reason, ts in sorted(reason_data.items()):
        rpnl = sum(t.pnl_krw for t in ts)
        lines.append(f"  {reason}: {len(ts)}건 {_fmt_krw(rpnl)}")

    # ── 개별 거래 목록 ────────────────────────────────────────────────────────
    lines.append("\n*[거래 내역]*")
    for t in sorted(today, key=lambda x: x.entry_ts):
        emoji = "✅" if t.pnl_krw >= 0 else "❌"
        time_label = t.entry_ts[8:12] if len(t.entry_ts) >= 12 else "??"
        lines.append(
            f"  {emoji} {t.name} [{time_label}] "
            f"{t.pnl_pct:+.2f}% {_fmt_krw(t.pnl_krw)}"
        )

    msg = "\n".join(lines)

    # 메시지가 4096자 초과하면 분할
    if len(msg) <= 4096:
        _send(msg)
    else:
        # 헤더 + 요약 / 군집+시간대 / 거래내역 분리
        split_idx = msg.find("\n*[거래 내역]*")
        if split_idx > 0:
            _send(msg[:split_idx])
            _send("*[거래 내역]*\n" + msg[split_idx + len("\n*[거래 내역]*"):])
        else:
            _send(msg[:4096])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYYMMDD (기본: 오늘)")
    args = ap.parse_args()

    engine = StrategyStateEngine()
    date_str = args.date or datetime.now(KST).strftime("%Y%m%d")
    send_eod_report(engine, date_str)


if __name__ == "__main__":
    main()
