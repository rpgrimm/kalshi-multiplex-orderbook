#!/usr/bin/env python3
"""
Example of strategy-style polling against the live orderbook manager.

This deliberately does NOT print every delta. It wakes up every --interval
seconds and reads the latest top of book for each market.
"""

from __future__ import annotations

import argparse
import logging
import time

from kx_orderbooks import SeriesOrderbookManager


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("series_ticker")
    parser.add_argument("--interval", type=float, default=0.25)
    parser.add_argument("--discovery", choices=["sdk", "rest"], default="sdk")
    parser.add_argument("--status", default="active,open")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    mgr = SeriesOrderbookManager.from_series(
        args.series_ticker,
        discovery=args.discovery,
        statuses=args.status,
    )
    mgr.start()

    try:
        while True:
            time.sleep(args.interval)

            for ticker in mgr.market_tickers:
                b = mgr.get_best(ticker)
                if b is None or b.yes_bid_cents is None or b.yes_ask_cents is None:
                    continue

                # Put your strategy logic here.
                if b.spread_cents is not None and b.spread_cents <= 3:
                    print(
                        f"{ticker}: tight book "
                        f"YES {b.yes_bid_cents}x{b.yes_ask_cents} "
                        f"NO {b.no_bid_cents}x{b.no_ask_cents}"
                    )
    finally:
        mgr.stop()


if __name__ == "__main__":
    raise SystemExit(main())
