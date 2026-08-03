"""
In-memory Kalshi orderbook state.

Important:
    The websocket subscription sets use_yes_price=True.

With use_yes_price=True:
    book.yes = YES bids keyed by YES-price cents.
    book.no  = NO-side levels also keyed by YES-price cents.

That gives you:
    best YES bid = max(book.yes)
    best YES ask = min(book.no)

A no-side level at 70 means "YES ask 70", equivalent to "NO bid 30".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional
import time


def price_to_cents(value: str | int | float | Decimal) -> int:
    """
    Convert either:
        '0.4200' -> 42
        42       -> 42
        '42'     -> 42

    WebSocket FP fields are usually dollar strings. Some older examples use
    integer cents.
    """
    d = Decimal(str(value))
    if d <= Decimal("1.0"):
        d *= Decimal("100")
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def qty_to_decimal(value: str | int | float | Decimal | None) -> Decimal:
    """Parse fixed-point quantity like '13.00' into Decimal."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class BestQuote:
    market_ticker: str

    yes_bid_cents: Optional[int]
    yes_bid_qty: Decimal

    yes_ask_cents: Optional[int]
    yes_ask_qty: Decimal

    spread_cents: Optional[int]
    mid_yes_cents: Optional[float]

    no_bid_cents: Optional[int]
    no_ask_cents: Optional[int]

    @property
    def is_crossed(self) -> bool:
        return (
            self.yes_bid_cents is not None
            and self.yes_ask_cents is not None
            and self.yes_bid_cents >= self.yes_ask_cents
        )


@dataclass(frozen=True)
class OrderbookView:
    market_ticker: str
    yes: Mapping[int, Decimal]
    no: Mapping[int, Decimal]
    seq: Optional[int]
    ready: bool
    last_local_ts: float
    last_exchange_ts_ms: Optional[int]
    last_client_order_id: Optional[str]
    best: BestQuote


@dataclass(frozen=True)
class OrderbookUpdate:
    kind: str
    market_ticker: str
    seq: Optional[int]
    view: OrderbookView
    raw: Dict[str, Any]
    side: Optional[str] = None
    price_cents: Optional[int] = None
    delta: Optional[Decimal] = None


class Orderbook:
    """Mutable orderbook. Protect it with a lock in higher-level code."""

    def __init__(self, market_ticker: str):
        self.market_ticker = market_ticker.upper()
        self.yes: dict[int, Decimal] = {}
        self.no: dict[int, Decimal] = {}
        self.seq: Optional[int] = None
        self.ready = False
        self.last_local_ts = 0.0
        self.last_exchange_ts_ms: Optional[int] = None
        self.last_client_order_id: Optional[str] = None

    def apply_snapshot(self, msg: Dict[str, Any], *, seq: Optional[int] = None) -> None:
        """Replace current book with a full snapshot."""
        self.market_ticker = str(msg.get("market_ticker", self.market_ticker)).upper()
        self.yes = _levels_from_snapshot(msg, "yes")
        self.no = _levels_from_snapshot(msg, "no")
        self.seq = seq
        self.ready = True
        self.last_local_ts = time.time()
        self.last_exchange_ts_ms = msg.get("ts_ms")
        self.last_client_order_id = msg.get("client_order_id")

    def apply_delta(self, msg: Dict[str, Any], *, seq: Optional[int] = None) -> tuple[str, int, Decimal]:
        """
        Apply an incremental delta.

        Returns:
            (side, price_cents, delta)
        """
        side = str(msg.get("side", "")).lower()
        if side not in ("yes", "no"):
            raise ValueError(f"Unknown orderbook side: {side!r}")

        price_value = (
            msg.get("price_dollars")
            if "price_dollars" in msg
            else msg.get("price")
        )
        if price_value is None:
            raise ValueError(f"Delta missing price: {msg}")

        price_cents = price_to_cents(price_value)
        delta = qty_to_decimal(msg.get("delta_fp", msg.get("delta")))

        levels = self.yes if side == "yes" else self.no
        new_qty = levels.get(price_cents, Decimal("0")) + delta

        if new_qty <= 0:
            levels.pop(price_cents, None)
        else:
            levels[price_cents] = new_qty

        self.seq = seq
        self.ready = True
        self.last_local_ts = time.time()
        self.last_exchange_ts_ms = msg.get("ts_ms")
        self.last_client_order_id = msg.get("client_order_id")
        return side, price_cents, delta

    def best_quote(self) -> BestQuote:
        yes_bid = max(self.yes.keys()) if self.yes else None
        yes_ask = min(self.no.keys()) if self.no else None

        yes_bid_qty = self.yes.get(yes_bid, Decimal("0")) if yes_bid is not None else Decimal("0")
        yes_ask_qty = self.no.get(yes_ask, Decimal("0")) if yes_ask is not None else Decimal("0")

        spread = None
        mid = None
        if yes_bid is not None and yes_ask is not None:
            spread = yes_ask - yes_bid
            mid = (yes_bid + yes_ask) / 2.0

        # Reciprocal NO top of book derived from YES-scale levels.
        no_bid = (100 - yes_ask) if yes_ask is not None else None
        no_ask = (100 - yes_bid) if yes_bid is not None else None

        return BestQuote(
            market_ticker=self.market_ticker,
            yes_bid_cents=yes_bid,
            yes_bid_qty=yes_bid_qty,
            yes_ask_cents=yes_ask,
            yes_ask_qty=yes_ask_qty,
            spread_cents=spread,
            mid_yes_cents=mid,
            no_bid_cents=no_bid,
            no_ask_cents=no_ask,
        )

    def view(self) -> OrderbookView:
        # Copy the dicts so consumers cannot mutate live state.
        return OrderbookView(
            market_ticker=self.market_ticker,
            yes=MappingProxyType(dict(self.yes)),
            no=MappingProxyType(dict(self.no)),
            seq=self.seq,
            ready=self.ready,
            last_local_ts=self.last_local_ts,
            last_exchange_ts_ms=self.last_exchange_ts_ms,
            last_client_order_id=self.last_client_order_id,
            best=self.best_quote(),
        )


def _levels_from_snapshot(msg: Dict[str, Any], side: str) -> dict[int, Decimal]:
    """
    Extract snapshot levels from common Kalshi field spellings.

    Current FP docs often show:
        yes_dollars / no_dollars

    Some websocket examples have shown:
        yes_dollars_fp / no_dollars_fp

    Older examples sometimes show:
        yes / no
    """
    candidates = [
        f"{side}_dollars_fp",
        f"{side}_dollars",
        f"{side}_fp",
        side,
    ]

    raw_levels = None
    for key in candidates:
        if key in msg and msg[key] is not None:
            raw_levels = msg[key]
            break

    levels: dict[int, Decimal] = {}
    if not raw_levels:
        return levels

    for row in raw_levels:
        if not row or len(row) < 2:
            continue
        price, qty = row[0], row[1]
        qty_dec = qty_to_decimal(qty)
        if qty_dec > 0:
            levels[price_to_cents(price)] = qty_dec

    return levels
