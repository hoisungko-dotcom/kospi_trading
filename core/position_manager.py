import os
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

if getattr(sys, 'frozen', False):
    PORTFOLIO_DIR = Path(sys.executable).parent / "paper_trading_logs"
else:
    PORTFOLIO_DIR = Path(__file__).parent.parent / "paper_trading_logs"


class PositionManager:
    """국내 주식 포지션 관리 (일일 JSON)"""

    def __init__(self, portfolio_dir: str | None = None):
        self._dir = Path(portfolio_dir) if portfolio_dir else PORTFOLIO_DIR
        self._dir.mkdir(exist_ok=True)
        self.portfolio = self._load()

    # ── 로드 / 저장 ───────────────────────────────────────────────────────

    def _portfolio_path(self, date_str: str) -> Path:
        return self._dir / f"portfolio_{date_str}.json"

    def _load(self) -> Dict:
        today = datetime.now().strftime('%Y%m%d')
        path = self._portfolio_path(today)

        if path.exists():
            try:
                with open(path, encoding='utf-8') as f:
                    return self._normalize_loaded_portfolio(json.load(f))
            except Exception as e:
                logger.warning(f"포트폴리오 로드 실패 ({path}): {e}")

        # 어제 파일에서 이월
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
        prev = self._portfolio_path(yesterday)
        if prev.exists():
            try:
                with open(prev, encoding='utf-8') as f:
                    return self._normalize_loaded_portfolio(json.load(f))
            except Exception:
                pass

        # 초기 상태
        initial_cash = float(os.getenv('ACCOUNT_BALANCE', '10000000'))
        return {
            'timestamp': datetime.now().isoformat(),
            'cash': initial_cash,
            'holdings': {},
            'total_value': initial_cash,
            'initial_capital': initial_cash,
            'profit_pct': 0.0,
        }

    def _normalize_loaded_portfolio(self, portfolio: Dict) -> Dict:
        """기존 포트폴리오 파일에는 source가 없으므로 legacy로 보정."""
        for h in portfolio.get('holdings', {}).values():
            h.setdefault('source', 'legacy')
        configured_initial = float(os.getenv('ACCOUNT_BALANCE', '0') or 0)
        loaded_initial = float(portfolio.get('initial_capital', 0) or 0)
        if configured_initial > 0 and (
            loaded_initial <= 0
            or loaded_initial > configured_initial * 5
            or loaded_initial < configured_initial * 0.2
        ):
            logger.warning(
                "⚠️ 초기자본 기준 보정: "
                f"₩{loaded_initial:,.0f} → ₩{configured_initial:,.0f}"
            )
            portfolio['initial_capital'] = configured_initial
        return portfolio

    def _recalc(self):
        """total_value, profit_pct 재계산"""
        holdings_value = sum(
            h.get('amount', h.get('price', 0) * h.get('quantity', 0))
            for h in self.portfolio['holdings'].values()
        )
        self.portfolio['total_value'] = self.portfolio['cash'] + holdings_value
        initial = self.portfolio.get('initial_capital', 10_000_000)
        self.portfolio['profit_pct'] = (
            (self.portfolio['total_value'] - initial) / initial * 100
            if initial else 0.0
        )

    def save(self):
        today = datetime.now().strftime('%Y%m%d')
        path = self._portfolio_path(today)
        try:
            self._recalc()
            self.portfolio['timestamp'] = datetime.now().isoformat()
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.portfolio, f, indent=2, ensure_ascii=False)
            logger.debug(f"💾 포트폴리오 저장: {path}")
        except Exception as e:
            logger.error(f"❌ 포트폴리오 저장 실패: {e}")

    # ── 포지션 조작 ───────────────────────────────────────────────────────

    def add_position(self, symbol: str, quantity: int, price: float, source: str = 'strategy') -> bool:
        """매수 체결 후 포지션 추가. 현금 부족 시 False 반환."""
        cost = quantity * price
        if self.portfolio['cash'] < cost:
            logger.warning(
                f"⚠️ 현금 부족 — {symbol} 매수 불가 "
                f"(필요 ₩{cost:,.0f} > 가용 ₩{self.portfolio['cash']:,.0f})"
            )
            return False

        h = self.portfolio['holdings']
        if symbol not in h:
            h[symbol] = {'quantity': 0, 'price': 0.0, 'amount': 0.0}

        old_qty  = h[symbol]['quantity']
        old_amt  = h[symbol]['amount']
        new_amt  = old_amt + cost
        new_qty  = old_qty + quantity

        h[symbol]['quantity']      = new_qty
        h[symbol]['price']         = new_amt / new_qty if new_qty else 0.0
        h[symbol]['amount']        = new_amt
        h[symbol]['highest_price'] = max(h[symbol].get('highest_price', 0), price)
        h[symbol]['source']        = source
        self.portfolio['cash'] -= cost

        logger.info(
            f"✅ 포지션 추가: {symbol} {quantity}주 @ ₩{price:,.0f} "
            f"| 구분 {source} | 잔여현금 ₩{self.portfolio['cash']:,.0f}"
        )
        self.save()
        return True

    def remove_position(self, symbol: str, quantity: int, price: float):
        """매도 체결 후 포지션 감소"""
        h = self.portfolio['holdings']
        if symbol not in h:
            logger.warning(f"⚠️ {symbol} 미보유")
            return

        if h[symbol]['quantity'] < quantity:
            logger.warning(
                f"⚠️ {symbol} 보유 수량 부족 "
                f"(보유 {h[symbol]['quantity']} < 매도 {quantity})"
            )
            quantity = h[symbol]['quantity']

        h[symbol]['quantity'] -= quantity
        self.portfolio['cash'] += quantity * price

        if h[symbol]['quantity'] == 0:
            del h[symbol]
        else:
            h[symbol]['amount'] = h[symbol]['quantity'] * h[symbol].get('price', price)

        logger.info(f"✅ 포지션 제거: {symbol} {quantity}주 @ ₩{price:,.0f}")
        self.save()

    def update_highest_price(self, symbol: str, current_price: float):
        """보유 종목의 최고가 갱신"""
        h = self.portfolio['holdings']
        if symbol in h:
            old_high = h[symbol].get('highest_price', 0)
            if current_price > old_high:
                h[symbol]['highest_price'] = current_price
                logger.debug(f"📈 [{symbol}] 최고가 갱신: ₩{old_high:,.0f} -> ₩{current_price:,.0f}")
                self.save()

    def sync_from_api(self, holdings: dict, cash: float) -> bool:
        """
        KIS API 잔고로 포트폴리오 덮어쓰기.
        holdings/cash 가 모두 비어있으면 동기화 거부(로컬 유지).
        """
        if not holdings and cash <= 0:
            logger.warning("⚠️ KIS API 잔고가 비어있음 — 로컬 포트폴리오 유지")
            return False

        current_total = float(self.portfolio.get('total_value', 0) or 0)
        new_holdings_value = sum(
            h.get('amount', h.get('price', 0) * h.get('quantity', 0))
            for h in holdings.values()
        )
        new_total = float(cash) + float(new_holdings_value)
        allow_large_drop = os.getenv("ALLOW_LARGE_API_SYNC_DROP", "false").lower() == "true"
        if (
            current_total > 0
            and new_total > 0
            and new_total < current_total * 0.75
            and not allow_large_drop
        ):
            logger.error(
                "🛑 KIS 계좌 동기화 차단: 총자산 급감 감지 "
                f"(₩{current_total:,.0f} → ₩{new_total:,.0f}). "
                "ALLOW_LARGE_API_SYNC_DROP=true 설정 없이는 로컬 포트폴리오를 덮어쓰지 않습니다."
            )
            return False

        existing_holdings = self.portfolio.get('holdings', {})
        normalized_holdings = {}
        for sym, info in holdings.items():
            normalized = dict(info)
            existing = existing_holdings.get(sym, {})
            normalized['source'] = existing.get('source', 'legacy')
            normalized['highest_price'] = max(
                float(existing.get('highest_price', 0) or 0),
                float(normalized.get('highest_price', 0) or 0),
            )
            for key in (
                'partial_take_profit_done',
                'partial_take_profit_price',
                'partial_take_profit_time',
                'target_plan',
                'target_stage_events',
                'target_plan_time',
                'last_target_action',
                'strategy_profile',
                'strategy_profile_reason',
                'strategy_profile_time',
                'effective_profile',
                'effective_profile_reason',
                'effective_profile_time',
            ):
                if key in existing:
                    normalized[key] = existing[key]
            normalized_holdings[sym] = normalized

        self.portfolio['holdings'] = normalized_holdings
        self.portfolio['cash']     = cash
        self.save()
        strategy_count = self.count_strategy_positions()
        legacy_count = len(self.portfolio['holdings']) - strategy_count
        logger.info(
            f"✅ KIS 계좌 동기화 완료: 예수금 ₩{cash:,.0f}, "
            f"보유종목 {len(holdings)}개 "
            f"(전략 {strategy_count}개, 기존 {legacy_count}개) {list(holdings.keys())}"
        )
        return True

    # ── 조회 ─────────────────────────────────────────────────────────────

    def get_holding_quantity(self, symbol: str) -> int:
        return self.portfolio['holdings'].get(symbol, {}).get('quantity', 0)

    def mark_partial_take_profit(self, symbol: str, price: float):
        h = self.portfolio.get('holdings', {})
        if symbol not in h:
            return
        h[symbol]['partial_take_profit_done'] = True
        h[symbol]['partial_take_profit_price'] = price
        h[symbol]['partial_take_profit_time'] = datetime.now().isoformat()
        self.save()

    def update_position_metadata(self, symbol: str, **metadata):
        """보유 포지션에 전략 타입 등 부가 정보를 저장."""
        h = self.portfolio.get('holdings', {})
        if symbol not in h:
            return
        for key, value in metadata.items():
            h[symbol][key] = value
        self.save()

    def count_strategy_positions(self) -> int:
        """봇이 신규 진입한 strategy 포지션만 카운트."""
        return sum(
            1 for h in self.portfolio.get('holdings', {}).values()
            if h.get('source') == 'strategy'
        )
