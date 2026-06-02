"""
KOSPI 6 + KOSDAQ 4 집중 거래 시스템

일일 스케줄:
  08:30 KST  — 코스피 전체 + 코스닥 상위 300 스캔 → 매수후보 코스피 6 + 코스닥 4 선정
  09:00~15:30 — 5분마다 매수후보 신호 확인 + 보유 종목 매도 타이밍 판단
  15:30 KST  — 장 마감
"""
import os
import sys
import math

# Windows 한글 인코딩 오류 방지 (cp1252 → utf-8)
if sys.platform == 'win32':
    os.environ.setdefault('PYTHONUTF8', '1')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import json
import time
import signal
import logging
import threading
import atexit
import sys as _sys
if _sys.platform == 'win32':
    import msvcrt as _lock_mod
else:
    import fcntl as _lock_mod
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd
import pytz
import schedule
import FinanceDataReader as fdr
from dotenv import load_dotenv
try:
    import anthropic as _anthropic_module
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from core.kis_client_kospi import KISClientKospi
from core.kis_balance_checker import KISBalanceChecker
from core.async_data_client_kospi import AsyncDataClientKospi
from core.market_data_kospi import is_valid_code
from core.database_manager_kospi import DatabaseManagerKospi
from core.signal_analyzer_kospi import SignalAnalyzerKospi
from core.position_manager import PositionManager
from core.dynamic_exit_analyzer_kospi import DynamicExitAnalyzerKospi
from core.risk_management import RiskManagement
from core.reporting import TelegramReporter
from core.sector_monitor import SectorMonitor

load_dotenv(override=True)

# 내부 시그널: 매수 대기열 중단 트리거
class _InsufficientFunds(Exception):
    pass

