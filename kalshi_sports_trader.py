#!/usr/bin/env python3
"""kalshi_sports_trader.py — sports game market tools.

Slice 1: discover markets for a game id such as kxnflgame-26sep13atlpit.
Slice 1.5: stream live orderbooks over Kalshi WebSocket (not REST polling).

Architecture note
-----------------
Kalshi WebSockets stream orderbook/ticker/trade updates for *known* market
tickers. They do not replace catalog discovery: listing which markets exist
for a game still needs a small number of REST catalog calls.

This tool therefore:
  1) discovers related markets with a tight REST catalog path
     (GET /events/{event}?with_nested_markets=true per candidate series)
  2) streams live books via the existing kx_orderbooks multiplex WebSocket
     (one auth handshake, one socket, many markets)

Keep REST budget for discovery + later orders/account snapshots. Do not poll
REST orderbooks continuously.

Examples:
    ./kalshi_sports_trader.py kxnflgame-26sep13atlpit
    ./kalshi_sports_trader.py kxnflgame-26sep13atlpit --watch
    ./kalshi_sports_trader.py kxnflgame-26sep13atlpit --watch --watch-limit 40

Auth for --watch (same as kx_orderbooks / broadcast trader):
    export KALSHI_API_KEY_ID=...
    export KALSHI_PRIVATE_KEY_FILE=/path/to/key.pem
  or KALSHI_PROD_* / KALSHI_DEMO_* variants.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_REST_HOST = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
USER_AGENT = "kalshi-sports-trader/0.2"

SEASON_LONG_HINTS = (
    "WINS",
    "EXACTWINS",
    "DRAFT",
    "PLAYOFF",
    "CHAMP",
    "DIVISION",
    "COACH",
    "MVP",
    "ROY",
    "POTY",
    "ALLPRO",
    "HALLOFFAME",
    "HOF",
    "CONTRACT",
    "COMBINE",
    "SEASON",
    "MENTION",
    "DEPTHPOSITION",
    "COMBO",
)

# Default probe set: high-signal game-scoped series only (avoids 100+ empty 429s).
NFL_PRIORITY_SERIES = [
    "KXNFLGAME",
    "KXNFLSPREAD",
    "KXNFLTOTAL",
    "KXNFLTEAMTOTAL",
    "KXNFLWINMARGIN",
    "KXNFL1H",
    "KXNFL1HSPREAD",
    "KXNFL1HTOTAL",
    "KXNFL1HTEAMTOTAL",
    "KXNFL1HFT",
    "KXNFL2H",
    "KXNFL2HSPREAD",
    "KXNFL2HTOTAL",
    "KXNFL1Q",
    "KXNFL1QSPREAD",
    "KXNFL1QTOTAL",
    "KXNFL2Q",
    "KXNFL2QSPREAD",
    "KXNFL2QTOTAL",
    "KXNFL3Q",
    "KXNFL3QSPREAD",
    "KXNFL3QTOTAL",
    "KXNFL4Q",
    "KXNFL4QSPREAD",
    "KXNFL4QTOTAL",
    "KXNFLANYTD",
    "KXNFLFIRSTTD",
    "KXNFLFIRSTTDTEAM",
    "KXNFLPASSYDS",
    "KXNFLPASSTDS",
    "KXNFLPASSATT",
    "KXNFLPASSCOMP",
    "KXNFLPASSINT",
    "KXNFLRSHYDS",
    "KXNFLRSHATT",
    "KXNFLREC",
    "KXNFLRECYDS",
    "KXNFLRRYDS",
    "KXNFLMOSTRECYDS",
    "KXNFLMOSTRSHYDS",
    "KXNFLTOTALTD",
    "KXNFLTD",
    "KXNFLTEAMTD",
    "KXNFLTEAMYDS",
    "KXNFLTEAMSACK",
    "KXNFLFG",
    "KXNFLGAMESPECIALS",
    "KXNFLGAMETD",
    "KXNFLGAMEFG",
    "KXNFLGAMESACK",
    "KXNFLLONGREC",
    "KXNFLLONGRSH",
    "KXNFLBOTH",
    "KXNFLFFPTS",
]


@dataclass(frozen=True)
class MarketRow:
    ticker: str
    title: str
    event_ticker: str
    series_ticker: str
    status: str
    yes_sub_title: str = ""
    no_sub_title: str = ""
    raw: dict[str, Any] | None = None


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def normalize_status_filter(status: str | None) -> set[str]:
    if status is None:
        return set()
    text = str(status).strip().lower()
    if text in ("", "all", "*"):
        return set()
    return {p.strip().lower() for p in text.split(",") if p.strip()}


def parse_game_input(raw: str) -> tuple[str, str]:
    """Return (series_ticker, game_code) from URL or SERIES-GAMECODE text."""
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("empty game id")

    if "://" in text or (text.count("/") >= 1 and " " not in text):
        path = urllib.parse.urlparse(text).path if "://" in text else text
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError(f"could not parse game id from URL path: {raw!r}")
        text = parts[-1]

    text = urllib.parse.unquote(text)
    text = re.sub(r"\s+", "", text).upper().strip().strip("-")
    pieces = [p for p in text.split("-") if p]
    if len(pieces) >= 3 and len(pieces[-1]) <= 6 and pieces[-1].isalpha():
        text = "-".join(pieces[:-1])
        pieces = [p for p in text.split("-") if p]

    if len(pieces) < 2:
        raise ValueError(
            "expected SERIES-GAMECODE such as KXNFLGAME-26SEP13ATLPIT "
            f"(got {raw!r})"
        )

    series = pieces[0]
    game_code = "-".join(pieces[1:])
    if not series or not game_code:
        raise ValueError(f"expected SERIES-GAMECODE (got {raw!r})")
    return series, game_code


def league_prefix_from_series(series_ticker: str) -> str:
    s = series_ticker.upper()
    for prefix in ("KXNCAAF", "KXNCAAB", "KXNFL", "KXNBA", "KXMLB", "KXNHL"):
        if s.startswith(prefix):
            return prefix
    m = re.match(r"^(KX[A-Z]{2,10})", s)
    return m.group(1) if m else s


def looks_season_long(series_ticker: str) -> bool:
    s = series_ticker.upper()
    if "SEASON" in s or "DRAFT" in s or "EXACTWINS" in s or "MENTION" in s:
        return True
    if "DEPTHPOSITION" in s or "COMBO" in s:
        return True
    if re.search(r"WINS(?:[A-Z]{2,})?$", s) and "GAME" not in s:
        return True
    gameish = any(
        tok in s
        for tok in (
            "GAME",
            "SPREAD",
            "TOTAL",
            "ANYTD",
            "FIRSTTD",
            "PASS",
            "RSH",
            "REC",
            "1H",
            "2H",
            "1Q",
            "2Q",
            "3Q",
            "4Q",
            "TD",
            "FG",
            "SACK",
            "MARGIN",
        )
    )
    if gameish:
        return False
    return any(h in s for h in SEASON_LONG_HINTS)
def public_get_json(
    host: str,
    path: str,
    params: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    base = host.rstrip("/")
    query = urllib.parse.urlencode(
        {k: v for k, v in (params or {}).items() if v not in (None, "")}
    )
    url = base + path + (("?" + query) if query else "")
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        if not raw:
            return {}
        return json.loads(raw)


def get_json_with_retries(
    host: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = 20.0,
    max_retries: int = 8,
    label: str = "",
) -> dict[str, Any]:
    delay = 0.5
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return public_get_json(host, path, params=params, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code == 404:
                raise
            if exc.code != 429 or attempt >= max_retries:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                sleep_s = float(retry_after) if retry_after else delay
            except ValueError:
                sleep_s = delay
            sleep_s = min(max(sleep_s, 0.25), 30.0) + random.uniform(0, 0.25)
            eprint(
                f"429 rate limit on {label or path}; sleep {sleep_s:.2f}s "
                f"(attempt {attempt + 1})"
            )
            time.sleep(sleep_s)
            delay = min(delay * 1.8, 20.0)
        except urllib.error.URLError as exc:
            last_err = exc
            if attempt >= max_retries:
                raise
            sleep_s = min(delay, 10.0) + random.uniform(0, 0.2)
            eprint(f"network error on {label or path}: {exc}; sleep {sleep_s:.2f}s")
            time.sleep(sleep_s)
            delay = min(delay * 1.5, 15.0)
    raise RuntimeError(f"failed GET {path}: {last_err}")


def series_from_event_ticker(event_ticker: str) -> str:
    et = event_ticker.upper()
    if "-" not in et:
        return et
    return et.split("-", 1)[0]


def market_row_from_raw(raw: dict[str, Any]) -> MarketRow | None:
    ticker = str(raw.get("ticker") or "").upper()
    if not ticker:
        return None
    event_ticker = str(raw.get("event_ticker") or "").upper()
    return MarketRow(
        ticker=ticker,
        title=str(raw.get("title") or ""),
        event_ticker=event_ticker,
        series_ticker=(
            series_from_event_ticker(event_ticker)
            if event_ticker
            else series_from_event_ticker(ticker)
        ),
        status=str(raw.get("status") or ""),
        yes_sub_title=str(raw.get("yes_sub_title") or ""),
        no_sub_title=str(raw.get("no_sub_title") or ""),
        raw=raw,
    )


def fetch_markets_for_event_via_event_endpoint(
    host: str,
    event_ticker: str,
    *,
    statuses: set[str],
) -> list[dict[str, Any]] | None:
    """One REST call: GET /events/{event_ticker}?with_nested_markets=true.

    Returns None if the event does not exist (404). Prefer this over paginated
    /markets fan-out so discovery spends fewer read tokens.
    """
    path = f"/events/{urllib.parse.quote(event_ticker, safe='')}"
    try:
        data = get_json_with_retries(
            host,
            path,
            {"with_nested_markets": "true"},
            label=f"event:{event_ticker}",
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise

    event = data.get("event") if isinstance(data.get("event"), dict) else {}
    markets = event.get("markets") if isinstance(event, dict) else None
    if markets is None:
        markets = data.get("markets")
    if not isinstance(markets, list):
        return []

    out: list[dict[str, Any]] = []
    for raw in markets:
        if not isinstance(raw, dict):
            continue
        st = str(raw.get("status") or "").lower()
        if statuses and st not in statuses:
            continue
        # Ensure event_ticker is present for downstream grouping.
        raw = dict(raw)
        raw.setdefault("event_ticker", event_ticker)
        out.append(raw)
    return out


def fetch_markets_for_event_via_markets_endpoint(
    host: str,
    event_ticker: str,
    *,
    statuses: set[str],
    page_limit: int = 200,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """Fallback paginated GET /markets?event_ticker=..."""
    markets: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    for page in range(1, max_pages + 1):
        params: dict[str, Any] = {
            "event_ticker": event_ticker,
            "limit": str(page_limit),
        }
        if cursor:
            params["cursor"] = cursor
        data = get_json_with_retries(
            host,
            "/markets",
            params,
            label=f"markets:{event_ticker}:p{page}",
        )
        page_markets = data.get("markets") or []
        if not isinstance(page_markets, list):
            raise RuntimeError(f"unexpected /markets shape for {event_ticker}")
        for raw in page_markets:
            if not isinstance(raw, dict):
                continue
            st = str(raw.get("status") or "").lower()
            if statuses and st not in statuses:
                continue
            markets.append(raw)
        cursor = str(data.get("cursor") or "")
        if not cursor:
            break
        if cursor in seen_cursors:
            raise RuntimeError(f"cursor loop for {event_ticker}")
        seen_cursors.add(cursor)
    return markets


def fetch_markets_for_event(
    host: str,
    event_ticker: str,
    *,
    statuses: set[str],
    discovery: str = "event",
) -> list[dict[str, Any]]:
    discovery = (discovery or "event").strip().lower()
    if discovery not in ("event", "markets", "auto"):
        raise ValueError(f"unknown discovery mode: {discovery!r}")

    if discovery in ("event", "auto"):
        nested = fetch_markets_for_event_via_event_endpoint(
            host, event_ticker, statuses=statuses
        )
        if nested is None:
            return []
        if nested or discovery == "event":
            return nested
        # auto + empty nested list: still try markets endpoint once
    return fetch_markets_for_event_via_markets_endpoint(
        host, event_ticker, statuses=statuses
    )


def list_series_tickers(host: str, prefix: str) -> list[str]:
    data = get_json_with_retries(host, "/series", label="series")
    out: list[str] = []
    for row in data.get("series") or []:
        if not isinstance(row, dict):
            continue
        t = str(row.get("ticker") or "").upper()
        if t.startswith(prefix.upper()):
            out.append(t)
    return sorted(set(out))


def build_candidate_series(
    host: str,
    *,
    league_prefix: str,
    seed_series: str,
    explicit_series: list[str] | None,
    include_season_long: bool,
    scan_all_series: bool,
) -> list[str]:
    if explicit_series:
        return sorted({s.upper() for s in explicit_series if s.strip()})

    cands: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = s.upper()
        if not s or s in seen:
            return
        seen.add(s)
        cands.append(s)

    add(seed_series)
    if league_prefix == "KXNFL":
        for s in NFL_PRIORITY_SERIES:
            add(s)

    if scan_all_series:
        for s in list_series_tickers(host, league_prefix):
            if not include_season_long and looks_season_long(s):
                continue
            add(s)
    return cands


def discover_game_markets(
    host: str,
    *,
    game_code: str,
    statuses: set[str],
    candidate_series: Iterable[str],
    discovery: str = "event",
    pause_s: float = 0.05,
) -> tuple[list[MarketRow], dict[str, int], list[str]]:
    by_ticker: dict[str, MarketRow] = {}
    hits: dict[str, int] = {}
    errors: list[str] = []

    for series in candidate_series:
        event_ticker = f"{series}-{game_code}".upper()
        try:
            raw_markets = fetch_markets_for_event(
                host,
                event_ticker,
                statuses=statuses,
                discovery=discovery,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            errors.append(f"{event_ticker}: HTTP {exc.code}")
            eprint(f"error {event_ticker}: HTTP {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{event_ticker}: {exc}")
            eprint(f"error {event_ticker}: {exc}")
            continue

        if not raw_markets:
            if pause_s > 0:
                time.sleep(pause_s)
            continue

        n = 0
        for raw in raw_markets:
            row = market_row_from_raw(raw)
            if row is None:
                continue
            prev = by_ticker.get(row.ticker)
            if prev is None or (not prev.title and row.title):
                by_ticker[row.ticker] = row
            n += 1
        hits[series] = n
        eprint(f"hit {event_ticker}: {n} markets")
        if pause_s > 0:
            time.sleep(pause_s)

    rows = sorted(by_ticker.values(), key=lambda r: (r.series_ticker, r.ticker))
    return rows, hits, errors


def format_row(row: MarketRow) -> str:
    title = row.title or row.yes_sub_title or ""
    extra = ""
    if row.yes_sub_title and row.yes_sub_title not in title:
        extra = f" | yes={row.yes_sub_title}"
    return (
        f"{row.ticker:<48}  "
        f"{(row.status or '-'):<8}  "
        f"{row.series_ticker:<18}  "
        f"{title}{extra}"
    )


def fmt_cents(v: int | None) -> str:
    return "--" if v is None else f"{v:02d}c"


def resolve_ws_auth() -> tuple[str, Any, str]:
    """Return (api_key_id, private_key, source_label)."""
    try:
        from kx_orderbooks.auth import load_auth_from_env, load_private_key
    except ImportError as exc:
        raise RuntimeError(
            "kx_orderbooks is required for --watch. Run: pip install -e ."
        ) from exc

    pairs = [
        ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_FILE", "KALSHI_*"),
        ("KALSHI_PROD_API_KEY_ID", "KALSHI_PROD_PRIVATE_KEY_FILE", "KALSHI_PROD_*"),
        ("KALSHI_DEMO_API_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY_FILE", "KALSHI_DEMO_*"),
    ]
    for key_env, pem_env, label in pairs:
        key = os.environ.get(key_env)
        pem = os.environ.get(pem_env)
        if key and pem:
            return key, load_private_key(pem), label

    # Fall back to default env names used by kx_orderbooks.
    key, private_key = load_auth_from_env()
    return key, private_key, "KALSHI_*"


def select_watch_rows(rows: list[MarketRow], *, watch_limit: int) -> list[MarketRow]:
    """Prefer active/open markets for the websocket subscription."""
    preferred_status = {"active", "open"}
    live = [r for r in rows if r.status.lower() in preferred_status]
    chosen = live if live else list(rows)
    if watch_limit > 0:
        chosen = chosen[:watch_limit]
    return chosen


def run_watch(
    rows: list[MarketRow],
    *,
    ws_url: str,
    watch_limit: int,
    print_every: float,
    log_raw: bool,
) -> int:
    try:
        from kx_orderbooks.store import OrderbookStore
        from kx_orderbooks.ws_multiplex import MultiplexOrderbookWorker
    except ImportError as exc:
        eprint(f"error: kx_orderbooks import failed: {exc}")
        eprint("Install package deps: pip install -e .")
        return 2

    try:
        api_key_id, private_key, auth_label = resolve_ws_auth()
    except Exception as exc:  # noqa: BLE001
        eprint(f"error: WebSocket auth not configured: {exc}")
        eprint(
            "Set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_FILE "
            "(or KALSHI_PROD_* / KALSHI_DEMO_*)."
        )
        return 2

    chosen = select_watch_rows(rows, watch_limit=watch_limit)
    if not chosen:
        eprint("no markets available to watch")
        return 1

    tickers = [r.ticker for r in chosen]
    eprint(
        f"watch: {len(tickers)} markets over WebSocket "
        f"({ws_url}) auth={auth_label}"
    )
    eprint("REST is not polled for books; Ctrl-C stops the stream.")

    store = OrderbookStore()
    worker = MultiplexOrderbookWorker(
        market_tickers=tickers,
        store=store,
        ws_url=ws_url,
        api_key_id=api_key_id,
        private_key=private_key,
        use_yes_price=True,
        reconnect=True,
        log_raw=log_raw,
    )

    stop = False

    def _handle_signal(signum, frame):  # noqa: ANN001, ARG001
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def on_update(update) -> None:  # noqa: ANN001
        b = update.view.best
        print(
            f"{update.kind:<8} "
            f"{update.market_ticker:<48} "
            f"YES {fmt_cents(b.yes_bid_cents)} x {fmt_cents(b.yes_ask_cents)} "
            f"spr={fmt_cents(b.spread_cents)} "
            f"NO {fmt_cents(b.no_bid_cents)} x {fmt_cents(b.no_ask_cents)} "
            f"seq={update.seq}"
        )

    store.register_callback(on_update)
    worker.start()

    # Wait briefly for subscribe.
    deadline = time.time() + 15.0
    while time.time() < deadline and not stop:
        if worker.subscribed:
            break
        if worker.last_error is not None and not worker.connected:
            break
        time.sleep(0.05)

    if worker.last_error is not None and not worker.subscribed:
        eprint(f"websocket error before subscribe: {worker.last_error!r}")
        worker.stop()
        worker.join(timeout=5)
        return 1

    last_summary = time.time()
    try:
        while not stop:
            store.wait(timeout=1.0)
            if print_every > 0 and time.time() - last_summary >= print_every:
                last_summary = time.time()
                print("\n--- TOP BOOKS ---")
                views = list(store.views())
                views.sort(
                    key=lambda v: (
                        10_000 if v.best.spread_cents is None else v.best.spread_cents,
                        v.market_ticker,
                    )
                )
                for v in views[: min(20, len(views))]:
                    b = v.best
                    print(
                        f"{v.market_ticker:<48} "
                        f"YES {fmt_cents(b.yes_bid_cents)} x {fmt_cents(b.yes_ask_cents)} "
                        f"spr={fmt_cents(b.spread_cents)}"
                    )
                print("---\n")
    finally:
        worker.stop()
        worker.join(timeout=5)

    return 0


def print_discovery_text(
    *,
    seed_series: str,
    game_code: str,
    rows: list[MarketRow],
    hits: dict[str, int],
    errors: list[str],
    elapsed: float,
) -> None:
    print(f"Game: {seed_series}-{game_code}")
    print(
        f"Markets: {len(rows)}  series_hits: {len(hits)}  "
        f"errors: {len(errors)}  elapsed: {elapsed:.1f}s"
    )
    print("")
    if hits:
        print("Coverage by series:")
        for series, n in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {series:<24} {n:4d}")
        print("")
    print(f"{'TICKER':<48}  {'STATUS':<8}  {'SERIES':<18}  TITLE")
    print("-" * 120)
    for row in rows:
        print(format_row(row))
    if errors:
        print("")
        print(f"Errors ({len(errors)}):")
        for err in errors[:30]:
            print(f"  {err}")
        if len(errors) > 30:
            print(f"  ... {len(errors) - 30} more")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Discover Kalshi sports markets for a game id; optional WebSocket "
            "orderbook watch (no REST book polling)."
        )
    )
    p.add_argument(
        "game",
        help="Game/event id or URL tail, e.g. kxnflgame-26sep13atlpit",
    )
    p.add_argument(
        "--host",
        default=DEFAULT_REST_HOST,
        help=f"REST host for catalog discovery (default {DEFAULT_REST_HOST})",
    )
    p.add_argument(
        "--status",
        default="all",
        help="CSV statuses to include, or 'all' (default all). Example: open,active",
    )
    p.add_argument(
        "--series",
        action="append",
        default=[],
        help="Only probe these series tickers (repeatable). Default: priority set.",
    )
    p.add_argument(
        "--scan-all-series",
        action="store_true",
        help=(
            "Also probe every league series from GET /series (slow; more 429 risk). "
            "Default uses a curated priority list only."
        ),
    )
    p.add_argument(
        "--include-season-long",
        action="store_true",
        help="With --scan-all-series, do not filter season-long series.",
    )
    p.add_argument(
        "--max-series",
        type=int,
        default=0,
        help="Optional cap on candidate series after priority ordering (0 = no cap).",
    )
    p.add_argument(
        "--pause",
        type=float,
        default=0.05,
        help="Pause seconds between series probes (default 0.05).",
    )
    p.add_argument(
        "--discovery",
        choices=["event", "markets", "auto"],
        default="event",
        help=(
            "Catalog method: 'event' = GET /events/{ticker}?with_nested_markets "
            "(default, fewer REST calls); 'markets' = paginated /markets; "
            "'auto' tries event then markets."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit discovery JSON instead of text table.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stderr progress.",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help=(
            "After discovery, stream orderbooks over one authenticated Kalshi "
            "WebSocket (kx_orderbooks multiplex). Requires API key env."
        ),
    )
    p.add_argument(
        "--watch-limit",
        type=int,
        default=80,
        help="Max markets to subscribe on --watch (default 80; 0 = all discovered).",
    )
    p.add_argument(
        "--ws-url",
        default=DEFAULT_WS_URL,
        help=f"WebSocket URL (default {DEFAULT_WS_URL})",
    )
    p.add_argument(
        "--print-every",
        type=float,
        default=0.0,
        help="With --watch, print a top-of-book summary every N seconds.",
    )
    p.add_argument(
        "--log-raw",
        action="store_true",
        help="With --watch, log raw websocket messages.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.quiet:
        def _quiet_eprint(*_a: Any, **_k: Any) -> None:
            return None

        globals()["eprint"] = _quiet_eprint

    try:
        seed_series, game_code = parse_game_input(args.game)
    except ValueError as exc:
        eprint(f"error: {exc}")
        return 2

    statuses = normalize_status_filter(args.status)
    league = league_prefix_from_series(seed_series)

    t0 = time.time()
    eprint(f"game_code={game_code} seed_series={seed_series} league_prefix={league}")
    eprint(f"status_filter={'all' if not statuses else ','.join(sorted(statuses))}")
    eprint(f"discovery={args.discovery} scan_all_series={bool(args.scan_all_series)}")

    candidates = build_candidate_series(
        args.host,
        league_prefix=league,
        seed_series=seed_series,
        explicit_series=args.series or None,
        include_season_long=bool(args.include_season_long),
        scan_all_series=bool(args.scan_all_series),
    )
    if args.max_series and args.max_series > 0:
        candidates = candidates[: args.max_series]
    eprint(f"probing {len(candidates)} series")

    rows, hits, errors = discover_game_markets(
        args.host,
        game_code=game_code,
        statuses=statuses,
        candidate_series=candidates,
        discovery=str(args.discovery),
        pause_s=max(0.0, float(args.pause)),
    )
    elapsed = time.time() - t0

    if args.json and not args.watch:
        payload = {
            "seed_series": seed_series,
            "game_code": game_code,
            "league_prefix": league,
            "market_count": len(rows),
            "series_hits": hits,
            "errors": errors,
            "elapsed_seconds": round(elapsed, 3),
            "discovery": args.discovery,
            "markets": [
                {
                    "ticker": r.ticker,
                    "title": r.title,
                    "event_ticker": r.event_ticker,
                    "series_ticker": r.series_ticker,
                    "status": r.status,
                    "yes_sub_title": r.yes_sub_title,
                    "no_sub_title": r.no_sub_title,
                }
                for r in rows
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_discovery_text(
            seed_series=seed_series,
            game_code=game_code,
            rows=rows,
            hits=hits,
            errors=errors,
            elapsed=elapsed,
        )

    if not rows:
        eprint("no markets found")
        return 1

    if args.watch:
        return run_watch(
            rows,
            ws_url=str(args.ws_url),
            watch_limit=int(args.watch_limit),
            print_every=float(args.print_every),
            log_raw=bool(args.log_raw),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
