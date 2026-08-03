"""
Thread-safe orderbook state store.

The websocket worker writes here.
Strategy code can:
    - poll get_view() / get_best()
    - register callbacks
    - block on wait_for_update()
"""

from __future__ import annotations

from collections.abc import Callable
from queue import Queue, Full, Empty
from threading import RLock
from typing import Any, Dict, Optional
import logging

from .book import Orderbook, OrderbookUpdate, OrderbookView, BestQuote


log = logging.getLogger(__name__)

Callback = Callable[[OrderbookUpdate], None]


class OrderbookStore:
    def __init__(self, *, update_queue_size: int = 10_000):
        self._lock = RLock()
        self._books: dict[str, Orderbook] = {}
        self._callbacks: list[Callback] = []
        self._update_queue_size = int(update_queue_size)
        self._updates: Queue[OrderbookUpdate] = Queue(maxsize=self._update_queue_size)

        # Queue statistics are intentionally about the notification queue only.
        # The live orderbook state is applied before notifications are queued.
        self._updates_enqueued = 0
        self._updates_consumed = 0
        self._updates_dropped_oldest = 0
        self._updates_dropped_newest = 0

    def register_callback(self, callback: Callback) -> None:
        """Register a callback called after each snapshot/delta."""
        with self._lock:
            self._callbacks.append(callback)

    def tickers(self) -> list[str]:
        with self._lock:
            return sorted(self._books.keys())

    def get_view(self, market_ticker: str) -> Optional[OrderbookView]:
        with self._lock:
            book = self._books.get(market_ticker.upper())
            return book.view() if book else None

    def get_best(self, market_ticker: str) -> Optional[BestQuote]:
        view = self.get_view(market_ticker)
        return view.best if view else None

    def views(self) -> list[OrderbookView]:
        with self._lock:
            return [b.view() for b in self._books.values()]

    def wait_for_update(self, timeout: float | None = None) -> Optional[OrderbookUpdate]:
        """Block until a snapshot/delta arrives, or return None on timeout."""
        try:
            update = self._updates.get(timeout=timeout)
        except Empty:
            return None

        with self._lock:
            self._updates_consumed += 1

        return update

    def update_queue_stats(self) -> dict[str, int]:
        """Return counters for the wait_for_update() notification queue."""
        with self._lock:
            return {
                "maxsize": self._update_queue_size,
                "qsize": self._updates.qsize(),
                "enqueued": self._updates_enqueued,
                "consumed": self._updates_consumed,
                "dropped_oldest": self._updates_dropped_oldest,
                "dropped_newest": self._updates_dropped_newest,
            }

    def apply_ws_message(self, data: Dict[str, Any]) -> Optional[OrderbookUpdate]:
        """Apply a raw WebSocket JSON message if it is an orderbook message."""
        msg_type = data.get("type")
        if msg_type == "orderbook_snapshot":
            return self.apply_snapshot(data)
        if msg_type == "orderbook_delta":
            return self.apply_delta(data)
        return None

    def apply_snapshot(self, data: Dict[str, Any]) -> OrderbookUpdate:
        msg = data.get("msg") or {}
        ticker = str(msg.get("market_ticker", "")).upper()
        if not ticker:
            raise ValueError(f"Snapshot missing market_ticker: {data}")

        with self._lock:
            book = self._books.setdefault(ticker, Orderbook(ticker))
            book.apply_snapshot(msg, seq=data.get("seq"))
            update = OrderbookUpdate(
                kind="snapshot",
                market_ticker=ticker,
                seq=data.get("seq"),
                view=book.view(),
                raw=data,
            )
            callbacks = list(self._callbacks)

        self._publish(update, callbacks)
        return update

    def apply_delta(self, data: Dict[str, Any]) -> OrderbookUpdate:
        msg = data.get("msg") or {}
        ticker = str(msg.get("market_ticker", "")).upper()
        if not ticker:
            raise ValueError(f"Delta missing market_ticker: {data}")

        with self._lock:
            book = self._books.setdefault(ticker, Orderbook(ticker))
            side, price_cents, delta = book.apply_delta(msg, seq=data.get("seq"))
            update = OrderbookUpdate(
                kind="delta",
                market_ticker=ticker,
                seq=data.get("seq"),
                view=book.view(),
                raw=data,
                side=side,
                price_cents=price_cents,
                delta=delta,
            )
            callbacks = list(self._callbacks)

        self._publish(update, callbacks)
        return update

    def _publish(self, update: OrderbookUpdate, callbacks: list[Callback]) -> None:
        """Publish an update notification after live book state has changed.

        Important: this queue is only for wait_for_update() notifications. The
        live book has already been updated before _publish() is called. If the
        notification queue is full, prefer freshness: remove the oldest queued
        notification and enqueue the newest update. That prevents stale wake-up
        events from crowding out fresh orderbook notifications.
        """
        self._enqueue_update_drop_oldest(update)

        for cb in callbacks:
            try:
                cb(update)
            except Exception:
                log.exception("Orderbook callback failed")

    def _enqueue_update_drop_oldest(self, update: OrderbookUpdate) -> None:
        """Enqueue newest notification; on overflow, evict the oldest one.

        This intentionally does NOT drop applied orderbook deltas. By the time
        this method runs, apply_snapshot()/apply_delta() has already updated the
        in-memory book. Only the optional wait_for_update() notification is
        coalesced.
        """
        try:
            self._updates.put_nowait(update)
            with self._lock:
                self._updates_enqueued += 1
            return
        except Full:
            pass

        dropped = None
        try:
            dropped = self._updates.get_nowait()
        except Empty:
            # Race/edge case: queue looked full but was drained before we could
            # evict. Just try to enqueue the fresh update below.
            pass

        try:
            self._updates.put_nowait(update)
        except Full:
            # Very unlikely with a single websocket producer, but keep a counter
            # so callers can see if the newest notification was ever lost.
            with self._lock:
                self._updates_dropped_newest += 1
            log.warning(
                "Orderbook update queue full even after oldest-drop; "
                "dropping newest notification for %s",
                update.market_ticker,
            )
            return

        with self._lock:
            self._updates_enqueued += 1
            if dropped is not None:
                self._updates_dropped_oldest += 1
