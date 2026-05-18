"""
KOSPI 6 + KOSDAQ 4 집중 거래 시스템

일일 스케줄:
  08:30 KST  — 코스피 전체 + 코스닥 상위 300 스캔 → 매수후보 코스피 6 + 코스닥 4 선정
  09:00~15:30 — 5분마다 매수후보 신호 확인 + 보유 종목 매도 타이밍 판단
  15:30 KST  — 장 마감
"""
import os
import sys

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
_LOCK_HANDLE = None


def acquire_lock():
    """동일 봇 중복 실행 방지."""
    global _LOCK_HANDLE
    DATA_DIR.mkdir(exist_ok=True)
    current_pid = str(os.getpid())
    _LOCK_HANDLE = open(LOCK_FILE, "a+")
    try:
        if _sys.platform == 'win32':
            _lock_mod.locking(_LOCK_HANDLE.fileno(), _lock_mod.LK_NBLCK, 1)
        else:
            _lock_mod.lockf(_LOCK_HANDLE, _lock_mod.LOCK_EX | _lock_mod.LOCK_NB)
    except OSError:
        logger.critical("🛑 이미 실행 중인 한국주식 봇이 있어 새 실행을 중단합니다.")
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
    POSITION_AMOUNT  = 5_000_000   # 종목당 투자금 500만원
    STOP_LOSS_PCT    = -3.0        # 하드 손절 기준 (-3%)
    KOSPI_COUNT      = 6           # 매수후보 코스피 종목 수
    KOSDAQ_COUNT     = 4           # 매수후보 코스닥 종목 수
    MAX_HOLDINGS     = int(os.getenv("MAX_STRATEGY_POSITIONS", "10"))  # 봇 신규 진입(strategy) 최대 종목 수
    KOSDAQ_TOP_N     = 300         # 코스닥 시총 상위 N개만 스캔
    RESCREEN_INTERVAL_SEC = 3600   # 장중 재선정 주기 (1시간)
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
                (max(2.5, atr_pct * 1.2), 0.30),
                (max(4.0, atr_pct * 2.0), 0.45),
                (max(6.0, atr_pct * 3.0), 1.00),
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
        """장 막판에는 약한 종목만 정리한다."""
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
            except Exception as _me:
                logger.debug(f"시장 감성 조회 실패: {_me}")
        except Exception as e:
            logger.warning(f"⚠️ 시장 상태 조회 실패 — 기존 상태 유지: {e}")

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

        if volatility_pct > strong_trend_limit:
            return False, f"강한 상승장 허용폭 초과 {volatility_pct:.1f}%>{strong_trend_limit:.1f}%"

        if score < override_score:
            return False, f"점수 부족 {score:.1f}<{override_score:.0f}"

        return True, (
            f"강한 상승장 선택 허용: 점수 {score:.1f}>={override_score:.0f}, "
            f"KOSPI+{mc.get('trend_gap_pct', 0):.1f}%, "
            f"변동성 {volatility_pct:.1f}%<={strong_trend_limit:.1f}%"
        )

    def show_balance(self):
        """KIS 계좌 잔고 및 포트폴리오를 콘솔에 출력"""
        KISBalanceChecker(self.kis_client).print_balance()

    # ── Top 10 영속화 ──────────────────────────────────────────────────────

    def _save_top10(self, symbols: list[str]):
        DATA_DIR.mkdir(exist_ok=True)
        TOP10_JSON.write_text(
            json.dumps({
                'date'      : datetime.now(self.KST).strftime('%Y%m%d'),
                'symbols'   : symbols,
                'kospi_set' : [s for s in symbols if s in self.kospi_symbols_set],
            }, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def _load_top10(self) -> dict:
        """오늘 날짜의 매수후보 + 코스피 구분 정보 복원"""
        if not TOP10_JSON.exists():
            return {'symbols': [], 'kospi_set': []}
        try:
            obj   = json.loads(TOP10_JSON.read_text(encoding='utf-8'))
            today = datetime.now(self.KST).strftime('%Y%m%d')
            if obj.get('date') == today:
                syms  = obj.get('symbols', [])
                kospi = obj.get('kospi_set', syms)
                logger.info(f"📂 매수후보 복원: {syms}")
                return {'symbols': syms, 'kospi_set': kospi}
        except Exception:
            pass
        return {'symbols': [], 'kospi_set': []}

    # ── 종목 리스트 ────────────────────────────────────────────────────────

    def get_kospi_symbols(self) -> list[str]:
        try:
            codes = fdr.StockListing("KOSPI")["Code"].tolist()
            return [c for c in codes if is_valid_code(c)]
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

        for r in all_results:
            if not r['data']:
                continue
            sym    = r['symbol']
            market = 'KOSPI' if sym in self.kospi_symbols_set else 'KOSDAQ'
            self.db.insert_price_data(sym, r['name'], market, r['data'])
            sector_bonus = self.sector_monitor.get_sector_bonus(sym)
            score = self.analyzer.calculate_score(sym, r['data'], sector_bonus=sector_bonus)
            price_map[sym] = r['data']['close']
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

        self.top_10_symbols = top_kospi + top_kosdaq
        self._save_top10(self.top_10_symbols)

        elapsed = time.time() - t0
        logger.info(f"\n✅ 스크리닝 완료! ({elapsed:.1f}초)\n")

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

        self.is_market_open = True
        self.last_rescreen_time = time.time()

    # ── 장중 1시간 재선정 ──────────────────────────────────────────────────

    def hourly_rescreen(self):
        """
        장중 1시간마다 매수후보 재선정.
        - 보유 종목은 항상 감시 유지
        - 기존 후보와 50% 이상 겹치면 전체 교체, 미만이면 점진 교체 (상위 8개 유지 + 신규 2개)
        - 아침 스크리닝과 동일한 데이터 소스 사용
        """
        now_str = datetime.now(self.KST).strftime('%H:%M:%S')
        logger.info("=" * 70)
        logger.info(f"🔄 장중 재선정 시작 — {now_str}")
        logger.info("=" * 70)

        kospi_syms  = self.get_kospi_symbols()
        kosdaq_syms = [s for s in self.get_kosdaq_symbols(self.KOSDAQ_TOP_N)
                       if s not in set(kospi_syms)]
        all_syms    = kospi_syms + kosdaq_syms

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

        # 장중 KIS 잔고 재동기화 — 로컬 예수금 drift 방지
        try:
            balance = self.kis_client.get_balance()
            if balance:
                self.position_mgr.sync_from_api(
                    holdings=balance.get('holdings', {}),
                    cash=balance.get('cash', 0),
                )
        except Exception as e:
            logger.warning(f"⚠️ 장중 잔고 재동기화 실패 — 로컬 유지: {e}")

        # 시장 변동성 상태 갱신
        self._update_market_condition()

    # ── 09:00~15:30 장중 모니터링 ─────────────────────────────────────────

    def realtime_monitoring(self):
        """
        매수후보(top_10_symbols) → 매수 신호만 체크
        보유 종목(holdings)      → 매도 타이밍만 체크
        """
        if not self.is_market_open:
            return

        self._sync_portfolio_from_kis_throttled("장중 모니터링")

        holdings     = set(self.position_mgr.portfolio['holdings'].keys())
        watch_set    = set(self.top_10_symbols)
        all_watch    = list(watch_set | holdings)

        if not all_watch:
            return

        now_str = datetime.now(self.KST).strftime('%H:%M:%S')
        now_hm = int(datetime.now(self.KST).strftime('%H%M'))
        buy_cutoff_hm = int(os.getenv("NEW_BUY_CUTOFF_HHMM", "1430") or 1430)
        buy_start_hm  = int(os.getenv("BUY_START_HHMM", "0930") or 930)  # 장 시작 30분 안정화
        logger.info(
            f"[{now_str}] 🔍 매수후보 {len(watch_set)}개 | 보유 {len(holdings)}개 모니터링"
        )

        try:
            results = self.async_client.fetch_all_stocks(all_watch, kospi_set=self.kospi_symbols_set)

            # 보유 종목은 KIS API 개별 조회로 지표 교체 (yfinance 데이터 오래됨)
            kis_data: dict = {}
            for sym in holdings:
                try:
                    d = self.kis_client.get_daily_ohlcv(sym)
                    if d:
                        kis_data[sym] = d
                except Exception:
                    pass
            if kis_data:
                logger.info(f"  📡 보유종목 KIS 지표 갱신: {len(kis_data)}/{len(holdings)}개")

            # 현재가 KIS API 실시간 조회
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

                    # AI 매도 판단 — 수익 2%~15% 구간에서만 호출 (비용 최적화)
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
                        if ai_action == "FULL_SELL":
                            sell_queue.append((sym, holding_qty, cur_price, f"AI익절:{ai_reason}", None))
                            continue
                        elif ai_action == "PARTIAL_SELL" and not partial_done and holding_qty > 1:
                            p_qty = max(1, int(holding_qty * 0.5))
                            if p_qty < holding_qty:
                                sell_queue.append((sym, p_qty, cur_price, f"AI부분익절:{ai_reason}", None))
                                continue

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
                    if now_hm >= buy_cutoff_hm:
                        logger.debug(f"  [{sym}] 신규 매수 컷오프 이후 — 진입 보류")
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
                    signal = self.analyzer.detect_signal(sym, price_data, strong_market=strong_market)
                    if signal == 'BUY':
                        score = self.analyzer.calculate_score(sym, price_data)
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

                        market_tag = 'KS' if sym in self.kospi_symbols_set else 'KQ'
                        logger.warning(f"⚡ [{sym}/{market_tag}] 🟢 매수 신호! ₩{cur_price:,.0f} | 점수 {score:.1f}")
                        buy_queue.append((sym, cur_price, price_data))

            # ── 2단계: 매도 먼저 순서대로 실행 ─────────────────────────────
            if sell_queue:
                logger.info(f"  📋 매도 대기열: {len(sell_queue)}건 순서대로 실행")
            for idx, (sym, qty, price, reason, meta) in enumerate(sell_queue, 1):
                logger.warning(f"🛑 [{sym}] ({idx}/{len(sell_queue)}) {reason} 매도")
                with self._order_lock:
                    sell_success = self.execute_sell(sym, qty, price, reason=reason)
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

            # SCALP 프로필 종목은 과열/고점 매수 위험으로 진입 제외
            if os.getenv("SCALP_BUY_ENABLED", "false").lower() != "true":
                pre_score = self.analyzer.calculate_score(symbol, price_data)
                pre_profile, pre_reason = self._classify_trade_profile(symbol, price_data, pre_score)
                if pre_profile == "SCALP":
                    logger.info(
                        f"⛔ [{symbol}] 매수 스킵 — SCALP 프로필 진입 제외 ({pre_reason})"
                    )
                    return

            atr = float(price_data.get('atr') or price * 0.02)
            stop_loss = max(price - atr * 2.0, price * 0.97)
            risk_pct = float(os.getenv("RISK_PER_TRADE", "0.02"))
            max_position_pct = float(os.getenv("MAX_POSITION_PCT", "0.15"))
            signal_score = self.analyzer.calculate_score(symbol, price_data)
            full_position_score = float(os.getenv("FULL_POSITION_SCORE", "90") or 90)
            full_position_pct = float(os.getenv("FULL_POSITION_PCT", "0.4") or 0.4)
            effective_position_pct = (
                full_position_pct if signal_score >= full_position_score else max_position_pct
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
            value_cap = self.POSITION_AMOUNT
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
                self.position_mgr.add_position(symbol, quantity, expected_order_price, source='strategy')
                profile, profile_reason = self._classify_trade_profile(symbol, price_data, signal_score)
                target_plan = self._build_target_plan(expected_order_price, atr, profile)
                self.position_mgr.update_position_metadata(
                    symbol,
                    strategy_profile=profile,
                    strategy_profile_reason=profile_reason,
                    strategy_profile_time=datetime.now(self.KST).isoformat(),
                    effective_profile=profile,
                    effective_profile_reason="진입 프로필",
                    effective_profile_time=datetime.now(self.KST).isoformat(),
                    target_plan=target_plan,
                    target_stage_events={},
                    target_plan_time=datetime.now(self.KST).isoformat(),
                    last_target_action="진입 직후 목표가 설정",
                )
                logger.info(f"🧭 [{symbol}] 진입 전략 프로필: {profile} — {profile_reason}")
                if target_plan:
                    logger.info(
                        "  🎯 목표가: "
                        + " / ".join(
                            f"{item['stage']}차 ₩{item['target_price']:,.0f}({item['target_pct']:.1f}%)"
                            for item in target_plan
                        )
                    )
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
                    ],
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

    def execute_sell(self, symbol: str, quantity: int, price: float, reason: str = "") -> bool:
        try:
            logger.warning(
                f"\n💰 [{symbol}] 매도 주문!\n"
                f"   시간: {datetime.now(self.KST).strftime('%H:%M:%S')}\n"
                f"   가격: ₩{price:,.0f}  수량: {quantity}주\n"
                f"   금액: ₩{price * quantity:,.0f}"
            )
            previous_qty = self.position_mgr.get_holding_quantity(symbol)
            # 매도 전에 매수가 확보 (remove_position 이후 holdings에서 사라질 수 있음)
            holding_snap = self.position_mgr.portfolio.get('holdings', {}).get(symbol, {})
            buy_price_snap = float(holding_snap.get('price', 0) or 0)
            success = self.kis_client.place_sell_order(symbol, quantity, price)
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
                remaining_qty = self.position_mgr.get_holding_quantity(symbol)
                extra_lines = [f"잔여수량: {remaining_qty}주"]
                if reason:
                    extra_lines.insert(0, f"사유: {reason}")
                holding = self.position_mgr.portfolio.get('holdings', {}).get(symbol)
                if holding:
                    buy_price = float(holding.get('price', 0) or 0)
                    if buy_price > 0:
                        profit_pct = (price - buy_price) / buy_price * 100
                        extra_lines.append(f"기준수익률: {profit_pct:+.2f}%")
                self._notify_trade("SELL", symbol, quantity, price, extra_lines=extra_lines)
                return True
            else:
                logger.error(f"❌ [{symbol}] 매도 주문 실패 — 포지션 유지")
                return False

        except Exception as e:
            logger.error(f"❌ 매도 실패 ({symbol}): {e}")
            return False

    # ── 장 마감 ────────────────────────────────────────────────────────────

    def stop_monitoring(self):
        self.is_market_open = False
        self.last_rescreen_time = 0.0   # 다음 날 재선정을 위해 초기화
        logger.info("=" * 70)
        logger.info(f"🌙 장 마감 — {datetime.now(self.KST).strftime('%H:%M:%S')}")
        logger.info("   내일 08:30 스크리닝까지 대기")
        logger.info("=" * 70)
        self.show_balance()

    # ── 스케줄 설정 ────────────────────────────────────────────────────────

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
            "   09:00~14:30 — 5분마다 재선정+모니터링 / 30초마다 손절·트레일링\n"
            "   14:30~15:30 — 모니터링만 (재선정 스킵)\n"
            "   15:30 KST — 마감", os.getpid()
        )
        schedule.every().day.at("08:00").do(self._refresh_tokens_scheduled)
        schedule.every().day.at("08:30").do(self.morning_screening)
        schedule.every(30).seconds.do(self._holdings_gate)  # 보유 종목 손절/트레일링 빠른 체크
        schedule.every(5).minutes.do(self._intraday_gate)   # 매수 스캔 + 전체 모니터링
        schedule.every().day.at("15:30").do(self.stop_monitoring)

    def _intraday_gate(self):
        now = datetime.now(self.KST)
        if now.weekday() >= 5:
            return
        hm = now.hour * 100 + now.minute
        if hm < 900 or hm >= 1530:
            return

        buy_cutoff_hm = int(os.getenv("NEW_BUY_CUTOFF_HHMM", "1430") or 1430)

        # 매수 컷오프 이후(14:30~)는 재선정 불필요 — 보유 종목 감시만
        if hm >= buy_cutoff_hm:
            self.realtime_monitoring()
            return

        # 1시간마다 재선정 (모니터링보다 우선)
        if time.time() - self.last_rescreen_time >= self.RESCREEN_INTERVAL_SEC:
            try:
                self.hourly_rescreen()
            except Exception as e:
                logger.error(f"❌ 장중 재선정 실패: {e}")
                # 실패해도 last_rescreen_time 갱신 — 다음 틱에서 모니터링이 작동하도록
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


# ── 엔트리포인트 ─────────────────────────────────────────────────────────

def main():
    acquire_lock()
    logger.info("🧩 코드버전: kospi-v3.2 / profile-classifier / profit-harvest-reentry")
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
            time.sleep(10)


if __name__ == "__main__":
    main()
