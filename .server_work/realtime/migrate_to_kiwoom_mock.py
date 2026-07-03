from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from realtime.kiwoom_mock_broker import KiwoomMockDomesticBroker
from realtime.paper_engine import PaperEngine

KST = ZoneInfo("Asia/Seoul")
BOT_ROOT = Path(__file__).parents[1]
STATE_PATH = BOT_ROOT / "data" / "paper_state.json"
ARCHIVE_DIR = BOT_ROOT / "data" / "account_migrations"


def main() -> None:
    engine = PaperEngine()
    broker = KiwoomMockDomesticBroker()

    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = ARCHIVE_DIR / f"{stamp}_pre_kiwoom_migration.json"

    snapshot = {
        "migrated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "old_state_path": str(STATE_PATH),
        "old_state": json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {},
        "old_cumulative_stats": engine.cumulative_stats(),
    }
    archive_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    broker.reset_state()
    balance = broker.get_balance()
    holdings = broker.get_holdings()
    sync_ts = datetime.now(KST).strftime("%Y%m%d%H%M%S")
    engine.cash = float(balance.get("cash", 0) or 0)
    engine.positions = {}
    engine.trades = []
    engine.sync_from_broker(engine.cash, holdings, sync_ts)

    print(json.dumps({
        "archive_path": str(archive_path),
        "new_cash": int(engine.cash),
        "new_positions": len(engine.positions),
        "new_trades": len(engine.trades),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
