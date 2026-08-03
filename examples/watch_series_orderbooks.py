#!/usr/bin/env python3
"""
Watch all orderbooks in a Kalshi series over ONE multiplexed websocket.

Examples:
    ./examples/watch_series_orderbooks.py KXTRUMPMENTIONB
    ./examples/watch_series_orderbooks.py KXTRUMPMENTIONB --print-top 20
    ./examples/watch_series_orderbooks.py KXTRUMPMENTIONB --discovery rest --status open

Required env:
    export KALSHI_API_KEY_ID='...'
    export KALSHI_PRIVATE_KEY_FILE="$HOME/path/to/private_key.pem"

Install deps:
    pip install websockets cryptography kalshi_python_sync
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from kx_orderbooks import SeriesOrderbookManager, OrderbookUpdate


def fmt_cents(v):
    return "--" if v is None else f"{v:02d}c"


def _print_update(update: OrderbookUpdate) -> None:
    b = update.view.best
    print(
        f"{update.kind:<8} "
        f"{update.market_ticker:<36} "
        f"YES {fmt_cents(b.yes_bid_cents)} x {fmt_cents(b.yes_ask_cents)} "
        f"spr={fmt_cents(b.spread_cents)} "
        f"NO {fmt_cents(b.no_bid_cents)} x {fmt_cents(b.no_ask_cents)} "
        f"seq={update.seq}"
    )

def print_update(update: OrderbookUpdate) -> None:
    b = update.view.best

    changed = ""
    if update.kind == "delta" and update.side is not None:
        levels = update.view.yes if update.side == "yes" else update.view.no
        new_qty = levels.get(update.price_cents, 0)

        changed = (
            f"chg={update.side.upper()}@{fmt_cents(update.price_cents)} "
            f"delta={update.delta:+} "
            f"qty={new_qty}"
        )

    print(
        f"{update.kind:<8} "
        f"{update.market_ticker:<36} "
        f"YES {fmt_cents(b.yes_bid_cents)} x {fmt_cents(b.yes_ask_cents)} "
        f"spr={fmt_cents(b.spread_cents)} "
        f"NO {fmt_cents(b.no_bid_cents)} x {fmt_cents(b.no_ask_cents)} "
        f"seq={update.seq} "
        f"{changed}"
    )

def print_top(manager: SeriesOrderbookManager, n: int) -> None:
    rows = []
    for view in manager.views():
        b = view.best
        if b.yes_bid_cents is None and b.yes_ask_cents is None:
            continue
        rows.append((view.market_ticker, b))

    rows.sort(key=lambda x: (-1 if x[1].spread_cents is None else x[1].spread_cents, x[0]))

    print("\n--- TOP BOOKS ---")
    for ticker, b in rows[:n]:
        print(
            f"{ticker:<36} "
            f"YES {fmt_cents(b.yes_bid_cents)} x {fmt_cents(b.yes_ask_cents)} "
            f"spr={fmt_cents(b.spread_cents)} "
            f"NO {fmt_cents(b.no_bid_cents)} x {fmt_cents(b.no_ask_cents)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("series_ticker", help="Kalshi series ticker, e.g. KXTRUMPMENTIONB")
    parser.add_argument("--discovery", choices=["sdk", "rest"], default="sdk")
    parser.add_argument("--status", default="active,open", help="CSV statuses to include; default active,open")
    parser.add_argument("--rest-host", default="https://api.elections.kalshi.com/trade-api/v2")
    parser.add_argument("--ws-url", default="wss://external-api-ws.kalshi.com/trade-api/ws/v2")
    parser.add_argument("--raw", action="store_true", help="Log raw websocket messages")
    parser.add_argument("--print-every", type=float, default=0.0, help="Print summary every N seconds")
    parser.add_argument("--print-top", type=int, default=0, help="Print top N books in periodic summary")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    stop = False

    def handle_signal(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    manager = SeriesOrderbookManager.from_series(
        args.series_ticker,
        discovery=args.discovery,
        statuses=args.status,
        rest_host=args.rest_host,
        ws_url=args.ws_url,
        log_raw=args.raw,
    )

    print(f"Found {len(manager.market_tickers)} markets:")
    for t in manager.market_tickers:
        print(f"  {t}")

    manager.register_callback(print_update)
    manager.start()

    last_print = time.time()

    try:
        while not stop:
            # Callback prints updates. This wait just keeps main alive and gives
            # you a place to do periodic strategy checks.
            manager.wait_for_update(timeout=1.0)

            if args.print_every > 0 and time.time() - last_print >= args.print_every:
                last_print = time.time()
                print_top(manager, args.print_top or 20)
    finally:
        manager.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
