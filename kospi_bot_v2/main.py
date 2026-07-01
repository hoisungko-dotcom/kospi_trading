from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
from pathlib import Path
import signal
import time

from kospi_bot_v2.config.settings import PROJECT_ROOT, V2_ROOT, load_settings
from kospi_bot_v2.market.data_provider import CsvMarketDataProvider, write_sample_csv
from kospi_bot_v2.notifications import send_telegram
from runtime.market_hours import is_active_time, now_in_active_timezone
from kospi_bot_v2.shadow.snapshot import load_and_validate_snapshot


def _do_post_close_snapshot(
    runner: object,
    snap_base_dir: Path,
    max_retries: int = 3,
    sleep_sec: int = 60,
) -> bool:
    """Write the final EOD snapshot via the snapshot-only path with bounded retry.

    Returns True after a validated final snapshot is confirmed on disk.
    Returns False if all attempts are exhausted (operator alert required).
    Calls runner.run_post_close_snapshot_only() — no live broker operations.
    """
    _log = logging.getLogger(__name__)
    for attempt in range(1, max_retries + 1):
        try:
            runner.run_post_close_snapshot_only()  # type: ignore[union-attr]
            load_and_validate_snapshot(snap_base_dir)
            _log.info(
                "📸 post-close snapshot validated (attempt %d/%d)", attempt, max_retries
            )
            return True
        except Exception as exc:
            if attempt < max_retries:
                _log.warning(
                    "post-close snapshot attempt %d/%d failed, retry in %ds: %s",
                    attempt, max_retries, sleep_sec, exc,
                )
                time.sleep(sleep_sec)
            else:
                _log.error(
                    "post-close snapshot failed after %d attempts: %s", max_retries, exc
                )
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KR bot runner")
    parser.add_argument("--csv", type=Path, help="OHLCV CSV path for shadow evaluation")
    parser.add_argument("--sample", action="store_true", help="create and run with bundled sample CSV")
    parser.add_argument("--broker-quote", action="store_true", help="use broker quote-only market data")
    parser.add_argument("--loop", action="store_true", help="run repeatedly using V2_LOOP_INTERVAL_SEC")
    parser.add_argument("--notify", action="store_true", help="send Telegram summary when configured")
    parser.add_argument("--no-account", action="store_true", help="disable read-only account snapshot")
    parser.add_argument("--ignore-hours", action="store_true", help="run loop outside configured KST market window")
    parser.add_argument("--live", action="store_true", help="send real broker orders with the v4.3 engine")
    return parser.parse_args()


