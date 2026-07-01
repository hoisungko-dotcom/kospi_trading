"""
호환용 래퍼. 신규 코드는 AccountBalanceReporter를 사용한다.
"""
from core.account_balance_reporter import AccountBalanceReporter


class KISBalanceChecker(AccountBalanceReporter):
    """과거 코드 호환용 별칭."""
