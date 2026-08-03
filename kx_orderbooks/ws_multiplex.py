"""
One authenticated Kalshi websocket, many market orderbooks.

This is the piece you want for low overhead:
    - one auth handshake
    - one socket
    - one event loop
    - one state store keyed by market_ticker
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import threading
from typing import Any, Iterable, Optional

import websockets

from .auth import WS_PATH, create_ws_headers, load_auth_from_env
from .store import OrderbookStore


log = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


def _websockets_header_kwargs(headers: dict[str, str]) -> dict[str, Any]:
    """
    websockets changed the kwarg name from extra_headers to additional_headers.
    Support both so this works across Ubuntu/pip environments.
    """
    params = inspect.signature(websockets.connect).parameters
    if "additional_headers" in params:
        return {"additional_headers": headers}
    return {"extra_headers": headers}


class MultiplexOrderbookWorker(threading.Thread):
    """
    Maintains all requested market orderbooks over one WebSocket.

    Start it from sync code:
        worker.start()

    Stop it:
        worker.stop()
        worker.join()
    """

    def __init__(
        self,
        *,
        market_tickers: Iterable[str],
        store: OrderbookStore,
        ws_url: str = DEFAULT_WS_URL,
        api_key_id: str | None = None,
        private_key: Any | None = None,
        use_yes_price: bool = True,
        reconnect: bool = True,
        log_raw: bool = False,
        name: str = "kx-orderbook-mux",
    ):
        super().__init__(name=name, daemon=True)
        self.market_tickers = sorted({t.upper() for t in market_tickers})
        self.store = store
        self.ws_url = ws_url
        self.api_key_id = api_key_id
        self.private_key = private_key
        self.use_yes_price = use_yes_price
        self.reconnect = reconnect
        self.log_raw = log_raw

        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._subscribed_event = threading.Event()
        self._last_error: Optional[BaseException] = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._cmd_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._next_id = 1

        # Kalshi assigns a subscription ID (sid) to the orderbook_delta stream.
        # update_subscription, including get_snapshot, must name exactly one sid.
        # The worker owns writes; callers may ask for a refresh from other threads.
        self._sid_lock = threading.Lock()
        self._orderbook_sid: int | None = None

    @property
    def connected(self) -> bool:
        return self._connected_event.is_set()

    @property
    def subscribed(self) -> bool:
        return self._subscribed_event.is_set()

    @property
    def last_error(self) -> Optional[BaseException]:
        return self._last_error

    @property
    def orderbook_subscription_id(self) -> int | None:
        """Current orderbook_delta subscription ID, if the socket has subscribed."""
        with self._sid_lock:
            return self._orderbook_sid

    def _set_orderbook_subscription_id(self, sid: int | None) -> None:
        with self._sid_lock:
            self._orderbook_sid = sid

    def stop(self) -> None:
        self._stop_event.set()
        self._submit_command({"cmd": "_stop"})

    def _update_subscription(
        self,
        *,
        market_tickers: Iterable[str],
        action: str,
    ) -> bool:
        """Queue an update for the one active orderbook_delta subscription.

        Kalshi requires exactly one subscription ID for update_subscription. The
        original v35 snapshot-refresh request omitted it, producing server error
        code 12: "Exactly one subscription ID is required".
        """
        tickers = sorted({t.upper() for t in market_tickers})
        sid = self.orderbook_subscription_id
        if not tickers or sid is None:
            log.debug(
                "Cannot queue orderbook update_subscription action=%s tickers=%d sid=%s",
                action,
                len(tickers),
                sid,
            )
            return False

        return self._submit_command(
            {
                "cmd": "update_subscription",
                "params": {
                    "sids": [sid],
                    "market_tickers": tickers,
                    "action": action,
                },
            }
        )

    def add_markets(self, market_tickers: Iterable[str], *, get_snapshot: bool = True) -> bool:
        """Dynamically add markets to the existing websocket subscription."""
        tickers = sorted({t.upper() for t in market_tickers})
        if not tickers:
            return False

        self.market_tickers = sorted(set(self.market_tickers) | set(tickers))
        # Kalshi's update_subscription add_markets action returns the normal
        # snapshot/delta stream for added tickers; get_snapshot is retained only
        # for API compatibility with existing callers.
        return self._update_subscription(market_tickers=tickers, action="add_markets")

    def delete_markets(self, market_tickers: Iterable[str]) -> bool:
        """Dynamically remove markets from the existing websocket subscription."""
        tickers = sorted({t.upper() for t in market_tickers})
        if not tickers:
            return False

        self.market_tickers = sorted(set(self.market_tickers) - set(tickers))
        return self._update_subscription(market_tickers=tickers, action="delete_markets")

    def request_snapshots(self, market_tickers: Iterable[str] | None = None) -> bool:
        """Ask Kalshi to resend snapshots for some/all subscribed markets."""
        tickers = sorted({t.upper() for t in (market_tickers or self.market_tickers)})
        return self._update_subscription(market_tickers=tickers, action="get_snapshot")

    def _submit_command(self, cmd: dict[str, Any]) -> bool:
        """Thread-safe command submission into the worker's asyncio loop."""
        loop = self._loop
        queue = self._cmd_queue
        if loop is None or queue is None or loop.is_closed():
            return False
        asyncio.run_coroutine_threadsafe(queue.put(cmd), loop)
        return True

    def run(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self._last_error = exc
            log.exception("Fatal multiplex worker error")

    async def _run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._cmd_queue = asyncio.Queue()

        if self.api_key_id is None or self.private_key is None:
            self.api_key_id, self.private_key = load_auth_from_env()

        attempt = 0
        while not self._stop_event.is_set():
            try:
                await self._connect_and_stream()
                attempt = 0
                if not self.reconnect:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = exc
                self._connected_event.clear()
                self._subscribed_event.clear()
                attempt += 1

                if not self.reconnect or self._stop_event.is_set():
                    log.warning("Multiplex worker exiting after error: %s", exc)
                    return

                sleep_s = min(30.0, 0.5 * (2 ** min(attempt, 6)))
                sleep_s += random.random() * 0.25
                log.warning("WebSocket disconnected: %s; reconnecting in %.2fs", exc, sleep_s)
                await asyncio.sleep(sleep_s)

    async def _connect_and_stream(self) -> None:
        headers = create_ws_headers(
            api_key_id=self.api_key_id,
            private_key=self.private_key,
            method="GET",
            path=WS_PATH,
        )

        async with websockets.connect(
            self.ws_url,
            **_websockets_header_kwargs(headers),
        ) as ws:
            self._connected_event.set()
            self._subscribed_event.clear()
            self._set_orderbook_subscription_id(None)

            log.info("Connected Kalshi orderbook websocket with %d markets", len(self.market_tickers))
            await self._send_subscribe(ws)

            consumer = asyncio.create_task(self._consumer_loop(ws))
            producer = asyncio.create_task(self._producer_loop(ws))

            done, pending = await asyncio.wait(
                {consumer, producer},
                return_when=asyncio.FIRST_EXCEPTION,
            )

            for task in pending:
                task.cancel()

            for task in done:
                exc = task.exception()
                if exc:
                    raise exc

    async def _send_subscribe(self, ws: Any) -> None:
        if not self.market_tickers:
            raise RuntimeError("No market tickers to subscribe to")

        msg = {
            "id": self._next_msg_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": self.market_tickers,
                "use_yes_price": self.use_yes_price,
            },
        }
        await ws.send(json.dumps(msg))
        log.info("Subscribed request sent for %d markets", len(self.market_tickers))

    async def _producer_loop(self, ws: Any) -> None:
        while not self._stop_event.is_set():
            assert self._cmd_queue is not None
            cmd = await self._cmd_queue.get()

            if cmd.get("cmd") == "_stop":
                await ws.close()
                return

            wire = dict(cmd)
            wire.setdefault("id", self._next_msg_id())

            # Make sure every orderbook subscription/update keeps yes-price scale.
            params = wire.setdefault("params", {})
            if "channels" in params and "orderbook_delta" in params.get("channels", []):
                params.setdefault("use_yes_price", self.use_yes_price)

            await ws.send(json.dumps(wire))
            log.debug("Sent WS command: %s", wire)

    async def _consumer_loop(self, ws: Any) -> None:
        async for raw in ws:
            if self._stop_event.is_set():
                return

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Non-JSON websocket message: %r", raw)
                continue

            if self.log_raw:
                log.info("WS RAW %s", data)

            msg_type = data.get("type")

            if msg_type == "subscribed":
                msg = data.get("msg") or {}
                if msg.get("channel") == "orderbook_delta" and msg.get("sid") is not None:
                    try:
                        sid = int(msg["sid"])
                    except (TypeError, ValueError):
                        log.warning("Invalid orderbook subscription ID in message: %s", data)
                    else:
                        self._set_orderbook_subscription_id(sid)
                        log.info("Orderbook subscription ready: sid=%s", sid)
                self._subscribed_event.set()
                log.info("Subscribed: %s", data)
                continue

            if msg_type == "error":
                log.error("Kalshi websocket error: %s", data)
                continue

            try:
                update = self.store.apply_ws_message(data)
                if update is not None and update.view.best.is_crossed:
                    log.warning(
                        "Crossed book? %s YES bid=%s ask=%s seq=%s",
                        update.market_ticker,
                        update.view.best.yes_bid_cents,
                        update.view.best.yes_ask_cents,
                        update.seq,
                    )
            except Exception:
                log.exception("Failed applying websocket message: %s", data)

    def _next_msg_id(self) -> int:
        msg_id = self._next_id
        self._next_id += 1
        return msg_id