def setup_live_logging() -> None:
    log_dir = V2_ROOT / "logs"
    legacy_log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    legacy_log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s")
    for path in (log_dir / "live.log", legacy_log_dir / "trading.log"):
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=10 * 1024 * 1024,
            backupCount=14,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def load_daily_candidates() -> tuple[str, ...] | None:
    """Use the legacy morning screener output as the v4.3 live watch universe."""
    path = PROJECT_ROOT / "data" / "top_10_daily.json"
    if not path.exists():
        logging.getLogger(__name__).warning("daily candidate file not found: %s", path)
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logging.getLogger(__name__).warning("daily candidate file read failed: %s", exc)
        return None
    symbols = [str(s).strip() for s in payload.get("symbols", []) if str(s).strip()]
    if not symbols:
        logging.getLogger(__name__).warning("daily candidate file has no symbols: %s", path)
        return None
    symbols = list(dict.fromkeys(symbols))
    logging.getLogger(__name__).info(
        "📂 daily candidates loaded: %d symbols from %s", len(symbols), path
    )
    return tuple(symbols)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s - %(message)s")
    args = parse_args()
    settings = load_settings()
    if args.no_account:
        settings = type(settings)(**{**settings.__dict__, "include_account_snapshot": False})
    if args.live:
        setup_live_logging()
        logging.getLogger(__name__).warning("⚠️ KR live bot started — real broker orders enabled")
        daily_symbols = load_daily_candidates()
        if daily_symbols:
            settings = type(settings)(**{**settings.__dict__, "universe_symbols": daily_symbols})

    if args.broker_quote:
        from brokers.kis.quote_provider import KISQuoteOnlyProvider

        provider = KISQuoteOnlyProvider(settings.universe_symbols)
    else:
        csv_path = args.csv
        if args.sample or csv_path is None:
            csv_path = V2_ROOT / "data" / "sample_prices.csv"
            if not csv_path.exists():
                write_sample_csv(csv_path)
        provider = CsvMarketDataProvider(csv_path)

    if args.live:
        from runtime.live_runner import LiveRunner

        runner = LiveRunner(settings, provider)
        mode_label = "KR live bot"
    else:
        from kospi_bot_v2.runtime.shadow_runner import ShadowRunner

        runner = ShadowRunner(settings, provider)
        mode_label = "KR shadow bot"

    current_daily_symbols = tuple(settings.universe_symbols)

    def refresh_live_universe() -> None:
        nonlocal current_daily_symbols
        if not (args.live and args.broker_quote):
            return
        daily_symbols = load_daily_candidates()
        if not daily_symbols:
            return
        if daily_symbols == current_daily_symbols:
            return
        current_daily_symbols = daily_symbols
        if hasattr(provider, "symbols"):
            provider.symbols = daily_symbols
        logging.getLogger(__name__).warning(
            "📂 live universe refreshed: %d symbols", len(daily_symbols)
        )

    def run_and_print() -> None:
        refresh_live_universe()
        result = runner.run_once()
        summary = (
            f"{mode_label}: regime={result.regime.value}, "
            f"signals={len(result.signals)}, equity={result.equity:,.0f}, "
            f"report={result.report_path}"
        )
        print(summary, flush=True)

    if args.loop:
        _market_opened = [False]   # True while we are inside the active trading window
        _post_close_done = [False]  # True once the post-close snapshot run has fired today

        def _send_shutdown(signum=None, frame=None) -> None:
            if args.notify:
                now = now_in_active_timezone(settings)
                send_telegram(f"🛑 한국봇 종료\n시각: {now:%m/%d %H:%M KST}")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _send_shutdown)

        while True:
            try:
                active = args.ignore_hours or is_active_time(settings)
                if active:
                    if not _market_opened[0]:
                        # Newly entered the active window — reset post-close flag for today
                        _market_opened[0] = True
                        _post_close_done[0] = False
                        if args.notify:
                            now = now_in_active_timezone(settings)
                            send_telegram(
                                f"🔔 한국봇 개장\n"
                                f"시각: {now:%m/%d %H:%M KST}"
                            )
                    run_and_print()
                else:
                    if _market_opened[0] and not _post_close_done[0]:
                        # P0-1/P0-3: active → inactive transition.
                        # Live mode: snapshot-only path (no broker calls), with retry.
                        # Shadow mode: full run_once() is safe (no real orders).
                        _market_opened[0] = False
                        if args.live:
                            import os as _os
                            _snap_base = Path(
                                _os.getenv("SHADOW_STATE_DIR", "data/shadow_league")
                            )
                            if _do_post_close_snapshot(runner, _snap_base):
                                _post_close_done[0] = True
                        else:
                            try:
                                run_and_print()
                                _post_close_done[0] = True
                                logging.getLogger(__name__).info(
                                    "📸 post-close snapshot run completed (is_final=True)"
                                )
                            except Exception as exc:
                                logging.exception("post-close snapshot run failed: %s", exc)
                    else:
                        _market_opened[0] = False
                        now = now_in_active_timezone(settings)
                        print(
                            f"{mode_label}: sleeping outside active window "
                            f"now={now:%Y-%m-%d %H:%M:%S %Z} "
                            f"window={settings.active_start_hhmm:04d}-{settings.active_end_hhmm:04d}",
                            flush=True,
                        )
            except KeyboardInterrupt:
                _send_shutdown()
            except SystemExit:
                raise
            except Exception as exc:
                logging.exception("shadow loop failed: %s", exc)
            time.sleep(settings.loop_interval_sec)
    else:
        run_and_print()


if __name__ == "__main__":
    main()
