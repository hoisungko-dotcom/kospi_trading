from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging

from brokers.kis.api_client import KISClient


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountHolding:
    symbol: str
    name: str
    quantity: int
    avg_price: float
    current_price: float
    eval_amount: float
    pnl_amount: float
    pnl_pct: float


@dataclass(frozen=True)
class AccountSnapshot:
    timestamp: datetime
    mode: str
    cash: float
    stock_eval: float
    total_eval: float
    buy_total: float
    pnl_amount: float
    pnl_pct: float
    holdings: list[AccountHolding]
    error: str | None = None


class ReadOnlyBrokerAccountClient:
    """Read-only broker account adapter for v2 reports.

    This class intentionally exposes no order methods. It only calls broker
    inquire-balance and parses the response for reporting.
    """

    def __init__(self):
        self.client = KISClient()
        self.client.trade_token = self.client._get_token(
            self.client.trade_base_url,
            self.client.trade_appkey,
            self.client.trade_appsecret,
        )
        if not self.client.trade_token:
            raise RuntimeError("Broker trade token unavailable for read-only balance.")

    def snapshot(self) -> AccountSnapshot:
        raw = self.client.get_kr_balance()
        if not raw or raw.get("rt_cd") != "0":
            return AccountSnapshot(
                timestamp=datetime.now(),
                mode=self._mode(),
                cash=0,
                stock_eval=0,
                total_eval=0,
                buy_total=0,
                pnl_amount=0,
                pnl_pct=0,
                holdings=[],
                error=str(raw or "no response"),
            )

        summary = raw.get("output2", {})
        if isinstance(summary, list):
            summary = summary[0] if summary else {}

        cash = self._num(summary, "dnca_tot_amt")
        stock_eval = self._num(summary, "scts_evlu_amt")
        buy_total = self._num(summary, "pchs_amt_smtl_amt")
        pnl_amount = self._num(summary, "evlu_pfls_smtl_amt")
        total_eval = self._num(summary, "tot_evaluat_amt") or (cash + stock_eval)
        pnl_pct = self._num(summary, "evlu_pfls_rt1") or self._num(summary, "tot_stk_pfls_rt_smtl")
        if not pnl_pct and buy_total > 0:
            pnl_pct = pnl_amount / buy_total * 100

        holdings = []
        for row in raw.get("output1", []) or []:
            quantity = int(self._num(row, "hldg_qty"))
            if quantity <= 0:
                continue
            holdings.append(
                AccountHolding(
                    symbol=str(row.get("pdno", "")).strip(),
                    name=str(row.get("prdt_name", "") or row.get("prdt_abrv_name", "") or "").strip(),
                    quantity=quantity,
                    avg_price=self._num(row, "pchs_avg_pric"),
                    current_price=self._num(row, "prpr"),
                    eval_amount=self._num(row, "evlu_amt") or self._num(row, "eval_amt"),
                    pnl_amount=self._num(row, "evlu_pfls_amt"),
                    pnl_pct=self._num(row, "evlu_pfls_rt"),
                )
            )

        return AccountSnapshot(
            timestamp=datetime.now(),
            mode=self._mode(),
            cash=cash,
            stock_eval=stock_eval,
            total_eval=total_eval,
            buy_total=buy_total,
            pnl_amount=pnl_amount,
            pnl_pct=pnl_pct,
            holdings=holdings,
        )

    def _mode(self) -> str:
        return "MOCK" if self.client.is_mock else "LIVE"

    @staticmethod
    def _num(row: dict, key: str) -> float:
        try:
            return float(str(row.get(key, 0) or 0).replace(",", ""))
        except Exception:
            return 0.0


ReadOnlyAccountClient = ReadOnlyBrokerAccountClient
