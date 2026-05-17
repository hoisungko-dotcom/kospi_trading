"""
국내 주식 로컬 시계열 데이터베이스 (SQLite).
"""
import sqlite3
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

if getattr(sys, 'frozen', False):
    DB_PATH = Path(sys.executable).parent / "data" / "kospi_kosdaq.db"
else:
    DB_PATH = Path(__file__).parent.parent / "data" / "kospi_kosdaq.db"


class DatabaseManagerKospi:
    """국내 주식 OHLCV 시계열 DB 관리"""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(exist_ok=True)
        self._init()

    def _init(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stocks (
                    symbol       TEXT PRIMARY KEY,
                    name         TEXT,
                    market       TEXT,
                    last_updated TIMESTAMP,
                    last_price   REAL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol    TEXT    NOT NULL,
                    timestamp TEXT    NOT NULL,
                    open      REAL,
                    high      REAL,
                    low       REAL,
                    close     REAL,
                    volume    INTEGER,
                    UNIQUE(symbol, timestamp),
                    FOREIGN KEY(symbol) REFERENCES stocks(symbol)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_sym_ts "
                "ON price_history(symbol, timestamp)"
            )
            conn.commit()
        logger.info(f"✅ DB 초기화: {self.db_path}")

    # ── 쓰기 ─────────────────────────────────────────────────────────────

    def insert_price_data(self, symbol: str, name: str, market: str, price_data: Dict):
        """단일 종목 가격 저장"""
        ts = price_data.get('timestamp') or datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO price_history
                (symbol, timestamp, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, ts,
                price_data.get('open'),  price_data.get('high'),
                price_data.get('low'),   price_data.get('close'),
                price_data.get('volume'),
            ))
            conn.execute("""
                INSERT OR REPLACE INTO stocks
                (symbol, name, market, last_updated, last_price)
                VALUES (?, ?, ?, ?, ?)
            """, (symbol, name, market, datetime.now().isoformat(), price_data.get('close')))
            conn.commit()

    # ── 읽기 ─────────────────────────────────────────────────────────────

    def get_recent_data(self, symbol: str, days: int = 30) -> List[Dict]:
        """최근 N일 가격 이력 조회"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT symbol, timestamp, open, high, low, close, volume
                FROM price_history
                WHERE symbol = ? AND timestamp > ?
                ORDER BY timestamp DESC
            """, (symbol, cutoff)).fetchall()
        return [
            {'symbol': r[0], 'timestamp': r[1], 'open': r[2],
             'high': r[3], 'low': r[4], 'close': r[5], 'volume': r[6]}
            for r in rows
        ]
