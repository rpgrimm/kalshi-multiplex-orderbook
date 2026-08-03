"""
High-level manager: series ticker in, live multiplexed orderbooks out.

This is the class your strategy code should use.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Iterable, Optional
import logging

from .book import BestQuote, OrderbookUpdate, OrderbookView
from .discovery import MarketInfo, get_markets_for_series
from .store import OrderbookStore
from .ws_multiplex import MultiplexOrderbookWorker, DEFAULT_WS_URL


log = logging.getLogger(__name__)


class SeriesOrderbookManager:
    """
    Discover markets for a series ticker, then stream all books over one socket.

    Example:
        mgr = SeriesOrderbookManager.from_series("KXTRUMPMENTIONB")
        mgr.start()

        update = mgr.wait_for_update(timeout=5)
        best = mgr.get_best(update.market_ticker)

        mgr.stop()
    """

    def __init__(
        self,
        *,
        market_tickers: Iterable[str],
        markets: Iterable[MarketInfo] | None = None,
        ws_url: str = DEFAULT_WS_URL,
        use_yes_price: bool = True,
        log_raw: bool = False,
        update_queue_size: int = 10_000,
        api_key_id: str | None = None,
        private_key: object | None = None,
    ):
        self.market_tickers = sorted({t.upper() for t in market_tickers})
        self.markets = list(markets or [])
        self.store = OrderbookStore(update_queue_size=update_queue_size)
        self.worker = MultiplexOrderbookWorker(
            market_tickers=self.market_tickers,
            store=self.store,
            ws_url=ws_url,
            use_yes_price=use_yes_price,
            log_raw=log_raw,
            api_key_id=api_key_id,
            private_key=private_key,
        )

    @classmethod
    def from_series(
        cls,
        series_ticker: str,
        *,
        discovery: str = "sdk",
        statuses: str = "active,open",
        rest_host: str = "https://api.elections.kalshi.com/trade-api/v2",
        ws_url: str = DEFAULT_WS_URL,
        use_yes_price: bool = True,
        log_raw: bool = False,
        update_queue_size: int = 10_000,
        api_key_id: str | None = None,
        private_key: object | None = None,
    ) -> "SeriesOrderbookManager":
        markets = get_markets_for_series(
            series_ticker,
            discovery=discovery,
            statuses=statuses,
            rest_host=rest_host,
        )
        tickers = [m.ticker for m in markets]
        if not tickers:
            raise RuntimeError(f"No markets found for series {series_ticker!r} with statuses {statuses!r}")

        return cls(
            market_tickers=tickers,
            markets=markets,
            ws_url=ws_url,
            use_yes_price=use_yes_price,
            log_raw=log_raw,
            update_queue_size=update_queue_size,
            api_key_id=api_key_id,
            private_key=private_key,
        )

    @classmethod
    def from_market_tickers(
        cls,
        market_tickers: Iterable[str],
        *,
        ws_url: str = DEFAULT_WS_URL,
        use_yes_price: bool = True,
        log_raw: bool = False,
        update_queue_size: int = 10_000,
        api_key_id: str | None = None,
        private_key: object | None = None,
    ) -> "SeriesOrderbookManager":
        return cls(
            market_tickers=market_tickers,
            ws_url=ws_url,
            use_yes_price=use_yes_price,
            log_raw=log_raw,
            update_queue_size=update_queue_size,
            api_key_id=api_key_id,
            private_key=private_key,
        )

    def start(self) -> None:
        log.info("Starting orderbook manager with %d markets", len(self.market_tickers))
        self.worker.start()

    def stop(self) -> None:
        self.worker.stop()
        self.worker.join(timeout=5)

    def register_callback(self, callback: Callable[[OrderbookUpdate], None]) -> None:
        self.store.register_callback(callback)

    def wait_for_update(self, timeout: float | None = None) -> Optional[OrderbookUpdate]:
        return self.store.wait_for_update(timeout=timeout)

    def update_queue_stats(self) -> dict[str, int]:
        """Return counters for the wait_for_update() notification queue."""
        return self.store.update_queue_stats()

    def get_view(self, market_ticker: str) -> Optional[OrderbookView]:
        return self.store.get_view(market_ticker)

    def get_best(self, market_ticker: str) -> Optional[BestQuote]:
        return self.store.get_best(market_ticker)

    def views(self) -> list[OrderbookView]:
        return self.store.views()

    def add_markets(self, market_tickers: Iterable[str], *, get_snapshot: bool = True) -> bool:
        tickers = sorted({t.upper() for t in market_tickers})
        self.market_tickers = sorted(set(self.market_tickers) | set(tickers))
        return self.worker.add_markets(tickers, get_snapshot=get_snapshot)

    def delete_markets(self, market_tickers: Iterable[str]) -> bool:
        tickers = sorted({t.upper() for t in market_tickers})
        self.market_tickers = sorted(set(self.market_tickers) - set(tickers))
        return self.worker.delete_markets(tickers)

    def request_snapshots(self, market_tickers: Iterable[str] | None = None) -> bool:
        return self.worker.request_snapshots(market_tickers)
