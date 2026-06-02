from __future__ import annotations

import argparse
import logging

from main import KospiTopTenSystem


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the legacy broad screener once.")
    parser.add_argument(
        "--mode",
        choices=("morning", "rescreen"),
        default="morning",
        help="morning scans the broad universe; rescreen re-ranks the stored pool.",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s - %(message)s")
    args = parse_args()
    system = KospiTopTenSystem()

    if args.mode == "morning":
        logging.info("candidate scan started: broad morning screening")
        system.morning_screening()
        return

    if not system.rescan_pool:
        logging.warning("rescan pool is empty; falling back to broad morning screening")
        system.morning_screening()
        return

    logging.info("candidate scan started: intraday rescreen from %d symbols", len(system.rescan_pool))
    system._update_market_condition()
    system.hourly_rescreen()


if __name__ == "__main__":
    main()
