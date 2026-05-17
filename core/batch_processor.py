"""
23:00 배치 프로세서: 코스피+코스닥 전종목(~2700개) 스캔
기존 IntegratedSignal + MarketDataKOSPI 재사용
"""
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import FinanceDataReader as fdr

from core.market_data_kospi import MarketDataKOSPI
from core.integrated_signal import IntegratedSignal

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
DB_PATH  = DATA_DIR / "trading.db"


def _init_db():
    DATA_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                date          TEXT,
                symbol        TEXT,
                market        TEXT,
                final_score   REAL,
                signal_type   TEXT,
                price         REAL,
                smc_score     REAL,
                flow_score    REAL,
                ichimoku_score REAL,
                mtf_score     REAL,
                vol_candle_score REAL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                rank        INTEGER PRIMARY KEY,
                symbol      TEXT,
                market      TEXT,
                signal_type TEXT,
                final_score REAL,
                price       REAL,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS holdings (
                symbol        TEXT PRIMARY KEY,
                qty           INTEGER,
                buy_price     REAL,
                buy_date      TEXT,
                highest_price REAL,
                resistance    REAL DEFAULT 0
            )
        """)
        conn.commit()


class BatchProcessor:
    """매일 23:00 코스피+코스닥 전종목 분석 후 상위 10개 선별"""

    def __init__(self, kis_client, max_workers: int = 4):
        self.market_data   = MarketDataKOSPI(kis_client)
        self.signal_engine = IntegratedSignal()
        self.max_workers   = max_workers
        _init_db()

    def run(self) -> list:
        logger.info("=" * 70)
        logger.info("배치 시작: 코스피+코스닥 전종목 스캔")
        t0 = datetime.now()

        try:
            kospi_codes  = fdr.StockListing("KOSPI")["Code"].tolist()
            kosdaq_codes = fdr.StockListing("KOSDAQ")["Code"].tolist()
        except Exception as e:
            logger.error(f"종목 리스트 조회 실패: {e}")
            return []

        all_stocks = [(c, "KOSPI") for c in kospi_codes] + [(c, "KOSDAQ") for c in kosdaq_codes]
        logger.info(f"대상: 코스피 {len(kospi_codes)}개 + 코스닥 {len(kosdaq_codes)}개 = {len(all_stocks)}개")

        results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as exe:
            futs = {exe.submit(self._analyze, code, market): (code, market)
                    for code, market in all_stocks}
            done = 0
            for fut in as_completed(futs):
                done += 1
                try:
                    r = fut.result()
                    if r:
                        results.append(r)
                except Exception:
                    pass
                if done % 100 == 0:
                    logger.info(f"진행: {done}/{len(all_stocks)}")

        results.sort(key=lambda x: x["final_score"], reverse=True)
        self._save_signals(results)
        self._save_watchlist(results[:10])

        elapsed = (datetime.now() - t0).total_seconds()
        logger.info(f"배치 완료 | 신호: {len(results)}개 | 소요: {elapsed:.0f}초")
        logger.info("상위 10개:")
        for i, r in enumerate(results[:10], 1):
            logger.info(f"  [{i}] {r['symbol']:8s} ({r['market']:6s}) | "
                        f"{r['final_score']:.1%} | {r['signal_type']}")
        logger.info("=" * 70)
        return results[:10]

    def _analyze(self, symbol: str, market: str) -> dict | None:
        try:
            df = self.market_data.get_kospi_ohlcv(symbol, interval="1d", lookback=100)
            if df is None or df.empty or len(df) < 30:
                return None

            # 배치에서는 investor flow API 호출 생략 (속도 우선)
            final_score, scores = self.signal_engine.calculate_combined_score(
                symbol, df, None, []
            )
            recommendation, _ = self.signal_engine.get_recommendation(final_score)
            if recommendation == "PASS":
                return None

            return {
                "symbol":      symbol,
                "market":      market,
                "final_score": final_score,
                "signal_type": recommendation,
                "price":       float(df["close"].iloc[-1]),
                "scores":      scores,
                "date":        datetime.now().strftime("%Y%m%d"),
            }
        except Exception as e:
            logger.debug(f"_analyze {symbol}: {e}")
            return None

    def _save_signals(self, results: list):
        today = datetime.now().strftime("%Y%m%d")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM signals WHERE date = ?", (today,))
            conn.executemany("""
                INSERT INTO signals
                (date, symbol, market, final_score, signal_type, price,
                 smc_score, flow_score, ichimoku_score, mtf_score, vol_candle_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, [
                (r["date"], r["symbol"], r["market"], r["final_score"], r["signal_type"], r["price"],
                 r["scores"].get("SMC", 0),    r["scores"].get("Flow", 0.5),
                 r["scores"].get("Ichimoku", 0.5), r["scores"].get("MTF", 0.5),
                 r["scores"].get("VolCandle", 0))
                for r in results
            ])
        logger.info(f"DB 저장: {len(results)}건")

    def _save_watchlist(self, top10: list):
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM watchlist")
            conn.executemany("""
                INSERT INTO watchlist (rank, symbol, market, signal_type, final_score, price)
                VALUES (?,?,?,?,?,?)
            """, [
                (i + 1, r["symbol"], r["market"], r["signal_type"], r["final_score"], r["price"])
                for i, r in enumerate(top10)
            ])
        logger.info(f"워치리스트 갱신: {len(top10)}개")

    @staticmethod
    def record_buy(symbol: str, qty: int, price: float):
        """매수 체결 후 보유 기록"""
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO holdings (symbol, qty, buy_price, buy_date, highest_price, resistance)
                VALUES (?, ?, ?, ?, ?, 0)
            """, (symbol, qty, price, datetime.now().strftime('%Y%m%d'), price))

    @staticmethod
    def record_sell(symbol: str):
        """매도 체결 후 보유 기록 삭제"""
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM holdings WHERE symbol = ?", (symbol,))

    @staticmethod
    def load_holdings() -> dict:
        """DB에서 보유 종목 읽기"""
        if not DB_PATH.exists():
            return {}
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT symbol, qty, buy_price, buy_date, highest_price, resistance FROM holdings"
            ).fetchall()
        return {
            r[0]: {"qty": r[1], "price": r[2], "buy_date": r[3],
                   "highest_price": r[4], "resistance": r[5]}
            for r in rows
        }

    @staticmethod
    def load_watchlist() -> list[dict]:
        """DB에서 워치리스트 읽기 (main.py에서 호출)"""
        if not DB_PATH.exists():
            return []
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT symbol, market, signal_type, final_score, price FROM watchlist ORDER BY rank"
            ).fetchall()
        return [{"symbol": r[0], "market": r[1], "signal_type": r[2],
                 "final_score": r[3], "price": r[4]} for r in rows]
