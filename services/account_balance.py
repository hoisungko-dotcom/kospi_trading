"""
브로커 중립 계좌 잔고 리포터.
"""
import logging
from typing import Dict

logger = logging.getLogger(__name__)


class AccountBalanceReporter:
    """활성 브로커 계좌 잔고 및 포트폴리오 출력."""

    def __init__(self, account_client):
        self.account_client = account_client

    def get_balance(self) -> Dict:
        raw_client = getattr(self.account_client, "_client", self.account_client)
        get_raw_balance = getattr(raw_client, "get_kr_balance", None)
        if callable(get_raw_balance):
            raw = get_raw_balance()
            if not raw:
                return {"rt_cd": "999", "account_summary": {}, "holdings": [], "error": "응답 없음"}
            if raw.get("rt_cd") != "0":
                msg = raw.get("msg1", "Unknown Error")
                logger.error("❌ 잔고 조회 실패: %s", msg)
                return {"rt_cd": raw.get("rt_cd", "1"), "account_summary": {}, "holdings": [], "error": msg}

            output2 = raw.get("output2", {})
            if isinstance(output2, list):
                output2 = output2[0] if output2 else {}
            output1 = raw.get("output1", []) or []
            return {"rt_cd": "0", "account_summary": output2, "holdings": output1, "error": None}

        normalized = self.account_client.get_balance() or {}
        holdings = normalized.get("holdings", {}) or {}
        holding_rows = []
        for code, pos in holdings.items():
            qty = int(float(pos.get("quantity", 0) or 0))
            price = int(float(pos.get("current_price", pos.get("price", 0)) or 0))
            avg_price = int(float(pos.get("avg_price", pos.get("entry_price", 0)) or 0))
            eval_amt = int(qty * price)
            buy_amt = int(qty * avg_price)
            profit_amt = eval_amt - buy_amt
            profit_pct = (profit_amt / buy_amt * 100) if buy_amt > 0 else 0.0
            holding_rows.append({
                "prdt_name": pos.get("name", code),
                "pdno": code,
                "hldg_qty": qty,
                "prpr": price,
                "pchs_avg_pric": avg_price,
                "evlu_amt": eval_amt,
                "evlu_pfls_amt": profit_amt,
                "evlu_pfls_rt": profit_pct,
            })

        cash = int(float(normalized.get("cash", 0) or 0))
        stock_eval = sum(int(float(h.get("evlu_amt", 0) or 0)) for h in holding_rows)
        buy_total = sum(int(float(h.get("hldg_qty", 0) or 0)) * int(float(h.get("pchs_avg_pric", 0) or 0)) for h in holding_rows)
        profit_total = stock_eval - buy_total
        total_eval = cash + stock_eval
        profit_pct = (profit_total / buy_total * 100) if buy_total > 0 else 0.0
        return {
            "rt_cd": "0",
            "account_summary": {
                "dnca_tot_amt": cash,
                "scts_evlu_amt": stock_eval,
                "pchs_amt_smtl_amt": buy_total,
                "evlu_pfls_smtl_amt": profit_total,
                "tot_evaluat_amt": total_eval,
                "evlu_pfls_rt1": profit_pct,
            },
            "holdings": holding_rows,
            "error": None,
        }

    def print_balance(self):
        result = self.get_balance()
        if result["rt_cd"] != "0":
            print(f"❌ 오류: {result['error']}")
            return

        summary = result["account_summary"]
        holdings = result["holdings"]

        def amt(key, default=0):
            try:
                return int(float(summary.get(key, default) or default))
            except (ValueError, TypeError):
                return default

        cash = amt("dnca_tot_amt")
        stock_eval = amt("scts_evlu_amt")
        buy_total = amt("pchs_amt_smtl_amt")
        profit_total = amt("evlu_pfls_smtl_amt")
        total_eval = amt("tot_evaluat_amt") or (cash + stock_eval)
        profit_pct_raw = float(summary.get("evlu_pfls_rt1", 0) or summary.get("tot_stk_pfls_rt_smtl", 0) or 0)
        profit_pct = (profit_total / buy_total * 100) if profit_pct_raw == 0 and buy_total > 0 else profit_pct_raw

        raw_client = getattr(self.account_client, "_client", self.account_client)
        broker_name = str(getattr(raw_client, "broker_name", getattr(self.account_client, "broker_name", "broker"))).upper()
        is_mock = bool(getattr(raw_client, "is_mock", True))
        mode_label = "모의투자" if is_mock else "실전투자"

        print()
        print("=" * 70)
        print(f"  💰 {broker_name} {mode_label} 계좌 잔고")
        print("=" * 70)
        print(f"\n  {'예수금 (현금)':<20} ₩{cash:>15,}")
        print(f"  {'주식 평가금액':<20} ₩{stock_eval:>15,}")
        print(f"  {'총 자산 (추정)':<20} ₩{total_eval:>15,}")
        print(f"  {'총 매입금액':<20} ₩{buy_total:>15,}")
        print(f"  {'평가 손익':<20} ₩{profit_total:+15,}  ({profit_pct:+.2f}%)")

        if holdings:
            print(f"\n  보유 종목 ({len(holdings)}개)")
            print("  " + "-" * 66)
            print(f"  {'종목명':<14} {'코드':<8} {'수량':>6} {'현재가':>10} {'평가금액':>14} {'손익':>14} {'수익률':>7}")
            print("  " + "-" * 66)
            for h in holdings:
                name = (h.get("prdt_name") or "N/A")[:12]
                code = h.get("pdno", "")[:6]
                qty = int(float(h.get("hldg_qty", 0) or 0))
                cur_price = int(float(h.get("prpr", 0) or 0))
                eval_amt = int(float(h.get("evlu_amt", 0) or h.get("eval_amt", 0) or 0))
                profit_amt = int(float(h.get("evlu_pfls_amt", 0) or 0))
                profit_pct = float(h.get("evlu_pfls_rt", 0) or 0)
                print(
                    f"  {name:<14} {code:<8} {qty:>6,} "
                    f"{cur_price:>10,} {eval_amt:>14,} "
                    f"{profit_amt:+15,} {profit_pct:>+6.2f}%"
                )
        else:
            print("\n  보유 종목: 없음")

        print()
        print("=" * 70)
        print()
