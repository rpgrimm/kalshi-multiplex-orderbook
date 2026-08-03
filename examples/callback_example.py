#!/usr/bin/env python3
"""
Example of event-driven strategy code.

Your callback gets called on every snapshot/delta after the in-memory book has
already been updated.
"""

from __future__ import annotations

import argparse
import logging
import time

from kx_orderbooks import SeriesOrderbookManager, OrderbookUpdate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("series_ticker")
    parser.add_argument("--discovery", choices=["sdk", "rest"], default="sdk")
    parser.add_argument("--status", default="active,open")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    mgr = SeriesOrderbookManager.from_series(
        args.series_ticker,
        discovery=args.discovery,
        statuses=args.status,
    )

    def on_book_change(update: OrderbookUpdate) -> None:
        b = update.view.best
        if b.yes_bid_cents is None or b.yes_ask_cents is None:
            return

        # Example trigger: YES ask gets cheap.
        if b.yes_ask_cents <= 10:
            print(
                f"CHEAP YES? {update.market_ticker} "
                f"bid={b.yes_bid_cents} ask={b.yes_ask_cents} "
                f"seq={update.seq}"
            )

    mgr.register_callback(on_book_change)
    mgr.start()

    try:
        while True:
            time.sleep(1)
    finally:
        mgr.stop()


if __name__ == "__main__":
    raise SystemExit(main())