# ── 로깅 ────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler(LOG_DIR / "kospi_trading.log",  maxBytes=10*1024*1024, backupCount=5),
        RotatingFileHandler(LOG_DIR / "screening.log",      maxBytes=5*1024*1024,  backupCount=3),
        logging.StreamHandler(sys.stdout),
    ],
)
for _noisy in ("pykrx", "requests", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

logger = logging.getLogger(__name__)

DATA_DIR  = Path(__file__).parent / "data"
TOP10_JSON = DATA_DIR / "top_10_daily.json"
LOCK_FILE = DATA_DIR / "kospi_bot.lock"
REENTRY_COOLDOWN_JSON = DATA_DIR / "sell_reentry_cooldowns.json"
PROFIT_HARVEST_JSON = DATA_DIR / "profit_harvest_state.json"
TRADES_LOG       = LOG_DIR / "trades.jsonl"
REJECTIONS_LOG   = LOG_DIR / "rejections.jsonl"
POOL_COMPARE_LOG = LOG_DIR / "pool_compare.jsonl"
SHADOW_PERF_LOG  = LOG_DIR / "shadow_perf.jsonl"
_LOCK_HANDLE = None


def acquire_lock():
    """동일 봇 중복 실행 방지."""
    global _LOCK_HANDLE
    DATA_DIR.mkdir(exist_ok=True)
    current_pid = str(os.getpid())

    # 기존 락 파일에 살아있는 PID가 있으면 사전 차단 (더 명확한 오류 메시지)
    if LOCK_FILE.exists():
        try:
            existing_pid_str = LOCK_FILE.read_text().strip()
            if existing_pid_str and existing_pid_str != current_pid:
                existing_pid = int(existing_pid_str)
                try:
                    os.kill(existing_pid, 0)   # 프로세스 존재 여부만 확인 (신호 미전달)
                    logger.critical(
                        f"🛑 이미 실행 중인 봇 감지 (PID: {existing_pid}) — "
                        f"이중 실행 차단. 기존 봇을 먼저 종료하세요."
                    )
                    raise SystemExit(1)
                except ProcessLookupError:
                    # PID가 죽어있으면 stale 락 파일 → 삭제 후 진행
                    logger.info(f"🧹 잔존 락 파일 정리 (PID {existing_pid} 이미 종료됨)")
                    LOCK_FILE.unlink(missing_ok=True)
        except SystemExit:
            raise
        except Exception:
            pass   # PID 파싱 실패 등 → fcntl에게 최종 판단 위임

    _LOCK_HANDLE = open(LOCK_FILE, "a+")
    try:
        if _sys.platform == 'win32':
            _lock_mod.locking(_LOCK_HANDLE.fileno(), _lock_mod.LK_NBLCK, 1)
        else:
            _lock_mod.lockf(_LOCK_HANDLE, _lock_mod.LOCK_EX | _lock_mod.LOCK_NB)
    except OSError:
        try:
            pid_in_file = LOCK_FILE.read_text().strip()
        except Exception:
            pid_in_file = "?"
        logger.critical(
            f"🛑 이미 실행 중인 한국주식 봇이 있어 새 실행을 중단합니다. "
            f"(실행 중 PID: {pid_in_file})"
        )
        raise SystemExit(1)
    _LOCK_HANDLE.seek(0)
    _LOCK_HANDLE.truncate()
    _LOCK_HANDLE.write(current_pid)
    _LOCK_HANDLE.flush()
    os.fsync(_LOCK_HANDLE.fileno())

    def _cleanup():
        try:
            should_unlink = False
            try:
                _LOCK_HANDLE.seek(0)
                should_unlink = _LOCK_HANDLE.read().strip() == current_pid
            except Exception:
                pass
            if _sys.platform == 'win32':
                _lock_mod.locking(_LOCK_HANDLE.fileno(), _lock_mod.LK_UNLCK, 1)
            else:
                _lock_mod.lockf(_LOCK_HANDLE, _lock_mod.LOCK_UN)
            _LOCK_HANDLE.close()
            if should_unlink:
                LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    atexit.register(_cleanup)


_AI_SELL_SYSTEM_PROMPT = (
    "당신은 국내주식 단타/스윙 매도 전문 AI입니다.\n"
    "하드 손절(-3%)과 트레일링 스톱은 이미 처리됨 — 당신의 역할은 수익 구간(+2%~+15%)에서 최적 익절 타이밍 판단.\n\n"
    "판단 기준:\n"
    "- FULL_SELL: 추세 꺾임 명확, 고점 대비 큰 하락, RSI 과열 후 반락, 장 마감 임박\n"
    "- PARTIAL_SELL: 일부 수익 확보 권장 (상승 여력 있으나 리스크 공존)\n"
    "- HOLD: 추세 유효, 더 큰 수익 가능성\n\n"
    "응답 형식 (반드시 준수):\n"
    "첫 줄: HOLD 또는 PARTIAL_SELL 또는 FULL_SELL\n"
    "둘째 줄: 이유 (30자 이내)"
)


class KospiTopTenSystem:
    """코스피 6 + 코스닥 4 매수후보 선정 + 보유 종목 매도 모니터링"""

    KST              = pytz.timezone('Asia/Seoul')
    POSITION_AMOUNT  = int(os.getenv("POSITION_AMOUNT", "999999999"))   # v4.3 기본: 계좌 비중으로 제한
    STOP_LOSS_PCT    = -3.0        # 하드 손절 기준 (-3%)
    KOSPI_COUNT      = 6           # 매수후보 코스피 종목 수
    KOSDAQ_COUNT     = 4           # 매수후보 코스닥 종목 수
    MAX_HOLDINGS     = int(os.getenv("MAX_STRATEGY_POSITIONS", "8"))  # v4.3: 8슬롯 고정
    KOSDAQ_TOP_N     = int(os.getenv("KOSDAQ_TOP_N", "300"))  # 코스닥 시총 상위 N개만 스캔
    RESCREEN_INTERVAL_SEC = int(os.getenv("RESCREEN_INTERVAL_SEC", "600"))  # 장중 재선정 주기 (기본 10분)
    BUY_FAIL_COOLDOWN_SEC = 3600   # 매수 실패 종목 재시도 대기 (1시간)

    def __init__(self):
        mock_trading = os.getenv("MOCK_TRADING", "true").lower() == "true"
        live_confirmed = os.getenv("LIVE_TRADING_CONFIRMED", "false").lower() == "true"
        if not mock_trading and not live_confirmed:
            logger.critical(
                "🛑 실전 주문 차단: MOCK_TRADING=false 이지만 "
                "LIVE_TRADING_CONFIRMED=true 가 설정되지 않았습니다."
            )
            raise SystemExit(1)

        logger.info("=" * 70)
        logger.info("🚀 코스피 6 + 코스닥 4 거래 시스템 초기화")
        logger.info(f"   투자 환경: {'모의투자' if mock_trading else '실전투자'}")
        logger.info("=" * 70)

        self.kis_client     = KISClientKospi()
        self.async_client   = AsyncDataClientKospi(self.kis_client)
        self.db             = DatabaseManagerKospi()
        self.analyzer       = SignalAnalyzerKospi()
        self.sector_monitor = SectorMonitor(self.kis_client._client)
        self.exit_analyzer  = DynamicExitAnalyzerKospi()
        self.position_mgr   = PositionManager()
        self.risk_mgr       = RiskManagement()
        self.reporter       = TelegramReporter()
        self._order_lock    = threading.Lock()   # 주문 직렬화 락

        self._sync_portfolio_from_kis()

        self.kospi_symbols_set: set[str] = set()   # yfinance .KS/.KQ 구분용
        self.is_market_open: bool = False
        self.last_rescreen_time: float = 0.0       # 마지막 재선정 시각
        self.last_balance_sync_time: float = 0.0   # 장중 예수금/보유 동기화 시각
        self.buy_fail_cooldowns: dict[str, float] = {}
        self.sell_reentry_cooldowns: dict[str, float] = self._load_sell_reentry_cooldowns()
        self.profit_harvest_state: dict[str, dict] = self._load_profit_harvest_state()
        # 당일 손실 서킷브레이커
        self._daily_realized_pnl: float = 0.0
        self._circuit_breaker_date: str = ""
        self._circuit_breaker_active: bool = False
        self._market_condition: dict = {           # 시장 변동성 필터 상태
            'trend_ok': True, 'volatility_ok': True, 'volatility_pct': 0.0,
        }
        # 풀 비교용 섀도 후보 (11~20위 — 실거래 안 함, 신호만 추적)
        self.shadow_candidates: list[str] = []
        # 폭락 반등 예외 매수
        self._crash_buy_count_today: int = 0
        self._crash_buy_date: str = ""
        self._crash_buy_cooldowns: dict[str, float] = {}   # 종목별 쿨다운
        self._ai_market_cache: tuple | None = None         # (threshold, timestamp) — 30분 캐시
        self.rescan_pool: list[str] = []                    # 아침 스캔 점수 상위 종목풀 (재선정 전용)
        self._ohlcv_cache: dict[str, tuple] = {}           # {sym: (price_data, timestamp)} — 일봉 캐시
        self._ohlcv_cache_ttl: int = int(os.getenv("OHLCV_CACHE_TTL_SEC", "300"))  # 기본 5분

        # 변동성 돌파(VB) 전략
        self.vb_candidates: dict[str, float] = {}          # {symbol: entry_price}
        self.vb_entered_today: set[str]      = set()       # 당일 이미 진입한 VB 종목
        self.vb_candidate_date: str          = ""          # vb_candidates 유효 날짜
        self.vb_split_sold: set[str]         = set()       # 14:50 1차 분할 매도 완료 VB 종목

        # 장 시작 후 개장 검증
        self._opening_validated: bool        = False       # 09:05 1회 watchlist 유동성 재검증 완료 여부

        # AI 매도 판단 클라이언트 (ANTHROPIC_API_KEY 없으면 비활성)
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if _ANTHROPIC_AVAILABLE and api_key:
            self._ai_client = _anthropic_module.Anthropic(api_key=api_key)
            logger.info("🤖 AI 매도 판단 활성화 (Claude Haiku)")
        else:
            self._ai_client = None
            logger.info("ℹ️  AI 매도 판단 비활성 (ANTHROPIC_API_KEY 미설정)")

        saved = self._load_top10()
        self.top_10_symbols: list[str] = saved['symbols']
        self.kospi_symbols_set          = set(saved['kospi_set'])
        if saved.get('rescan_pool'):
            self.rescan_pool = saved['rescan_pool']

        # 전략 성과 기반 비중 조정 캐시
        self._strategy_multipliers: dict[str, float] = {}
        self._strategy_multipliers_ts: float = 0.0  # 마지막 산출 시각 (epoch)

    def _update_daily_pnl(self, realized_pnl: float):
        """당일 실현 손익 누적 및 서킷브레이커 판단."""
        today = datetime.now(self.KST).strftime("%Y%m%d")
        if self._circuit_breaker_date != today:
            self._circuit_breaker_date = today
            self._daily_realized_pnl = 0.0
            self._circuit_breaker_active = False
        self._daily_realized_pnl += realized_pnl
        limit_pct = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "-5.0") or -5.0)
        portfolio = self.position_mgr.portfolio
        account_value = float(portfolio.get('total_value') or portfolio.get('cash') or 1_000_000)
        loss_threshold = account_value * (limit_pct / 100)
        if self._daily_realized_pnl <= loss_threshold and not self._circuit_breaker_active:
            self._circuit_breaker_active = True
            msg = (
                f"🛑 서킷브레이커 발동\n"
                f"당일 실현손익 ₩{self._daily_realized_pnl:,.0f} "
                f"(한도 {limit_pct:.1f}%)\n신규 매수 차단"
            )
            logger.critical(msg)
            self.reporter.send_message(msg)

    def _notify_trade(self, side: str, symbol: str, quantity: int, price: float, extra_lines: list[str] | None = None):
        if not self.reporter.is_enabled():
            return

        lines = [
            f"{'📈 매수 체결' if side == 'BUY' else '📉 매도 체결'}",
            f"종목: {symbol}",
            f"수량: {quantity}주",
            f"가격: ₩{price:,.0f}",
        ]
        if extra_lines:
            lines.extend(extra_lines)
        self.reporter.send_message("\n".join(lines))

    # ── KIS 계좌 동기화 ────────────────────────────────────────────────────

    def _sync_portfolio_from_kis(self):
        """시작 시 KIS 실제 계좌와 로컬 포트폴리오 동기화"""
        logger.info("🔄 KIS 계좌 잔고 동기화 중...")
        try:
            balance = self.kis_client.get_balance()
            if not balance:
                logger.warning("⚠️ KIS 잔고 조회 실패 — 로컬 포트폴리오 유지")
                return
            self.position_mgr.sync_from_api(
                holdings=balance.get('holdings', {}),
                cash=balance.get('cash', 0),
            )
        except Exception as e:
            logger.warning(f"⚠️ KIS 동기화 예외 — 로컬 포트폴리오 유지: {e}")

    # ── 구조화 거래 로그 ──────────────────────────────────────────────────

    def _log_trade_entry(self, symbol: str, price: float, quantity: int,
                          entry_type: str, score: float,
                          market_phase: str, position_pct: float):
        record = {
            "event": "ENTRY",
            "ts": datetime.now(self.KST).isoformat(),
            "symbol": symbol,
            "entry_type": entry_type,
            "price": price,
            "quantity": quantity,
            "amount": round(price * quantity),
            "score": score,
            "market_phase": market_phase,
            "position_pct": round(position_pct * 100, 1),
        }
        try:
            with open(TRADES_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"거래 로그 기록 실패: {e}")

    def _recover_entry_type(self, symbol: str) -> str:
        """trades.jsonl에서 해당 심볼의 가장 최근 ENTRY entry_type 복원."""
        if not TRADES_LOG.exists():
            return ''
        last = ''
        try:
            with open(TRADES_LOG, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get('event') == 'ENTRY' and r.get('symbol') == symbol:
                        et = r.get('entry_type', '')
                        if et and et != 'UNKNOWN':
                            last = et
        except Exception:
            pass
        return last

    def _log_trade_exit(self, symbol: str, price: float, quantity: int,
                         reason: str, holding_snap: dict):
        buy_price = float(holding_snap.get('price', 0) or 0)
        entry_time_str = holding_snap.get('entry_time', '')
        profit_pct = (price - buy_price) / buy_price * 100 if buy_price > 0 else 0.0
        hold_sec = 0
        if entry_time_str:
            try:
                from datetime import timezone
                entry_dt = datetime.fromisoformat(entry_time_str)
                hold_sec = int((datetime.now(self.KST) - entry_dt).total_seconds())
            except Exception:
                pass
        # sell_type: reason 문자열 앞부분 추출
        if reason.startswith("AI"):
            sell_type = "AI_SELL_EXIT"
        elif "손절" in reason:
            sell_type = "STOP_LOSS"
        elif "트레일링" in reason:
            sell_type = "TRAILING_STOP"
        elif "부분익절" in reason or "AI부분익절" in reason:
            sell_type = "PARTIAL_PROFIT"
        elif "VB당일청산" in reason or "VB손절" in reason:
            sell_type = "VB_EOD_CLOSE"
        elif "브레이크이븐" in reason:
            sell_type = "BREAKEVEN_STOP"
        elif "EOD" in reason or "장마감" in reason or "약세" in reason:
            sell_type = "EOD_CLEANUP"
        elif "목표" in reason:
            sell_type = "TARGET_PROFIT"
        else:
            sell_type = "OTHER"
        # entry_type 복원: holding_snap → trades.jsonl ENTRY 기록 순으로 fallback
        _et = holding_snap.get('entry_type', '')
        if not _et or _et in ('UNKNOWN', 'RESTORED'):
            _recovered = self._recover_entry_type(symbol)
            if _recovered:
                logger.debug(f"  [{symbol}] entry_type 복원: {_et!r} → {_recovered!r}")
                _et = _recovered
        if not _et:
            _et = 'UNKNOWN'

        record = {
            "event": "EXIT",
            "ts": datetime.now(self.KST).isoformat(),
            "symbol": symbol,
            "entry_type": _et,
            "sell_type": sell_type,
            "price": price,
            "buy_price": buy_price,
            "quantity": quantity,
            "profit_pct": round(profit_pct, 2),
            "hold_sec": hold_sec,
            "reason": reason[:80],
        }
        try:
            with open(TRADES_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"거래 로그 기록 실패: {e}")

    def _log_rejection(self, symbol: str, score: float, reason: str,
                        market_phase: str, pool_rank: int = 0,
                        reject_price: float = 0.0):
        record = {
            "event": "REJECTION",
            "ts": datetime.now(self.KST).isoformat(),
            "symbol": symbol,
            "pool_rank": pool_rank,
            "score": round(score, 1),
            "market_phase": market_phase,
            "reasons": reason,
            "reject_price": reject_price,
        }
        try:
            with open(REJECTIONS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"탈락 로그 기록 실패: {e}")

    def _log_pool_paper_signal(self, symbol: str, pool_rank: int, score: float,
                                signal: str, reason: str, market_phase: str):
        record = {
            "event": "PAPER_SIGNAL",
            "ts": datetime.now(self.KST).isoformat(),
            "symbol": symbol,
            "pool_rank": pool_rank,
            "score": round(score, 1),
            "signal": signal,
            "reason": reason,
            "market_phase": market_phase,
        }
        try:
            with open(POOL_COMPARE_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"풀비교 로그 기록 실패: {e}")

    def _daily_performance_report(self):
        """당일 trades.jsonl 기반 전략별 성과 집계 → 텔레그램 전송."""
        try:
            today = datetime.now(self.KST).strftime('%Y-%m-%d')
            if not TRADES_LOG.exists():
                return

            from collections import defaultdict
            entries: dict[str, dict] = {}
            perf: dict[str, dict] = defaultdict(lambda: {
                "wins": 0, "losses": 0, "total_pnl": 0.0,
                "hold_sec": 0, "count": 0, "max_loss": 0.0,
            })

            with open(TRADES_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("event") == "ENTRY" and r.get("ts", "").startswith(today):
                        entries[r.get("symbol", "")] = r
                    elif r.get("event") == "EXIT" and r.get("ts", "").startswith(today):
                        et = r.get("entry_type", "UNKNOWN")
                        pnl = float(r.get("profit_pct", 0))
                        hs  = int(r.get("hold_sec", 0))
                        perf[et]["count"] += 1
                        perf[et]["total_pnl"] += pnl
                        perf[et]["hold_sec"] += hs
                        if pnl > 0:
                            perf[et]["wins"] += 1
                        else:
                            perf[et]["losses"] += 1
                        if pnl < perf[et]["max_loss"]:
                            perf[et]["max_loss"] = pnl

            # 탈락 사유 상위 5개
            top_rejects: dict[str, int] = defaultdict(int)
            if REJECTIONS_LOG.exists():
                with open(REJECTIONS_LOG, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if r.get("ts", "").startswith(today):
                            for part in r.get("reasons", "").split(","):
                                part = part.strip()
                                if part:
                                    top_rejects[part] += 1

            # 풀 비교: 섀도 풀 BUY 신호 건수
            shadow_buy_count = 0
            if POOL_COMPARE_LOG.exists():
                with open(POOL_COMPARE_LOG, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line.strip())
                            if r.get("ts", "").startswith(today) and r.get("signal") == "BUY":
                                shadow_buy_count += 1
                        except Exception:
                            pass

            total = sum(v["count"] for v in perf.values())
            if total == 0 and not top_rejects:
                logger.info(f"📊 [{today}] 당일 체결 없음 — 성과 리포트 생략")
                return

            lines = [f"📊 일간 성과 리포트 [{today}]"]
            lines.append(f"총 {total}건 체결\n")

            if perf:
                lines.append("[ 전략별 성과 ]")
                for et, v in sorted(perf.items(), key=lambda x: x[1]["count"], reverse=True):
                    n   = v["count"]
                    wr  = v["wins"] / n * 100 if n else 0
                    avg = v["total_pnl"] / n if n else 0
                    ml  = v["max_loss"]
                    avgh = v["hold_sec"] // n // 60 if n else 0
                    lines.append(
                        f"  {et}\n"
                        f"  {n}건 | 승률{wr:.0f}% | 평균{avg:+.1f}% | 최대손실{ml:.1f}% | 보유{avgh}분"
                    )

            if top_rejects:
                lines.append("\n[ 매수 탈락 사유 Top5 ]")
                for reason, cnt in sorted(top_rejects.items(), key=lambda x: x[1], reverse=True)[:5]:
                    lines.append(f"  {reason}: {cnt}건")

            if shadow_buy_count > 0:
                lines.append(f"\n[ 풀 비교 ] 섀도후보(11~20위) BUY 신호: {shadow_buy_count}건 (미거래)")

            msg = "\n".join(lines)
            logger.info(msg)
            try:
                self.reporter.send_message(msg)
            except Exception as e:
                logger.debug(f"성과 리포트 텔레그램 실패: {e}")
        except Exception as e:
            logger.warning(f"⚠️ 일간 성과 리포트 오류: {e}")

    def _sync_portfolio_from_kis_throttled(self, reason: str = "장중"):
        """장중 입금/수동 변경을 반영하되 KIS 잔고 API 호출은 과도하지 않게 제한."""
        interval = int(os.getenv("KIS_BALANCE_SYNC_INTERVAL_SEC", "300") or 300)
        now = time.time()
        if now - self.last_balance_sync_time < interval:
            return

        before_cash = float(self.position_mgr.portfolio.get('cash', 0) or 0)
        before_holdings = set(self.position_mgr.portfolio.get('holdings', {}).keys())
        self._sync_portfolio_from_kis()
        self.last_balance_sync_time = now

        after_cash = float(self.position_mgr.portfolio.get('cash', 0) or 0)
        after_holdings = set(self.position_mgr.portfolio.get('holdings', {}).keys())
        if abs(after_cash - before_cash) >= 1 or after_holdings != before_holdings:
            logger.info(
                f"🔄 {reason} 계좌 변화 반영: "
                f"예수금 ₩{before_cash:,.0f} → ₩{after_cash:,.0f}, "
                f"보유 {len(before_holdings)}개 → {len(after_holdings)}개"
            )

    def _load_sell_reentry_cooldowns(self) -> dict[str, float]:
        """재시작해도 방금 매도한 종목을 곧바로 재매수하지 않도록 쿨다운 복원."""
        try:
            if not REENTRY_COOLDOWN_JSON.exists():
                return {}
            raw = json.loads(REENTRY_COOLDOWN_JSON.read_text(encoding='utf-8'))
            now = time.time()
            restored = {
                str(sym): float(until)
                for sym, until in raw.items()
                if float(until or 0) > now
            }
            if restored:
                logger.info(f"♻️ 매도 후 재진입 쿨다운 복원: {len(restored)}개")
            return restored
        except Exception as e:
            logger.warning(f"⚠️ 재진입 쿨다운 로드 실패: {e}")
            return {}

    def _save_sell_reentry_cooldowns(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            now = time.time()
            self.sell_reentry_cooldowns = {
                sym: until
                for sym, until in self.sell_reentry_cooldowns.items()
                if until > now
            }
            REENTRY_COOLDOWN_JSON.write_text(
                json.dumps(self.sell_reentry_cooldowns, indent=2, ensure_ascii=False),
                encoding='utf-8',
            )
        except Exception as e:
            logger.warning(f"⚠️ 재진입 쿨다운 저장 실패: {e}")

    def _load_profit_harvest_state(self) -> dict[str, dict]:
        """변동성 장에서 '어깨 매도 후 무릎 재진입' 판단에 쓰는 최근 매도 정보."""
        try:
            if not PROFIT_HARVEST_JSON.exists():
                return {}
            raw = json.loads(PROFIT_HARVEST_JSON.read_text(encoding='utf-8'))
            now = time.time()
            max_age = int(os.getenv("PROFIT_HARVEST_STATE_TTL_SEC", "14400") or 14400)
            return {
                str(sym): info
                for sym, info in raw.items()
                if now - float(info.get('time', 0) or 0) <= max_age
            }
        except Exception as e:
            logger.warning(f"⚠️ 수익채굴 상태 로드 실패: {e}")
            return {}

    def _save_profit_harvest_state(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            now = time.time()
            max_age = int(os.getenv("PROFIT_HARVEST_STATE_TTL_SEC", "14400") or 14400)
            self.profit_harvest_state = {
                sym: info
                for sym, info in self.profit_harvest_state.items()
                if now - float(info.get('time', 0) or 0) <= max_age
            }
            PROFIT_HARVEST_JSON.write_text(
                json.dumps(self.profit_harvest_state, indent=2, ensure_ascii=False),
                encoding='utf-8',
            )
        except Exception as e:
            logger.warning(f"⚠️ 수익채굴 상태 저장 실패: {e}")

    def _candle_shape(self, price_data: dict) -> dict:
        """일봉 캔들 꼬리/몸통 구조 분석."""
        close = float(price_data.get('close', 0) or 0)
        open_p = float(price_data.get('open', close) or close)
        high = float(price_data.get('high', close) or close)
        low = float(price_data.get('low', close) or close)
        rng = max(high - low, 1.0)
        body = abs(close - open_p)
        upper = max(high - max(open_p, close), 0.0)
        lower = max(min(open_p, close) - low, 0.0)
        return {
            'bullish': close >= open_p,
            'body_pct': body / rng * 100,
            'upper_pct': upper / rng * 100,
            'lower_pct': lower / rng * 100,
            'range_pct': (rng / close * 100) if close else 0.0,
        }

    def _classify_trade_profile(self, symbol: str, price_data: dict, score: float) -> tuple[str, str]:
        """진입 시 단타/중기/장기 성격을 부여."""
        candle = self._candle_shape(price_data)
        close = float(price_data.get('close', 0) or 0)
        sma20 = float(price_data.get('sma_20', close) or close)
        sma60 = float(price_data.get('sma_60', close) or close)
        rsi = float(price_data.get('rsi', 50) or 50)
        atr = float(price_data.get('atr', close * 0.02) or close * 0.02)
        atr_pct = (atr / close * 100) if close else 0.0

        # SCALP 일봉 변동폭/ATR 기준을 시장 상황에 따라 동적 조정
        # 강한 상승장(KOSPI SMA20 +X%)일수록 개별 종목 변동폭도 커지므로
        # base + trend_gap * 0.5 로 완화 (예: +7.8% 상승장 → 8 + 3.9 = 11.9%)
        base_range_pct = float(os.getenv("PROFILE_SCALP_RANGE_PCT", "8") or 8)
        trend_gap = float(self._market_condition.get('trend_gap_pct', 0) or 0)
        dynamic_range_pct = base_range_pct + max(0.0, trend_gap * 0.5)
        base_atr_pct = float(os.getenv("PROFILE_SCALP_ATR_PCT", "5") or 5)
        dynamic_atr_pct = base_atr_pct + max(0.0, trend_gap * 0.5)

        if (
            candle['bullish']
            and candle['upper_pct'] <= float(os.getenv("PROFILE_LONG_MAX_UPPER_WICK_PCT", "18") or 18)
            and candle['body_pct'] >= float(os.getenv("PROFILE_LONG_MIN_BODY_PCT", "55") or 55)
            and close > sma20 >= sma60
            and score >= float(os.getenv("PROFILE_LONG_MIN_SCORE", "88") or 88)
            and atr_pct <= float(os.getenv("PROFILE_LONG_MAX_ATR_PCT", "4.5") or 4.5)
        ):
            return "POSITION", "밑꼬리/윗꼬리 부담 적은 강한 양봉 + 정배열"

        if (
            candle['upper_pct'] >= float(os.getenv("PROFILE_SCALP_UPPER_WICK_PCT", "28") or 28)
            or candle['range_pct'] >= dynamic_range_pct
            or atr_pct >= dynamic_atr_pct
            or rsi >= float(os.getenv("PROFILE_SCALP_RSI_PCT", "78") or 78)
        ):
            return (
                "SCALP",
                f"윗꼬리/큰 변동성/과열 신호 — 짧게 수익 확보 "
                f"(range기준 {dynamic_range_pct:.1f}%, atr기준 {dynamic_atr_pct:.1f}%)"
            )

        return "SWING", "추세는 있으나 과열·꼬리 부담 일부 — 중기 대응"

    def _buy_minute_confirm(self, symbol: str, cur_price: float) -> tuple[bool, str]:
        """매수 직전 1분봉 확인 — 단기 역전(하락 전환) 감지 시 진입 차단.

        분봉 데이터 미수신 시 통과(차단하지 않음).
        """
        if os.getenv("BUY_MINUTE_CONFIRM_ENABLED", "true").lower() != "true":
            return True, ""
        try:
            mdf = self.kis_client.get_intraday_ohlcv(symbol, interval='1m', lookback=6)
            if mdf is None or len(mdf) < 4:
                return True, ""  # 데이터 부족 → 통과
            closes = mdf['close'].astype(float).values
            volumes = mdf['volume'].astype(float).values
            # 최근 3봉 중 2봉 이상 하락 → 단기 역전
            diffs = [closes[i] - closes[i - 1] for i in range(-3, 0)]
            down_count = sum(1 for d in diffs if d < 0)
            if down_count >= 2:
                return False, f"1분봉 하락전환({down_count}/3봉 하락)"
            # 최근 2봉 거래량이 앞선 2봉의 절반 미만 → 수급 소멸
            recent_vol = float(volumes[-1]) + float(volumes[-2])
            prev_vol   = float(volumes[-3]) + float(volumes[-4])
            if prev_vol > 0 and recent_vol < prev_vol * 0.4:
                return False, f"1분봉 거래량 소멸({recent_vol:.0f}<{prev_vol*0.4:.0f})"
            return True, ""
        except Exception as e:
            logger.debug(f"[{symbol}] 분봉 확인 실패(통과): {e}")
            return True, ""

    def _minute_state(self, df, lookback: int = 10) -> dict:
        if df is None or len(df) < 5:
            return {'available': False}
        recent = df.tail(lookback).copy()
        close = recent['close'].astype(float)
        high = recent['high'].astype(float)
        low = recent['low'].astype(float)
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        recent_high = float(high.max())
        recent_low = float(low.min())
        pullback = ((recent_high - last) / recent_high * 100) if recent_high else 0.0
        rebound = ((last - recent_low) / recent_low * 100) if recent_low else 0.0
        down_ticks = int((close.diff().tail(3) < 0).sum())
        up_ticks = int((close.diff().tail(3) > 0).sum())
        return {
            'available': True,
            'last': last,
            'prev': prev,
            'pullback_pct': pullback,
            'rebound_pct': rebound,
            'down_ticks': down_ticks,
            'up_ticks': up_ticks,
            'reversal_down': pullback >= float(os.getenv("MINUTE_PULLBACK_FROM_HIGH_PCT", "1.5") or 1.5) and down_ticks >= 2,
            'bounce_up': rebound >= float(os.getenv("PROFIT_REENTRY_BOUNCE_PCT", "0.5") or 0.5) and up_ticks >= 2 and last > prev,
        }

    def _effective_trade_profile(
        self,
        base_profile: str,
        minute_state: dict,
        five_min_state: dict,
        intraday_range_pct: float,
        atr_pct: float,
        profit: float,
    ) -> tuple[str, str]:
        """보유 중 1분봉/5분봉 흐름에 따라 단타/중기/장기 성격을 동적으로 조정."""
        if minute_state.get('reversal_down') and profit >= float(os.getenv("PROFILE_SCALP_SWITCH_PROFIT_PCT", "4") or 4):
            return "SCALP", "1분봉 꺾임 — 단타 수익보호 전환"
        if five_min_state.get('reversal_down') and profit >= float(os.getenv("PROFILE_SWING_SWITCH_PROFIT_PCT", "6") or 6):
            return "SWING", "5분봉 꺾임 — 중기 수익보호 전환"
        if intraday_range_pct >= float(os.getenv("VOL_SCALP_RANGE_PCT", "8.0") or 8.0) or atr_pct >= float(os.getenv("VOL_SCALP_ATR_PCT", "5.0") or 5.0):
            return "SCALP", "변동성 장 — 수익채굴 모드"
        return base_profile, "초기 전략 프로필 유지"

    def _profile_exit_settings(self, profile: str) -> dict:
        if profile == "SCALP":
            return {
                'partial_profit_pct': float(os.getenv("VOL_SCALP_PARTIAL_PROFIT_PCT", "5.0") or 5.0),
                'spike_profit_pct': float(os.getenv("VOL_SCALP_SPIKE_PROFIT_PCT", "9.0") or 9.0),
                'pullback_pct': float(os.getenv("VOL_SCALP_PULLBACK_PCT", "1.2") or 1.2),
                'partial_ratio': float(os.getenv("VOL_SCALP_PARTIAL_RATIO", "0.6") or 0.6),
            }
        if profile == "POSITION":
            return {
                'partial_profit_pct': float(os.getenv("POSITION_PARTIAL_PROFIT_PCT", "12.0") or 12.0),
                'spike_profit_pct': float(os.getenv("POSITION_SPIKE_PROFIT_PCT", "18.0") or 18.0),
                'pullback_pct': float(os.getenv("POSITION_PULLBACK_PCT", "3.0") or 3.0),
                'partial_ratio': float(os.getenv("POSITION_PARTIAL_RATIO", "0.35") or 0.35),
            }
        return {
            'partial_profit_pct': float(os.getenv("SWING_PARTIAL_PROFIT_PCT", "8.0") or 8.0),
            'spike_profit_pct': float(os.getenv("SWING_SPIKE_PROFIT_PCT", "12.0") or 12.0),
            'pullback_pct': float(os.getenv("SWING_PULLBACK_PCT", "2.0") or 2.0),
            'partial_ratio': float(os.getenv("SWING_PARTIAL_RATIO", "0.5") or 0.5),
        }

    def _build_target_plan(self, buy_price: float, atr: float, profile: str) -> list[dict]:
        """진입 시 목표가 구간을 생성한다. 프로필별로 목표 거리와 기본 청산 비중이 다르다."""
        if buy_price <= 0:
            return []

        atr_pct = ((atr / buy_price) * 100) if atr > 0 else 2.0
        if profile == "SCALP":
            levels = [
                (max(2.0, atr_pct * 0.9), 0.35),
                (max(3.5, atr_pct * 1.5), 0.50),
                (max(5.0, atr_pct * 2.5), 1.00),
            ]
        elif profile == "POSITION":
            levels = [
                (max(6.0, atr_pct * 2.0), 0.20),
                (max(10.0, atr_pct * 3.6), 0.35),
                (max(15.0, atr_pct * 5.2), 1.00),
            ]
        else:
            levels = [
                (max(4.0, atr_pct * 1.5), 0.25),
                (max(7.0, atr_pct * 2.5), 0.40),
                (max(11.0, atr_pct * 4.0), 1.00),
            ]

        plan: list[dict] = []
        seen_prices: set[int] = set()
        for idx, (target_pct, sell_ratio) in enumerate(levels, 1):
            target_price = int(round(buy_price * (1 + target_pct / 100)))
            if target_price in seen_prices:
                target_price += idx
            seen_prices.add(target_price)
            plan.append(
                {
                    'stage': idx,
                    'target_pct': round(target_pct, 2),
                    'target_price': target_price,
                    'sell_ratio': sell_ratio,
                }
            )
        return plan

    def _target_breakout_strength(
        self,
        price_data: dict,
        minute_state: dict,
        five_min_state: dict,
        cur_price: float,
        target_price: float,
    ) -> tuple[bool, str]:
        """목표가 돌파가 힘 있는지 판단한다."""
        score = 0
        reasons: list[str] = []
        volume = float(price_data.get('volume', 0) or 0)
        avg_volume = float(price_data.get('avg_volume_20', volume) or volume)
        volume_ratio = (volume / avg_volume) if avg_volume > 0 else 1.0
        rsi = float(price_data.get('rsi', 50) or 50)
        close = float(price_data.get('close', cur_price) or cur_price)
        sma_5 = float(price_data.get('sma_5', close) or close)
        sma_20 = float(price_data.get('sma_20', close) or close)
        candle = self._candle_shape(price_data)

        if cur_price >= target_price * 1.003:
            score += 1
            reasons.append("목표가 여유 돌파")
        if volume_ratio >= 1.3:
            score += 1
            reasons.append(f"거래량 {volume_ratio:.1f}배")
        if rsi >= 62:
            score += 1
            reasons.append(f"RSI {rsi:.0f}")
        if close >= sma_5 >= sma_20:
            score += 1
            reasons.append("단기 정배열")
        if candle.get('bullish') and candle.get('upper_pct', 100) <= 20:
            score += 1
            reasons.append("윗꼬리 부담 적음")
        if minute_state.get('bounce_up') and not minute_state.get('reversal_down'):
            score += 1
            reasons.append("1분봉 유지")
        if five_min_state.get('bounce_up') and not five_min_state.get('reversal_down'):
            score += 1
            reasons.append("5분봉 유지")

        return score >= 4, ", ".join(reasons[:4]) if reasons else "돌파 강도 부족"

    def _target_plan_action(
        self,
        symbol: str,
        holding: dict,
        holding_qty: int,
        cur_price: float,
        price_data: dict,
        minute_state: dict,
        five_min_state: dict,
    ) -> tuple[str | None, dict | None]:
        """다음 목표가 도달 시 강한 돌파면 통과, 약하면 분할 익절한다."""
        plan = holding.get('target_plan') or []
        if not plan or holding_qty <= 0:
            return None, None

        events = holding.get('target_stage_events') or {}
        for target in plan:
            stage_key = str(target.get('stage'))
            if stage_key in events:
                continue

            target_price = float(target.get('target_price') or 0)
            if cur_price < target_price:
                return None, None

            strong, detail = self._target_breakout_strength(
                price_data, minute_state, five_min_state, cur_price, target_price
            )
            is_final = int(target.get('stage', 0) or 0) >= len(plan)
            if strong and not is_final:
                return "pass", {
                    'stage': stage_key,
                    'target_price': target_price,
                    'detail': detail,
                    'status': 'passed',
                }

            sell_ratio = float(target.get('sell_ratio', 1.0) or 1.0)
            if is_final:
                qty = holding_qty
            else:
                qty = max(1, int(holding_qty * sell_ratio))
                qty = min(qty, max(1, holding_qty - 1))
            if qty <= 0:
                return None, None

            return "sell", {
                'stage': stage_key,
                'target_price': target_price,
                'detail': detail,
                'qty': qty,
                'status': 'sold',
                'reason': (
                    f"목표가{stage_key}차 도달 후 약세 익절"
                    f"(₩{target_price:,.0f}, {detail})"
                ),
            }

        return None, None

    # ── 변동성 돌파(VB) 전략 ──────────────────────────────────────────────

    def _vb_ai_prefilter(
        self,
        candidates: list[str],
        scores: dict[str, float],
        data_map: dict[str, dict],
    ) -> dict[str, float]:
        """아침 스크리닝 후보에 대해 Haiku로 변동성 돌파 진입 적합 여부 사전 판단.
        반환: {symbol: entry_price} — AI 승인 종목만 포함."""
        import json as _json, re as _re

        k = float(os.getenv("VB_K_FACTOR", "0.5"))
        result: dict[str, float] = {}

        for sym in candidates:
            pd         = data_map.get(sym, {})
            prev_high  = float(pd.get('high', 0) or 0)
            prev_low   = float(pd.get('low', 0) or 0)
            prev_close = float(pd.get('close', 0) or 0)
            prev_range = prev_high - prev_low
            if prev_range <= 0 or prev_close <= 0:
                continue
            entry_price = round(prev_close + prev_range * k)

            if not self._ai_client:
                result[sym] = entry_price
                continue

            score   = scores.get(sym, 0)
            sector  = self.sector_monitor.get_sector_name(sym) or '미분류'
            atr     = float(pd.get('atr', 0) or 0)
            sma20   = float(pd.get('sma_20', prev_close) or prev_close)
            sma20_gap = (prev_close / sma20 - 1) * 100 if sma20 else 0

            prompt = (
                f"종목: {sym} | 섹터: {sector} | 스크리닝점수: {score:.0f}/100\n"
                f"전일종가: ₩{prev_close:,.0f} | 전일범위: ₩{prev_range:,.0f} ({prev_range/prev_close*100:.1f}%)\n"
                f"ATR: ₩{atr:,.0f} | SMA20 대비: {sma20_gap:+.1f}%\n"
                f"변동성돌파 예상진입가: ₩{entry_price:,.0f} (전일종가 + 전일범위×{k})\n\n"
                f"오늘 변동성 돌파 전략 진입 적합 여부를 판단하세요.\n"
                f"전제: 당일 청산, 손절 -2%, 목표 +2~3%\n"
                f"JSON만 답변: {{\"decision\":\"BUY\"또는\"SKIP\",\"reason\":\"한줄이유\"}}"
            )
            try:
                resp = self._ai_client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=80,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content[0].text.strip()
                m = _re.search(r'\{.*\}', text, _re.DOTALL)
                if m:
                    data = _json.loads(m.group())
                    decision = data.get("decision", "SKIP")
                    reason   = data.get("reason", "")
                    if decision == "BUY":
                        result[sym] = entry_price
                        logger.info(f"  ✅ [VB] {sym} AI승인 — {reason} (진입가 ₩{entry_price:,.0f})")
                    else:
                        logger.info(f"  ❌ [VB] {sym} AI거절 — {reason}")
            except Exception as e:
                logger.warning(f"  [VB] {sym} AI판단 실패: {e} — 기본 승인")
                result[sym] = entry_price

        logger.info(f"🤖 [VB] 사전 필터 완료: {len(result)}/{len(candidates)}종목 승인")
        return result

    def _execute_vb_buy(self, symbol: str, price: float) -> bool:
        """변동성 돌파 전략 전용 매수. VB_CAPITAL_PCT × 1/VB_MAX_POSITIONS 자금 사용."""
        try:
            vb_capital_pct   = float(os.getenv("VB_CAPITAL_PCT", "0.30"))
            vb_max_positions = int(os.getenv("VB_MAX_POSITIONS", "3"))
            vb_stop_pct      = float(os.getenv("VB_STOP_LOSS_PCT", "2.0")) / 100

            holdings = self.position_mgr.portfolio.get('holdings', {})
            vb_count = sum(1 for h in holdings.values() if h.get('source') == 'vb')
            if vb_count >= vb_max_positions:
                logger.info(f"⛔ [{symbol}] VB 포지션 상한 ({vb_count}/{vb_max_positions}) — 진입 취소")
                return False

            portfolio     = self.position_mgr.portfolio
            account_value = float(portfolio.get('total_value') or 0)
            if account_value <= 0:
                holdings_value = sum(
                    h.get('quantity', 0) * h.get('price', 0)
                    for h in portfolio.get('holdings', {}).values()
                )
                account_value = float(portfolio.get('cash', 0)) + holdings_value

            per_pos_amount = account_value * vb_capital_pct / vb_max_positions
            try:
                orderable = self.kis_client.get_orderable_cash(symbol, price, use_max=True)
            except Exception:
                orderable = float(portfolio.get('cash', 0))

            budget   = min(per_pos_amount, orderable * 0.99)
            quantity = max(1, int(budget / price))
            if quantity <= 0 or price <= 0:
                return False

            def _krx_tick(p: float) -> int:
                if p < 2_000:    return 1
                if p < 5_000:    return 5
                if p < 10_000:   return 10
                if p < 50_000:   return 50
                if p < 100_000:  return 100
                if p < 500_000:  return 500
                return 1_000

            tick        = _krx_tick(price)
            order_price = (int(price) // tick) * tick
            stop_price  = (int(order_price * (1 - vb_stop_pct)) // tick) * tick

            _vb_market = os.getenv("VB_ORDER_TYPE", "market").lower() == "market"
            logger.warning(
                f"💥 [VB] {symbol} 변동성돌파 진입! ₩{order_price:,.0f} × {quantity}주 "
                f"(예산 ₩{per_pos_amount:,.0f} | 손절 ₩{stop_price:,.0f} | {'시장가' if _vb_market else '지정가'})"
            )
            success = self.kis_client.place_buy_order(symbol, quantity, order_price, market_order=_vb_market)
            if not success:
                logger.warning(f"⚠️ [VB] {symbol} 주문 실패")
                return False

            self.position_mgr.add_position(symbol, quantity, order_price, source='vb', entry_type='VB_INTRADAY')
            vb_entry_ts = datetime.now(self.KST).isoformat()
            self.position_mgr.update_position_metadata(
                symbol,
                strategy_profile='SCALP',
                strategy_profile_reason='변동성돌파전략(VB)',
                strategy_profile_time=vb_entry_ts,
                effective_profile='SCALP',
                vb_stop_price=stop_price,
                entry_type='VB_INTRADAY',
                entry_time=vb_entry_ts,
                entry_position_pct=round(vb_capital_pct / vb_max_positions * 100, 1),
            )
            self._log_trade_entry(symbol, order_price, quantity,
                                  'VB_INTRADAY', 0.0,
                                  self._market_condition.get('market_mode', 'UNKNOWN'),
                                  vb_capital_pct / vb_max_positions)
            logger.info(f"  ✅ [VB] {symbol} 진입 완료 — 손절 ₩{stop_price:,.0f}")
            try:
                self.reporter.send_message(
                    f"💥 [VB돌파] {symbol} 진입\n"
                    f"₩{order_price:,.0f} × {quantity}주 | 손절 ₩{stop_price:,.0f}"
                )
            except Exception:
                pass
            return True
        except Exception as e:
            logger.error(f"❌ [VB] {symbol} 매수 오류: {e}", exc_info=True)
            return False

    def _should_eod_cleanup(
        self,
        now_hm: int,
        profile: str,
        trend_score: int,
        profit: float,
        minute_state: dict,
        five_min_state: dict,
        price_data: dict,
    ) -> tuple[bool, str | None]:
        """장 막판에는 약한 종목만 정리한다. SCALP 재분류 종목은 15:00에 당일 강제 청산."""
        scalp_eod_hm = int(os.getenv("SCALP_EOD_CLOSE_HHMM", "1500") or 1500)
        if profile == "SCALP" and now_hm >= scalp_eod_hm:
            return True, f"SCALP 당일청산 ({now_hm // 100:02d}:{now_hm % 100:02d} >= {scalp_eod_hm // 100:02d}:{scalp_eod_hm % 100:02d})"

        start_hm = int(os.getenv("EOD_WEAK_SELL_START_HHMM", "1520") or 1520)
        if now_hm < start_hm:
            return False, None

        close = float(price_data.get('close', 0) or 0)
        sma_5 = float(price_data.get('sma_5', close) or close)
        sma_20 = float(price_data.get('sma_20', close) or close)

        if profile == "POSITION" and trend_score >= 80 and profit >= 2.0 and not five_min_state.get('reversal_down'):
            return False, None
        if minute_state.get('reversal_down') and profit > 0:
            return True, "장마감약세:1분봉 꺾임"
        if five_min_state.get('reversal_down') and profit >= 0:
            return True, "장마감약세:5분봉 꺾임"
        if close < sma_5 < sma_20:
            return True, "장마감약세:단기추세 훼손"
        if trend_score < 55 and profit > 0:
            return True, f"장마감약세:추세점수 {trend_score}"
        if profit < -0.5 and profile != "POSITION":
            return True, "장마감약세:음수수익"
        return False, None

    def _ai_sell_judgment(
        self,
        symbol: str,
        profit: float,
        max_profit: float,
        peak_pullback: float,
        minute_state: dict,
        five_min_state: dict,
        effective_profile: str,
        price_data: dict,
    ) -> tuple[str, str]:
        """Claude Haiku 기반 익절 타이밍 판단. 수익 2~15% 구간에서만 호출.
        반환: ('HOLD'|'PARTIAL_SELL'|'FULL_SELL', reason)
        """
        if self._ai_client is None:
            return "HOLD", "AI비활성"
        try:
            close = float(price_data.get('close', 0) or 0)
            bb_mid = float(price_data.get('bb_middle', close) or close)
            bb_top = float(price_data.get('bb_upper', close) or close)
            bb_pos = (
                round((close - bb_mid) / (bb_top - bb_mid) * 100)
                if bb_top > bb_mid else 0
            )
            msg = (
                f"종목:{symbol} 프로필:{effective_profile}\n"
                f"현재수익:{profit:.1f}% 최고수익:{max_profit:.1f}% 고점낙폭:{peak_pullback:.1f}%\n"
                f"RSI:{price_data.get('rsi', 50):.0f} "
                f"1분봉꺾임:{'Y' if minute_state.get('reversal_down') else 'N'} "
                f"5분봉꺾임:{'Y' if five_min_state.get('reversal_down') else 'N'}\n"
                f"MACD상승:{'Y' if price_data.get('macd', 0) > price_data.get('macd_signal', 0) else 'N'} "
                f"BB위치:{bb_pos}%"
            )
            resp = self._ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                system=[{
                    "type": "text",
                    "text": _AI_SELL_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{"role": "user", "content": msg}],
            )
            text = resp.content[0].text.strip()
            lines = text.split('\n', 1)
            action = lines[0].strip().upper()
            reason = lines[1].strip() if len(lines) > 1 else ""
            if action not in ("HOLD", "PARTIAL_SELL", "FULL_SELL"):
                return "HOLD", "AI응답파싱실패"
            logger.info(f"  🤖 [{symbol}] AI판단: {action} — {reason}")
            return action, reason
        except Exception as e:
            logger.debug(f"  [{symbol}] AI 판단 오류: {e}")
            return "HOLD", "AI오류"

    def _record_profit_harvest_exit(self, symbol: str, price: float, reason: str):
        if os.getenv("PROFIT_HARVEST_ENABLED", "false").lower() != "true":
            return
        today = datetime.now(self.KST).strftime("%Y%m%d")
        info = self.profit_harvest_state.get(symbol, {})
        if info.get('date') != today:
            info = {'date': today, 'reentries': 0}
        info.update({'last_exit_price': price, 'last_exit_reason': reason, 'time': time.time()})
        self.profit_harvest_state[symbol] = info
        self._save_profit_harvest_state()

    def _allow_profit_harvest_reentry(self, symbol: str, cur_price: float, minute_df) -> tuple[bool, str]:
        if os.getenv("PROFIT_HARVEST_ENABLED", "false").lower() != "true":
            return False, "수익채굴 비활성"
        info = self.profit_harvest_state.get(symbol)
        if not info:
            return False, "최근 매도 없음"
        today = datetime.now(self.KST).strftime("%Y%m%d")
        if info.get('date') != today:
            return False, "당일 매도 아님"
        max_reentries = int(os.getenv("PROFIT_HARVEST_MAX_REENTRIES_PER_DAY", "2") or 2)
        if int(info.get('reentries', 0) or 0) >= max_reentries:
            return False, "당일 재진입 횟수 초과"
        last_exit = float(info.get('last_exit_price', 0) or 0)
        pullback_pct = float(os.getenv("PROFIT_REENTRY_PULLBACK_PCT", "2.5") or 2.5)
        if not last_exit or cur_price > last_exit * (1 - pullback_pct / 100):
            return False, f"눌림 부족(매도가 대비 {pullback_pct:.1f}% 미만)"
        state = self._minute_state(minute_df, lookback=10)
        if not state.get('bounce_up'):
            return False, "1분봉 반등 미확인"
        info['reentries'] = int(info.get('reentries', 0) or 0) + 1
        info['last_reentry_price'] = cur_price
        info['last_reentry_time'] = time.time()
        self.profit_harvest_state[symbol] = info
        self._save_profit_harvest_state()
        return True, f"무릎 재진입: 매도가 ₩{last_exit:,.0f} 대비 눌림 후 1분봉 반등"

    def _update_market_condition(self):
        """KOSPI 지수 추세·변동성 평가 — 매수 허용 여부 갱신.

        기본 조건:
          - 추세: KOSPI 종가 >= SMA20
          - 변동성: ATR(14)/종가 < MARKET_VOL_THRESHOLD

        자동 시장 모드:
          - 기본장: 기본 변동성 이내면 일반 매수 허용
          - 강한 상승장: KOSPI가 SMA20보다 충분히 위이고 변동성만 높으면
            고점수 종목만 선택적으로 예외 허용
          - 위험장: 추세 하락 또는 하드 변동성 초과면 차단
        """
        try:
            from datetime import timedelta
            end   = datetime.now(self.KST)
            start = end - timedelta(days=70)   # SMA20 + ATR14 충분한 기간
            df = fdr.DataReader('KS11',
                                start.strftime('%Y-%m-%d'),
                                end.strftime('%Y-%m-%d'))
            if df is None or len(df) < 20:
                logger.warning("⚠️ KOSPI 지수 데이터 부족 — 시장 조건 기존 유지")
                return

            close = df['Close'].astype(float)
            high  = df['High'].astype(float)
            low   = df['Low'].astype(float)

            sma20 = close.rolling(20).mean()
            tr    = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low  - close.shift()).abs(),
            ], axis=1).max(axis=1)
            atr14 = tr.rolling(14).mean()

            last_close = float(close.iloc[-1])
            last_sma20 = float(sma20.iloc[-1])
            last_atr   = float(atr14.iloc[-1])
            vol_pct    = last_atr / last_close * 100
            base_threshold = float(
                os.getenv('MARKET_VOL_BASE_THRESHOLD')
                or os.getenv('MARKET_VOL_THRESHOLD', '2.5')
            )
            strong_trend_gap_required = float(os.getenv('MARKET_VOL_STRONG_TREND_GAP_PCT', '5.0') or 5.0)
            strong_trend_limit = float(
                os.getenv('MARKET_VOL_STRONG_TREND_LIMIT')
                or os.getenv('MARKET_VOL_HARD_LIMIT', '4.0')
            )
            hard_vol_limit = float(os.getenv('MARKET_VOL_HARD_LIMIT', str(strong_trend_limit)) or strong_trend_limit)

            trend_ok = last_close >= last_sma20
            trend_gap_pct = ((last_close - last_sma20) / last_sma20 * 100) if last_sma20 else 0.0
            strong_trend = trend_ok and trend_gap_pct >= strong_trend_gap_required
            vol_ok = vol_pct <= base_threshold
            selective_ok = strong_trend and base_threshold < vol_pct <= strong_trend_limit

            if trend_ok and vol_ok:
                market_mode = "NORMAL"
            elif selective_ok:
                market_mode = "STRONG_TREND_SELECTIVE"
            elif not trend_ok:
                market_mode = "TREND_DOWN"
            elif vol_pct > hard_vol_limit:
                market_mode = "HARD_VOL_BLOCK"
            else:
                market_mode = "VOL_BLOCK"

            self._market_condition = {
                'trend_ok':       trend_ok,
                'volatility_ok':  vol_ok,
                'volatility_pct': vol_pct,
                'kospi':          last_close,
                'sma20':          last_sma20,
                'threshold':      base_threshold,
                'base_threshold': base_threshold,
                'strong_trend':   strong_trend,
                'trend_gap_pct':  trend_gap_pct,
                'strong_trend_gap_required': strong_trend_gap_required,
                'strong_trend_limit': strong_trend_limit,
                'hard_vol_limit': hard_vol_limit,
                'selective_ok':   selective_ok,
                'market_mode':    market_mode,
            }

            buy_ok = trend_ok and vol_ok
            mode_text = {
                "NORMAL": "✅ 일반 매수 허용",
                "STRONG_TREND_SELECTIVE": "⚡ 강한 상승장 — 고점수만 선택 허용",
                "TREND_DOWN": "⛔ 추세 하락 차단",
                "HARD_VOL_BLOCK": "⛔ 변동성 하드컷 차단",
                "VOL_BLOCK": "⛔ 변동성 과열 차단",
            }.get(market_mode, market_mode)
            logger.info(
                f"📊 시장 상태: {mode_text} | "
                f"KOSPI {last_close:,.0f} (SMA20 {last_sma20:,.0f}, +{trend_gap_pct:.1f}%) | "
                f"변동성 {vol_pct:.2f}% "
                f"(기본 {base_threshold:.1f}%, 강한상승 {strong_trend_limit:.1f}%, 하드 {hard_vol_limit:.1f}%)"
            )
            try:
                from core.market_monitor import get_summary, format_log, get_news
                summary = get_summary()
                self._market_condition['vix'] = summary.get('vix')
                self._market_condition['fear_greed'] = summary.get('fear_greed')
                sentiment = format_log(summary)
                if sentiment:
                    logger.info(f"🌐 글로벌 감성: {sentiment}")
                news = summary.get('news', [])
                if news:
                    logger.info("📰 주요뉴스: " + " / ".join(news[:3]))

                # AI 시장 조건 판단 — strong_trend 임계값 동적 조정
                ai_gap = self._ai_market_gap_threshold(
                    vix=summary.get('vix'),
                    fear_greed=summary.get('fear_greed'),
                    trend_gap_pct=trend_gap_pct,
                    vol_pct=vol_pct,
                    base_threshold=base_threshold,
                )
                if ai_gap is not None:
                    new_strong = trend_ok and trend_gap_pct >= ai_gap
                    new_selective = new_strong and base_threshold < vol_pct <= strong_trend_limit
                    changed = ai_gap != strong_trend_gap_required
                    logger.info(
                        f"🤖 AI 시장 판단: 진입 임계값 {ai_gap:.1f}%"
                        f"{' (기존 ' + str(strong_trend_gap_required) + '% → 변경)' if changed else ' (유지)'}"
                        f" | strong_trend={new_strong}"
                    )
                    self._market_condition['strong_trend_gap_required'] = ai_gap
                    self._market_condition['strong_trend'] = new_strong
                    self._market_condition['selective_ok'] = new_selective
            except Exception as _me:
                logger.debug(f"시장 감성 조회 실패: {_me}")
        except Exception as e:
            logger.warning(f"⚠️ 시장 상태 조회 실패 — 기존 상태 유지: {e}")

    # ── AI 시장 조건 판단 ──────────────────────────────────────────────────

    def _ai_market_gap_threshold(
        self,
        vix: float | None,
        fear_greed: dict | None,
        trend_gap_pct: float,
        vol_pct: float,
        base_threshold: float,
    ) -> float | None:
        """Claude Haiku로 strong_trend 진입 임계값 동적 판단 (30분 캐시).
        반환: 2.0~7.0% 범위의 임계값, 실패 시 None."""
        if self._ai_client is None:
            return None
        import time as _time
        now = _time.time()
        if self._ai_market_cache is not None:
            val, ts = self._ai_market_cache
            if now - ts < 1800:  # 30분
                return val
        try:
            fg_val = fear_greed.get('value') if isinstance(fear_greed, dict) else fear_greed
            prompt = (
                "한국 주식 자동매매 시스템입니다. 현재 시장 데이터를 보고 "
                "'KOSPI가 SMA20보다 몇 % 이상 높아야 변동성 높은 장에서도 매수를 허용할지' "
                "임계값을 결정해주세요.\n\n"
                f"현재 데이터:\n"
                f"- VIX: {vix or '?'}\n"
                f"- Fear & Greed: {fg_val or '?'} (0=극도공포, 100=극도탐욕)\n"
                f"- KOSPI vs SMA20: +{trend_gap_pct:.1f}%\n"
                f"- KOSPI ATR 변동성: {vol_pct:.1f}% (기본 허용: {base_threshold:.1f}%)\n\n"
                "기준:\n"
                "- VIX 낮고 탐욕 높음 → 낮은 임계값(3~4%) 허용\n"
                "- VIX 높고 공포 강함 → 높은 임계값(5~6%) 필요\n"
                "- 중립 → 4~5%\n\n"
                "숫자 하나만 응답 (예: 4.0)"
            )
            resp = self._ai_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=10,
                messages=[{"role": "user", "content": prompt}],
            )
            val = float(resp.content[0].text.strip())
            val = max(2.0, min(7.0, val))
            self._ai_market_cache = (val, now)
            return val
        except Exception as e:
            logger.debug(f"AI 시장 임계값 조회 실패: {e}")
            return None

    # ── 폭락 반등 예외 매수 ─────────────────────────────────────────────────

    def _is_crash_environment(self) -> tuple[bool, str]:
        """VIX·공포탐욕지수 기준으로 폭락 환경 여부 판단."""
        if os.getenv("CRASH_RECOVERY_ENABLED", "true").lower() != "true":
            return False, "비활성"
        mc = self._market_condition
        vix = mc.get('vix')
        fg_raw = mc.get('fear_greed')
        fg = float(fg_raw['value']) if isinstance(fg_raw, dict) else fg_raw
        vix_thresh = float(os.getenv("CRASH_VIX_THRESHOLD", "40") or 40)
        fg_thresh  = float(os.getenv("CRASH_FEAR_GREED_THRESHOLD", "15") or 15)
        reasons = []
        if vix and vix >= vix_thresh:
            reasons.append(f"VIX {vix:.1f}>={vix_thresh:.0f}")
        if fg is not None and fg <= fg_thresh:
            reasons.append(f"FearGreed {fg:.0f}<={fg_thresh:.0f}")
        if reasons:
            return True, " + ".join(reasons)
        return False, f"VIX={vix or '?'} FG={fg or '?'} — 폭락 환경 아님"

    def _detect_stock_recovery_signal(self, sym: str, price_data: dict) -> tuple[bool, str]:
        """종목 단위 반등 신호 감지: 극단 oversold + 아래꼬리 hammer + 거래량 급증."""
        rsi = price_data.get('rsi', 50)
        rsi_thresh = float(os.getenv("CRASH_RSI_THRESHOLD", "25") or 25)
        if rsi > rsi_thresh:
            return False, f"RSI {rsi:.0f}>{rsi_thresh:.0f}"
        volume     = price_data.get('volume', 0)
        avg_volume = price_data.get('avg_volume_20', volume) or volume
        vol_spike  = float(os.getenv("CRASH_VOLUME_SPIKE", "2.0") or 2.0)
        if avg_volume > 0 and volume < avg_volume * vol_spike:
            return False, f"거래량 미달 ({volume/avg_volume:.1f}x < {vol_spike:.1f}x)"
        # 1분봉 hammer 확인. 자동매수에서는 데이터 확인 실패를 진입 근거로 쓰지 않는다.
        require_intraday = (
            os.getenv("CRASH_REQUIRE_INTRADAY_CONFIRM", "true").lower() == "true"
        )
        try:
            minute_df = self.kis_client.get_intraday_ohlcv(sym, interval='1m', lookback=5)
            if minute_df is not None and len(minute_df) >= 2:
                last = minute_df.iloc[-1]
                o, h, l, c = float(last['open']), float(last['high']), float(last['low']), float(last['close'])
                total_range = h - l
                if total_range > 0:
                    lower_wick = min(o, c) - l
                    body       = abs(c - o)
                    # hammer: 아래꼬리가 전체 봉 높이의 50% 이상, 양봉, 실체가 있음(doji 제외)
                    if lower_wick >= total_range * 0.5 and c > o and body > 0:
                        return True, f"RSI {rsi:.0f} + 거래량 {volume/avg_volume:.1f}x + hammer"
                    return False, f"캔들 패턴 미충족 (아래꼬리 {lower_wick/total_range*100:.0f}%)"
        except Exception:
            if require_intraday:
                return False, "1분봉 반전 확인 실패"
        if require_intraday:
            return False, "1분봉 반전 데이터 부족"
        # 명시적으로 완화한 경우에만 RSI + 거래량으로 판단
        return True, f"RSI {rsi:.0f} + 거래량 {volume/avg_volume:.1f}x"

    def _crash_daily_reset(self):
        """날짜 바뀌면 당일 폭락 매수 카운트 초기화."""
        today = datetime.now(self.KST).strftime("%Y%m%d")
        if self._crash_buy_date != today:
            self._crash_buy_date = today
            self._crash_buy_count_today = 0

    def _allow_market_filter_override(self, symbol: str, price_data: dict, score: float) -> tuple[bool, str]:
        """강한 매수 신호에 한해 변동성 필터 예외 허용 여부를 판단한다."""
        mc = self._market_condition
        allow_override = os.getenv("ALLOW_STRONG_MARKET_FILTER_OVERRIDE", "false").lower() == "true"
        override_score = float(os.getenv("MARKET_FILTER_OVERRIDE_SCORE", "85") or 85)
        hard_vol_limit = float(mc.get('hard_vol_limit') or os.getenv("MARKET_VOL_HARD_LIMIT", "4.0") or 4.0)
        strong_trend_limit = float(mc.get('strong_trend_limit') or hard_vol_limit)

        if not allow_override:
            return False, "예외허용 OFF"

        if not mc.get('trend_ok'):
            return False, "KOSPI 추세 하락"

        if not mc.get('strong_trend'):
            return False, (
                f"강한 상승장 아님 "
                f"({mc.get('trend_gap_pct', 0):.1f}%<{mc.get('strong_trend_gap_required', 5.0):.1f}%)"
            )

        if mc.get('volatility_ok'):
            return True, "시장 조건 충족"

        volatility_pct = float(mc.get('volatility_pct') or 0)
        if volatility_pct > hard_vol_limit:
            return False, f"변동성 하드컷 초과 {volatility_pct:.1f}%>{hard_vol_limit:.1f}%"

        # 섹터 모멘텀 연동: 상위 섹터 종목은 진입 점수 완화
        sector_bonus = 0.0
        sector_name  = ''
        try:
            sector_bonus = self.sector_monitor.get_sector_bonus(symbol)
            sector_name  = self.sector_monitor.get_sector_name(symbol) or ''
        except Exception:
            pass
        # 섹터 모멘텀 0.05 이상(상위권) → 5pt 완화, 0.02 이상 → 3pt 완화
        sector_discount  = 5.0 if sector_bonus >= 0.05 else (3.0 if sector_bonus >= 0.02 else 0.0)
        effective_score  = override_score - sector_discount

        # strong_trend_limit 초과(5.5~6.0%) 구간: 상위 섹터 종목만 진입 허용
        if volatility_pct > strong_trend_limit and sector_discount == 0:
            return False, f"강한 상승장 허용폭 초과 {volatility_pct:.1f}%>{strong_trend_limit:.1f}% (상위 섹터 아님)"

        if score < effective_score:
            discount_str = f" (섹터 {sector_name} 완화 -{sector_discount:.0f}pt)" if sector_discount > 0 else ""
            return False, f"점수 부족 {score:.1f}<{effective_score:.0f}{discount_str}"

        discount_str = f" (섹터 {sector_name} 모멘텀 -{sector_discount:.0f}pt 완화)" if sector_discount > 0 else ""
        return True, (
            f"강한 상승장 선택 허용: 점수 {score:.1f}>={effective_score:.0f}{discount_str}, "
            f"KOSPI+{mc.get('trend_gap_pct', 0):.1f}%, "
            f"변동성 {volatility_pct:.1f}%<={hard_vol_limit:.1f}%"
        )

    def show_balance(self):
        """KIS 계좌 잔고 및 포트폴리오를 콘솔에 출력"""
        KISBalanceChecker(self.kis_client).print_balance()

    # ── Top 10 영속화 ──────────────────────────────────────────────────────

    def _save_top10(self, symbols: list[str]):
        DATA_DIR.mkdir(exist_ok=True)
        TOP10_JSON.write_text(
            json.dumps({
                'date'        : datetime.now(self.KST).strftime('%Y%m%d'),
                'symbols'     : symbols,
                'kospi_set'   : [s for s in symbols if s in self.kospi_symbols_set],
                'rescan_pool' : self.rescan_pool,
            }, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def _load_top10(self) -> dict:
        """오늘 날짜의 매수후보 + 코스피 구분 + 재선정 풀 복원"""
        if not TOP10_JSON.exists():
            return {'symbols': [], 'kospi_set': [], 'rescan_pool': []}
        try:
            obj   = json.loads(TOP10_JSON.read_text(encoding='utf-8'))
            today = datetime.now(self.KST).strftime('%Y%m%d')
            if obj.get('date') == today:
                syms  = obj.get('symbols', [])
                kospi = obj.get('kospi_set', syms)
                pool  = obj.get('rescan_pool', [])
                logger.info(f"📂 매수후보 복원: {syms}" + (f" | 재선정풀 {len(pool)}종목" if pool else ""))
                return {'symbols': syms, 'kospi_set': kospi, 'rescan_pool': pool}
        except Exception:
            pass
        return {'symbols': [], 'kospi_set': [], 'rescan_pool': []}

    # ── 종목 리스트 ────────────────────────────────────────────────────────

    def get_kospi_symbols(self) -> list[str]:
        try:
            df = fdr.StockListing("KOSPI")
            df = df[df["Code"].apply(is_valid_code)]
            top_n = int(os.getenv("KOSPI_SCAN_TOP_N", "0") or 0)
            if top_n > 0 and 'Marcap' in df.columns:
                df = df.nlargest(top_n, 'Marcap')
            return df["Code"].tolist()
        except Exception as e:
            logger.error(f"KOSPI 종목 조회 실패: {e}")
            return []

    def get_kosdaq_symbols(self, top_n: int = KOSDAQ_TOP_N) -> list[str]:
        """시가총액 상위 N개 코스닥 종목 (정상 코드만)"""
        try:
            df = fdr.StockListing("KOSDAQ")
            df = df[df["Code"].apply(is_valid_code)]
            return df.nlargest(top_n, 'Marcap')["Code"].tolist()
        except Exception as e:
            logger.error(f"KOSDAQ 종목 조회 실패: {e}")
            return []

    def _candidate_score_after_heat_filter(self, symbol: str, data: dict, score: float) -> tuple[float | None, str]:
        close = float(data.get('close', 0) or 0)
        close_5 = float(data.get('close_5d_ago', close) or close)
        close_20 = float(data.get('close_20d_ago', close) or close)
        if close <= 0 or close_5 <= 0 or close_20 <= 0:
            return score, ""
        return5 = close / close_5 - 1
        return20 = close / close_20 - 1
        rsi = float(data.get('rsi', 50) or 50)
        penalty = 0.0
        reasons: list[str] = []
        if return20 >= float(os.getenv("CANDIDATE_BLOCK_RETURN20", "0.60") or 0.60):
            return None, f"20일 과열 {return20 * 100:.1f}%"
        if return20 >= float(os.getenv("CANDIDATE_LATE_RETURN20", "0.45") or 0.45) and return5 <= 0.08:
            return None, f"상승후 둔화 {return20 * 100:.1f}%/{return5 * 100:.1f}%"
        if return5 >= float(os.getenv("CANDIDATE_BLOCK_RETURN5", "0.25") or 0.25):
            return None, f"5일 급등 {return5 * 100:.1f}%"
        if return5 >= 0.15:
            penalty += 12
            reasons.append(f"5일 +{return5 * 100:.1f}%")
        if return20 >= 0.30:
            penalty += 8
            reasons.append(f"20일 +{return20 * 100:.1f}%")
        if rsi >= 78:
            penalty += 8
            reasons.append(f"RSI {rsi:.1f}")
        adjusted = max(0.0, score - penalty)
        return adjusted, ", ".join(reasons)

    # ── 08:30 아침 스크리닝 ────────────────────────────────────────────────

    def morning_screening(self):
        """코스피 전체 + 코스닥 시총 상위 300 스캔 → 매수후보 코스피 6 + 코스닥 4 선정"""
        if datetime.now(self.KST).weekday() >= 5:
            logger.info("⏸️ 주말 — 아침 스크리닝 건너뜀")
            self.is_market_open = False
            return

        logger.info("=" * 80)
        logger.info(f"🌅 아침 스크리닝 시작 — {datetime.now(self.KST).strftime('%H:%M:%S')}")
        logger.info("=" * 80)

        # 전날 이후 계좌 변화(배당, 수동 주문 등) 반영
        self._sync_portfolio_from_kis()
        self._update_market_condition()

        kospi_syms  = self.get_kospi_symbols()
        kosdaq_syms = self.get_kosdaq_symbols(self.KOSDAQ_TOP_N)

        self.kospi_symbols_set = set(kospi_syms)
        # 코스닥 중 코스피와 중복 제거
        kosdaq_syms = [s for s in kosdaq_syms if s not in self.kospi_symbols_set]
        all_syms    = kospi_syms + kosdaq_syms

        logger.info(
            f"📊 코스피 {len(kospi_syms)}개 + 코스닥 상위 {len(kosdaq_syms)}개 "
            f"= 총 {len(all_syms)}개 스캔"
        )

        t0          = time.time()
        all_results = self.async_client.fetch_all_stocks(all_syms, kospi_set=self.kospi_symbols_set)

        # 섹터 모멘텀 갱신 (10분 캐시)
        self.sector_monitor.update(force=True)

        # 점수 계산 + DB 저장
        logger.info("📈 점수 계산 중...")
        kospi_scores:  dict[str, float] = {}
        kosdaq_scores: dict[str, float] = {}
        price_map:     dict[str, float] = {}
        vb_data_map:   dict[str, dict]  = {}

        for r in all_results:
            if not r['data']:
                continue
            sym    = r['symbol']
            market = 'KOSPI' if sym in self.kospi_symbols_set else 'KOSDAQ'
            self.db.insert_price_data(sym, r['name'], market, r['data'])
            sector_bonus = self.sector_monitor.get_sector_bonus(sym)
            score = self.analyzer.calculate_score(sym, r['data'], sector_bonus=sector_bonus)
            score, heat_reason = self._candidate_score_after_heat_filter(sym, r['data'], score)
            if score is None:
                logger.info(f"  🔥 [{sym}] 후보 제외 — {heat_reason}")
                continue
            if heat_reason:
                logger.info(f"  🌡️ [{sym}] 후보 점수 감점 — {heat_reason} → {score:.1f}")
            price_map[sym] = r['data']['close']
            vb_data_map[sym] = r['data']
            if sym in self.kospi_symbols_set:
                kospi_scores[sym] = score
            else:
                kosdaq_scores[sym] = score

        # 이미 보유 중인 종목은 매수후보에서 제외 (매도 모니터링으로만 추적)
        existing = set(self.position_mgr.portfolio['holdings'].keys())

        def _dedup_preferred(scores: dict[str, float]) -> list[str]:
            """우선주(코드 끝자리 5) 중복 제거 — 보통주가 있으면 우선주 제외."""
            all_codes = set(scores.keys())
            result, seen_base = [], set()
            for sym, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True):
                if sym in existing:
                    continue
                base = sym[:-1] + '0'
                if sym.endswith('5') and base in all_codes:
                    logger.debug(f"  [{sym}] 우선주 제외 — 보통주 {base} 존재")
                    continue
                seen_base.add(sym)
                result.append(sym)
            return result

        top_kospi  = _dedup_preferred(kospi_scores)[:self.KOSPI_COUNT]
        top_kosdaq = _dedup_preferred(kosdaq_scores)[:self.KOSDAQ_COUNT]

        # 외국계 순매수 실데이터로 top 후보 CBD 갱신 (프록시 → 실거래 대체)
        top_candidates = top_kospi + top_kosdaq
        for _sym in top_candidates:
            if _sym not in vb_data_map:
                continue
            try:
                flow = self.kis_client.get_foreign_net_buying(_sym, lookback=5)
                if flow:
                    real_cbd = 0
                    for f in reversed(flow):
                        if f.get('foreigner_net', 0) > 0:
                            real_cbd += 1
                        else:
                            break
                    proxy_cbd = vb_data_map[_sym].get('consecutive_buy_days', 0)
                    vb_data_map[_sym]['consecutive_buy_days'] = real_cbd
                    # 실데이터로 점수 재계산
                    _sb = self.sector_monitor.get_sector_bonus(_sym)
                    new_sc = self.analyzer.calculate_score(_sym, vb_data_map[_sym], sector_bonus=_sb)
                    if _sym in kospi_scores:
                        kospi_scores[_sym] = new_sc
                    else:
                        kosdaq_scores[_sym] = new_sc
                    logger.info(
                        f"  🌍 [{_sym}] 외국계 순매수 {real_cbd}일 "
                        f"(프록시 {proxy_cbd}일 → 실거래) | 점수 {new_sc:.1f}"
                    )
            except Exception as _fe:
                logger.debug(f"외국계 CBD 갱신 실패 ({_sym}): {_fe}")

        # 외국계 실데이터 반영 후 최종 top10 재정렬
        top_kospi  = _dedup_preferred(kospi_scores)[:self.KOSPI_COUNT]
        top_kosdaq = _dedup_preferred(kosdaq_scores)[:self.KOSDAQ_COUNT]

        self.top_10_symbols = top_kospi + top_kosdaq
        self._save_top10(self.top_10_symbols)

        # 풀 비교용 섀도 후보: 실거래 10개 이후 상위 10개 (11~20위)
        _trading_set = set(self.top_10_symbols)
        _shadow_kospi  = [s for s in _dedup_preferred(kospi_scores)  if s not in _trading_set][:self.KOSPI_COUNT]
        _shadow_kosdaq = [s for s in _dedup_preferred(kosdaq_scores) if s not in _trading_set][:self.KOSDAQ_COUNT]
        self.shadow_candidates = _shadow_kospi + _shadow_kosdaq
        if self.shadow_candidates:
            logger.info(f"  🔬 [풀비교] 섀도후보 {len(self.shadow_candidates)}개 (11~20위 추적): {self.shadow_candidates}")

        # 변동성 돌파 전략 AI 사전 필터
        today_str = datetime.now(self.KST).strftime('%Y-%m-%d')
        all_scores_full = {**kospi_scores, **kosdaq_scores}
        # VB 후보: 전체 스캔 종목 중 SMA20 근접(+12% 이내) + 점수 60 이상인 상위 20종목
        _vb_sma_max = float(os.getenv("VB_SMA_MAX_GAP_PCT", "12.0"))
        _vb_min_sc  = float(os.getenv("VB_MIN_SCORE", "60.0"))
        _vb_stable: list[tuple[str, float]] = []
        for _sym, _d in vb_data_map.items():
            if _sym in existing:
                continue
            _close = float(_d.get('close', 0) or 0)
            _sma20 = float(_d.get('sma_20', _close) or _close)
            if _close <= 0 or _sma20 <= 0:
                continue
            _gap = (_close / _sma20 - 1) * 100
            _sc  = all_scores_full.get(_sym, 0)
            if _gap <= _vb_sma_max and _sc >= _vb_min_sc:
                _vb_stable.append((_sym, _sc))
        _vb_stable.sort(key=lambda x: x[1], reverse=True)
        vb_input = [s for s, _ in _vb_stable[:20]] or self.top_10_symbols
        logger.info(f"  [VB] 풀 구성: SMA20+{_vb_sma_max:.0f}% 이내+점수{_vb_min_sc:.0f}+ → {len(vb_input)}종목")
        self.vb_candidates    = self._vb_ai_prefilter(vb_input, all_scores_full, vb_data_map)
        self.vb_candidate_date = today_str
        self.vb_entered_today  = set()
        self.vb_split_sold     = set()
        self._opening_validated = False
        if self.vb_candidates:
            logger.info(f"  🎯 [VB] AI 승인 {len(self.vb_candidates)}종목: {list(self.vb_candidates.keys())}")

        # 재선정 풀 구성: 점수 상위 N종목 (섹터 무관)
        pool_size = int(os.getenv("RESCAN_POOL_SIZE", "150"))
        all_scores = {**kospi_scores, **kosdaq_scores}
        self.rescan_pool = [
            sym for sym, _ in sorted(all_scores.items(), key=lambda x: x[1], reverse=True)[:pool_size]
        ]

        elapsed = time.time() - t0
        logger.info(f"\n✅ 스크리닝 완료! ({elapsed:.1f}초) | 재선정 풀 {len(self.rescan_pool)}종목 (점수 상위)\n")

        logger.info(f"  ▶ 코스피 매수후보 ({len(top_kospi)}개)")
        for i, sym in enumerate(top_kospi, 1):
            sector = self.sector_monitor.get_sector_name(sym)
            bonus  = self.sector_monitor.get_sector_bonus(sym)
            bonus_str = f" [{sector} {bonus:+.2f}]" if sector else ""
            logger.info(f"    {i}. {sym:6s} | 점수: {kospi_scores[sym]:5.1f} | ₩{price_map.get(sym,0):,.0f}{bonus_str}")

        logger.info(f"  ▶ 코스닥 매수후보 ({len(top_kosdaq)}개)")
        for i, sym in enumerate(top_kosdaq, 1):
            sector = self.sector_monitor.get_sector_name(sym)
            bonus  = self.sector_monitor.get_sector_bonus(sym)
            bonus_str = f" [{sector} {bonus:+.2f}]" if sector else ""
            logger.info(f"    {i}. {sym:6s} | 점수: {kosdaq_scores[sym]:5.1f} | ₩{price_map.get(sym,0):,.0f}{bonus_str}")

        if existing:
            logger.info(f"\n  📌 보유 종목 → 매도 타이밍 모니터링: {sorted(existing)}")

        logger.info("=" * 80)
        logger.info("🔍 09:00 ~ 15:30 모니터링 대기")
        logger.info("=" * 80)

        # 아침 스크리닝 완료 텔레그램 알림
        try:
            mc = self._market_condition
            market_str = "✅ 매수 가능" if mc.get('selective_ok') or mc.get('strong_trend') else "⛔ 매수 차단"
            kospi_lines = "\n".join(
                f"  {i}. {sym} | {kospi_scores[sym]:.0f}점 | ₩{price_map.get(sym,0):,.0f}"
                for i, sym in enumerate(top_kospi, 1)
            )
            kosdaq_lines = "\n".join(
                f"  {i}. {sym} | {kosdaq_scores[sym]:.0f}점 | ₩{price_map.get(sym,0):,.0f}"
                for i, sym in enumerate(top_kosdaq, 1)
            )
            msg = (
                f"🌅 장 시작\n"
                f"KOSPI {mc.get('kospi',0):,.0f} ({mc.get('trend_gap_pct',0):+.1f}%) | {market_str}\n"
                f"매수후보 {len(top_kospi)}개(KOSPI) + {len(top_kosdaq)}개(KOSDAQ)"
            )
            self.reporter.send_message(msg)
        except Exception as e:
            logger.debug(f"아침 스크리닝 텔레그램 발송 실패: {e}")

        self.is_market_open = True
        self.last_rescreen_time = time.time()

    # ── 장중 1시간 재선정 ──────────────────────────────────────────────────

    def hourly_rescreen(self):
        """
        장중 재선정 (기본 10분 주기).
        - 아침 스캔에서 선정된 상위 섹터풀 종목만 재스캔 (섹터풀 없으면 전체 폴백)
        - 보유 종목은 항상 감시 유지
        - 기존 후보와 50% 이상 겹치면 전체 교체, 미만이면 점진 교체
        """
        now_str = datetime.now(self.KST).strftime('%H:%M:%S')
        logger.info("=" * 70)

        if self.rescan_pool:
            all_syms = self.rescan_pool
            logger.info(f"🔄 재선정 시작 — {now_str} (점수풀 {len(all_syms)}종목)")
        else:
            kospi_syms  = self.get_kospi_symbols()
            kosdaq_syms = [s for s in self.get_kosdaq_symbols(self.KOSDAQ_TOP_N)
                           if s not in set(kospi_syms)]
            all_syms    = kospi_syms + kosdaq_syms
            logger.info(f"🔄 전체 재선정 시작 (풀 없음) — {now_str} ({len(all_syms)}종목)")

        logger.info("=" * 70)

        t0          = time.time()
        all_results = self.async_client.fetch_all_stocks(all_syms, kospi_set=self.kospi_symbols_set)

        # 섹터 모멘텀 갱신 (10분 캐시 — 변경 없으면 스킵)
        self.sector_monitor.update()

        kospi_scores:  dict[str, float] = {}
        kosdaq_scores: dict[str, float] = {}
        for r in all_results:
            if not r['data']:
                continue
            sym   = r['symbol']
            sector_bonus = self.sector_monitor.get_sector_bonus(sym)
            score = self.analyzer.calculate_score(sym, r['data'], sector_bonus=sector_bonus)
            score, heat_reason = self._candidate_score_after_heat_filter(sym, r['data'], score)
            if score is None:
                logger.info(f"  🔥 [{sym}] 재선정 제외 — {heat_reason}")
                continue
            if heat_reason:
                logger.info(f"  🌡️ [{sym}] 재선정 점수 감점 — {heat_reason} → {score:.1f}")
            self.db.insert_price_data(sym, sym, 'KOSPI' if sym in self.kospi_symbols_set else 'KOSDAQ', r['data'])
            if sym in self.kospi_symbols_set:
                kospi_scores[sym] = score
            else:
                kosdaq_scores[sym] = score

        existing = set(self.position_mgr.portfolio['holdings'].keys())

        def _dedup_pref(scores: dict[str, float]) -> list[str]:
            all_codes = set(scores.keys())
            result = []
            for sym, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True):
                if sym in existing:
                    continue
                if sym.endswith('5') and (sym[:-1] + '0') in all_codes:
                    continue
                result.append(sym)
            return result

        new_kospi  = _dedup_pref(kospi_scores)[:self.KOSPI_COUNT]
        new_kosdaq = _dedup_pref(kosdaq_scores)[:self.KOSDAQ_COUNT]
        new_candidates = new_kospi + new_kosdaq

        # 풀 비교 섀도 후보 갱신
        _tr_set = set(new_candidates)
        self.shadow_candidates = (
            [s for s in _dedup_pref(kospi_scores)  if s not in _tr_set][:self.KOSPI_COUNT]
            + [s for s in _dedup_pref(kosdaq_scores) if s not in _tr_set][:self.KOSDAQ_COUNT]
        )

        # 연속성 필터
        current_set = set(self.top_10_symbols)
        new_set     = set(new_candidates)
        overlap     = len(current_set & new_set)
        threshold   = max(1, len(self.top_10_symbols) // 2)

        if not self.top_10_symbols or overlap >= threshold:
            self.top_10_symbols = new_candidates
            mode = "전체 교체"
        else:
            kept  = [s for s in self.top_10_symbols if s in new_set][: self.KOSPI_COUNT + self.KOSDAQ_COUNT - 2]
            added = [s for s in new_candidates if s not in current_set][: (self.KOSPI_COUNT + self.KOSDAQ_COUNT) - len(kept)]
            self.top_10_symbols = kept + added
            mode = f"점진 교체 (유지 {len(kept)}개 + 신규 {len(added)}개)"

        self._save_top10(self.top_10_symbols)
        self.last_rescreen_time = time.time()

        added_list   = [s for s in new_candidates if s not in current_set]
        removed_list = [s for s in current_set    if s not in new_set]

        logger.info(f"✅ 재선정 완료 [{mode}] ({time.time()-t0:.1f}초)")
        logger.info(f"   매수후보: {self.top_10_symbols}")
        if added_list:
            logger.info(f"   ➕ 신규: {added_list}")
        if removed_list:
            logger.info(f"   ➖ 제외: {removed_list}")
        if existing:
            logger.info(f"   📌 보유(계속 감시): {sorted(existing)}")
        logger.info("=" * 70)

        # 시장 변동성 상태 갱신
        self._update_market_condition()

    # ── 09:00~15:30 장중 모니터링 ─────────────────────────────────────────

    def _update_max_holdings(self) -> None:
        """계좌 규모에 따라 MAX_HOLDINGS를 동적으로 조정."""
        if os.getenv("KOSPI_V43_LIVE_FILTER", "true").lower() == "true":
            prev = self.MAX_HOLDINGS
            self.MAX_HOLDINGS = int(os.getenv("V43_MAX_POSITIONS", "8") or 8)
            if self.MAX_HOLDINGS != prev:
                logger.info(f"  📏 v4.3 최대종목 고정: {prev}→{self.MAX_HOLDINGS}개")
            return

        portfolio = self.position_mgr.portfolio
        account_value = float(portfolio.get('total_value') or 0)
        if account_value <= 0:
            holdings_value = sum(
                h.get('amount', h.get('price', 0) * h.get('quantity', 0))
                for h in portfolio.get('holdings', {}).values()
            )
            account_value = float(portfolio.get('cash', 0)) + holdings_value
        if account_value <= 0:
            return

        prev = self.MAX_HOLDINGS
        if account_value < 3_000_000:
            self.MAX_HOLDINGS = int(os.getenv("MAX_HOLDINGS_SMALL",  "3") or 3)   # ~300만: 3종목
        elif account_value < 10_000_000:
            self.MAX_HOLDINGS = int(os.getenv("MAX_HOLDINGS_MID",    "4") or 4)   # 300만~1000만: 4종목
        else:
            self.MAX_HOLDINGS = int(os.getenv("MAX_STRATEGY_POSITIONS", "5") or 5)  # 1000만+: 5종목 이상
        if self.MAX_HOLDINGS != prev:
            logger.info(
                f"  📏 계좌규모별 최대종목 조정: {prev}→{self.MAX_HOLDINGS}개"
                f" (계좌 ₩{account_value/10000:.0f}만)"
            )

    def realtime_monitoring(self):
        """
        매수후보(top_10_symbols) → 매수 신호만 체크
        보유 종목(holdings)      → 매도 타이밍만 체크
        """
        if not self.is_market_open:
            return

        self._update_max_holdings()  # 계좌 규모에 따라 최대 종목 수 갱신

        holdings     = set(self.position_mgr.portfolio['holdings'].keys())
        watch_set    = set(self.top_10_symbols)
        all_watch    = list(watch_set | holdings)

        if not all_watch:
            return

        now_str = datetime.now(self.KST).strftime('%H:%M:%S')
        now_hm = int(datetime.now(self.KST).strftime('%H%M'))
        buy_cutoff_hm = int(os.getenv("NEW_BUY_CUTOFF_HHMM", "1430") or 1430)
        buy_start_hm  = int(os.getenv("BUY_START_HHMM", "0900") or 900)  # 장 시작부터 v4.3 필터로 감시
        logger.info(
            f"[{now_str}] 🔍 매수후보 {len(watch_set)}개 | 보유 {len(holdings)}개 모니터링"
        )

        try:
            import time as _t
            _now = _t.time()

            # ── 일봉 캐시 확인: TTL 내면 KIS API 재호출 생략 ──────────────────
            stale_syms = [s for s in all_watch
                          if _now - self._ohlcv_cache.get(s, (None, 0))[1] > self._ohlcv_cache_ttl]
            if stale_syms:
                fresh = self.async_client.fetch_all_stocks(stale_syms, kospi_set=self.kospi_symbols_set)
                for r in fresh:
                    if r.get('data'):
                        self._ohlcv_cache[r['symbol']] = (r['data'], _now)
                cached_miss = len(all_watch) - len(stale_syms)
                logger.info(f"  📦 일봉 캐시: {cached_miss}개 재사용 / {len(stale_syms)}개 갱신")
            else:
                logger.info(f"  📦 일봉 캐시: {len(all_watch)}개 전체 재사용 (TTL {self._ohlcv_cache_ttl}s)")
            results = [{'symbol': s, 'name': s, 'data': self._ohlcv_cache.get(s, (None, 0))[0], 'error': None}
                       for s in all_watch]

            # 보유 종목은 KIS API 개별 조회로 지표 교체 (yfinance 데이터 오래됨)
            kis_data: dict = {}
            for sym in holdings:
                cached_ts = self._ohlcv_cache.get(sym, (None, 0))[1]
                if _now - cached_ts > self._ohlcv_cache_ttl:
                    try:
                        d = self.kis_client.get_daily_ohlcv(sym)
                        if d:
                            kis_data[sym] = d
                            self._ohlcv_cache[sym] = (d, _now)
                    except Exception:
                        pass
                else:
                    cached_d = self._ohlcv_cache.get(sym, (None, 0))[0]
                    if cached_d:
                        kis_data[sym] = cached_d
            if kis_data:
                logger.info(f"  📡 보유종목 KIS 지표 갱신: {len(kis_data)}/{len(holdings)}개")

            # 현재가 KIS API 실시간 조회 (매 사이클마다 실시간 조회)
            live_prices = self.kis_client.get_current_prices(all_watch)
            if live_prices:
                logger.info(f"  📡 실시간 현재가 수신: {len(live_prices)}/{len(all_watch)}개")

            # ── 1단계: 신호 스캔 (주문 없이) ────────────────────────────────
            sell_queue: list[tuple] = []   # (sym, qty, price, reason, meta)
            buy_queue:  list[tuple] = []   # (sym, price, price_data)

            for r in results:
                sym        = r['symbol']
                # 보유 종목이면 KIS API 기반 지표 우선 사용
                price_data = kis_data.get(sym) or r['data']
                if not price_data:
                    continue

                # KIS API 현재가로 close 교체
                if sym in live_prices:
                    price_data = {**price_data, 'close': live_prices[sym]}

                holding_qty = self.position_mgr.get_holding_quantity(sym)
                cur_price   = price_data['close']

                # ── 보유 중: 매도 신호 수집 ──────────────────────────────────
                if holding_qty > 0:
                    holding   = self.position_mgr.portfolio['holdings'].get(sym, {})
                    buy_price = holding.get('price', 0)
                    high_p    = holding.get('highest_price', cur_price)
                    profit    = ((cur_price - buy_price) / buy_price * 100) if buy_price else 0

                    self.position_mgr.update_highest_price(sym, cur_price)
                    high_p = max(high_p, cur_price)
                    max_profit = ((high_p - buy_price) / buy_price * 100) if buy_price else 0

                    # 브레이크이븐 스톱: 수익 임계값 달성 시 손절선을 매수가+버퍼로 상향
                    if buy_price > 0 and holding.get('source') != 'vb':
                        _be_thresh = float(os.getenv("BREAKEVEN_THRESHOLD_PCT", "3.0") or 3.0)
                        _be_buf    = float(os.getenv("BREAKEVEN_BUFFER_PCT",    "0.3") or 0.3)
                        if profit >= _be_thresh:
                            _be_stop = buy_price * (1 + _be_buf / 100)
                            _cur_stop = float(holding.get('stop_loss', 0) or 0)
                            if _be_stop > _cur_stop + 1:
                                self.position_mgr.update_position_metadata(sym, stop_loss=_be_stop)
                                logger.info(
                                    f"  🛡️ [{sym}] 브레이크이븐 스톱: ₩{_be_stop:,.0f} "
                                    f"(수익 {profit:.1f}% → 매수가+{_be_buf:.1f}%)"
                                )

                    # ── VB 포지션 전용 조기 청산 (14:50 분할 + 손절) ────────
                    if holding.get('source') == 'vb':
                        vb_eod_hm   = int(os.getenv("VB_EOD_CLOSE_HHMM", "1450") or 1450)
                        vb_stop_price = float(holding.get('vb_stop_price', 0) or 0)
                        if now_hm >= vb_eod_hm:
                            if now_hm < 1500 and sym not in self.vb_split_sold:
                                # 1차: 14:50~14:59 절반 매도 (ceil → 1주도 안전하게 처리)
                                sell_qty = math.ceil(holding_qty / 2)
                                sell_queue.append((sym, sell_qty, cur_price,
                                    f"VB분할1차({now_hm//100:02d}:{now_hm%100:02d})", None))
                                self.vb_split_sold.add(sym)
                            elif now_hm >= 1500 and holding_qty > 0:
                                # 2차: 15:00+ 잔량 전량 청산 (0주 방어)
                                sell_queue.append((sym, holding_qty, cur_price,
                                    f"VB분할2차({now_hm//100:02d}:{now_hm%100:02d})", None))
                            continue
                        if vb_stop_price > 0 and cur_price <= vb_stop_price:
                            sell_queue.append((sym, holding_qty, cur_price,
                                f"VB손절(₩{vb_stop_price:,.0f})", None))
                            continue

                    market_tag = 'KS' if sym in self.kospi_symbols_set else 'KQ'
                    logger.info(
                        f"  [{sym}/{market_tag}] 보유 {holding_qty}주 | "
                        f"수익 {profit:+.2f}% | 최고가 ₩{high_p:,.0f}"
                    )

                    atr      = price_data.get('atr', cur_price * 0.02)
                    ts_price = self.risk_mgr.trailing_stop(cur_price, high_p, atr, multiplier=2.0)
                    peak_pullback = ((high_p - cur_price) / high_p * 100) if high_p else 0.0
                    minute_df = None
                    intraday_range_pct = (
                        (price_data.get('high', cur_price) - price_data.get('low', cur_price))
                        / cur_price * 100
                        if cur_price else 0.0
                    )
                    atr_pct = (atr / cur_price * 100) if cur_price else 0.0
                    try:
                        minute_df = self.kis_client.get_intraday_ohlcv(sym, interval='1m', lookback=30)
                        if minute_df is not None and len(minute_df) >= 5 and cur_price:
                            minute_high_30 = float(minute_df['high'].astype(float).max())
                            minute_low_30 = float(minute_df['low'].astype(float).min())
                            minute_range_pct = (minute_high_30 - minute_low_30) / cur_price * 100
                            if minute_range_pct > 0:
                                intraday_range_pct = minute_range_pct
                    except Exception as e:
                        logger.debug(f"  [{sym}] 1분봉 변동성 확인 실패: {e}")

                    five_min_df = None
                    try:
                        five_min_df = self.kis_client.get_intraday_ohlcv(sym, interval='5m', lookback=12)
                    except Exception as e:
                        logger.debug(f"  [{sym}] 5분봉 확인 실패: {e}")

                    partial_enabled = os.getenv("PARTIAL_TAKE_PROFIT_ENABLED", "true").lower() == "true"
                    partial_done = bool(holding.get('partial_take_profit_done'))
                    min_partial_qty = int(os.getenv("PARTIAL_TAKE_PROFIT_MIN_QTY", "1") or 1)
                    base_profile = holding.get('strategy_profile') or 'SWING'
                    minute_state = self._minute_state(minute_df, lookback=10)
                    five_min_state = self._minute_state(five_min_df, lookback=8)
                    effective_profile, profile_reason = self._effective_trade_profile(
                        base_profile,
                        minute_state,
                        five_min_state,
                        intraday_range_pct,
                        atr_pct,
                        profit,
                    )
                    settings = self._profile_exit_settings(effective_profile)
                    partial_profit_pct = settings['partial_profit_pct']
                    spike_profit_pct = settings['spike_profit_pct']
                    pullback_pct = settings['pullback_pct']
                    partial_ratio = settings['partial_ratio']
                    if holding.get('effective_profile') != effective_profile:
                        self.position_mgr.update_position_metadata(
                            sym,
                            effective_profile=effective_profile,
                            effective_profile_reason=profile_reason,
                            effective_profile_time=datetime.now(self.KST).isoformat(),
                        )
                        logger.info(
                            f"  🧭 [{sym}] 전략 프로필: {base_profile} → {effective_profile} "
                            f"({profile_reason})"
                        )

                    target_action, target_meta = self._target_plan_action(
                        sym,
                        holding,
                        holding_qty,
                        cur_price,
                        price_data,
                        minute_state,
                        five_min_state,
                    )
                    if target_action == "pass" and target_meta:
                        events = dict(holding.get('target_stage_events') or {})
                        events[target_meta['stage']] = {
                            'status': target_meta['status'],
                            'time': datetime.now(self.KST).isoformat(),
                            'price': cur_price,
                            'detail': target_meta['detail'],
                        }
                        self.position_mgr.update_position_metadata(
                            sym,
                            target_stage_events=events,
                            last_target_action=(
                                f"{target_meta['stage']}차 목표 강한 돌파 — "
                                f"보유 지속 ({target_meta['detail']})"
                            ),
                        )
                        logger.info(
                            f"  🎯 [{sym}] {target_meta['stage']}차 목표 강한 돌파 — "
                            f"보유 지속 ({target_meta['detail']})"
                        )
                    elif target_action == "sell" and target_meta:
                        sell_queue.append(
                            (sym, target_meta['qty'], cur_price, target_meta['reason'], {
                                'target_stage': target_meta['stage'],
                                'target_status': target_meta['status'],
                                'target_detail': target_meta['detail'],
                            })
                        )
                        continue

                    partial_reason = None
                    minute_drop = float(minute_state.get('pullback_pct', 0.0) or 0.0)
                    minute_reversal = bool(minute_state.get('reversal_down'))
                    five_min_reversal = bool(five_min_state.get('reversal_down'))

                    if partial_enabled and not partial_done and holding_qty > min_partial_qty:
                        if profit >= spike_profit_pct:
                            partial_reason = (
                                f"부분익절:{effective_profile}:급등수익보호"
                                f"(수익 {profit:.1f}%, 일중폭 {intraday_range_pct:.1f}%, ATR {atr_pct:.1f}%)"
                            )
                        elif max_profit >= partial_profit_pct and minute_reversal:
                            partial_reason = (
                                f"부분익절:{effective_profile}:1분봉꺾임"
                                f"({minute_drop:.1f}%↓, 수익 {profit:.1f}%/최고 {max_profit:.1f}%, 일중폭 {intraday_range_pct:.1f}%)"
                            )
                        elif max_profit >= partial_profit_pct and five_min_reversal:
                            partial_reason = (
                                f"부분익절:{effective_profile}:5분봉꺾임"
                                f"(수익 {profit:.1f}%/최고 {max_profit:.1f}%, 일중폭 {intraday_range_pct:.1f}%)"
                            )
                        elif max_profit >= partial_profit_pct and peak_pullback >= pullback_pct:
                            partial_reason = (
                                f"부분익절:{effective_profile}:고점대비하락"
                                f"({peak_pullback:.1f}%↓, 수익 {profit:.1f}%/최고 {max_profit:.1f}%, 일중폭 {intraday_range_pct:.1f}%)"
                            )

                    if partial_reason:
                        partial_qty = max(min_partial_qty, int(holding_qty * partial_ratio))
                        partial_qty = min(partial_qty, holding_qty - 1)
                        if partial_qty > 0:
                            sell_queue.append((sym, partial_qty, cur_price, partial_reason, None))
                            continue

                    # 하드 손절: -3% 이하
                    # 트레일링 스톱: 수익 0.5% 이상일 때 발동 (조기 수익 보호)
                    trailing_triggered = cur_price < ts_price and profit >= 0.5
                    if profit <= self.STOP_LOSS_PCT or trailing_triggered:
                        reason = "하드손절" if profit <= self.STOP_LOSS_PCT else f"트레일링스톱(₩{ts_price:,.0f})"
                        sell_queue.append((sym, holding_qty, cur_price, reason, None))
                        continue

                    # AI 매도 판단 — 보조 경고 전용 (실제 주문 결정 아님)
                    # 기계적 매도 조건(손절/트레일링/목표가)이 먼저이고,
                    # AI는 수익 2~15% 구간에서 판단 근거를 텔레그램으로만 알림
                    if 2.0 <= profit <= 15.0:
                        ai_action, ai_reason = self._ai_sell_judgment(
                            symbol=sym,
                            profit=profit,
                            max_profit=max_profit,
                            peak_pullback=peak_pullback,
                            minute_state=minute_state,
                            five_min_state=five_min_state,
                            effective_profile=effective_profile,
                            price_data=price_data,
                        )
                        if ai_action in ("FULL_SELL", "PARTIAL_SELL"):
                            logger.info(
                                f"  🤖 [{sym}] AI 매도 경고 ({ai_action}) — "
                                f"수익 {profit:.1f}% | {ai_reason} "
                                f"[보조 참고, 실주문 아님]"
                            )
                            pass  # AI 매도 경고 텔레그램 생략 (참고용 알림 제거)

                    trend_score = self.exit_analyzer.assess_trend_strength(sym, price_data)
                    should_sell, exit_reason, _ = self.exit_analyzer.calculate_dynamic_take_profit(
                        sym, buy_price, cur_price, trend_score, atr=price_data.get('atr', 0)
                    )
                    if should_sell:
                        sell_queue.append((sym, holding_qty, cur_price, f"익절:{exit_reason}", None))
                    elif trend_score >= 80:
                        is_break, break_reason = self.exit_analyzer.detect_trend_breakage(
                            sym, price_data, holding, profit
                        )
                        if is_break:
                            sell_queue.append((sym, holding_qty, cur_price, f"추세꺾임:{break_reason}", None))
                    else:
                        should_close_eod, eod_reason = self._should_eod_cleanup(
                            now_hm,
                            effective_profile,
                            trend_score,
                            profit,
                            minute_state,
                            five_min_state,
                            price_data,
                        )
                        if should_close_eod:
                            sell_queue.append((sym, holding_qty, cur_price, eod_reason, None))

                # ── 매수후보: 매수 신호 수집 ─────────────────────────────────
                elif sym in watch_set:
                    if now_hm < buy_start_hm:
                        logger.debug(f"  [{sym}] 장 시작 안정화 대기 (09:30까지 진입 금지)")
                        continue

                    # 14:30 이후 예외 진입 처리
                    late_buy_enabled = os.getenv("LATE_BUY_ENABLED", "false").lower() == "true"
                    late_buy_until   = int(os.getenv("LATE_BUY_UNTIL_HHMM", "1500") or 1500)
                    late_buy_score   = float(os.getenv("LATE_BUY_MIN_SCORE", "90") or 90)
                    late_buy_pos_pct = float(os.getenv("LATE_BUY_MAX_POSITION_PCT", "0.5") or 0.5)
                    is_late_window   = late_buy_enabled and buy_cutoff_hm <= now_hm < late_buy_until

                    if now_hm >= buy_cutoff_hm:
                        if not is_late_window:
                            logger.debug(f"  [{sym}] 신규 매수 컷오프 이후 — 진입 보류")
                            continue
                        # 늦은 진입 — 점수 사전 체크 (고비용 신호 계산 전에 필터)
                        pre_score = self.analyzer.calculate_score(sym, price_data,
                            self.sector_monitor.get_sector_bonus(sym))
                        if pre_score < late_buy_score:
                            logger.debug(
                                f"  [{sym}] 늦은 진입 점수 미달 ({pre_score:.1f} < {late_buy_score:.0f}) — 스킵"
                            )
                            continue
                    # 보유 상한 도달 시 매수 신호 자체를 무시
                    strategy_holdings = self.position_mgr.count_strategy_positions()
                    if strategy_holdings >= self.MAX_HOLDINGS:
                        logger.debug(
                            f"  [{sym}] 매수 스킵 — 전략 포지션 상한 "
                            f"({strategy_holdings}/{self.MAX_HOLDINGS})"
                        )
                        continue
                    cooldown_until = self.buy_fail_cooldowns.get(sym, 0)
                    if cooldown_until > time.time():
                        remain_min = int((cooldown_until - time.time()) / 60)
                        logger.debug(f"  [{sym}] 매수 실패 쿨다운 중 ({remain_min}분 남음)")
                        continue
                    reentry_until = self.sell_reentry_cooldowns.get(sym, 0)
                    if reentry_until > time.time():
                        try:
                            re_minute_df = self.kis_client.get_intraday_ohlcv(sym, interval='1m', lookback=10)
                        except Exception:
                            re_minute_df = None
                        allow_reentry, reentry_reason = self._allow_profit_harvest_reentry(
                            sym, cur_price, re_minute_df
                        )
                        if allow_reentry:
                            self.sell_reentry_cooldowns.pop(sym, None)
                            self._save_sell_reentry_cooldowns()
                            logger.warning(f"  🔁 [{sym}] 수익채굴 재진입 허용 — {reentry_reason}")
                        else:
                            remain_min = int((reentry_until - time.time()) / 60)
                            logger.info(
                                f"  ⏸️ [{sym}] 매도 후 재진입 쿨다운 중 ({remain_min}분 남음) — "
                                f"{reentry_reason}"
                            )
                            continue
                    strong_market = bool(self._market_condition.get('strong_trend', False))
                    selective     = bool(self._market_condition.get('selective_ok', False))
                    signal, sig_reason = self.analyzer.detect_signal(
                        sym, price_data,
                        strong_market=strong_market,
                        selective=selective,
                    )
                    if signal == 'HOLD':
                        _score_for_log = self.analyzer.calculate_score(sym, price_data)
                        self._log_rejection(sym, _score_for_log, sig_reason,
                                            self._market_condition.get('market_mode','?'),
                                            pool_rank=self.top_10_symbols.index(sym) + 1
                                            if sym in self.top_10_symbols else 0,
                                            reject_price=float(price_data.get('close', 0) or 0))
                    if signal == 'BUY':
                        score = self.analyzer.calculate_score(sym, price_data)
                        # score_norm: 0~100 → 0~1 정규화
                        # IntegratedSignal 기준과 동일: ≥0.75=STRONG_BUY / ≥0.65=BUY / ≥0.55=WEAK_BUY
                        score_norm = score / 100.0

                        # 진입 시간대 가중치: 갭 노이즈·유동성 저하 구간에서 최소 점수 상향
                        _now_hm = int(datetime.now(self.KST).strftime('%H%M'))
                        _open_noise_end  = int(os.getenv("BUY_OPEN_NOISE_END_HHMM",  "1000") or 1000)
                        _golden_end      = int(os.getenv("BUY_GOLDEN_END_HHMM",      "1300") or 1300)
                        _open_noise_score  = float(os.getenv("BUY_OPEN_NOISE_SCORE",  "0.75") or 0.75)
                        _golden_score      = float(os.getenv("BUY_GOLDEN_SCORE",       "0") or 0)
                        _late_window_score = float(os.getenv("BUY_LATE_WINDOW_SCORE", "0.70") or 0.70)
                        if _now_hm < _open_noise_end:
                            _time_min_score = _open_noise_score
                            _time_zone = f"갭노이즈({_open_noise_score:.2f})"
                        elif _now_hm < _golden_end:
                            _time_min_score = _golden_score
                            _time_zone = ""
                        else:
                            _time_min_score = _late_window_score
                            _time_zone = f"후반({_late_window_score:.2f})"
                        if _time_min_score > 0 and score_norm < _time_min_score:
                            logger.info(
                                f"  🕐 [{sym}] 시간대 필터 — {_time_zone} 점수 부족 "
                                f"{score_norm:.2f}<{_time_min_score:.2f}"
                            )
                            continue

                        # 시장 변동성 필터 — KOSPI 추세 하락 또는 변동성 과열 시 매수 억제
                        mc = self._market_condition
                        if not mc.get('trend_ok') or not mc.get('volatility_ok'):
                            override_ok, override_reason = self._allow_market_filter_override(
                                sym, price_data, score
                            )
                            if override_ok:
                                logger.warning(f"⚡ [{sym}] 시장 필터 예외 통과 — {override_reason}")
                            else:
                                parts = []
                                if not mc.get('trend_ok'):
                                    parts.append(
                                        f"KOSPI↓SMA20 "
                                        f"({mc.get('kospi',0):,.0f}<{mc.get('sma20',0):,.0f})"
                                    )
                                if not mc.get('volatility_ok'):
                                    parts.append(
                                        f"변동성 {mc.get('volatility_pct',0):.1f}%"
                                        f">{mc.get('threshold',1.5)}%"
                                    )
                                logger.info(
                                    f"  ⛔ [{sym}] 시장 조건 불충족 — 매수 억제: "
                                    f"{', '.join(parts)} | 점수 {score:.1f} | {override_reason}"
                                )
                                continue

                        if not mc.get('trend_ok') or not mc.get('volatility_ok'):
                            parts = []
                            if not mc.get('trend_ok'):
                                parts.append(
                                    f"KOSPI↓SMA20 "
                                    f"({mc.get('kospi',0):,.0f}<{mc.get('sma20',0):,.0f})"
                                )
                            if not mc.get('volatility_ok'):
                                parts.append(
                                    f"변동성 {mc.get('volatility_pct',0):.1f}%"
                                    f">{mc.get('threshold',1.5)}%"
                                )
                            logger.info(f"  ℹ️ [{sym}] 시장 조건 참고: {', '.join(parts)} | 점수 {score:.1f}")

                        # 1분봉 확인: 단기 역전·거래량 소멸 시 진입 보류
                        min_ok, min_reason = self._buy_minute_confirm(sym, cur_price)
                        if not min_ok:
                            logger.info(f"  ⏸️ [{sym}] 1분봉 확인 실패 — {min_reason} (다음 주기 재확인)")
                            continue

                        # 섹터 분산: 동일 섹터 포지션 상한
                        _max_same_sector = int(os.getenv("MAX_SAME_SECTOR_POSITIONS", "2") or 2)
                        _sym_sector = self.sector_monitor.get_sector_name(sym) or ''
                        if _sym_sector:
                            _holdings = self.position_mgr.portfolio['holdings']
                            _same_cnt = sum(
                                1 for _h in _holdings
                                if (self.sector_monitor.get_sector_name(_h) or '') == _sym_sector
                            )
                            if _same_cnt >= _max_same_sector:
                                logger.info(
                                    f"  🔀 [{sym}] 섹터 집중 차단 — "
                                    f"{_sym_sector} 이미 {_same_cnt}개 보유 (상한 {_max_same_sector}개)"
                                )
                                continue

                        market_tag = 'KS' if sym in self.kospi_symbols_set else 'KQ'
                        logger.warning(f"⚡ [{sym}/{market_tag}] 🟢 매수 신호! ₩{cur_price:,.0f} | 점수 {score:.1f}")
                        buy_queue.append((sym, cur_price, price_data))

            # ── 1.45단계: 섀도 풀 신호 체크 (풀 비교 — 실거래 없음) ──────────
            if self.shadow_candidates and now_hm < buy_cutoff_hm:
                result_map = {r['symbol']: r for r in results}
                for _rank, _sym in enumerate(self.shadow_candidates, start=len(self.top_10_symbols) + 1):
                    if _sym in {s for s, _, _ in buy_queue}:
                        continue
                    if self.position_mgr.get_holding_quantity(_sym) > 0:
                        continue
                    _r = result_map.get(_sym)
                    if not _r or not _r['data']:
                        continue
                    _pd = _r['data']
                    if _sym in live_prices:
                        _pd = {**_pd, 'close': live_prices[_sym]}
                    _sig, _reason = self.analyzer.detect_signal(
                        _sym, _pd,
                        strong_market=bool(self._market_condition.get('strong_trend')),
                        selective=bool(self._market_condition.get('selective_ok')),
                    )
                    _sc = self.analyzer.calculate_score(_sym, _pd)
                    self._log_pool_paper_signal(_sym, _rank, _sc, _sig, _reason,
                                                self._market_condition.get('market_mode','?'))
                    if _sig == 'BUY':
                        logger.info(
                            f"  🔬 [풀비교] {_sym} (rank {_rank}) — 페이퍼 BUY 신호 "
                            f"점수{_sc:.1f} (실거래 풀 미포함)"
                        )

            # ── 1.5단계: 폭락 반등 예외 매수 스캔 ──────────────────────────
            self._crash_daily_reset()
            crash_max_daily = int(os.getenv("CRASH_MAX_DAILY_BUYS", "1") or 1)
            crash_queued = 0
            crash_remaining = max(0, crash_max_daily - self._crash_buy_count_today)
            crash_buy_until = int(os.getenv("CRASH_BUY_UNTIL_HHMM", "1450") or 1450)
            if crash_remaining <= 0:
                logger.debug(f"폭락반등 당일 한도 소진 ({self._crash_buy_count_today}/{crash_max_daily}건)")
            elif crash_remaining > 0 and buy_start_hm <= now_hm < crash_buy_until:
                is_crash, crash_env_reason = self._is_crash_environment()
                if is_crash:
                    crash_score_thresh = float(os.getenv("CRASH_SCORE_THRESHOLD", "70") or 70)
                    logger.warning(f"🚨 폭락 환경 감지: {crash_env_reason} — 반등 스캔 시작")
                    already_queued = {s for s, _, _ in buy_queue}
                    for r in results:
                        if crash_queued >= crash_remaining:
                            break
                        sym = r['symbol']
                        if sym in already_queued:
                            continue
                        if self.position_mgr.get_holding_quantity(sym) > 0:
                            continue
                        if self._crash_buy_cooldowns.get(sym, 0) > time.time():
                            continue
                        strategy_holdings = self.position_mgr.count_strategy_positions()
                        if strategy_holdings >= self.MAX_HOLDINGS:
                            break
                        price_data_c = kis_data.get(sym) or r['data']
                        if not price_data_c:
                            continue
                        if sym in live_prices:
                            price_data_c = {**price_data_c, 'close': live_prices[sym]}
                        recovery_ok, rec_reason = self._detect_stock_recovery_signal(sym, price_data_c)
                        if not recovery_ok:
                            continue
                        crash_score = self.analyzer.calculate_score(
                            sym, price_data_c, self.sector_monitor.get_sector_bonus(sym)
                        )
                        if crash_score < crash_score_thresh:
                            logger.debug(f"  [{sym}] 폭락반등 점수 미달 ({crash_score:.1f}<{crash_score_thresh})")
                            continue
                        crash_price_data = {**price_data_c, '_crash_mode': True}
                        market_tag = 'KS' if sym in self.kospi_symbols_set else 'KQ'
                        logger.warning(
                            f"🚨 [{sym}/{market_tag}] 폭락 반등 진입 신호! "
                            f"₩{price_data_c['close']:,.0f} | 점수 {crash_score:.1f} | {rec_reason}"
                        )
                        buy_queue.append((sym, price_data_c['close'], crash_price_data))
                        already_queued.add(sym)
                        crash_queued += 1
            elif now_hm >= crash_buy_until:
                logger.debug(f"폭락반등 매수 컷오프 이후 — 진입 보류 ({now_hm}>={crash_buy_until})")

            # ── 1.6단계: 변동성 돌파(VB) 진입 체크 ──────────────────────────
            today_str_vb  = datetime.now(self.KST).strftime('%Y-%m-%d')
            if self.vb_candidate_date != today_str_vb:
                self.vb_entered_today  = set()
                self.vb_candidate_date = today_str_vb

            vb_cutoff_hm = int(os.getenv("VB_BUY_CUTOFF_HHMM", "1430") or 1430)
            if self.vb_candidates and buy_start_hm <= now_hm < vb_cutoff_hm:
                for _vb_sym, _vb_entry in list(self.vb_candidates.items()):
                    if _vb_sym in self.vb_entered_today:
                        continue
                    if self.position_mgr.get_holding_quantity(_vb_sym) > 0:
                        continue
                    _vb_price = live_prices.get(_vb_sym, 0)
                    if _vb_price <= 0:
                        for _r in results:
                            if _r['symbol'] == _vb_sym and _r.get('data'):
                                _vb_price = float(_r['data'].get('close', 0) or 0)
                                break
                    if _vb_price <= 0:
                        continue
                    if _vb_price >= _vb_entry:
                        logger.warning(
                            f"🔥 [VB] {_vb_sym} 돌파! 현재가 ₩{_vb_price:,.0f} >= "
                            f"진입가 ₩{_vb_entry:,.0f}"
                        )
                        with self._order_lock:
                            _ok = self._execute_vb_buy(_vb_sym, _vb_price)
                        if _ok:
                            self.vb_entered_today.add(_vb_sym)

            # ── 2단계: 매도 먼저 순서대로 실행 ─────────────────────────────
            _eod_market  = os.getenv("EOD_ORDER_TYPE",  "market").lower() == "market"
            _sl_market   = os.getenv("SL_ORDER_TYPE",   "market").lower() == "market"
            if sell_queue:
                logger.info(f"  📋 매도 대기열: {len(sell_queue)}건 순서대로 실행")
            for idx, (sym, qty, price, reason, meta) in enumerate(sell_queue, 1):
                logger.warning(f"🛑 [{sym}] ({idx}/{len(sell_queue)}) {reason} 매도")
                _reason_str = str(reason)
                _use_market = (
                    (_eod_market and ("EOD" in _reason_str or "VB당일청산" in _reason_str
                                      or "VB분할" in _reason_str))
                    or (_sl_market and ("손절" in _reason_str or "SL" in _reason_str or "VB손절" in _reason_str))
                )
                with self._order_lock:
                    sell_success = self.execute_sell(sym, qty, price, reason=reason, market_order=_use_market)
                if sell_success:
                    self._record_profit_harvest_exit(sym, price, str(reason))
                    if meta and meta.get('target_stage'):
                        current_holding = self.position_mgr.portfolio.get('holdings', {}).get(sym, {})
                        events = dict(current_holding.get('target_stage_events') or {})
                        events[str(meta['target_stage'])] = {
                            'status': meta.get('target_status', 'sold'),
                            'time': datetime.now(self.KST).isoformat(),
                            'price': price,
                            'detail': meta.get('target_detail', ''),
                        }
                        self.position_mgr.update_position_metadata(
                            sym,
                            target_stage_events=events,
                            last_target_action=f"{meta['target_stage']}차 목표 익절 완료",
                        )
                    if "부분익절" in str(reason):
                        self.position_mgr.mark_partial_take_profit(sym, price)
                        cooldown_sec = int(os.getenv("PARTIAL_REENTRY_COOLDOWN_SEC", "900") or 900)
                        self.sell_reentry_cooldowns[sym] = time.time() + cooldown_sec
                        self._save_sell_reentry_cooldowns()
                    else:
                        cooldown_sec = int(os.getenv("SELL_REENTRY_COOLDOWN_SEC", "1800") or 1800)
                        self.sell_reentry_cooldowns[sym] = time.time() + cooldown_sec
                        self._save_sell_reentry_cooldowns()
                if idx < len(sell_queue):
                    time.sleep(2.0)   # 주문 사이 2초 간격

            # ── 3단계: 매수 순서대로 실행 ───────────────────────────────────
            if buy_queue:
                buy_queue.sort(
                    key=lambda item: self.analyzer.calculate_score(item[0], item[2]),
                    reverse=True,
                )
                logger.info(f"  📋 매수 대기열: {len(buy_queue)}건 순서대로 실행")
            for idx, (sym, price, price_data) in enumerate(buy_queue, 1):
                logger.warning(f"💰 [{sym}] ({idx}/{len(buy_queue)}) 매수 주문 실행")
                try:
                    with self._order_lock:
                        self.execute_buy(sym, price, price_data)
                except _InsufficientFunds:
                    self.buy_fail_cooldowns[sym] = time.time() + self.BUY_FAIL_COOLDOWN_SEC
                    logger.warning(
                        f"⚠️ 주문가능금액 부족 — [{sym}] {self.BUY_FAIL_COOLDOWN_SEC//60}분 쿨다운, "
                        f"매수 대기열 {idx}/{len(buy_queue)}부터 전체 중단"
                    )
                    break
                if idx < len(buy_queue):
                    time.sleep(2.0)   # 주문 사이 2초 간격

        except Exception as e:
            logger.error(f"❌ 모니터링 오류: {e}", exc_info=True)
            self._error_alert("실시간 모니터링(realtime_monitoring)", e)

    # ── 주문 실행 ──────────────────────────────────────────────────────────

    def execute_buy(self, symbol: str, price: float, price_data: dict | None = None):
        try:
            # 서킷브레이커 — 당일 손실 한도 초과 시 신규 매수 차단
            self._update_daily_pnl(0)   # 날짜 초기화만 수행 (PnL 변화 없음)
            if self._circuit_breaker_active:
                logger.info(
                    f"⛔ [{symbol}] 매수 스킵 — 서킷브레이커 활성 "
                    f"(당일 손익 ₩{self._daily_realized_pnl:,.0f})"
                )
                return

            # 보유 상한 체크 (매도 이후 슬롯이 생겼을 수 있으므로 실행 직전에도 확인)
            strategy_holdings = self.position_mgr.count_strategy_positions()
            if strategy_holdings >= self.MAX_HOLDINGS:
                logger.info(
                    f"⛔ [{symbol}] 매수 스킵 — 전략 포지션 상한 도달 "
                    f"({strategy_holdings}/{self.MAX_HOLDINGS})"
                )
                return

            price_data = price_data or {}
            crash_mode = bool(price_data.get('_crash_mode', False))

            # SCALP → SWING 재분류: 자동매매에 스캘프 부적합, SWING으로 재분류 후 포지션 50% 축소
            _is_scalp_reclassified = False
            if not crash_mode:
                pre_score = self.analyzer.calculate_score(symbol, price_data)
                pre_profile, pre_reason = self._classify_trade_profile(symbol, price_data, pre_score)
                if pre_profile == "SCALP":
                    # 거래량 필터: SCALP 당일매매는 충분한 유동성 필요
                    scalp_min_vol_ratio = float(os.getenv("SCALP_MIN_VOLUME_RATIO", "1.0") or 1.0)
                    scalp_min_turnover = float(os.getenv("SCALP_MIN_TURNOVER", "5000000000") or 5000000000)
                    _vol = float(price_data.get('volume', 0) or 0)
                    _avg_vol = float(price_data.get('avg_volume_20', _vol) or _vol)
                    _turnover = _vol * price
                    _vol_ratio_ok = (_avg_vol <= 0) or (_vol >= _avg_vol * scalp_min_vol_ratio)
                    _turnover_ok = _turnover >= scalp_min_turnover
                    if not _vol_ratio_ok:
                        logger.info(
                            f"⛔ [{symbol}] SCALP 당일매매 거래량 미달 "
                            f"({_vol/max(_avg_vol,1):.2f}x < {scalp_min_vol_ratio:.1f}x 20일평균) — 진입 취소"
                        )
                        return
                    if not _turnover_ok:
                        logger.info(
                            f"⛔ [{symbol}] SCALP 당일매매 거래대금 미달 "
                            f"({_turnover/1e8:.0f}억 < {scalp_min_turnover/1e8:.0f}억) — 진입 취소"
                        )
                        return
                    _is_scalp_reclassified = True
                    logger.info(
                        f"↪️ [{symbol}] SCALP → 당일매매 재분류 (거래량 {_vol/max(_avg_vol,1):.2f}x, "
                        f"거래대금 {_turnover/1e8:.0f}억 | {pre_reason})"
                    )

            atr = float(price_data.get('atr') or price * 0.02)
            stop_loss = max(price - atr * 2.0, price * 0.97)
            risk_pct = float(os.getenv("RISK_PER_TRADE", "0.02"))

            signal_score = self.analyzer.calculate_score(symbol, price_data)

            # ── 3단계 점수 티어 ──────────────────────────────────────────────
            max_position_pct    = float(os.getenv("MAX_POSITION_PCT",    "0.22"))  # 65-79점
            mid_position_score  = float(os.getenv("MID_POSITION_SCORE",  "80")  or 80)
            mid_position_pct    = float(os.getenv("MID_POSITION_PCT",    "0.28") or 0.28)  # 80-89점
            full_position_score = float(os.getenv("FULL_POSITION_SCORE", "90")  or 90)
            full_position_pct   = float(os.getenv("FULL_POSITION_PCT",   "0.34") or 0.34)  # 90+점
            quality_cap_pct     = float(os.getenv("QUALITY_MAX_PCT",     "0.40") or 0.40)  # 절대 상한

            if signal_score >= full_position_score:
                effective_position_pct = full_position_pct
            elif signal_score >= mid_position_score:
                effective_position_pct = mid_position_pct
            else:
                effective_position_pct = max_position_pct

            # ── 수급 품질 보너스: 기관/외인 연속 매집 ───────────────────────
            cbd = price_data.get('consecutive_buy_days', 0)
            if cbd >= 5:
                quality_bonus = float(os.getenv("QUALITY_CBD5_BONUS", "0.04") or 0.04)
            elif cbd >= 3:
                quality_bonus = float(os.getenv("QUALITY_CBD3_BONUS", "0.02") or 0.02)
            else:
                quality_bonus = 0.0
            if quality_bonus > 0:
                _prev_pct = effective_position_pct
                effective_position_pct = min(effective_position_pct + quality_bonus, quality_cap_pct)
                logger.info(
                    f"  🏆 [{symbol}] 수급 보너스 +{quality_bonus*100:.0f}%"
                    f" (연속매집 {cbd}일): {_prev_pct*100:.0f}% → {effective_position_pct*100:.0f}%"
                )

            # 변동성 기반 포지션 사이징: ATR%가 높을수록 비중 자동 축소
            # 목표: 포지션 하루 변동폭이 계좌의 POSITION_TARGET_VOL_PCT% 이내
            atr_pct = (atr / price) * 100 if price > 0 else 2.0
            target_vol_pct = float(os.getenv("POSITION_TARGET_VOL_PCT", "2.0") or 2.0)
            min_position_pct = float(os.getenv("POSITION_MIN_PCT", "0.10") or 0.10)
            vol_adjusted_pct = min(effective_position_pct, target_vol_pct / max(atr_pct, 0.5))
            vol_adjusted_pct = max(vol_adjusted_pct, min_position_pct)
            if vol_adjusted_pct < effective_position_pct - 0.005:
                logger.info(
                    f"  📐 [{symbol}] 변동성 조정: {effective_position_pct*100:.0f}%"
                    f" → {vol_adjusted_pct*100:.0f}%"
                    f" (ATR {atr_pct:.1f}%, 목표 {target_vol_pct:.1f}%)"
                )
            effective_position_pct = vol_adjusted_pct

            # 5일 급등 이벤트 프록시: 단기 급등 종목은 뉴스 등 이벤트 반영 가능 → 포지션 축소
            _close_5d = float(price_data.get('close_5d_ago', price) or price)
            if _close_5d > 0:
                _gain_5d = (price - _close_5d) / _close_5d * 100
                _event_thresh = float(os.getenv("EVENT_PROXY_5D_GAIN_PCT", "12.0") or 12.0)
                if _gain_5d >= _event_thresh:
                    _event_ratio = float(os.getenv("EVENT_PROXY_SIZE_RATIO", "0.6") or 0.6)
                    _prev_pct = effective_position_pct
                    effective_position_pct = max(effective_position_pct * _event_ratio, min_position_pct)
                    logger.info(
                        f"  📰 [{symbol}] 5일 급등({_gain_5d:.1f}%) 이벤트 조정: "
                        f"{_prev_pct*100:.0f}% → {effective_position_pct*100:.0f}%"
                    )

            # SCALP→SWING 재분류 시 포지션 50% 축소 (변동성 리스크 완화)
            if _is_scalp_reclassified:
                effective_position_pct *= 0.5
                logger.info(f"  📉 [{symbol}] SCALP→SWING 포지션 축소: {effective_position_pct*100:.0f}%")

            # 폭락 반등 예외 매수 — 포지션을 CRASH_POSITION_PCT로 제한
            if crash_mode:
                crash_pos_pct = float(os.getenv("CRASH_POSITION_PCT", "0.10") or 0.10)
                effective_position_pct = min(effective_position_pct, crash_pos_pct)
                logger.warning(f"  🚨 [{symbol}] 폭락반등 진입 — 포지션 상한 {crash_pos_pct*100:.0f}% 적용")

            # 14:30 이후 늦은 진입 — 포지션 상한을 LATE_BUY_MAX_POSITION_PCT 로 제한
            now_hm = int(datetime.now(self.KST).strftime('%H%M'))
            buy_cutoff_hm = int(os.getenv("NEW_BUY_CUTOFF_HHMM", "1430") or 1430)
            _late_buy_active = now_hm >= buy_cutoff_hm and os.getenv("LATE_BUY_ENABLED", "false").lower() == "true"
            if _late_buy_active:
                late_pos_pct = float(os.getenv("LATE_BUY_MAX_POSITION_PCT", "0.5") or 0.5)
                effective_position_pct = min(effective_position_pct, late_pos_pct)
                logger.info(f"  🌙 [{symbol}] 늦은 진입 — 포지션 상한 {late_pos_pct*100:.0f}% 적용")

            # 진입 유형 레이블 결정 (성과 추적용)
            if crash_mode:
                entry_type = "CRASH_RECOVERY"
            elif price_data.get('_v43_strategy'):
                entry_type = f"V43_{price_data.get('_v43_grade', 'U')}_{price_data.get('_v43_strategy')}"
            elif _is_scalp_reclassified:
                entry_type = "SCALP_REDUCED"
            elif _late_buy_active:
                entry_type = "LATE_BUY_EXCEPTION"
            else:
                entry_type = "NORMAL_SWING"

            if os.getenv("KOSPI_V43_LIVE_FILTER", "true").lower() == "true" and not crash_mode:
                v43_base_pct = float(os.getenv("V43_BASE_POSITION_PCT", "0.125") or 0.125)
                v43_mult = float(price_data.get('_v43_size_multiplier', 1.0) or 1.0)
                _prev_pct = effective_position_pct
                effective_position_pct = min(v43_base_pct, v43_base_pct * v43_mult)
                logger.info(
                    f"  🧭 [{symbol}] v4.3 포지션 적용 "
                    f"({entry_type}): {_prev_pct*100:.1f}% → {effective_position_pct*100:.1f}%"
                )

            # 전략 성과 기반 비중 자동 조정
            _strat_mult = self._get_strategy_multiplier(entry_type)
            if _strat_mult != 1.0:
                _prev_pct = effective_position_pct
                effective_position_pct = min(effective_position_pct * _strat_mult, quality_cap_pct)
                logger.info(
                    f"  📊 [{symbol}] 전략성과 비중 조정 "
                    f"({entry_type} ×{_strat_mult:.2f}): "
                    f"{_prev_pct*100:.0f}%→{effective_position_pct*100:.0f}%"
                )

            allow_strong_chase = (
                os.getenv("ALLOW_STRONG_BUY_CHASE", "true").lower() == "true"
            )
            strong_chase_score = float(os.getenv("STRONG_BUY_CHASE_SCORE", "90") or 90)
            allow_price_chase = allow_strong_chase and signal_score >= strong_chase_score
            cash_buffer_pct = float(os.getenv("ORDER_CASH_BUFFER_PCT", "0.01") or 0.01)

            def krx_tick(p: float) -> int:
                if p < 2_000:       return 1
                if p < 5_000:       return 5
                if p < 20_000:      return 10
                if p < 50_000:      return 50
                if p < 200_000:     return 100
                if p < 500_000:     return 500
                return 1_000

            buy_slippage = float(os.getenv("BUY_PRICE_SLIPPAGE_PCT", "0.0") or 0.0)
            if allow_price_chase:
                raw_order_price = price * (1 + max(0.0, buy_slippage))
            else:
                raw_order_price = price * (1 + min(0.0, buy_slippage))
            tick = krx_tick(raw_order_price)
            expected_order_price = max(int(raw_order_price // tick * tick), tick)

            portfolio = self.position_mgr.portfolio
            account_value = float(portfolio.get('total_value') or 0)
            if account_value <= 0:
                holdings_value = sum(
                    h.get('amount', h.get('price', 0) * h.get('quantity', 0))
                    for h in portfolio.get('holdings', {}).values()
                )
                account_value = float(portfolio.get('cash', 0)) + holdings_value

            risk_qty = self.risk_mgr.calculate_position_size(account_value, risk_pct, price, stop_loss)
            pct_cap_value = account_value * effective_position_pct if account_value > 0 else self.POSITION_AMOUNT
            value_cap = min(self.POSITION_AMOUNT, pct_cap_value)
            cap_qty = int((value_cap * (1 - cash_buffer_pct)) / expected_order_price)
            quantity = min(risk_qty, cap_qty)

            if quantity <= 0:
                logger.info(
                    f"ℹ️ [{symbol}] 리스크 계산 수량 0 — 최소 1주로 진입 시도 "
                    f"(ATR ₩{atr:,.0f}, 손절 ₩{stop_loss:,.0f}, 계좌 ₩{account_value:,.0f})"
                )
                quantity = 1

            cost = quantity * expected_order_price

            # ── KIS API 실제 주문가능금액 확인 (당일 매도 재사용 포함 최대 매수가능금액) ──
            orderable = self.kis_client.get_orderable_cash(symbol, expected_order_price, use_max=True)
            if orderable >= 0:  # 조회 성공
                if orderable < cost:
                    available_cash = float(self.position_mgr.portfolio.get('cash', 0))

                    # KIS API가 ₩0을 반환하는 경우 — 직전 주문 직후 미체결 처리 딜레이로
                    # 인한 오류일 가능성이 높으므로 모의/실전 무관하게 예수금으로 보정.
                    # 실제로 잔고가 없다면 이후 place_buy_order에서 실패하므로 안전하다.
                    allow_fallback = (
                        os.getenv("ALLOW_ORDERABLE_FALLBACK", "true").lower() == "true"
                    )
                    if allow_fallback and orderable == 0 and available_cash >= expected_order_price:
                        partial_qty = min(
                            quantity,
                            int((available_cash * (1 - cash_buffer_pct)) / expected_order_price),
                        )
                        is_mock_env = bool(getattr(self.kis_client, 'is_mock', False))
                        logger.warning(
                            f"⚠️ [{symbol}] KIS 주문가능금액 0원 응답 "
                            f"({'모의' if is_mock_env else '실전'}) — "
                            f"예수금 기준으로 보정 (예수금 ₩{available_cash:,.0f})"
                        )
                        quantity = partial_qty
                        cost = quantity * expected_order_price
                    else:
                        # orderable > 0이지만 cost보다 부족 → 부분 매수 시도
                        partial_qty = int((orderable * (1 - cash_buffer_pct)) / expected_order_price)
                        if partial_qty <= 0:
                            logger.warning(
                                f"⚠️ [{symbol}] KIS 주문가능금액 부족 — 매수 스킵 "
                                f"(주문가능 ₩{orderable:,.0f} < 1주 ₩{expected_order_price:,.0f})"
                            )
                            raise _InsufficientFunds(symbol)
                        logger.info(
                            f"⚠️ [{symbol}] 부분 매수: {quantity}주 → {partial_qty}주 "
                            f"(주문가능 ₩{orderable:,.0f})"
                        )
                        quantity = partial_qty
                        cost = quantity * expected_order_price
            else:
                # API 조회 실패 → 로컬 예수금으로 폴백
                available_cash = self.position_mgr.portfolio['cash']
                if available_cash < cost:
                    partial_qty = int((available_cash * (1 - cash_buffer_pct)) / expected_order_price)
                    if partial_qty <= 0:
                        logger.warning(
                            f"⚠️ [{symbol}] 현금 부족 (로컬) — 매수 스킵 "
                            f"(가용 ₩{available_cash:,.0f} < 1주 ₩{expected_order_price:,.0f})"
                        )
                        raise _InsufficientFunds(symbol)
                    logger.info(
                        f"⚠️ [{symbol}] 부분 매수 (로컬): {quantity}주 → {partial_qty}주 "
                        f"(가용 ₩{available_cash:,.0f})"
                    )
                    quantity = partial_qty
                    cost = quantity * expected_order_price

            if quantity <= 0:
                logger.warning(f"⚠️ [{symbol}] 버퍼 반영 후 수량 0 — 매수 스킵")
                raise _InsufficientFunds(symbol)

            orderable_text = f"₩{orderable:,.0f}" if orderable >= 0 else "API실패"
            logger.warning(
                f"\n💰 [{symbol}] 매수 주문!\n"
                f"   시간: {datetime.now(self.KST).strftime('%H:%M:%S')}\n"
                f"   현재가: ₩{price:,.0f}  예상주문가: ₩{expected_order_price:,.0f}  수량: {quantity}주\n"
                f"   손절기준: ₩{stop_loss:,.0f}  리스크: {risk_pct*100:.1f}%  종목상한: {effective_position_pct*100:.0f}%\n"
                f"   금액: ₩{cost:,.0f}  주문가능: {orderable_text}  현금버퍼: {cash_buffer_pct*100:.1f}%\n"
                f"   신호점수: {signal_score:.1f}  풀베팅기준: {full_position_score:.0f}  추격허용: {'ON' if allow_price_chase else 'OFF'}"
            )
            previous_qty = self.position_mgr.get_holding_quantity(symbol)
            success = self.kis_client.place_buy_order(
                symbol,
                quantity,
                price,
                allow_price_chase=allow_price_chase,
            )
            if success:
                verify_enabled = os.getenv("ORDER_FILL_VERIFY_ENABLED", "true").lower() == "true"
                if verify_enabled and not self.kis_client.verify_domestic_fill(
                    symbol, "BUY", previous_qty, quantity
                ):
                    self._sync_portfolio_from_kis()
                    # 체결 확인 실패 종목 재시도 방지 (브로커에서 체결됐을 수 있으므로 재매수 차단)
                    self.buy_fail_cooldowns[symbol] = time.time() + self.BUY_FAIL_COOLDOWN_SEC
                    logger.warning(
                        f"⚠️ [{symbol}] 체결 미확인 → {self.BUY_FAIL_COOLDOWN_SEC // 60}분 재시도 차단"
                    )
                    return
                self.position_mgr.add_position(symbol, quantity, expected_order_price, source='strategy', entry_type=entry_type)
                profile, profile_reason = self._classify_trade_profile(symbol, price_data, signal_score)
                if _is_scalp_reclassified and profile == "SCALP":
                    profile_reason = f"[당일청산] {profile_reason}"
                target_plan = self._build_target_plan(expected_order_price, atr, profile)
                mc = self._market_condition
                market_phase = mc.get('market_mode', 'UNKNOWN')
                entry_ts = datetime.now(self.KST).isoformat()
                self.position_mgr.update_position_metadata(
                    symbol,
                    strategy_profile=profile,
                    strategy_profile_reason=profile_reason,
                    strategy_profile_time=entry_ts,
                    effective_profile=profile,
                    effective_profile_reason="진입 프로필",
                    effective_profile_time=entry_ts,
                    target_plan=target_plan,
                    target_stage_events={},
                    target_plan_time=entry_ts,
                    last_target_action="진입 직후 목표가 설정",
                    entry_type=entry_type,
                    entry_score=round(signal_score, 1),
                    entry_market_phase=market_phase,
                    entry_position_pct=round(effective_position_pct * 100, 1),
                    entry_time=entry_ts,
                )
                self._log_trade_entry(symbol, expected_order_price, quantity,
                                      entry_type, signal_score, market_phase,
                                      effective_position_pct)
                logger.info(f"🧭 [{symbol}] 진입 전략 프로필: {profile} — {profile_reason}")
                if target_plan:
                    logger.info(
                        "  🎯 목표가: "
                        + " / ".join(
                            f"{item['stage']}차 ₩{item['target_price']:,.0f}({item['target_pct']:.1f}%)"
                            for item in target_plan
                        )
                    )
                crash_extra = []
                if crash_mode:
                    self._crash_daily_reset()
                    self._crash_buy_count_today += 1
                    crash_cooldown_sec = float(os.getenv("CRASH_STOCK_COOLDOWN_SEC", "14400") or 14400)
                    self._crash_buy_cooldowns[symbol] = time.time() + crash_cooldown_sec
                    crash_extra = [
                        f"🚨 폭락반등 예외매수 ({self._crash_buy_count_today}/"
                        f"{os.getenv('CRASH_MAX_DAILY_BUYS', '2')})"
                    ]
                self._notify_trade(
                    "BUY",
                    symbol,
                    quantity,
                    expected_order_price,
                    extra_lines=[
                        f"프로필: {profile}",
                        f"점수: {signal_score:.1f}",
                        "목표가: " + " / ".join(
                            f"{item['stage']}차 ₩{item['target_price']:,.0f}"
                            for item in target_plan
                        ) if target_plan else "목표가: 없음",
                    ] + crash_extra,
                )
            else:
                logger.error(f"❌ [{symbol}] 매수 주문 실패 — 포지션 미등록")
                self.buy_fail_cooldowns[symbol] = time.time() + self.BUY_FAIL_COOLDOWN_SEC
                logger.warning(
                    f"⚠️ [{symbol}] 주문 실패 → {self.BUY_FAIL_COOLDOWN_SEC // 60}분 재시도 차단"
                )

        except _InsufficientFunds:
            raise  # 상위 큐 루프로 전파
        except Exception as e:
            logger.error(f"❌ 매수 실패 ({symbol}): {e}")

    def execute_sell(self, symbol: str, quantity: int, price: float, reason: str = "", market_order: bool = False) -> bool:
        try:
            order_type_label = "시장가" if market_order else "지정가"
            logger.warning(
                f"\n💰 [{symbol}] 매도 주문! ({order_type_label})\n"
                f"   시간: {datetime.now(self.KST).strftime('%H:%M:%S')}\n"
                f"   가격: ₩{price:,.0f}  수량: {quantity}주\n"
                f"   금액: ₩{price * quantity:,.0f}"
            )
            previous_qty = self.position_mgr.get_holding_quantity(symbol)
            # 매도 전에 매수가 확보 (remove_position 이후 holdings에서 사라질 수 있음)
            holding_snap = self.position_mgr.portfolio.get('holdings', {}).get(symbol, {})
            buy_price_snap = float(holding_snap.get('price', 0) or 0)
            success = self.kis_client.place_sell_order(symbol, quantity, price, market_order=market_order)
            if success:
                verify_enabled = os.getenv("ORDER_FILL_VERIFY_ENABLED", "true").lower() == "true"
                if verify_enabled and not self.kis_client.verify_domestic_fill(
                    symbol, "SELL", previous_qty, quantity
                ):
                    self._sync_portfolio_from_kis()
                    return False
                self.position_mgr.remove_position(symbol, quantity, price)
                # 실현 손익 누적 → 서킷브레이커 판단
                if buy_price_snap > 0:
                    self._update_daily_pnl((price - buy_price_snap) * quantity)
                self._log_trade_exit(symbol, price, quantity, reason, holding_snap)
                extra_lines = []
                if reason:
                    extra_lines.append(f"사유: {reason}")
                if buy_price_snap > 0:
                    profit_pct = (price - buy_price_snap) / buy_price_snap * 100
                    extra_lines.append(f"수익률: {profit_pct:+.2f}%")
                self._notify_trade("SELL", symbol, quantity, price, extra_lines=extra_lines)
                return True
            else:
                logger.error(f"❌ [{symbol}] 매도 주문 실패 — 포지션 유지")
                return False

        except Exception as e:
            logger.error(f"❌ 매도 실패 ({symbol}): {e}")
            return False

    def _calc_strategy_multipliers(self, lookback_days: int = 20) -> dict[str, float]:
        """
        trades.jsonl 최근 lookback_days일 EXIT 기록으로 entry_type별 성과 평가,
        비중 조정 배수(multiplier) 딕셔너리 반환.

        기준:
          - 데이터 부족 (n < PERF_MIN_TRADES) → 1.0 (중립)
          - 승률 < 40% AND 평균손익 < -1%  → 0.50 (강한 축소)
          - 승률 < 50% OR  평균손익 < 0%   → 0.75 (약한 축소)
          - 승률 ≥ 65% AND 평균손익 ≥ 2%   → 1.10 (성과 보너스)
          - 나머지 → 1.0
        """
        from collections import defaultdict
        from datetime import timedelta

        min_trades = int(os.getenv("PERF_MIN_TRADES", "10") or 10)
        result: dict[str, float] = {}

        if not TRADES_LOG.exists():
            return result

        cutoff = (datetime.now(self.KST) - timedelta(days=lookback_days)).date()
        perf: dict[str, dict] = defaultdict(lambda: {"wins": 0, "n": 0, "total_pnl": 0.0})

        try:
            with open(TRADES_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("event") != "EXIT":
                        continue
                    try:
                        ts = datetime.fromisoformat(r.get("ts", ""))
                    except Exception:
                        continue
                    if ts.date() < cutoff:
                        continue
                    et  = r.get("entry_type", "UNKNOWN")
                    pnl = float(r.get("profit_pct", 0))
                    perf[et]["n"]         += 1
                    perf[et]["total_pnl"] += pnl
                    if pnl > 0:
                        perf[et]["wins"] += 1
        except Exception as e:
            logger.warning(f"⚠️ 전략 비중 배수 산출 오류: {e}")
            return result

        for et, v in perf.items():
            n = v["n"]
            if n < min_trades:
                result[et] = 1.0
                continue
            win_rate = v["wins"] / n
            avg_pnl  = v["total_pnl"] / n
            if win_rate < 0.40 and avg_pnl < -1.0:
                mult = 0.50
            elif win_rate < 0.50 or avg_pnl < 0.0:
                mult = 0.75
            elif win_rate >= 0.65 and avg_pnl >= 2.0:
                mult = 1.10
            else:
                mult = 1.0
            result[et] = mult

        if result:
            adjusted = {et: m for et, m in result.items() if m != 1.0}
            if adjusted:
                logger.info(
                    f"📊 전략 비중 배수 산출 완료 (최근 {lookback_days}일): "
                    + ", ".join(f"{et}×{m:.2f}" for et, m in adjusted.items())
                )
        return result

    def _get_strategy_multiplier(self, entry_type: str) -> float:
        """entry_type 에 해당하는 비중 조정 배수 반환 (일 1회 갱신 캐시)."""
        now_ts = time.time()
        # 하루에 한 번만 재산출 (86400초)
        if now_ts - self._strategy_multipliers_ts > 86400 or not self._strategy_multipliers:
            self._strategy_multipliers    = self._calc_strategy_multipliers()
            self._strategy_multipliers_ts = now_ts
        return self._strategy_multipliers.get(entry_type, 1.0)

    def _rolling_performance_report(self, days: int) -> str | None:
        """
        trades.jsonl 에서 최근 days일 EXIT 기록을 집계해 성과 문자열 반환.
        체결 없으면 None 반환.
        """
        from collections import defaultdict
        from datetime import timedelta

        if not TRADES_LOG.exists():
            return None

        cutoff = (datetime.now(self.KST) - timedelta(days=days)).date()

        perf: dict[str, dict] = defaultdict(lambda: {
            "wins": 0, "losses": 0, "total_pnl": 0.0,
            "hold_sec": 0, "count": 0, "max_loss": 0.0,
        })
        sell_type_counts: dict[str, int] = defaultdict(int)
        day_set: set[str] = set()

        try:
            with open(TRADES_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("event") != "EXIT":
                        continue
                    ts_str = r.get("ts", "")
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except Exception:
                        continue
                    if ts.date() < cutoff:
                        continue
                    day_set.add(ts.strftime('%Y-%m-%d'))
                    et  = r.get("entry_type", "UNKNOWN")
                    st  = r.get("sell_type", "OTHER")
                    pnl = float(r.get("profit_pct", 0))
                    hs  = int(r.get("hold_sec", 0))
                    perf[et]["count"]     += 1
                    perf[et]["total_pnl"] += pnl
                    perf[et]["hold_sec"]  += hs
                    if pnl > 0:
                        perf[et]["wins"] += 1
                    else:
                        perf[et]["losses"] += 1
                    if pnl < perf[et]["max_loss"]:
                        perf[et]["max_loss"] = pnl
                    sell_type_counts[st] += 1
        except Exception as e:
            logger.warning(f"⚠️ 롤링 성과 집계 오류: {e}")
            return None

        total = sum(v["count"] for v in perf.values())
        if total == 0:
            return None

        total_wins = sum(v["wins"]      for v in perf.values())
        total_pnl  = sum(v["total_pnl"] for v in perf.values())
        overall_wr = total_wins / total * 100 if total else 0
        avg_pnl    = total_pnl  / total       if total else 0
        today_str  = datetime.now(self.KST).strftime('%Y-%m-%d')

        lines = [f"📊 {days}일 롤링 성과 (~{today_str}, {len(day_set)}거래일)"]
        lines.append(f"총 {total}건 | 전체 승률 {overall_wr:.0f}% | 평균 {avg_pnl:+.2f}%\n")

        lines.append("[ 전략(entry_type)별 ]")
        for et, v in sorted(perf.items(), key=lambda x: x[1]["count"], reverse=True):
            n    = v["count"]
            wr   = v["wins"] / n * 100 if n else 0
            avg  = v["total_pnl"] / n  if n else 0
            ml   = v["max_loss"]
            avgh = v["hold_sec"] // n // 60 if n else 0
            lines.append(
                f"  {et}: {n}건 | 승률{wr:.0f}% | 평균{avg:+.2f}% | "
                f"최대손실{ml:.1f}% | 평균보유{avgh}분"
            )

        lines.append("\n[ 매도 유형별 ]")
        for st, cnt in sorted(sell_type_counts.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {st}: {cnt}건")

        return "\n".join(lines)

    def _send_rolling_report(self, days: int) -> None:
        """롤링 성과 리포트를 로그 + 텔레그램으로 전송."""
        try:
            msg = self._rolling_performance_report(days)
            if not msg:
                logger.info(f"📊 {days}일 롤링 — 체결 기록 없음, 생략")
                return
            logger.info(msg)
            try:
                self.reporter.send_message(msg)
            except Exception as e:
                logger.debug(f"롤링 성과 텔레그램 실패: {e}")
        except Exception as e:
            logger.warning(f"⚠️ {days}일 롤링 성과 리포트 오류: {e}")

    def _shadow_performance_report(self) -> str | None:
        """
        오늘 탈락(REJECTION) 종목의 장마감 현재가를 조회해
        '탈락 후 실제 등락'을 기록·요약한다.

        - rejections.jsonl 에서 오늘 날짜 기록만 추출
        - 종목별 첫 탈락 기록만 사용 (당일 중복 제거)
        - reject_price 가 없는 레코드는 스킵
        - 장마감 현재가 조회 후 SHADOW_RESULT 레코드를 shadow_perf.jsonl 에 기록
        - 요약 문자열 반환 (None = 데이터 없음)
        """
        try:
            if not REJECTIONS_LOG.exists():
                return None
            today = datetime.now(self.KST).strftime('%Y-%m-%d')

            # 종목별 첫 탈락 레코드만 수집
            first_reject: dict[str, dict] = {}
            with open(REJECTIONS_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if not r.get("ts", "").startswith(today):
                        continue
                    sym = r.get("symbol", "")
                    rp  = float(r.get("reject_price", 0) or 0)
                    if not sym or rp <= 0:
                        continue
                    if sym not in first_reject:
                        first_reject[sym] = r

            if not first_reject:
                return None

            # 장마감 현재가 일괄 조회
            syms = list(first_reject.keys())
            try:
                close_prices = self.kis_client.get_current_prices(syms)
            except Exception as e:
                logger.warning(f"⚠️ 섀도 성과 현재가 조회 실패: {e}")
                close_prices = {}

            results: list[dict] = []
            for sym, rec in first_reject.items():
                rp = float(rec["reject_price"])
                cp = float(close_prices.get(sym, 0) or 0)
                if cp <= 0:
                    continue
                chg = (cp - rp) / rp * 100
                result = {
                    "event":        "SHADOW_RESULT",
                    "ts":           datetime.now(self.KST).isoformat(),
                    "date":         today,
                    "symbol":       sym,
                    "score":        rec.get("score", 0),
                    "reasons":      rec.get("reasons", ""),
                    "market_phase": rec.get("market_phase", ""),
                    "reject_price": rp,
                    "close_price":  cp,
                    "chg_pct":      round(chg, 2),
                    "would_profit": chg > 0,
                }
                results.append(result)

            if not results:
                return None

            # shadow_perf.jsonl 에 기록
            try:
                with open(SHADOW_PERF_LOG, "a", encoding="utf-8") as f:
                    for r in results:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning(f"⚠️ 섀도 성과 기록 실패: {e}")

            # 요약 생성
            up   = [r for r in results if r["would_profit"]]
            down = [r for r in results if not r["would_profit"]]
            precision = len(down) / len(results) * 100  # 탈락이 실제로 맞은 비율

            lines = [f"🔍 섀도 성과 [{today}] — 탈락 {len(results)}종목"]
            lines.append(
                f"탈락 후 하락: {len(down)}건 / 상승: {len(up)}건 "
                f"| 필터 정확도 {precision:.0f}%\n"
            )

            # 놓친 상승 (탈락했지만 올라간 종목)
            if up:
                lines.append("[ 놓친 상승 — 재검토 대상 ]")
                for r in sorted(up, key=lambda x: x["chg_pct"], reverse=True)[:5]:
                    lines.append(
                        f"  {r['symbol']} +{r['chg_pct']:.1f}%"
                        f"  점수{r['score']}  {r['reasons']}"
                    )

            # 잘 막은 하락 (탈락이 맞은 종목 Top5)
            if down:
                lines.append("\n[ 올바른 탈락 — Top5 ]")
                for r in sorted(down, key=lambda x: x["chg_pct"])[:5]:
                    lines.append(
                        f"  {r['symbol']} {r['chg_pct']:.1f}%"
                        f"  점수{r['score']}  {r['reasons']}"
                    )

            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"⚠️ 섀도 성과 리포트 오류: {e}")
            return None

    # ── 장 마감 ────────────────────────────────────────────────────────────

    def stop_monitoring(self):
        self.is_market_open = False
        self.last_rescreen_time = 0.0   # 다음 날 재선정을 위해 초기화
        now = datetime.now(self.KST)
        logger.info("=" * 70)
        logger.info(f"🌙 장 마감 — {now.strftime('%H:%M:%S')}")
        logger.info("   내일 08:30 스크리닝까지 대기")
        logger.info("=" * 70)
        self.show_balance()
        self._daily_performance_report()

        # 섀도 성과: 매일 전송
        try:
            shadow_msg = self._shadow_performance_report()
            if shadow_msg:
                logger.info(shadow_msg)
                try:
                    self.reporter.send_message(shadow_msg)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"⚠️ 섀도 성과 리포트 실패: {e}")

        # 금요일: 7일 롤링
        if now.weekday() == 4:
            self._send_rolling_report(7)

        # 월말(25일 이후 목/금): 20일 롤링
        if now.day >= 25 and now.weekday() >= 3:
            self._send_rolling_report(20)

    # ── 스케줄 설정 ────────────────────────────────────────────────────────

    def _error_alert(self, context: str, exc: Exception) -> None:
        """에러 발생 시 텔레그램으로 에러 내용 + Claude Code 질의 문구 전송."""
        import traceback as _tb
        err_type  = type(exc).__name__
        err_msg   = str(exc)[:200]
        tb_lines  = _tb.format_exc().splitlines()
        # traceback에서 실제 파일/라인만 추출
        file_hints = [l.strip() for l in tb_lines if 'File "' in l and 'main.py' in l]
        location  = file_hints[-1] if file_hints else ""

        # 에러 유형별 힌트
        if "not supported between" in err_msg or isinstance(exc, TypeError):
            hint = "타입 비교 오류 — dict/float 혼용 가능성"
        elif isinstance(exc, KeyError):
            hint = "딕셔너리 키 없음 — 데이터 구조 확인 필요"
        elif isinstance(exc, (ConnectionError, TimeoutError)) or "timeout" in err_msg.lower():
            hint = "API 연결/타임아웃 — KIS 서버 상태 또는 토큰 확인"
        elif "token" in err_msg.lower() or "auth" in err_msg.lower():
            hint = "인증 오류 — KIS 토큰 만료 가능성"
        elif isinstance(exc, AttributeError):
            hint = "속성 없음 — None 반환값 또는 코드 변경 확인"
        else:
            hint = "예상치 못한 오류"

        msg = (
            f"❌ 에러 발생: {context}\n"
            f"{'─'*30}\n"
            f"유형: {err_type}\n"
            f"내용: {err_msg}\n"
            f"{location}\n"
            f"{'─'*30}\n"
            f"💡 {hint}\n\n"
            f"📋 Claude Code 질의:\n"
            f"kospi_trading_system {context} 오류:\n"
            f"{err_type}: {err_msg}\n"
            f"{location}\n"
            f"수정해줘"
        )
        try:
            self.reporter.send_message(msg)
        except Exception:
            pass

    def _refresh_tokens_scheduled(self):
        """08:00 선제 토큰 갱신 — 스크리닝 30분 전에 새 토큰을 발급해 만료를 방지한다."""
        try:
            logger.info("🔑 [08:00] 선제적 KIS 토큰 전체 갱신 시작")
            self.kis_client._client.proactive_refresh_tokens()
        except Exception as e:
            logger.error(f"❌ 선제 토큰 갱신 실패: {e}")

    def schedule_tasks(self):
        schedule.clear()  # 재시작/중복 호출 시 job 누적 방지
        logger.info(
            "\n📅 스케줄 (PID %d)\n"
            "   08:00 KST — KIS 토큰 선제 갱신\n"
            "   08:30 KST — 코스피 전체 + 코스닥 상위 300 스캔\n"
            "   09:00~14:30 — 1분마다 재선정+모니터링 (일봉 5분 캐시) / 30초마다 손절·트레일링\n"
            "   14:30~15:30 — 모니터링만 (재선정 스킵)\n"
            "   15:30 KST — 마감", os.getpid()
        )
        schedule.every().day.at("08:00").do(self._refresh_tokens_scheduled)
        schedule.every().day.at("08:30").do(self.morning_screening)
        schedule.every(30).seconds.do(self._holdings_gate)  # 보유 종목 손절/트레일링 빠른 체크
        _mon_interval = int(os.getenv("MONITOR_INTERVAL_SEC", "60"))
        schedule.every(_mon_interval).seconds.do(self._intraday_gate)  # 매수 스캔 + 전체 모니터링
        schedule.every().day.at("15:30").do(self.stop_monitoring)

        # 장중 재시작 시 후보 복원 또는 전체 스캔
        now = datetime.now(self.KST)
        hm  = now.hour * 100 + now.minute
        if now.weekday() < 5 and 900 <= hm < 1530:
            if self.top_10_symbols and self.rescan_pool:
                logger.info(
                    f"📂 장중 재시작 — 후보 {len(self.top_10_symbols)}개 + 재선정풀 {len(self.rescan_pool)}개 복원 "
                    f"(전체 스캔 생략, 다음 재선정 주기에 자동 갱신)"
                )
                self._update_market_condition()
                self.sector_monitor.update(force=True)
                self.is_market_open = True
            else:
                logger.info("🔄 장중 재시작 감지 — 아침 스크리닝 실행")
                threading.Thread(target=self.morning_screening, daemon=True).start()

    def _validate_opening_candidates(self) -> None:
        """09:05 1회 실행: 장 초반 5분 실거래 거래대금으로 watchlist 유동성 재검증."""
        if not self.top_10_symbols:
            self._opening_validated = True
            return
        min_turnover = float(os.getenv("OPENING_MIN_TURNOVER", "100000000") or 100_000_000)  # 기본 1억
        try:
            live_prices = self.kis_client.get_current_prices(self.top_10_symbols) or {}
        except Exception:
            live_prices = {}
        remove_list = []
        for sym in list(self.top_10_symbols):
            cur_price = live_prices.get(sym, 0)
            if cur_price <= 0:
                continue
            try:
                min_df = self.kis_client.get_intraday_ohlcv(sym, interval='1m', lookback=5)
                # 5분봉이 모두 쌓인 것을 확인 (데이터 서버 지연 방어)
                if min_df is None or len(min_df) < 5:
                    continue
                opening_vol = float(min_df['volume'].sum()) if 'volume' in min_df.columns else 0
                opening_turnover = opening_vol * cur_price
                if 0 < opening_turnover < min_turnover:
                    remove_list.append(sym)
                    logger.info(
                        f"  🌅 [{sym}] 개장 유동성 부족 — watchlist 제거 "
                        f"(5분 거래대금 ₩{opening_turnover/1e8:.1f}억 < {min_turnover/1e8:.0f}억)"
                    )
            except Exception:
                continue
        for sym in remove_list:
            if sym in self.top_10_symbols:
                self.top_10_symbols.remove(sym)
        self._opening_validated = True
        logger.info(f"🌅 개장 검증 완료: {len(remove_list)}개 제거, watchlist {len(self.top_10_symbols)}개 유지")

    def _intraday_gate(self):
        now = datetime.now(self.KST)
        if now.weekday() >= 5:
            return
        hm = now.hour * 100 + now.minute
        if hm < 900 or hm >= 1530:
            return

        buy_cutoff_hm = int(os.getenv("NEW_BUY_CUTOFF_HHMM", "1430") or 1430)

        # 09:07~09:29: 장 초반 1회 개장 유동성 검증 (09:05 API 지연 방어 → 09:07 기본값)
        opening_validate_hm = int(os.getenv("OPENING_VALIDATE_HHMM", "907") or 907)
        if opening_validate_hm <= hm < 930 and not self._opening_validated:
            self._validate_opening_candidates()

        # 매수 컷오프 이후(14:30~)는 보유 종목 있을 때만 감시
        if hm >= buy_cutoff_hm:
            if self.position_mgr.portfolio.get('holdings'):
                self.realtime_monitoring()
            return

        # 1시간마다 재선정 (모니터링보다 우선)
        if time.time() - self.last_rescreen_time >= self.RESCREEN_INTERVAL_SEC:
            try:
                self.hourly_rescreen()
            except Exception as e:
                logger.error(f"❌ 장중 재선정 실패: {e}", exc_info=True)
                self._error_alert("장중 재선정(hourly_rescreen)", e)
                self.last_rescreen_time = time.time()

        self.realtime_monitoring()

    # ── 1분 주기: 보유 종목 손절/트레일링 빠른 체크 ───────────────────────────

    def _fast_holdings_exit_check(self):
        """현재가만으로 하드손절·트레일링 스톱을 1분 주기로 체크."""
        holdings = dict(self.position_mgr.portfolio.get('holdings', {}))
        if not holdings:
            return
        try:
            live_prices = self.kis_client.get_current_prices(list(holdings.keys()))
        except Exception as e:
            logger.debug(f"  [1분체크] 현재가 조회 실패: {e}")
            return

        sell_queue: list[tuple] = []
        for sym, holding in holdings.items():
            cur_price = live_prices.get(sym)
            if not cur_price:
                continue
            buy_price = holding.get('price', 0)
            if not buy_price:
                continue
            profit  = (cur_price - buy_price) / buy_price * 100
            high_p  = holding.get('highest_price', cur_price)
            self.position_mgr.update_highest_price(sym, cur_price)
            high_p  = max(high_p, cur_price)
            atr     = cur_price * 0.02   # 정밀 ATR 없으므로 현재가 2% 사용
            ts_price = self.risk_mgr.trailing_stop(cur_price, high_p, atr, multiplier=2.0)
            trailing_triggered = cur_price < ts_price and profit >= 0.5
            if profit <= self.STOP_LOSS_PCT or trailing_triggered:
                qty = self.position_mgr.get_holding_quantity(sym)
                if qty > 0:
                    reason = "하드손절" if profit <= self.STOP_LOSS_PCT else f"트레일링스톱(₩{ts_price:,.0f})"
                    sell_queue.append((sym, qty, cur_price, reason))
                    logger.warning(
                        f"⚡ [1분체크] [{sym}] {reason} | 수익 {profit:+.2f}% | ₩{cur_price:,.0f}"
                    )

        for sym, qty, price, reason in sell_queue:
            with self._order_lock:
                self.execute_sell(sym, qty, price, reason=reason)

    def _holdings_gate(self):
        """1분 주기 게이트: 장중에만 _fast_holdings_exit_check 실행."""
        now = datetime.now(self.KST)
        if now.weekday() >= 5:
            return
        hm = now.hour * 100 + now.minute
        if hm < 900 or hm >= 1530:
            return
        try:
            self._fast_holdings_exit_check()
        except Exception as e:
            logger.error(f"❌ [1분체크] 오류: {e}", exc_info=True)
            self._error_alert("보유종목 손절/트레일링 체크", e)


# ── 엔트리포인트 ─────────────────────────────────────────────────────────

def main():
    acquire_lock()
    logger.info("🧩 코드버전: kospi-v3.6 / crash-recovery / dynamic-scalp / late-buy / sector-kis")
    system = KospiTopTenSystem()
    system.show_balance()
    system.schedule_tasks()

    now        = datetime.now(system.KST)
    hm         = now.hour * 100 + now.minute
    is_weekday = now.weekday() < 5

    if system.top_10_symbols:
        if is_weekday and 900 <= hm < 1530:
            system._update_market_condition()
            system.is_market_open = True
            logger.info(f"📂 매수후보 복원 완료 — 모니터링 활성화: {system.top_10_symbols}")
        else:
            logger.info(f"📂 매수후보 복원 완료 (장외 {now.strftime('%H:%M')} — 대기)")
    elif is_weekday and 830 <= hm < 1530:
        logger.info(f"⚡ 지금 진입 ({now.strftime('%H:%M')}) — 즉시 스크리닝!")
        system.morning_screening()
    else:
        logger.info(f"⏰ 현재 {now.strftime('%H:%M')} — 08:30 스크리닝 대기")

    logger.info("🚀 스케줄러 실행 중... (Ctrl+C로 종료)")

    def _shutdown(signum, frame):
        logger.info("⏹️ 종료 신호 — 스케줄러 정지")
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            logger.error(f"❌ 메인 루프 오류: {e}", exc_info=True)
            system._error_alert("메인 루프(schedule)", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
