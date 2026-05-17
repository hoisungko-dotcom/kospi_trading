"""
KIS API 계좌 잔고 조회 및 포트폴리오 출력.
KISClientKospi._client.get_kr_balance()의 raw 응답(output1/output2)을 파싱.
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class KISBalanceChecker:
    """KIS API 계좌 잔고 조회 및 포트폴리오 분석"""

    def __init__(self, kis_client):
        self.kis_client = kis_client

    def get_balance(self) -> Dict:
        """
        계좌 잔고 원시 조회.

        반환:
            {
                'rt_cd': '0',
                'account_summary': { dnca_tot_amt, tot_evaluat_amt, evlu_pfls_smtl_amt, ... },
                'holdings': [{ pdno, prdt_name, hldg_qty, pchs_avg_pric, prpr,
                               evlu_pfls_amt, evlu_pfls_rt, eval_amt }, ...],
                'error': None
            }
        """
        try:
            raw = self.kis_client._client.get_kr_balance()
        except AttributeError:
            # KISClient 직접 전달된 경우
            raw = self.kis_client.get_kr_balance()

        if not raw:
            return {'rt_cd': '999', 'account_summary': {}, 'holdings': [], 'error': '응답 없음'}

        if raw.get('rt_cd') != '0':
            msg = raw.get('msg1', 'Unknown Error')
            logger.error(f"❌ 잔고 조회 실패: {msg}")
            return {'rt_cd': raw.get('rt_cd', '1'), 'account_summary': {}, 'holdings': [], 'error': msg}

        # output2 → 계좌 요약 (list인 경우 첫 항목 사용)
        output2 = raw.get('output2', {})
        if isinstance(output2, list):
            output2 = output2[0] if output2 else {}

        # output1 → 보유종목 목록
        output1 = raw.get('output1', []) or []

        return {
            'rt_cd': '0',
            'account_summary': output2,
            'holdings': output1,
            'error': None,
        }

    def print_balance(self):
        """계좌 잔고 및 포트폴리오를 콘솔에 출력"""
        result = self.get_balance()

        if result['rt_cd'] != '0':
            print(f"❌ 오류: {result['error']}")
            return

        summary  = result['account_summary']
        holdings = result['holdings']

        def amt(key, default=0):
            try:
                return int(float(summary.get(key, default) or default))
            except (ValueError, TypeError):
                return default

        cash       = amt('dnca_tot_amt')
        stock_eval = amt('scts_evlu_amt')
        buy_total  = amt('pchs_amt_smtl_amt')
        pfls_total = amt('evlu_pfls_smtl_amt')

        # tot_evaluat_amt가 0인 모의계좌 대응: 예수금 + 주식평가금으로 계산
        total_eval = amt('tot_evaluat_amt') or (cash + stock_eval)

        # 수익률 필드가 없으면 직접 계산
        pfls_rate_raw = float(summary.get('evlu_pfls_rt1', 0) or summary.get('tot_stk_pfls_rt_smtl', 0) or 0)
        if pfls_rate_raw == 0 and buy_total > 0:
            pfls_rate = pfls_total / buy_total * 100
        else:
            pfls_rate = pfls_rate_raw

        print()
        print("=" * 70)
        is_mock = bool(getattr(getattr(self.kis_client, '_client', self.kis_client), 'is_mock', True))
        mode_label = "모의투자" if is_mock else "실전투자"
        print(f"  💰 KIS {mode_label} 계좌 잔고")
        print("=" * 70)

        print(f"\n  {'예수금 (현금)':<20} ₩{cash:>15,}")
        print(f"  {'주식 평가금액':<20} ₩{stock_eval:>15,}")
        print(f"  {'총 자산 (추정)':<20} ₩{total_eval:>15,}")
        print(f"  {'총 매입금액':<20} ₩{buy_total:>15,}")

        pfls_str = f"{pfls_total:+,}"
        print(f"  {'평가 손익':<20} ₩{pfls_str:>15}  ({pfls_rate:+.2f}%)")

        if holdings:
            print(f"\n  보유 종목 ({len(holdings)}개)")
            print("  " + "-" * 66)
            print(f"  {'종목명':<14} {'코드':<8} {'수량':>6} {'현재가':>10} {'평가금액':>14} {'손익':>14} {'수익률':>7}")
            print("  " + "-" * 66)

            for h in holdings:
                name      = (h.get('prdt_name') or 'N/A')[:12]
                code      = h.get('pdno', '')[:6]
                qty       = int(float(h.get('hldg_qty', 0) or 0))
                cur_price = int(float(h.get('prpr', 0) or 0))
                eval_amt  = int(float(h.get('evlu_amt', 0) or h.get('eval_amt', 0) or 0))
                pfls_amt  = int(float(h.get('evlu_pfls_amt', 0) or 0))
                pfls_rt   = float(h.get('evlu_pfls_rt', 0) or 0)

                pfls_str = f"{pfls_amt:+,}"
                print(
                    f"  {name:<14} {code:<8} {qty:>6,} "
                    f"{cur_price:>10,} {eval_amt:>14,} "
                    f"{pfls_str:>15} {pfls_rt:>+6.2f}%"
                )
        else:
            print("\n  보유 종목: 없음")

        print()
        print("=" * 70)
        print()
