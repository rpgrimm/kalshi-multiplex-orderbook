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
    ./kalshi_sports_trader.py --prod kxnflgame-26sep13atlpit
    ./kalshi_sports_trader.py --demo kxnflgame-26sep14denkc --browse --count-yes 5
    ./kalshi_sports_trader.py --demo kxnflgame-26sep14denkc --browse --count-yes 5 --live
    ./kalshi_sports_trader.py --prod kxnflgame-26sep13atlpit --browse
    ./kalshi_sports_trader.py --demo kxnflgame-26sep13atlpit --watch
    ./kalshi_sports_trader.py --prod kxnflgame-26sep13atlpit --watch --watch-limit 40

Environment gates (same safety model as broadcast trader):
    --demo or --prod is required (no implicit host).
    Default is dry-run. Real BUY/SELL only with --live.
    Browser Enter on a market buys YES for --count-yes contracts.
    o opens ORDERS/positions; Enter there SELL YES EXIT ALL (confirm).
    Order events stay in memory during submit and dump to disk when idle/exit.

Auth for --watch / --browse WS books (env vars or config files):
    ~/.config/kalshi-multiplex-orderbook/prod.env
    ~/.config/kalshi-multiplex-orderbook/demo.env
    (or process env KALSHI_PROD_* / KALSHI_DEMO_*; legacy KALSHI_* for prod)
"""

from __future__ import annotations

import argparse
import http.client
import uuid
import json
import os
import random
import re
import select
import signal
import sys
import termios
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from sports_engine.browse import (
    arm_extra_draft,
    arm_fg_draft,
    arm_ncaaf_fg_draft,
    arm_ncaaf_td_draft,
    arm_safety_draft,
    arm_qend_draft,
    arm_td_draft,
    apply_extra_draft,
    apply_extra_miss,
    apply_fg_draft,
    apply_qend_draft,
    apply_safety_draft,
    apply_td_draft,
    catalog_player_names,
    ingest_quarter_end,
    ingest_score,
    make_browse_session,
    parse_time_line,
)
from sports_engine.catalog import (
    is_college_rows,
    resolve_game_team,
    row_series,
    tab_complete_team,
    unique_player_rows,
)
from sports_engine.key_script import parse_key_script
from sports_engine.market_cache import (
    clear_market_cache,
    default_cache_path,
    load_market_cache,
    save_market_cache,
)
from sports_engine.play_protocol import parse_play_query, tab_complete_player

PROD_REST_HOST = "https://api.elections.kalshi.com/trade-api/v2"
PROD_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_REST_HOST = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_REST_HOST_ALT = "https://demo-api.kalshi.co/trade-api/v2"
DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
DEMO_WS_URL_ALT = "wss://demo-api.kalshi.co/trade-api/ws/v2"

# Preferred environment-specific auth variables (match broadcast trader).
PROD_API_KEY_ID_ENV = "KALSHI_PROD_API_KEY_ID"
PROD_PRIVATE_KEY_FILE_ENV = "KALSHI_PROD_PRIVATE_KEY_FILE"
DEMO_API_KEY_ID_ENV = "KALSHI_DEMO_API_KEY_ID"
DEMO_PRIVATE_KEY_FILE_ENV = "KALSHI_DEMO_PRIVATE_KEY_FILE"
LEGACY_PROD_API_KEY_ID_ENV = "KALSHI_API_KEY_ID"
LEGACY_PROD_PRIVATE_KEY_FILE_ENV = "KALSHI_PRIVATE_KEY_FILE"
DEFAULT_PRIVATE_KEY_FILE = "grimm.txt"

# Backward-compatible aliases used by older sports-trader call sites/docs.
DEFAULT_REST_HOST = PROD_REST_HOST
DEFAULT_WS_URL = PROD_WS_URL
USER_AGENT = "kalshi-sports-trader/0.4"

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

# College game-scoped series. Without this, KXNCAAFGAME is moneyline-only.
NCAAF_PRIORITY_SERIES = [
    "KXNCAAFGAME",
    "KXNCAAFSPREAD",
    "KXNCAAFTOTAL",
    "KXNCAAFTEAMTOTAL",
    "KXNCAAF1H",
    "KXNCAAF1HSPREAD",
    "KXNCAAF1HTOTAL",
    "KXNCAAF1HTEAMTOTAL",
    "KXNCAAF1HFT",
    "KXNCAAF2H",
    "KXNCAAF2HSPREAD",
    "KXNCAAF2HTOTAL",
    "KXNCAAF1Q",
    "KXNCAAF1QSPREAD",
    "KXNCAAF1QTOTAL",
    "KXNCAAF2Q",
    "KXNCAAF2QSPREAD",
    "KXNCAAF2QTOTAL",
    "KXNCAAF3Q",
    "KXNCAAF3QSPREAD",
    "KXNCAAF3QTOTAL",
    "KXNCAAF4Q",
    "KXNCAAF4QSPREAD",
    "KXNCAAF4QTOTAL",
    "KXNCAAFFIRSTTDTEAM",
    "KXNCAAFDSTTD",
    "KXNCAAFTEAMRECTD",
    "KXNCAAFTEAMRECYDS",
    "KXNCAAFTEAMFG",
    "KXNCAAFTEAMTD",
    "KXNCAAFTEAMYDS",
    "KXNCAAFTOTALFG",
    "KXNCAAFTOTALTD",
    "KXNCAAF2PT",
    "KXNCAAFOT",
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


@dataclass
class OrderLogEvent:
    """One in-memory order event (no secrets)."""

    ts: float
    kind: str
    summary: str
    detail: str = ""
    ticker: str = ""
    mode: str = ""
    live: bool = False
    ok: bool | None = None
    http_status: int | None = None

    def render(self) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))
        lines = [f"[{stamp}] {self.summary}"]
        if self.detail:
            lines.append(self.detail)
        return "\n".join(lines)


class OrderMemoryLog:
    """Ring buffer of order events. No disk writes during submit; dump on idle/exit."""

    def __init__(self, *, capacity: int = 500):
        self.capacity = max(50, int(capacity))
        self.events: list[OrderLogEvent] = []
        self._dirty = False
        self._last_event_ts = 0.0
        self._last_dump_ts = 0.0
        self.path: str | None = None
        self.idle_dump_s = 2.0
        self.enabled = True

    def configure(
        self,
        *,
        path: str | None,
        idle_dump_s: float = 2.0,
        capacity: int = 500,
        enabled: bool = True,
    ) -> None:
        self.path = str(Path(path).expanduser()) if path else None
        self.idle_dump_s = max(0.25, float(idle_dump_s))
        self.capacity = max(50, int(capacity))
        self.enabled = bool(enabled)

    def record(
        self,
        summary: str,
        *,
        kind: str = "order",
        detail: str = "",
        ticker: str = "",
        mode: str = "",
        live: bool = False,
        ok: bool | None = None,
        http_status: int | None = None,
    ) -> OrderLogEvent:
        ev = OrderLogEvent(
            ts=time.time(),
            kind=kind,
            summary=summary,
            detail=detail or "",
            ticker=ticker or "",
            mode=mode or "",
            live=bool(live),
            ok=ok,
            http_status=http_status,
        )
        if not self.enabled:
            return ev
        self.events.append(ev)
        if len(self.events) > self.capacity:
            self.events = self.events[-self.capacity :]
        self._dirty = True
        self._last_event_ts = ev.ts
        return ev

    def recent(self, n: int = 8) -> list[OrderLogEvent]:
        if n <= 0:
            return []
        return self.events[-n:]

    def maybe_dump_idle(self, *, force: bool = False) -> str | None:
        """If dirty and idle long enough (or force), write memory log to path."""
        if not self.enabled or not self.path or not self._dirty:
            return None
        now = time.time()
        if not force and (now - self._last_event_ts) < self.idle_dump_s:
            return None
        return self.dump(reason="idle" if not force else "force")

    def dump(self, *, reason: str = "manual") -> str | None:
        if not self.enabled or not self.path:
            return None
        if not self.events and not self._dirty:
            return None
        out = Path(self.path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# kalshi_sports_trader order memory log\n"
            f"# dumped_at={time.strftime('%Y-%m-%d %H:%M:%S')} reason={reason}\n"
            f"# events={len(self.events)} (no secrets)\n\n"
        )
        body = "\n\n".join(ev.render() for ev in self.events) + "\n"
        out.write_text(header + body, encoding="utf-8")
        try:
            os.chmod(out, 0o600)
        except OSError:
            pass
        self._dirty = False
        self._last_dump_ts = time.time()
        return str(out)


# Process-wide memory log used by order path + browser idle flusher.
ORDER_LOG = OrderMemoryLog()


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
            "OT",
            "2PT",
            "SAF",
        )
    )
    if gameish:
        return False
    return any(h in s for h in SEASON_LONG_HINTS)
def _drain_http_error(exc: urllib.error.HTTPError) -> None:
    """Consume/detach the HTTPError body so 3.14 GC close() is not a double-close."""
    try:
        exc.read()
    except Exception:
        pass
    fp = getattr(exc, "fp", None)
    if fp is not None:
        try:
            fp.close()
        except Exception:
            pass
        try:
            exc.fp = None
        except Exception:
            pass


def _patch_http_response_close() -> None:
    """Python 3.14 HTTPResponse.close() flushes an already-closed fp on GC."""
    orig = http.client.HTTPResponse.close

    def close(self, *args, **kwargs):  # noqa: ANN001
        try:
            return orig(self, *args, **kwargs)
        except ValueError:
            return None

    http.client.HTTPResponse.close = close  # type: ignore[method-assign]


_patch_http_response_close()


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
            _drain_http_error(exc)
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
    elif league_prefix == "KXNCAAF":
        for s in NCAAF_PRIORITY_SERIES:
            add(s)

    if scan_all_series:
        try:
            listed = list_series_tickers(host, league_prefix)
        except Exception as exc:  # noqa: BLE001
            eprint(f"series catalog failed ({exc}); using priority list only")
            listed = []
        for s in listed:
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


def cents_to_dollars(cents: int | None) -> str:
    if cents is None:
        return "--"
    return f"${cents / 100:.2f}"


def clamp_price_cents(cents: int) -> int:
    return max(1, min(99, int(cents)))


def fixed_contract_count(count: int | float | str) -> str:
    return f"{float(count):.2f}"


def fixed_dollar_price_from_cents(cents: int | float | str) -> str:
    return f"{(float(cents) / 100.0):.4f}"


def load_pem_private_key(private_key_file: str) -> Any:
    with Path(private_key_file).expanduser().open("rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_private_key_text(private_key: Any, text: str) -> str:
    import base64

    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def headers_to_dict(headers: Any) -> Any:
    if headers is None:
        return None
    if isinstance(headers, dict):
        return {str(k): str(v) for k, v in headers.items()}
    try:
        return {str(k): str(v) for k, v in dict(headers).items()}
    except Exception:
        return str(headers)


def make_buy_no_limit_payload(
    *,
    ticker: str,
    count: int,
    no_limit_cents: int,
    time_in_force: str = "immediate_or_cancel",
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "action": "buy",
        "side": "no",
        "count": int(count),
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
        "no_price": clamp_price_cents(no_limit_cents),
        "time_in_force": time_in_force,
    }


def make_buy_yes_limit_payload(
    *,
    ticker: str,
    count: int,
    yes_limit_cents: int,
    time_in_force: str = "immediate_or_cancel",
) -> dict[str, Any]:
    """Legacy-shaped BUY YES limit payload (pre-V2 conversion)."""
    return {
        "ticker": ticker,
        "action": "buy",
        "side": "yes",
        "count": int(count),
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
        "yes_price": clamp_price_cents(yes_limit_cents),
        "time_in_force": time_in_force,
    }


def make_sell_no_limit_payload(
    *,
    ticker: str,
    count: int,
    no_limit_cents: int,
    time_in_force: str = "immediate_or_cancel",
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "action": "sell",
        "side": "no",
        "count": int(count),
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
        "no_price": clamp_price_cents(no_limit_cents),
        "time_in_force": time_in_force,
    }


def make_sell_yes_limit_payload(
    *,
    ticker: str,
    count: int,
    yes_limit_cents: int,
    time_in_force: str = "immediate_or_cancel",
) -> dict[str, Any]:
    """Legacy-shaped SELL YES (exit long YES) limit payload."""
    return {
        "ticker": ticker,
        "action": "sell",
        "side": "yes",
        "count": int(count),
        "type": "limit",
        "client_order_id": str(uuid.uuid4()),
        "yes_price": clamp_price_cents(yes_limit_cents),
        "time_in_force": time_in_force,
    }


def make_event_order_v2_payload(legacy_payload: dict[str, Any]) -> dict[str, Any]:
    """Convert legacy BUY/SELL YES payload to Kalshi V2 event-order shape."""
    if str(legacy_payload.get("type") or "") != "limit":
        raise ValueError("sports trader currently supports limit orders only")
    action = str(legacy_payload.get("action") or "").lower()
    side = str(legacy_payload.get("side") or "").lower()
    if action not in {"buy", "sell"} or side not in {"yes", "no"}:
        raise ValueError(f"unsupported order shape action={action!r} side={side!r}")
    tif = legacy_payload.get("time_in_force") or "immediate_or_cancel"
    if tif == "GTT":
        tif = "good_till_canceled"
    if action == "buy" and side == "yes":
        price = legacy_payload.get("yes_price")
        if price is None:
            raise ValueError("BUY YES limit order missing yes_price")
        v2_side, v2_price, reduce = "bid", int(price), False
    elif action == "buy" and side == "no":
        price = legacy_payload.get("no_price")
        if price is None:
            raise ValueError("BUY NO limit order missing no_price")
        v2_side, v2_price, reduce = "ask", 100 - int(price), False
    elif action == "sell" and side == "yes":
        price = legacy_payload.get("yes_price")
        if price is None:
            raise ValueError("SELL YES limit order missing yes_price")
        v2_side, v2_price, reduce = "ask", int(price), True
    else:
        price = legacy_payload.get("no_price")
        if price is None:
            raise ValueError("SELL NO limit order missing no_price")
        v2_side, v2_price, reduce = "bid", 100 - int(price), True
    return {
        "ticker": legacy_payload["ticker"],
        "client_order_id": legacy_payload.get("client_order_id") or str(uuid.uuid4()),
        "side": v2_side,
        "count": fixed_contract_count(legacy_payload["count"]),
        "price": fixed_dollar_price_from_cents(v2_price),
        "time_in_force": tif,
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": False,
        "reduce_only": reduce,
    }


def signed_json_request(
    args: argparse.Namespace,
    *,
    method: str,
    path: str,
    body: dict | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Raw signed Kalshi REST request. Never logs secret values."""
    if not getattr(args, "_auth_api_key_id", None) or not getattr(args, "_auth_private_key_file", None):
        raise RuntimeError("Resolved auth settings are required before signed REST requests")

    # Kalshi signs the path without query params; append params to the URL only.
    signed_path = str(path).split("?", 1)[0]
    url = str(args.api_host).rstrip("/") + signed_path.removeprefix("/trade-api/v2")
    query = urllib.parse.urlencode(
        {str(k): v for k, v in (params or {}).items() if v not in (None, "")}
    )
    if query:
        url += "?" + query
    timestamp = str(int(time.time() * 1000))
    private_key = getattr(args, "_raw_rest_private_key", None)
    if private_key is None:
        private_key = load_pem_private_key(args._auth_private_key_file)
        args._raw_rest_private_key = private_key
    signature = sign_private_key_text(private_key, timestamp + method.upper() + signed_path)
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {
        "KALSHI-ACCESS-KEY": args._auth_api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                response_body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                response_body = {"raw": raw}
            return {
                "method": f"raw.{method.upper()} {path}",
                "http_status": getattr(resp, "status", None) or getattr(resp, "code", None),
                "headers": headers_to_dict(getattr(resp, "headers", None)),
                "response": response_body,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096).decode("utf-8", errors="replace")
        try:
            body_obj = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body_obj = raw
        raise RuntimeError(
            f"HTTP {exc.code} {method.upper()} {path}: {body_obj}"
        ) from exc


def effective_count_yes(args: argparse.Namespace) -> int:
    if getattr(args, "count_yes", None) is not None:
        return int(args.count_yes)
    return int(getattr(args, "count", 1) or 1)


def buy_yes_for_market(
    *,
    args: argparse.Namespace,
    row: "MarketRow",
    quote: "QuoteSnap | None",
) -> tuple[bool, str]:
    """BUY YES for --count-yes contracts. Dry-run unless --live.

    Prices from live YES ask + slippage (default 1c), IOC limit via V2 events API.
    """
    count = effective_count_yes(args)
    if count <= 0:
        return False, "ORDER ERROR: --count-yes must be positive"

    # Ensure auth is resolved for both dry-run metadata and live submit.
    try:
        resolve_auth_settings(args, required=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"ORDER ERROR auth: {exc}"

    ask = quote.yes_ask if quote is not None else None
    if ask is None:
        return False, (
            f"ORDER BLOCKED {row.ticker}: no YES ask yet "
            "(wait for book / ensure market is on-screen)"
        )

    slip = int(getattr(args, "slippage_cents", 1) or 0)
    limit_cents = clamp_price_cents(int(ask) + slip)
    legacy = make_buy_yes_limit_payload(
        ticker=row.ticker,
        count=count,
        yes_limit_cents=limit_cents,
        time_in_force=str(getattr(args, "time_in_force", "immediate_or_cancel")),
    )
    try:
        v2 = make_event_order_v2_payload(legacy)
    except Exception as exc:  # noqa: BLE001
        return False, f"ORDER ERROR payload: {exc}"

    mode = "LIVE" if args.live else "DRY-RUN"
    summary = (
        f"{mode} BUY YES {row.ticker} count={count} "
        f"ask={fmt_cents(ask)} limit={fmt_cents(limit_cents)} "
        f"slip=+{slip}c tif={legacy['time_in_force']}"
    )

    # Memory-only during submit (no disk I/O, no stderr spam that breaks TUI).
    detail = f"payload_v2={json.dumps(v2, sort_keys=True)}"
    ORDER_LOG.record(
        summary,
        kind="order_built",
        detail=detail,
        ticker=row.ticker,
        mode=mode,
        live=bool(args.live),
        ok=None if args.live else True,
    )

    if not args.live:
        SESSION_BETS.record_buy(
            ticker=row.ticker,
            title=row.title or row.yes_sub_title or "",
            count=count,
            limit_cents=limit_cents,
            live=False,
            dry_run=True,
            note=summary,
        )
        return True, summary + " (not submitted; memory-log)"

    try:
        info = signed_json_request(
            args,
            method="POST",
            path="/trade-api/v2/portfolio/events/orders",
            body=v2,
            timeout=float(getattr(args, "order_submit_timeout", 10.0)),
        )
    except Exception as exc:  # noqa: BLE001
        fail = f"ORDER FAIL {row.ticker}: {exc}"
        ORDER_LOG.record(
            fail,
            kind="order_error",
            detail=str(exc),
            ticker=row.ticker,
            mode=mode,
            live=True,
            ok=False,
        )
        return False, fail

    http_status = info.get("http_status")
    resp = info.get("response")
    resp_s = json.dumps(resp, default=str)[:800]
    ok = http_status is None or (isinstance(http_status, int) and 200 <= http_status < 300)
    if ok:
        msg = (
            f"LIVE OK BUY YES {row.ticker} count={count} "
            f"limit={fmt_cents(limit_cents)} http={http_status}"
        )
    else:
        msg = f"LIVE REJECT BUY YES {row.ticker} http={http_status}"
    ORDER_LOG.record(
        msg,
        kind="order_response",
        detail=f"http={http_status} body={resp_s}",
        ticker=row.ticker,
        mode=mode,
        live=True,
        ok=ok,
        http_status=http_status if isinstance(http_status, int) else None,
    )
    if ok:
        SESSION_BETS.record_buy(
            ticker=row.ticker,
            title=row.title or row.yes_sub_title or "",
            count=count,
            limit_cents=limit_cents,
            live=True,
            dry_run=False,
            note=msg,
        )
    return ok, msg


def buy_no_for_market(
    *,
    args: argparse.Namespace,
    row: "MarketRow",
    quote: "QuoteSnap | None",
) -> tuple[bool, str]:
    """BUY NO. Dry-run unless --live. Prices from NO ask + slippage."""
    count = effective_count_yes(args)
    if count <= 0:
        return False, "ORDER ERROR: --count-yes must be positive"
    try:
        resolve_auth_settings(args, required=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"ORDER ERROR auth: {exc}"
    ask = quote.no_ask if quote is not None else None
    if ask is None:
        return False, f"ORDER BLOCKED {row.ticker}: no NO ask yet"
    slip = int(getattr(args, "slippage_cents", 1) or 0)
    limit_cents = clamp_price_cents(int(ask) + slip)
    legacy = make_buy_no_limit_payload(
        ticker=row.ticker,
        count=count,
        no_limit_cents=limit_cents,
        time_in_force=str(getattr(args, "time_in_force", "immediate_or_cancel")),
    )
    try:
        v2 = make_event_order_v2_payload(legacy)
    except Exception as exc:  # noqa: BLE001
        return False, f"ORDER ERROR payload: {exc}"
    mode = "LIVE" if args.live else "DRY-RUN"
    summary = (
        f"{mode} BUY NO {row.ticker} count={count} "
        f"ask={fmt_cents(ask)} limit={fmt_cents(limit_cents)}"
    )
    ORDER_LOG.record(
        summary,
        kind="order_built",
        detail=f"payload_v2={json.dumps(v2, sort_keys=True)}",
        ticker=row.ticker,
        mode=mode,
        live=bool(args.live),
        ok=None if args.live else True,
    )
    if not args.live:
        SESSION_BETS.record_buy(
            ticker=row.ticker,
            title=row.title or row.yes_sub_title or "",
            count=count,
            limit_cents=limit_cents,
            live=False,
            dry_run=True,
            note=summary,
            side="no",
        )
        return True, summary + " (not submitted; memory-log)"
    try:
        info = signed_json_request(
            args,
            method="POST",
            path="/trade-api/v2/portfolio/events/orders",
            body=v2,
            timeout=float(getattr(args, "order_submit_timeout", 10.0)),
        )
    except Exception as exc:  # noqa: BLE001
        fail = f"ORDER FAIL {row.ticker}: {exc}"
        ORDER_LOG.record(fail, kind="order_error", detail=str(exc), ticker=row.ticker, mode=mode, live=True, ok=False)
        return False, fail
    http_status = info.get("http_status")
    ok = http_status is None or (isinstance(http_status, int) and 200 <= http_status < 300)
    msg = (
        f"LIVE OK BUY NO {row.ticker} count={count} limit={fmt_cents(limit_cents)} http={http_status}"
        if ok
        else f"LIVE REJECT BUY NO {row.ticker} http={http_status}"
    )
    ORDER_LOG.record(msg, kind="order_response", ticker=row.ticker, mode=mode, live=True, ok=ok)
    if ok:
        SESSION_BETS.record_buy(
            ticker=row.ticker,
            title=row.title or row.yes_sub_title or "",
            count=count,
            limit_cents=limit_cents,
            live=True,
            dry_run=False,
            note=msg,
            side="no",
        )
    return ok, msg


@dataclass
class SessionBet:
    """One session-tracked BUY that went through (live or dry-run)."""

    ticker: str
    title: str
    count: int
    limit_cents: int | None
    ts: float
    live: bool
    dry_run: bool
    note: str = ""
    sold_count: int = 0
    side: str = "yes"

    @property
    def remaining(self) -> int:
        return max(0, int(self.count) - int(self.sold_count))


class SessionBetBook:
    """In-memory bets opened this browser session (plus optional exchange positions)."""

    def __init__(self) -> None:
        self.bets: list[SessionBet] = []
        self.exchange_positions: dict[str, int] = {}  # ticker -> net YES contracts
        self.positions_error: str = ""
        self.positions_ts: float = 0.0

    def record_buy(
        self,
        *,
        ticker: str,
        title: str,
        count: int,
        limit_cents: int | None,
        live: bool,
        dry_run: bool,
        note: str = "",
        side: str = "yes",
    ) -> SessionBet:
        bet = SessionBet(
            ticker=ticker,
            title=title or "",
            count=int(count),
            limit_cents=limit_cents,
            ts=time.time(),
            live=bool(live),
            dry_run=bool(dry_run),
            note=note or "",
            side=str(side or "yes").lower(),
        )
        self.bets.append(bet)
        return bet

    def mark_sold(self, ticker: str, count: int, side: str | None = None) -> None:
        left = int(count)
        want = str(side or "").lower() or None
        if left <= 0:
            return
        for bet in reversed(self.bets):
            if bet.ticker.upper() != str(ticker).upper() or bet.remaining <= 0:
                continue
            if want and str(bet.side or "yes").lower() != want:
                continue
            take = min(bet.remaining, left)
            bet.sold_count += take
            left -= take
            if left <= 0:
                break

    def open_session_rows(self) -> list[tuple[str, str, int, bool]]:
        """Aggregate open session exposure: (ticker, title, remaining, any_live)."""
        agg: dict[str, list[Any]] = {}
        for bet in self.bets:
            rem = bet.remaining
            if rem <= 0:
                continue
            cur = agg.get(bet.ticker)
            if cur is None:
                agg[bet.ticker] = [bet.title, rem, bet.live and not bet.dry_run]
            else:
                cur[1] += rem
                cur[2] = cur[2] or (bet.live and not bet.dry_run)
                if bet.title and not cur[0]:
                    cur[0] = bet.title
        return [(t, v[0], int(v[1]), bool(v[2])) for t, v in sorted(agg.items())]

    def set_exchange_positions(self, positions: dict[str, int]) -> None:
        self.exchange_positions = {
            str(k).upper(): int(v) for k, v in positions.items() if int(v) != 0
        }
        self.positions_ts = time.time()
        self.positions_error = ""


SESSION_BETS = SessionBetBook()


def _parse_position_contracts(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(str(value))))
    except Exception:
        return None


def fetch_yes_positions(
    args: argparse.Namespace,
    *,
    tickers: Iterable[str] | None = None,
    timeout: float | None = None,
) -> dict[str, int]:
    """Fetch net YES positions (positive = long YES). Filter to tickers when given."""
    resolve_auth_settings(args, required=True)
    known = {str(t).upper() for t in (tickers or []) if t}
    params: dict[str, Any] = {"limit": 200, "count_filter": "position"}
    info = signed_json_request(
        args,
        method="GET",
        path="/trade-api/v2/portfolio/positions",
        params=params,
        timeout=float(timeout if timeout is not None else getattr(args, "order_submit_timeout", 10.0)),
    )
    response = info.get("response") if isinstance(info, dict) else None
    rows = response.get("market_positions") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected /portfolio/positions response: {response!r}")

    out: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        if known and ticker not in known:
            continue
        pos = _parse_position_contracts(row.get("position_fp"))
        if pos is None:
            pos = _parse_position_contracts(row.get("position"))
        if pos is None or pos == 0:
            continue
        out[ticker] = pos
    return out


def refresh_exchange_positions(state: "BrowserState") -> str:
    """Pull exchange positions for known game tickers (+ session bets)."""
    try:
        resolve_auth_settings(state.args, required=True)
    except Exception as exc:  # noqa: BLE001
        SESSION_BETS.positions_error = str(exc)
        return f"positions auth error: {exc}"
    known = {r.ticker.upper() for r in state.rows}
    known.update(b.ticker.upper() for b in SESSION_BETS.bets)
    try:
        # Fetch all non-zero positions, then keep those related to this game/session.
        all_pos = fetch_yes_positions(state.args, tickers=None)
        filtered = {
            t: n
            for t, n in all_pos.items()
            if t in known or any(t.endswith(state.game_code.upper()) for _ in [0])
        }
        # Prefer game-code suffix match so we catch props even if series list incomplete.
        game = state.game_code.upper()
        filtered = {t: n for t, n in all_pos.items() if game in t or t in known}
        SESSION_BETS.set_exchange_positions(filtered)
        return f"positions refreshed: {len(filtered)} open (YES net)"
    except Exception as exc:  # noqa: BLE001
        SESSION_BETS.positions_error = str(exc)
        return f"positions refresh failed: {exc}"


def orders_page_items(state: "BrowserState") -> list[dict[str, Any]]:
    """Rows for the ORDERS page: exchange positions + session dry-run exposure."""
    title_by = {r.ticker.upper(): (r.title or r.yes_sub_title or "") for r in state.rows}
    for bet in SESSION_BETS.bets:
        title_by.setdefault(bet.ticker.upper(), bet.title)

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    # Live exchange positions first (authoritative when --live).
    for ticker, net in sorted(SESSION_BETS.exchange_positions.items()):
        if net == 0:
            continue
        side = "YES" if net > 0 else "NO"
        qty = abs(int(net))
        items.append(
            {
                "ticker": ticker,
                "title": title_by.get(ticker, ""),
                "qty": qty,
                "side": side,
                "source": "exchange",
                "sellable": net > 0,  # sports UI sells YES longs for now
                "net": int(net),
            }
        )
        seen.add(ticker)

    # Session dry-run / pending exposure not yet on exchange.
    for ticker, title, rem, any_live in SESSION_BETS.open_session_rows():
        t = ticker.upper()
        if t in seen:
            # still show session remainder as note via qty bump only if dry-run only
            continue
        items.append(
            {
                "ticker": t,
                "title": title or title_by.get(t, ""),
                "qty": int(rem),
                "side": next(
                    (
                        str(b.side or "yes").upper()
                        for b in SESSION_BETS.bets
                        if b.ticker.upper() == t and b.remaining > 0
                    ),
                    "YES",
                ),
                "source": "session-live" if any_live else "session-dry",
                "sellable": True,
                "net": int(rem),
            }
        )
    return items


def sell_yes_for_position(
    *,
    args: argparse.Namespace,
    ticker: str,
    title: str,
    count: int,
    quote: "QuoteSnap | None",
) -> tuple[bool, str]:
    """SELL YES to exit a long YES position. Default count = full position (sell all).

    Limit = YES bid - slippage (cross the bid). reduce_only on V2. Dry-run unless --live.
    """
    count = int(count)
    if count <= 0:
        return False, "SELL ERROR: count must be positive"

    try:
        resolve_auth_settings(args, required=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"SELL ERROR auth: {exc}"

    bid = quote.yes_bid if quote is not None else None
    if bid is None:
        return False, (
            f"SELL BLOCKED {ticker}: no YES bid yet "
            "(wait for book / ensure market is subscribed)"
        )

    slip = int(getattr(args, "slippage_cents", 1) or 0)
    limit_cents = clamp_price_cents(int(bid) - slip)
    legacy = make_sell_yes_limit_payload(
        ticker=ticker,
        count=count,
        yes_limit_cents=limit_cents,
        time_in_force=str(getattr(args, "time_in_force", "immediate_or_cancel")),
    )
    try:
        v2 = make_event_order_v2_payload(legacy)
    except Exception as exc:  # noqa: BLE001
        return False, f"SELL ERROR payload: {exc}"

    mode = "LIVE" if args.live else "DRY-RUN"
    # Explicit action text so the operator never confuses buy vs sell.
    summary = (
        f"{mode} SELL YES (EXIT ALL x{count}) {ticker} "
        f"bid={fmt_cents(bid)} limit={fmt_cents(limit_cents)} "
        f"slip=-{slip}c reduce_only tif={legacy['time_in_force']}"
    )
    detail = f"payload_v2={json.dumps(v2, sort_keys=True)} title={title!r}"
    ORDER_LOG.record(
        summary,
        kind="sell_built",
        detail=detail,
        ticker=ticker,
        mode=mode,
        live=bool(args.live),
        ok=None if args.live else True,
    )

    if not args.live:
        SESSION_BETS.mark_sold(ticker, count)
        return True, summary + " (not submitted; memory-log)"

    try:
        info = signed_json_request(
            args,
            method="POST",
            path="/trade-api/v2/portfolio/events/orders",
            body=v2,
            timeout=float(getattr(args, "order_submit_timeout", 10.0)),
        )
    except Exception as exc:  # noqa: BLE001
        fail = f"SELL FAIL {ticker}: {exc}"
        ORDER_LOG.record(
            fail,
            kind="sell_error",
            detail=str(exc),
            ticker=ticker,
            mode=mode,
            live=True,
            ok=False,
        )
        return False, fail

    http_status = info.get("http_status")
    resp = info.get("response")
    resp_s = json.dumps(resp, default=str)[:800]
    ok = http_status is None or (isinstance(http_status, int) and 200 <= http_status < 300)
    if ok:
        msg = (
            f"LIVE OK SELL YES (EXIT x{count}) {ticker} "
            f"limit={fmt_cents(limit_cents)} http={http_status}"
        )
        SESSION_BETS.mark_sold(ticker, count)
    else:
        msg = f"LIVE REJECT SELL YES {ticker} http={http_status}"
    ORDER_LOG.record(
        msg,
        kind="sell_response",
        detail=f"http={http_status} body={resp_s}",
        ticker=ticker,
        mode=mode,
        live=True,
        ok=ok,
        http_status=http_status if isinstance(http_status, int) else None,
    )
    return ok, msg


def sell_no_for_position(
    *,
    args: argparse.Namespace,
    ticker: str,
    title: str,
    count: int,
    quote: "QuoteSnap | None",
) -> tuple[bool, str]:
    count = int(count)
    if count <= 0:
        return False, "SELL ERROR: count must be positive"
    try:
        resolve_auth_settings(args, required=True)
    except Exception as exc:  # noqa: BLE001
        return False, f"SELL ERROR auth: {exc}"
    bid = quote.no_bid if quote is not None else None
    if bid is None:
        return False, f"SELL BLOCKED {ticker}: no NO bid yet"
    slip = int(getattr(args, "slippage_cents", 1) or 0)
    limit_cents = clamp_price_cents(int(bid) - slip)
    legacy = make_sell_no_limit_payload(
        ticker=ticker,
        count=count,
        no_limit_cents=limit_cents,
        time_in_force=str(getattr(args, "time_in_force", "immediate_or_cancel")),
    )
    try:
        v2 = make_event_order_v2_payload(legacy)
    except Exception as extra:  # noqa: BLE001
        return False, f"SELL ERROR payload: {extra}"
    mode = "LIVE" if args.live else "DRY-RUN"
    summary = f"{mode} SELL NO {ticker} count={count} bid={fmt_cents(bid)} limit={fmt_cents(limit_cents)}"
    if not args.live:
        SESSION_BETS.mark_sold(ticker, count, side="no")
        return True, summary + " (not submitted; memory-log)"
    try:
        info = signed_json_request(
            args,
            method="POST",
            path="/trade-api/v2/portfolio/events/orders",
            body=v2,
            timeout=float(getattr(args, "order_submit_timeout", 10.0)),
        )
    except Exception as extra:  # noqa: BLE001
        return False, f"SELL FAIL {ticker}: {extra}"
    http_status = info.get("http_status")
    ok = http_status is None or (isinstance(http_status, int) and 200 <= http_status < 300)
    if ok:
        SESSION_BETS.mark_sold(ticker, count, side="no")
        return True, f"LIVE OK SELL NO {ticker} count={count} http={http_status}"
    return False, f"LIVE REJECT SELL NO {ticker} http={http_status}"


def _import_kx_auth():
    """Import kx_orderbooks.auth without requiring full package extras."""
    try:
        from kx_orderbooks import auth as auth_mod
        return auth_mod
    except Exception:
        pass
    import importlib.util
    import sys
    auth_path = Path(__file__).resolve().parent / "kx_orderbooks" / "auth.py"
    if not auth_path.is_file():
        raise ImportError(f"kx_orderbooks.auth not found at {auth_path}")
    if "kx_orderbooks" not in sys.modules:
        pkg = importlib.util.module_from_spec(
            importlib.util.spec_from_loader("kx_orderbooks", loader=None)
        )
        pkg.__path__ = [str(auth_path.parent)]
        sys.modules["kx_orderbooks"] = pkg
    spec = importlib.util.spec_from_file_location("kx_orderbooks.auth", auth_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kx_orderbooks.auth"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def auth_env_candidates(kalshi_env: str) -> list[tuple[str, str]]:
    """Return auth env-var pairs in preferred order for demo or prod."""
    return _import_kx_auth().auth_env_candidates(kalshi_env)


def default_auth_env_names(kalshi_env: str) -> tuple[str, str]:
    return _import_kx_auth().default_auth_env_names(kalshi_env)


def key_id_hint(api_key_id: str) -> str:
    return _import_kx_auth().key_id_hint(api_key_id)


def default_hosts_for_env(kalshi_env: str, *, demo_host_style: str = "external") -> tuple[str, str]:
    """Return (rest_host, ws_url) for --demo/--prod."""
    if kalshi_env == "demo":
        if demo_host_style == "direct":
            return DEMO_REST_HOST_ALT, DEMO_WS_URL_ALT
        return DEMO_REST_HOST, DEMO_WS_URL
    return PROD_REST_HOST, PROD_WS_URL


def apply_env_endpoints(args: argparse.Namespace) -> None:
    """Fill api_host / ws_url from --demo/--prod unless explicitly overridden."""
    rest_default, ws_default = default_hosts_for_env(
        args.kalshi_env,
        demo_host_style=str(getattr(args, "demo_host_style", "external") or "external"),
    )
    host_override = getattr(args, "host", None)
    api_host_override = getattr(args, "api_host", None)
    if api_host_override:
        args.api_host = str(api_host_override).rstrip("/")
    elif host_override:
        args.api_host = str(host_override).rstrip("/")
    else:
        args.api_host = rest_default.rstrip("/")
    args.host = args.api_host

    ws_override = getattr(args, "ws_url", None)
    args.ws_url = str(ws_override) if ws_override else ws_default


def resolve_auth_settings(args: argparse.Namespace, *, required: bool) -> bool:
    """Resolve auth onto args via shared config-dir/env loader.

    When required=True, raise RuntimeError on missing credentials.
    Never prints secret values.
    """
    try:
        auth_mod = _import_kx_auth()
    except Exception as exc:
        if required:
            raise RuntimeError(
                "kx_orderbooks.auth is required for auth resolution. Run: pip install -e ."
            ) from exc
        return False

    try:
        auth = auth_mod.resolve_kalshi_auth(
            args.kalshi_env,
            private_key_file=getattr(args, "private_key_file", None),
            api_key_id_env=getattr(args, "api_key_id_env", None),
            private_key_file_env=getattr(args, "private_key_file_env", None),
            required=required,
        )
    except Exception:
        if required:
            raise
        return False

    if auth is None:
        return False

    args._auth_api_key_id = auth.api_key_id
    args._auth_api_key_hint = auth.api_key_hint
    args._auth_api_key_env = auth.api_key_source
    args._auth_private_key_file = auth.private_key_path
    args._auth_private_key_file_env = auth.private_key_source
    args._auth_config_dir = auth.config_dir
    return True


def resolve_ws_auth(args: argparse.Namespace | None = None) -> tuple[str, Any, str]:
    """Return (api_key_id, private_key, source_label) for WebSocket workers."""
    try:
        auth_mod = _import_kx_auth()
    except Exception as exc:
        raise RuntimeError(
            "kx_orderbooks.auth is required for --watch/--browse WS. Run: pip install -e ."
        ) from exc

    if args is None:
        for probe in ("prod", "demo"):
            auth = auth_mod.resolve_kalshi_auth(probe, required=False)
            if auth is not None:
                return (
                    auth.api_key_id,
                    auth.load_private_key(),
                    f"{auth.kalshi_env}:{auth.api_key_source}",
                )
        raise RuntimeError(
            "Set auth in ~/.config/kalshi-multiplex-orderbook/prod.env "
            "(or demo.env), or export KALSHI_PROD_* / KALSHI_DEMO_*."
        )

    if not resolve_auth_settings(args, required=True):
        raise RuntimeError("auth resolution failed")
    return (
        args._auth_api_key_id,
        auth_mod.load_private_key(args._auth_private_key_file),
        f"{args.kalshi_env}:{args._auth_api_key_env}",
    )


def select_watch_rows(rows: list[MarketRow], *, watch_limit: int) -> list[MarketRow]:
    """Prefer active/open markets for the *initial* websocket subscription.

    Browse mode later expands this set to whatever is on-screen (viewport),
    because a fixed first-N seed misses later series like player props.
    """
    preferred_status = {"active", "open"}
    live = [r for r in rows if r.status.lower() in preferred_status]
    chosen = live if live else list(rows)
    if watch_limit > 0:
        chosen = chosen[:watch_limit]
    return chosen


def run_watch(
    rows: list[MarketRow],
    *,
    args: argparse.Namespace,
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
        api_key_id, private_key, auth_label = resolve_ws_auth(args)
    except Exception as exc:  # noqa: BLE001
        eprint(f"error: WebSocket auth not configured: {exc}")
        pref_k, pref_p = default_auth_env_names(args.kalshi_env)
        eprint(
            f"For --{args.kalshi_env}, set {pref_k}+{pref_p} in the environment or in "
            f"~/.config/kalshi-multiplex-orderbook/{args.kalshi_env}.env "
            f"(and place the PEM as {args.kalshi_env}.private-key.pem)."
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
            store.wait_for_update(timeout=1.0)
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



CATEGORY_ORDER = (
    "game_lines",
    "player_props",
    "team_props",
    "game_props",
    "other",
)

CATEGORY_LABELS = {
    "game_lines": "Game lines",
    "player_props": "Player props",
    "team_props": "Team props",
    "game_props": "Game props",
    "other": "Other",
}


def classify_market(row: MarketRow) -> str:
    """Bucket a market into the sports browser categories."""
    series = (row.series_ticker or "").upper()
    title = f"{row.title} {row.yes_sub_title} {row.no_sub_title}".lower()

    if any(tok in series for tok in ("TEAMTOTAL", "TEAMTD", "TEAMYDS", "TEAMSACK", "FIRSTTDTEAM")):
        return "team_props"
    if series.endswith("TEAM") and "GAME" not in series:
        return "team_props"

    player_series = (
        "PASSYDS", "PASSTDS", "PASSATT", "PASSCOMP", "PASSINT",
        "RSHYDS", "RSHATT", "REC", "RECYDS", "RRYDS",
        "ANYTD", "FIRSTTD", "FFPTS", "LONGREC", "LONGRSH",
        "MOSTREC", "MOSTRSH", "TOTALTD",
    )
    if any(tok in series for tok in player_series):
        # FIRSTTDTEAM already handled; bare FIRSTTD is player props
        if "TEAM" in series and "FIRSTTDTEAM" not in series and series.endswith("TEAM"):
            return "team_props"
        return "player_props"
    if "player" in title or re.search(r"\b(qb|rb|wr|te)\b", title):
        return "player_props"

    game_line_series = (
        "GAME", "SPREAD", "TOTAL", "WINMARGIN", "MONEYLINE", "ML",
        "1H", "2H", "1Q", "2Q", "3Q", "4Q",
    )
    # TEAMTOTAL already returned; plain TOTAL/SPREAD/GAME are lines
    if any(tok in series for tok in game_line_series) and "TEAM" not in series:
        # quarters/halves totals/spreads are still game lines
        if not any(tok in series for tok in ("PASS", "RSH", "REC", "TD", "FG", "SACK", "FFPTS")):
            return "game_lines"
        # e.g. unexpected mix
    if series in {"KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLWINMARGIN"} or re.fullmatch(r"KXNFL[1234]Q(SPREAD|TOTAL)?", series) or re.fullmatch(r"KXNFL[12]H(SPREAD|TOTAL|FT)?", series):
        return "game_lines"

    game_prop_series = (
        "BOTH", "GAMESPECIALS", "GAMETD", "GAMEFG", "GAMESACK", "FG", "SACK", "TD",
    )
    if any(tok in series for tok in game_prop_series):
        return "game_props"

    return "other"


def market_haystack(row: MarketRow) -> str:
    return " ".join(
        [
            row.ticker,
            row.title,
            row.yes_sub_title,
            row.no_sub_title,
            row.series_ticker,
            row.event_ticker,
            row.status,
            CATEGORY_LABELS.get(classify_market(row), ""),
        ]
    ).lower()


def filter_tokens(text: str) -> list[str]:
    """Whitespace-split query tokens (order-independent matching)."""
    return [tok for tok in str(text or "").lower().split() if tok]


def row_matches_filter(row: MarketRow, filter_text: str) -> bool:
    """Name + kind match. Intent tokens (re/ru) are not title search."""
    tokens = parse_play_query(filter_text).filter_tokens()
    if not tokens:
        return True
    hay = market_haystack(row)
    return all(tok in hay for tok in tokens)


def short_label(text: str, max_len: int = 56) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)] + "…"


@dataclass
class QuoteSnap:
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    spread: int | None = None
    ready: bool = False
    seq: int | None = None
    updated_ts: float = 0.0


class NullBookTracker:
    """Headless tracker for --script. No websocket, no quotes."""

    def __init__(self) -> None:
        self.status = "off"
        self.error: str | None = None
        self.subscribed_n = 0
        self.enabled = False
        self.auth_label = ""

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def poll_status(self) -> None:
        return None

    def ensure_quotes(self, tickers: Iterable[str], *, force: bool = False) -> None:
        return None

    def quote(self, ticker: str) -> QuoteSnap | None:
        return None

    def ready_count(self) -> int:
        return 0


class BackgroundBookTracker:
    """Silently maintain live books over one multiplex WebSocket.

    Starts with a seed subscription (watch_limit), then expands to whatever
    tickers the UI is currently showing via ensure_quotes().
    """

    def __init__(
        self,
        rows: list[MarketRow],
        *,
        ws_url: str,
        watch_limit: int,
        log_raw: bool = False,
        args: argparse.Namespace | None = None,
    ):
        self.rows = rows
        self.ws_url = ws_url
        self.watch_limit = watch_limit
        self.log_raw = log_raw
        self.args = args
        self.store = None
        self.worker = None
        self.enabled = False
        self.status = "off"
        self.auth_label = ""
        self.error: str | None = None
        self.subscribed_n = 0
        self._quotes: dict[str, QuoteSnap] = {}
        self._lock_err = ""
        self._seed_tickers: set[str] = set()
        self._subscribed: set[str] = set()
        self._keep_tickers: set[str] = set()
        self._last_ensure_ts = 0.0
        # Soft cap on concurrent WS markets. 0 watch_limit => larger ceiling.
        if watch_limit <= 0:
            self._max_subscribed = 400
        else:
            self._max_subscribed = max(watch_limit, 160)

    def start(self) -> None:
        try:
            from kx_orderbooks.store import OrderbookStore
            from kx_orderbooks.ws_multiplex import MultiplexOrderbookWorker
        except ImportError as exc:
            self.status = "no-deps"
            self.error = f"kx_orderbooks import failed: {exc}"
            return

        try:
            api_key_id, private_key, auth_label = resolve_ws_auth(self.args)
        except Exception as exc:  # noqa: BLE001
            self.status = "no-auth"
            self.error = str(exc)
            return

        chosen = select_watch_rows(self.rows, watch_limit=self.watch_limit)
        if not chosen:
            self.status = "no-markets"
            self.error = "no markets to subscribe"
            return

        tickers = [r.ticker.upper() for r in chosen]
        self._seed_tickers = set(tickers)
        self._subscribed = set(tickers)
        self._keep_tickers = set(tickers)
        self.subscribed_n = len(tickers)
        self.auth_label = auth_label
        self.store = OrderbookStore()
        self.worker = MultiplexOrderbookWorker(
            market_tickers=tickers,
            store=self.store,
            ws_url=self.ws_url,
            api_key_id=api_key_id,
            private_key=private_key,
            use_yes_price=True,
            reconnect=True,
            log_raw=self.log_raw,
        )

        def _on_update(update) -> None:  # noqa: ANN001
            # Silent tracking only — UI pulls snapshots on redraw.
            try:
                b = update.view.best
                self._quotes[update.market_ticker.upper()] = QuoteSnap(
                    yes_bid=b.yes_bid_cents,
                    yes_ask=b.yes_ask_cents,
                    no_bid=b.no_bid_cents,
                    no_ask=b.no_ask_cents,
                    spread=b.spread_cents,
                    ready=bool(update.view.ready),
                    seq=update.seq if isinstance(update.seq, int) else None,
                    updated_ts=time.time(),
                )
            except Exception as exc:  # noqa: BLE001
                self._lock_err = str(exc)

        self.store.register_callback(_on_update)
        self.worker.start()
        self.enabled = True
        self.status = "starting"

        # Brief non-blocking wait so first UI frame can show connect state.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self.worker.subscribed:
                self.status = "live"
                break
            if self.worker.last_error is not None and not self.worker.connected:
                self.status = "error"
                self.error = repr(self.worker.last_error)
                break
            time.sleep(0.05)
        else:
            if self.worker.connected:
                self.status = "connected"
            else:
                self.status = "connecting"

    def poll_status(self) -> None:
        if not self.enabled or self.worker is None:
            return
        if self.worker.last_error is not None and not self.worker.connected:
            self.status = "error"
            self.error = repr(self.worker.last_error)
        elif self.worker.subscribed:
            self.status = "live"
        elif self.worker.connected:
            self.status = "connected"
        else:
            self.status = "connecting"
        # Keep count in sync with worker's view of the world.
        try:
            self.subscribed_n = len(getattr(self.worker, "market_tickers", []) or self._subscribed)
            self._subscribed = {t.upper() for t in (self.worker.market_tickers or [])}
        except Exception:  # noqa: BLE001
            self.subscribed_n = len(self._subscribed)

    def ensure_quotes(self, tickers: Iterable[str], *, force: bool = False) -> None:
        """Subscribe any missing tickers the UI currently cares about.

        Kalshi books only arrive for subscribed markets. Browse starts with a
        seed (default first N), then calls this for the visible viewport so
        player props / filtered rows get books too.
        """
        if not self.enabled or self.worker is None:
            return
        if not self.worker.subscribed and not force:
            # Wait until the initial subscribe sid exists; add_markets needs it.
            return

        wanted = sorted({str(t).upper() for t in tickers if t})
        if not wanted:
            return

        now = time.time()
        # Light throttle so rapid redraws don't spam add/delete.
        if not force and now - self._last_ensure_ts < 0.15:
            self._keep_tickers |= set(wanted)
            return
        self._last_ensure_ts = now
        self._keep_tickers |= set(wanted)

        missing = [t for t in wanted if t not in self._subscribed]
        if missing:
            # Batch adds; worker.add_markets updates its market_tickers set.
            ok = self.worker.add_markets(missing)
            if ok:
                self._subscribed |= set(missing)
                self.subscribed_n = len(self._subscribed)

        # Soft prune far-away markets if over budget.
        if len(self._subscribed) > self._max_subscribed:
            protect = set(self._seed_tickers) | set(self._keep_tickers) | set(wanted)
            # Also protect anything with a fresh quote recently viewed.
            excess = [t for t in sorted(self._subscribed) if t not in protect]
            drop_n = len(self._subscribed) - self._max_subscribed
            if drop_n > 0 and excess:
                to_drop = excess[:drop_n]
                if self.worker.delete_markets(to_drop):
                    self._subscribed -= set(to_drop)
                    self.subscribed_n = len(self._subscribed)
                    for t in to_drop:
                        self._quotes.pop(t, None)

        # If already subscribed but still blank after a bit, nudge a snapshot.
        stale_need: list[str] = []
        for t in wanted:
            q = self._quotes.get(t)
            if q is None or not q.ready:
                # Only request once the ticker is known-subscribed.
                if t in self._subscribed:
                    stale_need.append(t)
        if stale_need and self.worker.subscribed:
            # Cheap: request snapshots for visible blanks only.
            self.worker.request_snapshots(stale_need)

    def quote(self, ticker: str) -> QuoteSnap | None:
        t = ticker.upper()
        q = self._quotes.get(t)
        if q is not None:
            return q
        if self.store is None:
            return None
        view = self.store.get_view(t)
        if view is None:
            return None
        b = view.best
        q = QuoteSnap(
            yes_bid=b.yes_bid_cents,
            yes_ask=b.yes_ask_cents,
            no_bid=b.no_bid_cents,
            no_ask=b.no_ask_cents,
            spread=b.spread_cents,
            ready=bool(view.ready),
            seq=view.seq if isinstance(view.seq, int) else None,
            updated_ts=view.last_local_ts or time.time(),
        )
        self._quotes[t] = q
        return q

    def ready_count(self) -> int:
        if self.store is None:
            return len([q for q in self._quotes.values() if q.ready])
        return sum(1 for v in self.store.views() if v.ready)

    def stop(self) -> None:
        if self.worker is not None:
            try:
                self.worker.stop()
                self.worker.join(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self.status = "stopped"


def read_terminal_key(fd: int) -> tuple[str, str]:
    """Read one keypress from raw terminal fd. Returns (kind, value)."""
    ch = os.read(fd, 1)
    if not ch:
        return "empty", ""
    if ch == b"\x1b":
        seq = bytearray(ch)
        deadline = time.time() + 0.03
        while len(seq) < 8 and time.time() < deadline:
            readable, _, _ = select.select([fd], [], [], 0.005)
            if not readable:
                break
            try:
                b = os.read(fd, 1)
            except OSError:
                break
            if not b:
                break
            seq.extend(b)
        mapping = {
            b"\x1b[A": "up",
            b"\x1b[B": "down",
            b"\x1b[C": "right",
            b"\x1b[D": "left",
            b"\x1b[5~": "pageup",
            b"\x1b[6~": "pagedown",
            b"\x1b[H": "home",
            b"\x1b[F": "end",
        }
        key = mapping.get(bytes(seq))
        if key:
            return key, bytes(seq).decode("ascii", errors="ignore")
        if bytes(seq) == b"\x1b":
            return "escape", "esc"
        return "escape", bytes(seq).decode("ascii", errors="ignore")
    if ch == b"\x08":
        return "ctrl", "h"  # Ctrl-H help (not backspace)
    if ch == b"\x7f":
        return "backspace", "del"
    if ch in (b"\r", b"\n"):
        return "enter", "enter"
    if ch == b"\x03":
        return "ctrl", "c"
    if ch == b"\x0c":
        return "ctrl", "l"
    if ch == b"\x12":
        return "ctrl", "r"
    if ch == b"\x04":
        return "ctrl", "d"
    if ch == b"\x15":
        return "ctrl", "u"
    try:
        s = ch.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown", ""
    if s.isprintable() or s == "\t":
        return "char", s
    if 0 < ch[0] < 32:
        return "ctrl", chr(ch[0] + 96)
    return "unknown", s
def clear_screen() -> None:
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def help_text() -> str:
    return """
SPORTS TRADER — KEYBOARD HELP (Ctrl-H)
======================================
Modes (vim-style)
  NORMAL             navigate; letters are commands (default)
  FILTER             type query; Tab completes player names
  f or /             enter FILTER mode
  Tab                complete player last name (wa → watson)
  Enter              lock player; after `td` arm First TD + 1+; with a draft, confirm/send
  `td` then Enter    arm First TD + player 1+ (no Q 6.5 yet — TD is only 6)
  then `re`          add same-team QB 1+ pass (once; rec == re)
  then `ru`          rush: drop QB pass
  then `pat`         PAT good: add this-Q over 6.5
  `fg m` then Enter  arm this game's team FG ladder (m→MIA, k→KC). Confirm Enter sends.
  NCAAF `/ f td`     team TD: first-TD YES/NOs + Q/H overs a 6-pt score clears.
                     `re` = receiving (2+ ladder). `d` = also D/ST. Confirm Enter sends.
  `/ pat` `/ 2pt`    after a TD: +1 or +2 and arm newly crossed Q/H/game/team totals.
  `/ nopat` `/ no2pt` miss: no points, close the extra-point window.
  `/ qend`           end this quarter (no TIME mode). Winner YES, loser/tie NO,
                     covered spreads YES, uncovered/loser spreads NO, Q totals YES/NO.
  Enter again        send armed YES legs (dry-run unless --live) and then update score
  `/ qend`           end this quarter: winner/tie, spreads, Q totals. Confirm Enter sends.
  Esc                leave FILTER/TIME → NORMAL
  Ctrl-U             clear filter + locked player

NORMAL navigation
  ↑ / k / Ctrl-P     move up
  ↓ / j / Ctrl-N     move down
  PgUp / PgDn        page up/down
  Home / End / g/G   first / last row
  Enter              categories: open · markets/detail: BUY YES (--count-yes)
  d / Space           open market detail (no order)
  o                  ORDERS page — session bets + exchange positions
  Esc / Backspace    clear filter if set, else go back
  1-5                jump to category (on category screen)
  a                  show All markets category
  Ctrl-L / Ctrl-R    redraw
  Ctrl-H             this help
  q                  ask to quit · Enter confirms · Esc/other cancels

Orders / exits
  Enter on a market  BUY YES for --count-yes contracts (default 1)
  Price (buy)        YES ask + --slippage-cents (default 1), IOC limit
  o then Enter       SELL YES EXIT ALL contracts on selected position
  Price (sell)       YES bid - slippage, reduce_only IOC (explicit SELL)
  r (on orders)      refresh exchange positions from API
  Dry-run default    memory-logs payload only; add --live to submit
  Order log          in-memory; dumps to file when idle / on exit
  Prefer --demo      for first live tests
  L                  show recent memory log + force dump

FILTER matching
  Multi-word, order-independent: "patrick mahomes td"
  matches markets containing all of those tokens anywhere
  in ticker/title/series (any order).
  q / k / j / numbers all type into the filter in FILTER mode.

Quotes
  Background WebSocket keeps books silently (no spam).
  YES bid/ask appears when a book snapshot has arrived.
  Browse works without API keys; quotes need config/env auth.
  Books refresh live for subscribed markets; viewport auto-subscribes
  more as you scroll/filter (seed starts at --watch-limit).

Screens
  Categories → Markets → Market detail
""".strip()


@dataclass
class BrowserState:
    seed_series: str
    game_code: str
    rows: list[MarketRow]
    tracker: BackgroundBookTracker
    args: argparse.Namespace
    mode: str = "categories"  # categories | markets | detail | help | orders
    input_mode: str = "normal"  # normal | filter | time
    category: str = "game_lines"
    filter_text: str = ""
    cursor: int = 0
    offset: int = 0
    page_size: int = 18
    selected_ticker: str = ""
    message: str = ""
    prev_mode: str = "categories"
    order_busy: bool = False
    last_order_ts: float = 0.0
    quit_confirm: bool = False
    sell_confirm: bool = False
    sell_confirm_ticker: str = ""
    sell_confirm_qty: int = 0
    return_mode: str = "categories"  # mode to restore when leaving orders
    session: Any = None
    committed_player: str = ""
    last_play_raw: str = ""
    time_text: str = ""
    draft: Any = None
    script_mode: bool = False
    script_log: list[dict[str, Any]] = field(default_factory=list)
    rollback_offer: Any = None

    def market_available(self, row: MarketRow) -> bool:
        if self.session is None:
            return True
        ticker = str(row.ticker or "").upper()
        if ticker in self.session.sent_markets:
            return False
        st = self.session.state()
        series = row_series(row)
        if st.game_tds >= 1 and series in {"KXNFLFIRSTTD", "KXNCAAFFIRSTTDTEAM"}:
            return False
        return True

    def category_counts(self) -> dict[str, int]:
        counts = {k: 0 for k in CATEGORY_ORDER}
        for row in self.rows:
            if not self.market_available(row):
                continue
            counts[classify_market(row)] = counts.get(classify_market(row), 0) + 1
        return counts

    def filtered_rows(self) -> list[MarketRow]:
        out: list[MarketRow] = []
        for row in self.rows:
            if not self.market_available(row):
                continue
            if self.category != "all" and classify_market(row) != self.category:
                continue
            if not row_matches_filter(row, self.filter_text):
                continue
            out.append(row)
        out.sort(key=lambda r: (r.series_ticker, r.ticker))
        return out

    def category_items(self) -> list[tuple[str, str, int]]:
        counts = self.category_counts()
        items = [(key, CATEGORY_LABELS[key], counts.get(key, 0)) for key in CATEGORY_ORDER]
        items.append(("all", "All markets", len(self.rows)))
        return items


def clamp_cursor(state: BrowserState, n_items: int) -> None:
    if n_items <= 0:
        state.cursor = 0
        state.offset = 0
        return
    state.cursor = max(0, min(state.cursor, n_items - 1))
    if state.cursor < state.offset:
        state.offset = state.cursor
    if state.cursor >= state.offset + state.page_size:
        state.offset = state.cursor - state.page_size + 1


def move_cursor(state: BrowserState, delta: int, n_items: int) -> None:
    if n_items <= 0:
        return
    state.cursor = max(0, min(n_items - 1, state.cursor + delta))
    clamp_cursor(state, n_items)


def format_quote_cell(q: QuoteSnap | None) -> str:
    if q is None or not q.ready:
        return "  -- x -- "
    return f"{fmt_cents(q.yes_bid):>4} x {fmt_cents(q.yes_ask):<4}"


def format_filter_line(state: BrowserState, match_count: int | None = None) -> str:
    """Visible filter bar. Live caret only while input_mode == filter."""
    if state.mode == "help":
        return "filter> (paused on help)"
    needle = state.filter_text
    filtering = state.input_mode == "filter"
    if match_count is None:
        count_bit = ""
    else:
        unit = "match" if match_count == 1 else "matches"
        count_bit = f"  · {match_count} {unit}"
    q = parse_play_query(needle)
    intent_bit = ""
    if q.intent == "rush":
        intent_bit = "  · RUSH"
    elif q.intent == "receiving":
        intent_bit = "  · REC"
    play_bit = f"  · player {state.committed_player}" if state.committed_player else ""
    if state.input_mode == "time":
        return f"TIME> {state.time_text}█  · qend · gb 7 · Esc normal"
    if filtering:
        if state.draft is not None:
            return (
                f"FILTER> {needle}█{count_bit}{intent_bit}{play_bit}  "
                f"· re QB · ru rush · Enter confirms send"
            )
        if state.committed_player:
            return (
                f"FILTER> {needle}█{count_bit}{play_bit}  "
                f"· td Enter · re/ru · pat · fg team"
            )
        return (
            f"FILTER> {needle}█{count_bit}  · Tab name · Enter lock player"
        )
    if needle:
        return f"filter: {needle}{count_bit}{intent_bit}{play_bit}  · f filter · Ctrl-U clear · NORMAL"
    return f"filter: (empty){count_bit}  · f filter · / qend · NORMAL"


def enter_filter_mode(state: BrowserState, *, jump_all: bool = False) -> None:
    """Enter FILTER input mode; optionally open All markets."""
    if state.mode == "detail":
        state.mode = "markets"
    if jump_all and state.mode == "categories":
        open_category(state, "all")
    if state.mode not in {"categories", "markets"}:
        return
    state.input_mode = "filter"
    state.message = ""
    state.cursor = 0
    state.offset = 0


def leave_filter_mode(state: BrowserState) -> None:
    state.input_mode = "normal"
    if not state.message:
        state.message = ""


def clear_filter(state: BrowserState) -> None:
    state.filter_text = ""
    state.cursor = 0
    state.offset = 0
    state.message = ""
    state.committed_player = ""
    state.last_play_raw = ""
    state.time_text = ""
    state.draft = None


def _play_quantity(state: BrowserState) -> int:
    return effective_count_yes(state.args)


def _college_mode(state: BrowserState) -> bool:
    if str(state.seed_series or "").upper().startswith("KXNCAAF"):
        return True
    return is_college_rows(state.rows)


def _sync_draft_intent(state: BrowserState) -> None:
    """If a TD draft is open, re/ru/pat/d updates legs when those tokens change."""
    if state.session is None or state.draft is None:
        return
    q = parse_play_query(state.filter_text)
    want_intent = q.intent if q.intent is not None else state.draft.intent
    if want_intent == state.draft.intent and q.pat == state.draft.pat:
        return
    if state.draft.kind == "ncaaf_td":
        draft, _cands, msg = arm_ncaaf_td_draft(
            state.session,
            state.rows,
            q,
            quantity=_play_quantity(state),
            previous=state.draft,
            intent=want_intent,
        )
    else:
        draft, _cands, msg = arm_td_draft(
            state.session,
            state.rows,
            q,
            quantity=_play_quantity(state),
            previous=state.draft,
            intent=want_intent,
            include_pat=q.pat,
        )
    state.draft = draft
    state.message = msg
    _log_script_draft(state)


def _log_script_draft(state: BrowserState) -> None:
    if not state.script_mode or state.session is None:
        return
    armed = state.session.arming.armed_bets()
    state.script_log.append(
        {
            "event": "draft",
            "filter": state.filter_text,
            "message": state.message,
            "armed": [
                {"ticker": b.market_id, "side": b.side.value, "quantity": b.quantity}
                for b in armed
            ],
        }
    )


def confirm_td_draft(state: BrowserState) -> None:
    if state.session is None or state.draft is None:
        return
    if state.order_busy:
        state.message = "order already in flight"
        return
    draft = state.draft
    armed = [b for b in state.session.arming.armed_bets() if b.armed_id in set(draft.armed_ids)]
    would_send = [
        {
            "ticker": b.market_id,
            "side": b.side.value,
            "quantity": b.quantity,
            "reason": next(
                (c.reason for c in state.session.arming.candidates() if c.candidate_id == b.candidate_id),
                b.trigger,
            ),
        }
        for b in armed
    ]
    sent = 0
    missed: list[str] = []
    if not state.script_mode:
        tickers = [b.market_id for b in armed]
        if tickers:
            state.tracker.ensure_quotes(tickers, force=True)
        state.order_busy = True
        try:
            for bet in armed:
                row = next((r for r in state.rows if r.ticker.upper() == bet.market_id.upper()), None)
                if row is None:
                    missed.append(bet.market_id)
                    continue
                quote = state.tracker.quote(row.ticker)
                live = bool(getattr(state.args, "live", False))
                side = str(getattr(bet.side, "value", bet.side) or "yes").lower()
                ask = quote.yes_ask if quote is not None else None
                if side == "no":
                    ask = quote.no_ask if quote is not None else None
                if ask is None and not live:
                    SESSION_BETS.record_buy(
                        ticker=row.ticker,
                        title=row.title or row.yes_sub_title or "",
                        count=_play_quantity(state),
                        limit_cents=None,
                        live=False,
                        dry_run=True,
                        note=f"DRY-RUN BUY {side.upper()} {row.ticker} (no ask yet)",
                        side=side,
                    )
                    sent += 1
                    continue
                if side == "no":
                    ok, status = buy_no_for_market(args=state.args, row=row, quote=quote)
                else:
                    ok, status = buy_yes_for_market(args=state.args, row=row, quote=quote)
                if ok:
                    sent += 1
                else:
                    missed.append(short_label(status, 40))
        finally:
            state.order_busy = False
            state.last_order_ts = time.time()
    else:
        sent = len(would_send)
        state.script_log.append({"event": "confirm", "would_send": would_send})
    pending_before = state.session.pending_extra_team
    before_n = len(state.session.store.events())
    state.session.mark_sent(b.market_id for b in armed)
    if draft.kind == "fg":
        recorded = apply_fg_draft(state.session, draft)
    elif draft.kind == "saf":
        recorded = apply_safety_draft(state.session, draft)
    elif draft.kind == "extra":
        recorded = apply_extra_draft(state.session, draft)
    elif draft.kind == "qend":
        recorded = apply_qend_draft(state.session, draft)
    else:
        recorded = apply_td_draft(state.session, draft)
    state.session.record_play(
        kind=str(draft.kind or "td"),
        label=recorded,
        n_events=len(state.session.store.events()) - before_n,
        sent=would_send,
        pending_extra_before=pending_before,
    )
    for armed_id in list(draft.armed_ids):
        try:
            state.session.arming.disarm(armed_id)
        except KeyError:
            pass
    state.draft = None
    extra = f" · missed {len(missed)}" if missed else ""
    state.message = f"sent {sent}/{len(armed)}{extra} · {recorded}"
    state.filter_text = ""
    state.committed_player = ""
    state.cursor = 0
    state.offset = 0
    leave_filter_mode(state)


def handle_filter_tab(state: BrowserState) -> None:
    q = parse_play_query(state.filter_text)
    if state.session is not None and (
        q.kind in {"fg", "saf"} or (_college_mode(state) and q.kind in {None, "td", "fg", "saf"})
    ):
        st = state.session.state()
        new_text, hits = tab_complete_team(state.filter_text, [st.away, st.home])
        state.filter_text = new_text
        state.cursor = 0
        state.offset = 0
        if len(hits) == 1:
            team = resolve_game_team(hits[0], st.away, st.home) or hits[0].upper()
            kind = q.kind or ("fg" if "fg" in state.filter_text.lower() else "")
            if kind == "fg":
                state.filter_text = f"fg {team.lower()} "
                state.message = f"team {team} · Enter to arm FG"
            elif kind == "td":
                state.filter_text = f"{team.lower()} td "
                state.message = f"team {team} · Enter to arm TD"
            else:
                state.filter_text = f"{team.lower()} "
                state.message = f"team {team} · type td"
            if not state.filter_text.endswith(" "):
                state.filter_text += " "
        elif hits:
            state.message = "tab: " + ", ".join(hits)
        else:
            state.message = f"fg team? {st.away.lower()} or {st.home.lower()}"
        if state.mode == "categories":
            open_category(state, "all")
        return
    names = catalog_player_names(state.rows)
    new_text, hits = tab_complete_player(state.filter_text, names)
    state.filter_text = new_text
    state.cursor = 0
    state.offset = 0
    if len(hits) == 1:
        state.committed_player = hits[0]
        if not state.filter_text.endswith(" "):
            state.filter_text += " "
        state.message = f"player {hits[0]} · type td then Enter"
    elif hits:
        state.message = "tab: " + ", ".join(hits[:8])
    else:
        state.message = "no name match"
    if state.mode == "categories":
        open_category(state, "all")


def handle_filter_enter(state: BrowserState) -> None:
    q = parse_play_query(state.filter_text)
    if state.draft is not None:
        confirm_td_draft(state)
        return
    if (
        q.extra
        and q.kind is None
        and not q.name_tokens
        and state.session is not None
    ):
        if q.extra in {"nopat", "no2pt"}:
            if not state.session.pending_extra_team:
                state.message = "no TD waiting for PAT/2PT"
                return
            pending_before = state.session.pending_extra_team
            before_n = len(state.session.store.events())
            state.message = apply_extra_miss(state.session, q.extra)
            state.session.record_play(
                kind="miss",
                label=state.message,
                n_events=len(state.session.store.events()) - before_n,
                sent=[],
                pending_extra_before=pending_before,
            )
            state.filter_text = ""
            state.cursor = 0
            leave_filter_mode(state)
            return
        if not state.session.pending_extra_team:
            state.message = "no TD waiting for PAT/2PT"
            return
        draft, cands, msg = arm_extra_draft(
            state.session,
            state.rows,
            q.extra,
            quantity=_play_quantity(state),
        )
        if not cands:
            pending_before = state.session.pending_extra_team
            before_n = len(state.session.store.events())
            recorded = apply_extra_draft(state.session, draft) if draft is not None else msg
            state.session.record_play(
                kind="extra",
                label=recorded,
                n_events=len(state.session.store.events()) - before_n,
                sent=[],
                pending_extra_before=pending_before,
            )
            state.message = recorded
            state.filter_text = ""
            state.cursor = 0
            leave_filter_mode(state)
            return
        state.draft = draft
        state.filter_text = f"{q.extra} "
        state.message = msg
        _log_script_draft(state)
        return
    if q.kind == "qend" and state.session is not None:
        draft, _cands, msg = arm_qend_draft(
            state.session, state.rows, quantity=_play_quantity(state)
        )
        state.draft = draft
        state.filter_text = "qend "
        state.message = msg
        _log_script_draft(state)
        return
    if q.kind == "td" and _college_mode(state) and state.session is not None:
        st = state.session.state()
        if not q.name_tokens:
            state.message = f"td which team? {st.away.lower()} / {st.home.lower()}"
            return
        team = resolve_game_team(q.name_tokens[0], st.away, st.home)
        if not team:
            handle_filter_tab(state)
            return
        draft, _cands, msg = arm_ncaaf_td_draft(
            state.session,
            state.rows,
            q,
            quantity=_play_quantity(state),
            intent=q.intent,
        )
        state.draft = draft
        trail = ""
        if q.intent == "receiving":
            trail = " re"
        elif q.intent == "defense":
            trail = " d"
        state.filter_text = f"{team.lower()} td{trail} "
        state.message = msg
        _log_script_draft(state)
        return
    if q.kind == "saf" and state.session is not None:
        st = state.session.state()
        if not q.name_tokens:
            state.message = f"saf which team? {st.away.lower()} / {st.home.lower()}"
            return
        team = resolve_game_team(q.name_tokens[0], st.away, st.home)
        if not team:
            handle_filter_tab(state)
            return
        draft, _cands, msg = arm_safety_draft(
            state.session,
            state.rows,
            q,
            quantity=_play_quantity(state),
        )
        state.draft = draft
        state.filter_text = f"{team.lower()} saf "
        state.message = msg
        _log_script_draft(state)
        return
    if q.kind == "fg" and _college_mode(state) and state.session is not None:
        st = state.session.state()
        if not q.name_tokens:
            state.message = f"fg which team? {st.away.lower()} / {st.home.lower()}"
            return
        team = resolve_game_team(q.name_tokens[0], st.away, st.home)
        if not team:
            handle_filter_tab(state)
            return
        draft, _cands, msg = arm_ncaaf_fg_draft(
            state.session,
            state.rows,
            q,
            quantity=_play_quantity(state),
        )
        state.draft = draft
        state.filter_text = f"{team.lower()} fg "
        state.message = msg
        _log_script_draft(state)
        return
    if q.kind == "fg" and state.session is not None:
        st = state.session.state()
        if not q.name_tokens:
            state.message = f"fg which team? {st.away.lower()} / {st.home.lower()}"
            return
        team = resolve_game_team(q.name_tokens[0], st.away, st.home)
        if not team:
            handle_filter_tab(state)
            return
        draft, _cands, msg = arm_fg_draft(
            state.session,
            state.rows,
            q,
            quantity=_play_quantity(state),
        )
        state.draft = draft
        state.filter_text = f"fg {team.lower()} "
        state.message = msg
        _log_script_draft(state)
        return
    if q.kind == "td" and q.name_tokens and state.session is not None:
        draft, _cands, msg = arm_td_draft(
            state.session,
            state.rows,
            q,
            quantity=_play_quantity(state),
            intent=q.intent,
        )
        state.draft = draft
        state.committed_player = " ".join(q.name_tokens)
        if not state.filter_text.endswith(" "):
            state.filter_text += " "
        state.message = msg
        _log_script_draft(state)
        return
    if q.name_tokens and q.kind is None and q.intent is None:
        if _college_mode(state) and state.session is not None:
            st = state.session.state()
            team = resolve_game_team(q.name_tokens[0], st.away, st.home)
            if team:
                state.filter_text = team.lower() + " "
                state.message = f"team {team} · type td then Enter"
                return
        hits, display = unique_player_rows(state.rows, q.name_tokens)
        if display:
            last = display.split()[-1]
            state.committed_player = last
            state.filter_text = last + " "
            state.message = f"player {display} · type td then Enter"
            return
        handle_filter_tab(state)
        return
    leave_filter_mode(state)


def enter_time_mode(state: BrowserState) -> None:
    state.input_mode = "time"
    state.time_text = ""
    st = state.session.state() if state.session is not None else None
    if st is not None:
        state.message = (
            f"TIME Q{st.quarter} {st.away} {st.away_score}-{st.home_score} {st.home} "
            f"· qend · {st.away.lower()} 7"
        )
    else:
        state.message = "TIME · qend · team score"


def handle_time_enter(state: BrowserState) -> None:
    if state.session is None:
        state.input_mode = "normal"
        return
    parsed = parse_time_line(state.time_text, state.session)
    if parsed is None:
        state.message = "time: qend | q 2 | gb 7"
        return
    kind, rest = parsed
    qty = _play_quantity(state)
    if kind == "qend":
        _cands, msg = ingest_quarter_end(state.session, quantity=qty, auto_arm=True)
        state.message = msg
        state.time_text = ""
    elif kind == "quarter":
        from sports_engine.models import EventType, GameEvent

        session = state.session
        session.ingest(GameEvent(type=EventType.QUARTER, quarter=int(rest), source="timekeeping"))
        st = session.state()
        state.message = f"quarter Q{st.quarter}"
        state.time_text = ""
    elif kind == "score":
        team, pts = rest.split()
        state.message = ingest_score(state.session, team, int(pts))
        state.time_text = ""


def append_filter_char(state: BrowserState, ch: str) -> None:
    """Append one character to the live filter query."""
    state.filter_text += ch
    state.cursor = 0
    state.offset = 0
    state.message = ""
    # Typing from categories with an active filter jumps into All markets.
    if state.mode == "categories":
        open_category(state, "all")
    _sync_draft_intent(state)


def render_browser(state: BrowserState) -> None:
    state.tracker.poll_status()
    ready = state.tracker.ready_count()
    sub_n = state.tracker.subscribed_n
    ws = state.tracker.status
    if state.tracker.error and ws in {"error", "no-auth", "no-deps"}:
        ws_line = f"WS {ws}: {short_label(state.tracker.error, 40)}"
    else:
        ws_line = f"WS {ws}  books {ready}/{sub_n}" if sub_n else f"WS {ws}"

    # Precompute market matches when filtering so the bar can show live counts.
    market_rows: list[MarketRow] | None = None
    match_count: int | None = None
    if state.mode == "markets":
        market_rows = state.filtered_rows()
        match_count = len(market_rows)
    elif state.filter_text.strip():
        match_count = sum(1 for row in state.rows if row_matches_filter(row, state.filter_text))

    lines: list[str] = []
    if state.input_mode == "filter":
        mode_tag = "FILTER"
    elif state.input_mode == "time":
        mode_tag = "TIME"
    else:
        mode_tag = "NORMAL"
    lines.append(
        f"{state.seed_series}-{state.game_code}   markets={len(state.rows)}   "
        f"{mode_tag}   {ws_line}"
    )
    if state.session is not None:
        st = state.session.state()
        n_arm = len(state.session.arming.armed_bets())
        lines.append(
            f"Q{st.quarter}  {st.away} {st.away_score}-{st.home_score} {st.home}  "
            f"thisQ {st.points_this_quarter}  | armed {n_arm}"
        )
    filter_line_idx = len(lines)  # 0-based index in lines; terminal row = idx + 1
    filter_line = format_filter_line(state, match_count)
    lines.append(filter_line)
    if state.message:
        lines.append(state.message)
    lines.append("")

    if state.mode == "help":
        lines.extend(help_text().splitlines())
        lines.append("")
        lines.append("Press Esc / Backspace / q to return.")
    elif state.mode == "categories":
        lines.append("CATEGORIES")
        items = state.category_items()
        clamp_cursor(state, len(items))
        for idx, (key, label, count) in enumerate(items):
            if idx < state.offset or idx >= state.offset + state.page_size:
                continue
            mark = ">" if idx == state.cursor else " "
            num = "A" if key == "all" else str(CATEGORY_ORDER.index(key) + 1 if key in CATEGORY_ORDER else " ")
            lines.append(f" {mark} {num}  {label:<14}  ({count})")
        lines.append("")
        if state.input_mode == "filter":
            lines.append("FILTER mode · type letters/digits/space · Esc/Enter normal · Ctrl-U clear · q is a letter")
        else:
            lines.append("NORMAL · Enter open · 1-5/A jump · o orders · f filter · j/k move · q quit? · Ctrl-H help")
    elif state.mode == "markets":
        rows = market_rows if market_rows is not None else state.filtered_rows()
        label = CATEGORY_LABELS.get(state.category, "All markets" if state.category == "all" else state.category)
        filt = f" filter={state.filter_text!r}" if state.filter_text else ""
        lines.append(f"MARKETS — {label}  showing {len(rows)}{filt}")
        clamp_cursor(state, len(rows))
        if not rows:
            lines.append("  (no markets match)")
        else:
            lines.append(f" {'':1} {'YES bid x ask':^11}  {'STATUS':<8}  {'SERIES':<16}  TICKER / TITLE")
            end = min(len(rows), state.offset + state.page_size)
            # Prefetch a small lookahead beyond the page so scrolling feels live.
            prefetch_end = min(len(rows), end + max(4, state.page_size // 2))
            visible = rows[state.offset:prefetch_end]
            state.tracker.ensure_quotes(r.ticker for r in visible)
            for idx in range(state.offset, end):
                row = rows[idx]
                mark = ">" if idx == state.cursor else " "
                q = state.tracker.quote(row.ticker)
                title = short_label(row.title or row.yes_sub_title or "", 42)
                lines.append(
                    f" {mark} {format_quote_cell(q)}  {(row.status or '-'):<8}  "
                    f"{row.series_ticker:<16}  {row.ticker}"
                )
                lines.append(f"      {title}")
        lines.append("")
        if state.input_mode == "filter":
            lines.append("FILTER mode · type freely (q/k/j ok) · Esc/Enter normal · Ctrl-U clear")
        else:
            count_yes = effective_count_yes(state.args)
            mode = "LIVE" if state.args.live else "DRY-RUN"
            lines.append(
                f"NORMAL · Enter BUY YES x{count_yes} ({mode}) · d detail · o orders · Esc back · f filter · q quit?"
            )
    elif state.mode == "detail":
        row = next((r for r in state.rows if r.ticker == state.selected_ticker), None)
        if row is None:
            lines.append("Market not found.")
        else:
            state.tracker.ensure_quotes([row.ticker], force=True)
            q = state.tracker.quote(row.ticker)
            lines.append("MARKET DETAIL")
            lines.append(f"  ticker : {row.ticker}")
            lines.append(f"  series : {row.series_ticker}")
            lines.append(f"  event  : {row.event_ticker}")
            lines.append(f"  status : {row.status}")
            lines.append(f"  cat    : {CATEGORY_LABELS.get(classify_market(row), classify_market(row))}")
            lines.append(f"  title  : {row.title}")
            if row.yes_sub_title:
                lines.append(f"  yes    : {row.yes_sub_title}")
            if row.no_sub_title:
                lines.append(f"  no     : {row.no_sub_title}")
            lines.append("")
            if q and q.ready:
                lines.append(
                    f"  YES {fmt_cents(q.yes_bid)} x {fmt_cents(q.yes_ask)}"
                    f"   NO {fmt_cents(q.no_bid)} x {fmt_cents(q.no_ask)}"
                    f"   spr {fmt_cents(q.spread)}"
                )
                if q.updated_ts:
                    age = max(0.0, time.time() - q.updated_ts)
                    lines.append(f"  book age: {age:.1f}s   seq={q.seq}")
            else:
                lines.append("  book: (waiting for websocket snapshot)")
            lines.append("")
            count_yes = effective_count_yes(state.args)
            mode = "LIVE" if state.args.live else "DRY-RUN"
            lines.append(
                f"Enter BUY YES x{count_yes} ({mode}) · o orders · Esc back · f filter · q quit?"
            )
    elif state.mode == "orders":
        items = orders_page_items(state)
        mode = "LIVE" if state.args.live else "DRY-RUN"
        age = ""
        if SESSION_BETS.positions_ts:
            age = f"  exch_age={max(0.0, time.time() - SESSION_BETS.positions_ts):.0f}s"
        err = f"  err={SESSION_BETS.positions_error}" if SESSION_BETS.positions_error else ""
        lines.append(f"ORDERS / POSITIONS  ({mode}){age}{err}")
        lines.append(
            "  Enter = SELL YES EXIT ALL qty on row  ·  r refresh exchange  ·  Esc back"
        )
        lines.append("")
        clamp_cursor(state, len(items))
        if not items:
            lines.append("  (no session bets or exchange positions yet)")
            lines.append("  Buy with Enter on a market, or press r to pull exchange positions.")
        else:
            lines.append(
                f" {'':1} {'SIDE':<4} {'QTY':>5}  {'SRC':<12}  {'YES bid x ask':^11}  TICKER"
            )
            end = min(len(items), state.offset + state.page_size)
            visible_tickers = [items[i]["ticker"] for i in range(state.offset, end)]
            state.tracker.ensure_quotes(visible_tickers)
            for idx in range(state.offset, end):
                it = items[idx]
                mark = ">" if idx == state.cursor else " "
                q = state.tracker.quote(it["ticker"])
                sell_tag = "SELL-ALL" if it.get("sellable") else "no-sell"
                lines.append(
                    f" {mark} {it['side']:<4} {it['qty']:>5}  {it['source']:<12}  "
                    f"{format_quote_cell(q)}  {it['ticker']}"
                )
                title = short_label(it.get("title") or "", 52)
                lines.append(f"      [{sell_tag}] {title}")
        lines.append("")
        if state.sell_confirm:
            lines.append(
                f"CONFIRM SELL YES EXIT ALL x{state.sell_confirm_qty} "
                f"{state.sell_confirm_ticker} — Enter submits · Esc/other cancels"
            )
        else:
            lines.append(
                f"NORMAL · highlight row · Enter arms SELL-ALL exit ({mode}) · r refresh · Esc back · q quit?"
            )

    lines.append("")
    clear_screen()
    sys.stdout.write("\n".join(lines) + "\n")
    # Park the real terminal cursor on the filter caret only in FILTER mode.
    if state.input_mode == "filter" and state.mode in {"categories", "markets"}:
        caret_col = 8 + len(state.filter_text) + 1
        caret_row = filter_line_idx + 1
        sys.stdout.write(f"\033[{caret_row};{caret_col}H")
    elif state.input_mode == "time":
        caret_col = 6 + len(state.time_text) + 1
        caret_row = filter_line_idx + 1
        sys.stdout.write(f"\033[{caret_row};{caret_col}H")
    sys.stdout.flush()


def open_category(state: BrowserState, key: str) -> None:
    state.category = key
    state.mode = "markets"
    state.cursor = 0
    state.offset = 0
    state.message = ""


def open_market_detail(state: BrowserState, ticker: str) -> None:
    state.selected_ticker = ticker
    state.mode = "detail"
    state.input_mode = "normal"
    state.message = ""


def selected_market_row(state: BrowserState) -> MarketRow | None:
    if state.mode == "markets":
        rows = state.filtered_rows()
        if not rows:
            return None
        clamp_cursor(state, len(rows))
        return rows[state.cursor]
    if state.mode == "detail" and state.selected_ticker:
        return next((r for r in state.rows if r.ticker == state.selected_ticker), None)
    return None


def handle_buy_yes(state: BrowserState) -> None:
    """Enter on market/detail: BUY YES for --count-yes."""
    if state.order_busy:
        state.message = "order already in flight"
        return
    # simple debounce against double Enter
    now = time.time()
    if now - state.last_order_ts < 0.35:
        state.message = "order debounced — wait a moment"
        return

    row = selected_market_row(state)
    if row is None:
        state.message = "no market selected"
        return

    state.tracker.ensure_quotes([row.ticker], force=True)
    quote = state.tracker.quote(row.ticker)
    state.order_busy = True
    try:
        ok, status = buy_yes_for_market(args=state.args, row=row, quote=quote)
    finally:
        state.order_busy = False
        state.last_order_ts = time.time()
    state.message = status if ok else status
    # Keep selection on the market; jump to detail so the status is readable.
    if state.mode == "markets":
        open_market_detail(state, row.ticker)


def cancel_quit_confirm(state: BrowserState, *, message: str = "quit cancelled") -> None:
    if state.quit_confirm:
        state.quit_confirm = False
        state.message = message


def request_quit_confirm(state: BrowserState) -> None:
    state.quit_confirm = True
    state.message = "Quit? Press Enter to confirm (Esc/other cancels)"


def cancel_sell_confirm(state: BrowserState, *, message: str = "sell cancelled") -> None:
    if state.sell_confirm:
        state.sell_confirm = False
        state.sell_confirm_ticker = ""
        state.sell_confirm_qty = 0
        state.message = message


def open_orders_page(state: BrowserState) -> None:
    state.return_mode = state.mode if state.mode != "orders" else state.return_mode
    state.prev_mode = state.mode if state.mode != "help" else state.prev_mode
    state.mode = "orders"
    state.input_mode = "normal"
    state.cursor = 0
    state.offset = 0
    state.sell_confirm = False
    state.sell_confirm_ticker = ""
    state.sell_confirm_qty = 0
    # Best-effort refresh so exchange positions show up without an extra key.
    state.message = refresh_exchange_positions(state)


def handle_sell_all(state: BrowserState) -> None:
    """Orders page Enter: arm or execute SELL YES EXIT ALL for selected row."""
    if state.order_busy:
        state.message = "order already in flight"
        return
    now = time.time()
    if now - state.last_order_ts < 0.35:
        state.message = "order debounced — wait a moment"
        return

    items = orders_page_items(state)
    if not items:
        state.message = "no positions to sell"
        return
    clamp_cursor(state, len(items))
    it = items[state.cursor]
    ticker = str(it["ticker"])
    qty = int(it["qty"])
    if not it.get("sellable"):
        state.message = f"cannot sell {it.get('side')} position here (YES longs only for now)"
        return
    if qty <= 0:
        state.message = "qty is zero"
        return

    if not state.sell_confirm or state.sell_confirm_ticker != ticker:
        state.sell_confirm = True
        state.sell_confirm_ticker = ticker
        state.sell_confirm_qty = qty
        mode = "LIVE" if state.args.live else "DRY-RUN"
        state.message = (
            f"SELL YES EXIT ALL x{qty} on {ticker}? Enter confirms ({mode}), Esc cancels"
        )
        return

    # Confirmed: submit sell-all.
    state.tracker.ensure_quotes([ticker], force=True)
    quote = state.tracker.quote(ticker)
    title = str(it.get("title") or "")
    state.order_busy = True
    try:
        ok, status = sell_yes_for_position(
            args=state.args,
            ticker=ticker,
            title=title,
            count=qty,
            quote=quote,
        )
    finally:
        state.order_busy = False
        state.last_order_ts = time.time()
        state.sell_confirm = False
        state.sell_confirm_ticker = ""
        state.sell_confirm_qty = 0
    state.message = status
    # Refresh exchange snapshot after live sells.
    if state.args.live and ok:
        extra = refresh_exchange_positions(state)
        state.message = f"{status} · {extra}"


def handle_enter(state: BrowserState) -> None:
    if state.quit_confirm:
        # Enter confirms quit; caller checks quit_confirm after this.
        return
    if state.input_mode == "filter":
        handle_filter_enter(state)
        return
    if state.draft is not None:
        confirm_td_draft(state)
        return
    if state.mode == "categories":
        items = state.category_items()
        if not items:
            return
        clamp_cursor(state, len(items))
        key = items[state.cursor][0]
        open_category(state, key)
    elif state.mode in {"markets", "detail"}:
        handle_buy_yes(state)
    elif state.mode == "orders":
        handle_sell_all(state)
    elif state.mode == "help":
        state.mode = state.prev_mode


def handle_back(state: BrowserState) -> bool:
    """Return True if caller should quit."""
    if state.sell_confirm:
        cancel_sell_confirm(state)
        return False
    if state.input_mode == "filter":
        leave_filter_mode(state)
        return False
    if state.input_mode == "time":
        state.input_mode = "normal"
        return False
    if state.mode == "help":
        state.mode = state.prev_mode
        return False
    if state.mode == "orders":
        state.mode = state.return_mode or "categories"
        state.input_mode = "normal"
        state.cursor = 0
        state.offset = 0
        state.message = "left orders"
        return False
    if state.mode == "detail":
        state.mode = "markets"
        state.input_mode = "normal"
        return False
    if state.mode == "markets":
        # Prefer clearing an active filter before leaving the market list.
        if state.filter_text:
            clear_filter(state)
            return False
        state.mode = "categories"
        state.input_mode = "normal"
        state.cursor = 0
        state.offset = 0
        return False
    if state.mode == "categories" and state.filter_text:
        clear_filter(state)
        return False
    return False


def make_headless_state(
    *,
    seed_series: str,
    game_code: str,
    rows: list[MarketRow],
    args: argparse.Namespace,
) -> BrowserState:
    return BrowserState(
        seed_series=seed_series,
        game_code=game_code,
        rows=rows,
        tracker=NullBookTracker(),
        args=args,
        session=make_browse_session(game_code, rows),
        mode="markets",
        category="all",
        script_mode=True,
    )


def run_key_script(state: BrowserState, script: str) -> dict[str, Any]:
    """Drive the same FILTER/Enter handlers as --browse. No TTY."""
    for kind, value in parse_key_script(script):
        if kind == "slash":
            enter_filter_mode(state, jump_all=True)
            continue
        if kind == "enter":
            if state.input_mode == "time":
                handle_time_enter(state)
            elif state.input_mode == "filter":
                handle_filter_enter(state)
            else:
                handle_enter(state)
            continue
        if kind == "escape":
            handle_back(state)
            continue
        if kind == "char" and value == "\t":
            if state.input_mode == "filter":
                handle_filter_tab(state)
            continue
        if kind == "word":
            if state.input_mode == "time":
                if state.time_text and not state.time_text.endswith(" "):
                    state.time_text += " "
                state.time_text += value
                continue
            if state.input_mode != "filter":
                enter_filter_mode(state, jump_all=True)
            if state.filter_text and not state.filter_text.endswith(" "):
                append_filter_char(state, " ")
            for ch in value:
                append_filter_char(state, ch)
            continue
        if kind == "char" and state.input_mode == "filter":
            append_filter_char(state, value)
    st = state.session.state() if state.session is not None else None
    return {
        "script": script,
        "game_code": state.game_code,
        "filter": state.filter_text,
        "message": state.message,
        "committed_player": state.committed_player,
        "state": None
        if st is None
        else {
            "quarter": st.quarter,
            "away": st.away,
            "home": st.home,
            "away_score": st.away_score,
            "home_score": st.home_score,
            "game_tds": st.game_tds,
        },
        "log": list(state.script_log),
    }


PROMPT_HELP = """
NCAAF prompt  (no market list, no /)
  f td          arm Florida TD bundle
  f fg          Florida FG +3 and newly cleared totals
  f saf         Florida defense safety +2 and newly cleared totals
  f td re       receiving (2+ on the next rec)
  f td d        plus D/ST
  pat / 2pt     extra point after a sent TD
  nopat / no2pt miss
  qend          end this quarter (Q2 also 1H)
  o             orders
  rb            undo last play, then y/n to sell those legs
  empty Enter   send the armed bundle
  x             cancel armed bundle
  q             quit
""".strip()


def format_game_stats(state: BrowserState) -> str:
    if state.session is None:
        return state.game_code
    st = state.session.state()
    extra = ""
    pending = state.session.pending_extra_team
    if pending:
        extra = f"  extra {pending}"
    live = "LIVE" if getattr(state.args, "live", False) else "dry-run"
    qbit = f"OT{st.quarter - 4}" if st.quarter >= 5 else f"Q{st.quarter}"
    return (
        f"{qbit}  {st.away} {st.away_score}-{st.home_score} {st.home}"
        f"  tds {st.game_tds}{extra}  {live}"
    )


def _draft_legs(state: BrowserState) -> list[dict[str, Any]]:
    if state.session is None or state.draft is None:
        return []
    armed = [
        b
        for b in state.session.arming.armed_bets()
        if b.armed_id in set(state.draft.armed_ids)
    ]
    reasons = {
        c.candidate_id: c.reason for c in state.session.arming.candidates()
    }
    out = []
    for b in armed:
        row = next((r for r in state.rows if r.ticker.upper() == b.market_id.upper()), None)
        title = ""
        if row is not None:
            title = row.title or row.yes_sub_title or ""
        out.append(
            {
                "side": b.side.value.upper(),
                "ticker": b.market_id,
                "reason": reasons.get(b.candidate_id) or b.trigger,
                "title": title,
            }
        )
    return out


def format_draft_lines(state: BrowserState) -> list[str]:
    legs = _draft_legs(state)
    lines = [f"READY {len(legs)}"]
    for leg in legs:
        label = leg["reason"] or leg["title"] or leg["ticker"]
        lines.append(f"  {leg['side']:<3} {label}  {leg['ticker']}")
    if not legs:
        lines.append("  (nothing to send)")
    lines.append("empty Enter sends")
    return lines


def format_order_lines(state: BrowserState) -> list[str]:
    items = orders_page_items(state)
    if not items:
        return ["no orders"]
    lines = [f"ORDERS {len(items)}"]
    for it in items:
        lines.append(
            f"  {it.get('side', 'YES'):<3} {it.get('qty', 0):>4}  "
            f"{it.get('ticker', '')}  {it.get('title', '')}  {it.get('source', '')}"
        )
    return lines


def _remaining_session(ticker: str, side: str) -> int:
    want = str(side or "yes").lower()
    total = 0
    for bet in SESSION_BETS.bets:
        if bet.ticker.upper() != str(ticker).upper():
            continue
        if str(bet.side or "yes").lower() != want:
            continue
        total += bet.remaining
    return total


def sell_rollback_legs(state: BrowserState, play: Any) -> list[str]:
    legs = list(getattr(play, "sent", None) or [])
    if not legs:
        return ["no positions from that play"]
    if not state.script_mode:
        tickers = [str(leg.get("ticker") or "") for leg in legs if leg.get("ticker")]
        if tickers:
            state.tracker.ensure_quotes(tickers, force=True)
    lines = [f"SELL {len(legs)}"]
    sold = 0
    live = bool(getattr(state.args, "live", False))
    for leg in legs:
        ticker = str(leg.get("ticker") or "")
        side = str(leg.get("side") or "yes").lower()
        qty = int(leg.get("quantity") or _play_quantity(state))
        rem = _remaining_session(ticker, side)
        if rem <= 0:
            lines.append(f"  skip {side.upper()} {ticker} (not holding)")
            continue
        qty = min(qty, rem)
        row = next((r for r in state.rows if r.ticker.upper() == ticker.upper()), None)
        title = (row.title if row is not None else "") or ""
        quote = state.tracker.quote(ticker) if ticker else None
        bid = None
        if quote is not None:
            bid = quote.yes_bid if side == "yes" else quote.no_bid
        if bid is None and not live:
            SESSION_BETS.mark_sold(ticker, qty, side=side)
            sold += 1
            lines.append(f"  DRY-RUN SELL {side.upper()} {ticker} x{qty} (no bid yet)")
            continue
        if side == "no":
            ok, msg = sell_no_for_position(
                args=state.args, ticker=ticker, title=title, count=qty, quote=quote
            )
        else:
            ok, msg = sell_yes_for_position(
                args=state.args, ticker=ticker, title=title, count=qty, quote=quote
            )
        lines.append(f"  {msg}")
        if ok:
            sold += 1
    lines.append(f"sold {sold}/{len(legs)}")
    return lines


def cancel_prompt_draft(state: BrowserState) -> None:
    if state.session is None or state.draft is None:
        return
    for armed_id in list(state.draft.armed_ids):
        try:
            state.session.arming.disarm(armed_id)
        except KeyError:
            pass
    state.draft = None
    state.filter_text = ""


def handle_prompt_line(state: BrowserState, line: str) -> list[str]:
    """One NCAAF prompt command. Empty line sends an armed draft."""
    raw = str(line or "").strip()
    low = raw.lower()
    if state.rollback_offer is not None:
        play = state.rollback_offer
        if low in {"y", "yes"}:
            state.rollback_offer = None
            return sell_rollback_legs(state, play)
        if low in {"n", "no", ""}:
            state.rollback_offer = None
            return ["kept those positions"]
        state.rollback_offer = None
    if low in {"q", "quit", "exit"}:
        return ["quit"]
    if low in {"h", "help", "?"}:
        return [PROMPT_HELP]
    if low in {"o", "orders"}:
        return format_order_lines(state)
    if low in {"rb", "rollback"}:
        if state.session is None:
            return ["nothing to rollback"]
        if state.draft is not None:
            cancel_prompt_draft(state)
        play = state.session.rollback_last_play()
        if play is None:
            return ["nothing to rollback"]
        lines = [f"rolled back {play.label}", format_game_stats(state)]
        if play.sent:
            lines.append(f"holding {len(play.sent)} legs from that play — sell them?")
            for leg in play.sent:
                side = str(leg.get("side") or "yes").upper()
                ticker = str(leg.get("ticker") or "")
                reason = str(leg.get("reason") or "")
                lines.append(f"  {side:<3} {reason}  {ticker}".rstrip())
            lines.append("y sell · n keep")
            state.rollback_offer = play
        return lines
    if low in {"x", "cancel"}:
        if state.draft is None:
            return ["nothing armed"]
        cancel_prompt_draft(state)
        return ["cancelled"]
    if not raw:
        if state.draft is None:
            return []
        confirm_td_draft(state)
        return [state.message or "sent"]
    if state.draft is not None:
        cancel_prompt_draft(state)
    state.filter_text = raw
    state.input_mode = "filter"
    handle_filter_enter(state)
    if state.draft is not None:
        return format_draft_lines(state)
    if state.message:
        return [state.message]
    return ["ok"]


def run_college_prompt(
    *,
    seed_series: str,
    game_code: str,
    rows: list[MarketRow],
    args: argparse.Namespace,
    ws_url: str,
    watch_limit: int,
    log_raw: bool,
    no_ws: bool,
) -> int:
    """Scrolling > prompt. No curses, no market list."""
    if not sys.stdin.isatty():
        eprint("error: --browse prompt needs stdin TTY")
        return 2
    tracker = BackgroundBookTracker(
        rows,
        ws_url=ws_url,
        watch_limit=0 if watch_limit < 0 else watch_limit,
        log_raw=log_raw,
        args=args,
    )
    if no_ws:
        tracker.status = "disabled"
    else:
        tracker.start()
    default_log = str(
        Path.home()
        / ".local"
        / "share"
        / "kalshi-multiplex-orderbook"
        / "sports-order-memory.log"
    )
    log_path = getattr(args, "order_log_file", None)
    if getattr(args, "no_order_log_file", False) or log_path == "":
        ORDER_LOG.configure(path=None, enabled=True)
    else:
        ORDER_LOG.configure(
            path=str(log_path) if log_path else default_log,
            idle_dump_s=float(getattr(args, "order_log_idle_s", 2.0) or 2.0),
            capacity=int(getattr(args, "order_log_capacity", 500) or 500),
            enabled=True,
        )
    state = BrowserState(
        seed_series=seed_series,
        game_code=game_code,
        rows=rows,
        tracker=tracker,
        args=args,
        session=make_browse_session(game_code, rows),
        mode="markets",
        category="all",
    )
    live = "LIVE" if getattr(args, "live", False) else "dry-run"
    print(f"{seed_series}-{game_code}  {len(rows)} markets  {live}")
    print("type a command (h help) · empty Enter sends")
    try:
        while True:
            print(format_game_stats(state))
            try:
                line = input("> ")
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                break
            for row in handle_prompt_line(state, line):
                if row == "quit":
                    return 0
                print(row)
    finally:
        tracker.stop()
        dumped = ORDER_LOG.maybe_dump_idle(force=True)
        if dumped:
            eprint(f"order log dumped: {dumped}")
    return 0


def run_browser(
    *,
    seed_series: str,
    game_code: str,
    rows: list[MarketRow],
    args: argparse.Namespace,
    ws_url: str,
    watch_limit: int,
    log_raw: bool,
    no_ws: bool,
) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        eprint("error: --browse requires an interactive TTY")
        return 2

    tracker = BackgroundBookTracker(
        rows,
        ws_url=ws_url,
        watch_limit=0 if watch_limit < 0 else watch_limit,
        log_raw=log_raw,
        args=args,
    )
    if no_ws:
        tracker.status = "disabled"
    else:
        # Start WS silently in background; UI does not dump ticks.
        eprint("starting background websocket book tracker (silent)...")
        tracker.start()
        if tracker.error and tracker.status in {"no-auth", "no-deps"}:
            eprint(f"browser continues without live quotes: {tracker.error}")
        elif tracker.enabled:
            eprint(
                f"ws tracker: status={tracker.status} subscribed={tracker.subscribed_n} "
                f"auth={tracker.auth_label or '-'}"
            )

    # Order log stays in memory during submit; dump only when idle / exit.
    default_log = str(
        Path.home()
        / ".local"
        / "share"
        / "kalshi-multiplex-orderbook"
        / "sports-order-memory.log"
    )
    log_path = getattr(args, "order_log_file", None)
    if getattr(args, "no_order_log_file", False) or log_path == "":
        ORDER_LOG.configure(path=None, enabled=True)  # memory only; no disk
        log_path_disp = "(memory-only; no disk dump)"
    else:
        ORDER_LOG.configure(
            path=str(log_path) if log_path else default_log,
            idle_dump_s=float(getattr(args, "order_log_idle_s", 2.0) or 2.0),
            capacity=int(getattr(args, "order_log_capacity", 500) or 500),
            enabled=True,
        )
        log_path_disp = ORDER_LOG.path or default_log
    eprint(f"order memory log: {log_path_disp} (no write during submit)")

    state = BrowserState(
        seed_series=seed_series,
        game_code=game_code,
        rows=rows,
        tracker=tracker,
        args=args,
        session=make_browse_session(game_code, rows),
    )

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    stop = False

    def _sig(_signum, _frame):  # noqa: ANN001
        nonlocal stop
        stop = True

    prev_int = signal.signal(signal.SIGINT, _sig)
    prev_term = signal.signal(signal.SIGTERM, _sig)

    try:
        tty.setcbreak(fd)
        # Disable OS XON/XOFF so Ctrl-S etc. are available later; leave ISIG for Ctrl-C path via handler if needed
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~termios.IXON
        termios.tcsetattr(fd, termios.TCSADRAIN, attrs)

        last_draw = 0.0
        while not stop:
            now = time.time()
            # periodic redraw so quotes fill in without keypresses
            render_browser(state)
            last_draw = now
            dumped = ORDER_LOG.maybe_dump_idle()
            if dumped and not state.message:
                state.message = f"order log dumped → {short_label(dumped, 48)}"

            readable, _, _ = select.select([fd], [], [], 0.5)
            if not readable:
                ORDER_LOG.maybe_dump_idle()
                continue
            kind, value = read_terminal_key(fd)

            if kind == "ctrl" and value == "c":
                stop = True
                break

            # Quit confirm: q arms it; Enter confirms; anything else cancels.
            if state.quit_confirm:
                if kind == "enter":
                    stop = True
                    break
                cancel_quit_confirm(state)
                # Swallow the cancel key so it does not also act.
                continue

            # Sell confirm on orders page: Enter submits; anything else cancels.
            if state.sell_confirm and state.mode == "orders":
                if kind == "enter":
                    handle_sell_all(state)
                    continue
                cancel_sell_confirm(state)
                continue

            if kind == "ctrl" and value == "h":
                # Help is always available; leave filter input first.
                if state.input_mode == "filter":
                    leave_filter_mode(state)
                state.prev_mode = state.mode if state.mode != "help" else state.prev_mode
                state.mode = "help"
                continue
            if kind == "ctrl" and value in {"l", "r"}:
                state.message = "redraw"
                continue

            # ---- HELP screen ----
            if state.mode == "help":
                if kind in {"escape", "backspace"} or (kind == "char" and value in {"q", "Q"}):
                    state.mode = state.prev_mode
                    state.input_mode = "normal"
                continue

            # ---- global clear filter ----
            if kind == "ctrl" and value == "u":
                clear_filter(state)
                continue

            if state.input_mode == "time":
                if kind == "escape":
                    state.input_mode = "normal"
                    continue
                if kind == "enter":
                    handle_time_enter(state)
                    continue
                if kind == "backspace":
                    state.time_text = state.time_text[:-1]
                    continue
                if kind == "ctrl" and value == "u":
                    state.time_text = ""
                    continue
                if kind == "char" and value.isprintable() and value != "\t":
                    state.time_text += value
                    continue
                continue

            # ---- FILTER input mode: all printable chars type into the query ----
            if state.input_mode == "filter":
                if kind == "escape":
                    leave_filter_mode(state)
                    continue
                if kind == "enter":
                    handle_filter_enter(state)
                    continue
                if kind == "char" and value == "\t":
                    handle_filter_tab(state)
                    continue
                if kind == "backspace":
                    if state.filter_text:
                        state.filter_text = state.filter_text[:-1]
                        state.cursor = 0
                        state.offset = 0
                        state.message = ""
                    else:
                        leave_filter_mode(state)
                    continue
                # Arrows still navigate the filtered list while typing.
                n_items = (
                    len(state.category_items())
                    if state.mode == "categories"
                    else len(state.filtered_rows())
                    if state.mode == "markets"
                    else 0
                )
                if kind == "up" or (kind == "ctrl" and value == "p"):
                    move_cursor(state, -1, n_items)
                    continue
                if kind == "down" or (kind == "ctrl" and value == "n"):
                    move_cursor(state, 1, n_items)
                    continue
                if kind == "pageup":
                    move_cursor(state, -state.page_size, n_items)
                    continue
                if kind == "pagedown":
                    move_cursor(state, state.page_size, n_items)
                    continue
                if kind == "home":
                    state.cursor = 0
                    clamp_cursor(state, n_items)
                    continue
                if kind == "end":
                    state.cursor = max(0, n_items - 1)
                    clamp_cursor(state, n_items)
                    continue
                if kind == "char" and value.isprintable():
                    # Accept letters, digits, space, punctuation — including q/k/j.
                    append_filter_char(state, value)
                    continue
                continue

            # ---- NORMAL mode from here ----
            if kind == "backspace":
                if handle_back(state):
                    stop = True
                continue
            if kind == "escape":
                if handle_back(state):
                    stop = True
                continue
            if kind == "enter":
                handle_enter(state)
                continue

            n_items = (
                len(state.category_items())
                if state.mode == "categories"
                else len(state.filtered_rows())
                if state.mode == "markets"
                else len(orders_page_items(state))
                if state.mode == "orders"
                else 0
            )

            if kind == "up" or (kind == "char" and value in {"k", "K"}) or (kind == "ctrl" and value == "p"):
                move_cursor(state, -1, n_items)
                continue
            if kind == "down" or (kind == "char" and value in {"j", "J"}) or (kind == "ctrl" and value == "n"):
                move_cursor(state, 1, n_items)
                continue
            if kind == "pageup":
                move_cursor(state, -state.page_size, n_items)
                continue
            if kind == "pagedown":
                move_cursor(state, state.page_size, n_items)
                continue
            if kind == "home" or (kind == "char" and value == "g" and state.mode != "categories"):
                state.cursor = 0
                clamp_cursor(state, n_items)
                continue
            if kind == "end" or (kind == "char" and value == "G"):
                state.cursor = max(0, n_items - 1)
                clamp_cursor(state, n_items)
                continue

            if kind == "char" and value in {"q", "Q"}:
                request_quit_confirm(state)
                continue

            if kind == "char" and value in {"f", "F", "/"}:
                # f or / enters FILTER mode. From categories, open All so the query applies.
                enter_filter_mode(state, jump_all=(state.mode == "categories"))
                continue

            if kind == "char" and value in {"l", "L"} and state.mode != "help":
                recent = ORDER_LOG.recent(3)
                if not recent:
                    state.message = "order memory log empty"
                else:
                    state.message = " | ".join(short_label(ev.summary, 42) for ev in recent)
                    dumped = ORDER_LOG.maybe_dump_idle(force=True)
                    if dumped:
                        state.message += f" · dumped {short_label(dumped, 36)}"
                continue

            if kind == "char" and value in {"o", "O"} and state.mode != "help":
                open_orders_page(state)
                continue

            if state.mode == "orders" and kind == "char" and value in {"r", "R"}:
                state.message = refresh_exchange_positions(state)
                continue

            if state.mode == "markets" and kind == "char" and value in {"d", "D", " "}:
                rows = state.filtered_rows()
                if rows:
                    clamp_cursor(state, len(rows))
                    open_market_detail(state, rows[state.cursor].ticker)
                continue

            if state.mode == "categories" and kind == "char":
                if value in {"1", "2", "3", "4", "5"}:
                    idx = int(value) - 1
                    if 0 <= idx < len(CATEGORY_ORDER):
                        open_category(state, CATEGORY_ORDER[idx])
                        state.input_mode = "normal"
                    continue
                if value in {"a", "A"}:
                    open_category(state, "all")
                    state.input_mode = "normal"
                    continue
                continue

            if state.mode == "detail" and kind == "char" and value == "g":
                state.mode = "categories"
                state.input_mode = "normal"
                state.cursor = 0
                state.offset = 0
                continue

    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        tracker.stop()
        dumped = ORDER_LOG.maybe_dump_idle(force=True)
        # leave a clean line after raw mode
        sys.stdout.write("\n")
        sys.stdout.flush()
        if dumped:
            eprint(f"order memory log dumped: {dumped}")

    eprint("browser exit")
    return 0




def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Discover Kalshi sports markets for a game id; keyboard browser "
            "and/or WebSocket orderbook tracking (no REST book polling). "
            "Requires --demo or --prod. Default is dry-run; --live submits real "
            "BUY YES orders from the browser (Enter on a market)."
        )
    )

    env = p.add_mutually_exclusive_group(required=True)
    env.add_argument(
        "--demo",
        dest="kalshi_env",
        action="store_const",
        const="demo",
        help="Use Kalshi demo REST/WS endpoints. Required choice: --demo or --prod.",
    )
    env.add_argument(
        "--prod",
        dest="kalshi_env",
        action="store_const",
        const="prod",
        help="Use Kalshi production REST/WS endpoints. Required choice: --demo or --prod.",
    )

    p.add_argument(
        "--demo-host-style",
        choices=["external", "direct"],
        default="external",
        help=(
            "Demo endpoint pair. external = external-api.demo / external-api-ws.demo; "
            "direct = demo-api REST + demo-api WS. Default: external"
        ),
    )
    p.add_argument(
        "--live",
        action="store_true",
        help=(
            "Submit real orders. Default is dry-run (logs BUY YES payload only). "
            "Prefer --demo --live for first tests."
        ),
    )
    p.add_argument(
        "--count",
        type=int,
        default=1,
        help="Fallback contracts per BUY YES when --count-yes is omitted. Default: 1",
    )
    p.add_argument(
        "--count-yes",
        type=int,
        default=None,
        help=(
            "Contracts to BUY YES when pressing Enter on a market/detail. "
            "Default: --count (1)."
        ),
    )
    p.add_argument(
        "--slippage-cents",
        "--slippage",
        type=int,
        default=1,
        help="BUY YES limit = YES ask + this many cents (clamped 1..99). Default: 1",
    )
    p.add_argument(
        "--time-in-force",
        default="immediate_or_cancel",
        choices=["immediate_or_cancel", "good_till_canceled", "fill_or_kill"],
        help="Limit order time-in-force for Enter BUY YES. Default: immediate_or_cancel",
    )
    p.add_argument(
        "--order-submit-timeout",
        type=float,
        default=10.0,
        help="HTTP timeout seconds for live order submit. Default: 10",
    )
    p.add_argument(
        "--order-log-file",
        default=None,
        help=(
            "Where to dump the in-memory order log when idle/exit. "
            "Default: ~/.local/share/kalshi-multiplex-orderbook/sports-order-memory.log. "
            "Use --no-order-log-file for memory-only."
        ),
    )
    p.add_argument(
        "--order-log-idle-s",
        type=float,
        default=2.0,
        help="Seconds after last order event before dumping memory log to disk. Default: 2",
    )
    p.add_argument(
        "--order-log-capacity",
        type=int,
        default=500,
        help="Max in-memory order events retained. Default: 500",
    )
    p.add_argument(
        "--no-order-log-file",
        action="store_true",
        help="Keep order log in memory only; never dump to disk.",
    )

    p.add_argument(
        "game",
        help="Game/event id or URL tail, e.g. kxnflgame-26sep13atlpit",
    )
    p.add_argument(
        "--host",
        default=None,
        help="Override REST catalog host selected by --demo/--prod.",
    )
    p.add_argument(
        "--api-host",
        default=None,
        help="Alias for --host (broadcast-trader naming). Overrides --demo/--prod REST host.",
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
        help="Only probe these series tickers (repeatable). Default: every game-scoped series.",
    )
    p.add_argument(
        "--scan-all-series",
        action="store_true",
        help=(
            "Probe every game-scoped league series from GET /series (now the default)."
        ),
    )
    p.add_argument(
        "--priority-only",
        action="store_true",
        help="Probe only the curated series list (old default). Skips GET /series.",
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
        "--browse",
        action="store_true",
        help=(
            "After discovery, open the operator UI. NCAAF is a scrolling > prompt "
            "(no market list). NFL is the keyboard browser. Silent WS books when auth is available."
        ),
    )
    p.add_argument(
        "--engine-demo",
        action="store_true",
        help=(
            "Run the sports_engine architecture slice (event → state → candidate "
            "→ arm → mock execution). Skips catalog/browse. Never sends Kalshi orders."
        ),
    )
    p.add_argument(
        "--cache-market",
        action="store_true",
        help=(
            "Write discovered markets to disk (default: "
            "~/.local/share/kalshi-multiplex-orderbook/market-cache/{env}-{game}.json)."
        ),
    )
    p.add_argument(
        "--use-cache",
        action="store_true",
        help="Skip REST discovery; load --cache-market file instead.",
    )
    p.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete the cache file, then discover again unless --use-cache finds nothing.",
    )
    p.add_argument(
        "--cache-file",
        default=None,
        help="Override market-cache JSON path.",
    )
    p.add_argument(
        "--script",
        default=None,
        help=(
            "Headless operator script using the same FILTER/Enter handlers, e.g. "
            "'/ wa Enter td Enter re Enter'. Dumps JSON bets. Implies no TTY. "
            "Does not submit Kalshi orders."
        ),
    )
    p.add_argument(
        "--script-file",
        default=None,
        help="Read --script text from a file.",
    )
    p.add_argument(
        "--no-ws",
        action="store_true",
        help="With --browse, do not start the background WebSocket tracker.",
    )
    p.add_argument(
        "--list-only",
        action="store_true",
        help="Print discovery table only (default when not using --browse/--watch).",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help=(
            "After discovery, stream orderbooks over one authenticated Kalshi "
            "WebSocket (kx_orderbooks multiplex). Requires env-matching API keys. "
            "Prints ticks (use --browse for silent tracking + UI)."
        ),
    )
    p.add_argument(
        "--watch-limit",
        type=int,
        default=80,
        help=(
            "Initial market seed size for WebSocket books (default 80). "
            "--watch uses this as a hard subscribe cap (0 = all discovered). "
            "--browse starts with this seed then auto-subscribes the visible "
            "viewport so later series (e.g. player props) still get quotes."
        ),
    )
    p.add_argument(
        "--ws-url",
        default=None,
        help="Override the WebSocket URL selected by --demo/--prod.",
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
    p.add_argument(
        "--api-key-id-env",
        default=None,
        help=(
            "Env var holding API key id. Defaults: "
            f"demo={DEMO_API_KEY_ID_ENV}, prod={PROD_API_KEY_ID_ENV} "
            f"(legacy prod {LEGACY_PROD_API_KEY_ID_ENV})."
        ),
    )
    p.add_argument(
        "--private-key-file-env",
        default=None,
        help=(
            "Env var holding private-key PEM path. Defaults: "
            f"demo={DEMO_PRIVATE_KEY_FILE_ENV}, prod={PROD_PRIVATE_KEY_FILE_ENV} "
            f"(legacy prod {LEGACY_PROD_PRIVATE_KEY_FILE_ENV})."
        ),
    )
    p.add_argument(
        "--private-key-file",
        default=None,
        help="Private key PEM path (overrides env-var file path).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    apply_env_endpoints(args)
    if getattr(args, "no_order_log_file", False):
        args.order_log_file = ""

    if args.quiet:
        def _quiet_eprint(*_a: Any, **_k: Any) -> None:
            return None

        globals()["eprint"] = _quiet_eprint

    mode = "LIVE" if args.live else "DRY-RUN"
    if args.count is not None and args.count <= 0:
        eprint("error: --count must be positive")
        return 2
    if args.count_yes is not None and args.count_yes <= 0:
        eprint("error: --count-yes must be positive")
        return 2
    if args.slippage_cents < 0:
        eprint("error: --slippage-cents must be >= 0")
        return 2
    if args.live:
        eprint(
            f"LIVE mode: Enter on a market will BUY YES x{effective_count_yes(args)} "
            f"(ask+{args.slippage_cents}c IOC). Prefer --demo for first tests."
        )
    else:
        eprint(
            f"DRY-RUN mode: Enter logs BUY YES x{effective_count_yes(args)} payload only "
            "(add --live to submit)."
        )

    try:
        seed_series, game_code = parse_game_input(args.game)
    except ValueError as exc:
        eprint(f"error: {exc}")
        return 2

    if bool(getattr(args, "engine_demo", False)):
        from sports_engine.demo import run_engine_demo

        eprint("--engine-demo: mock execution only (Kalshi orders are not sent)")
        return run_engine_demo(args, game_code=game_code, seed_series=seed_series)

    statuses = normalize_status_filter(args.status)
    league = league_prefix_from_series(seed_series)
    cache_path = Path(args.cache_file) if getattr(args, "cache_file", None) else default_cache_path(
        game_code, str(args.kalshi_env)
    )
    if bool(getattr(args, "clear_cache", False)):
        if clear_market_cache(cache_path):
            eprint(f"cleared cache {cache_path}")
        else:
            eprint(f"no cache file {cache_path}")

    t0 = time.time()
    eprint(
        f"env={args.kalshi_env} mode={mode} rest={args.api_host} ws={args.ws_url}"
    )
    eprint(f"game_code={game_code} seed_series={seed_series} league_prefix={league}")
    eprint(f"status_filter={'all' if not statuses else ','.join(sorted(statuses))}")
    scan_all = True
    if args.series or bool(getattr(args, "priority_only", False)):
        scan_all = False
    if bool(args.scan_all_series):
        scan_all = True
    eprint(
        f"discovery={args.discovery} scan_all_series={scan_all} "
        f"priority_only={bool(getattr(args, 'priority_only', False))}"
    )

    rows: list[MarketRow] | None = None
    hits: list[Any] = []
    errors: list[Any] = []
    loaded_cache = False
    use_cache = bool(getattr(args, "use_cache", False))
    if use_cache and cache_path.is_file():
        data = load_market_cache(cache_path)
        rows = []
        for rec in data.get("markets") or []:
            if not isinstance(rec, dict):
                continue
            row = market_row_from_raw(rec)
            if row is not None:
                rows.append(row)
        hits = sorted({r.series_ticker for r in rows})
        loaded_cache = True
        eprint(f"loaded {len(rows)} markets from cache {cache_path}")
    elif use_cache:
        eprint(f"error: --use-cache but no file at {cache_path} (run --cache-market first)")
        return 2

    if rows is None:
        candidates = build_candidate_series(
            args.host,
            league_prefix=league,
            seed_series=seed_series,
            explicit_series=args.series or None,
            include_season_long=bool(args.include_season_long),
            scan_all_series=scan_all,
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
        if bool(getattr(args, "cache_market", False)) and rows:
            save_market_cache(
                cache_path,
                rows=rows,
                game_code=game_code,
                seed_series=seed_series,
                kalshi_env=str(args.kalshi_env),
            )
            eprint(f"cached {len(rows)} markets → {cache_path}")
    elapsed = time.time() - t0

    if not rows:
        eprint("no markets found")
        # still print empty discovery for list mode
        if args.json and not args.browse and not args.watch:
            print(json.dumps({
                "seed_series": seed_series,
                "game_code": game_code,
                "league_prefix": league,
                "kalshi_env": args.kalshi_env,
                "mode": mode.lower().replace("-", "_"),
                "api_host": args.api_host,
                "ws_url": args.ws_url,
                "market_count": 0,
                "series_hits": hits,
                "errors": errors,
                "elapsed_seconds": round(elapsed, 3),
                "markets": [],
            }, indent=2, sort_keys=True))
        elif not args.browse:
            print_discovery_text(
                seed_series=seed_series,
                game_code=game_code,
                rows=rows,
                hits=hits,
                errors=errors,
                elapsed=elapsed,
            )
        return 1

    script_text = getattr(args, "script", None)
    script_file = getattr(args, "script_file", None)
    if script_file:
        script_text = Path(script_file).expanduser().read_text(encoding="utf-8")
    if script_text:
        state = make_headless_state(
            seed_series=seed_series,
            game_code=game_code,
            rows=rows,
            args=args,
        )
        report = run_key_script(state, script_text)
        print(json.dumps(report, indent=2))
        return 0

    if (
        bool(getattr(args, "cache_market", False))
        and not args.browse
        and not args.watch
        and not args.json
    ):
        print(f"{len(rows)} markets → {cache_path}")
        return 0

    if args.browse:
        college = str(seed_series or "").upper().startswith("KXNCAAF") or is_college_rows(rows)
        if college:
            eprint(
                f"discovered {len(rows)} markets across {len(hits)} series "
                f"in {elapsed:.1f}s; NCAAF prompt ({args.kalshi_env}/{mode})"
            )
            return run_college_prompt(
                seed_series=seed_series,
                game_code=game_code,
                rows=rows,
                args=args,
                ws_url=str(args.ws_url),
                watch_limit=int(args.watch_limit),
                log_raw=bool(args.log_raw),
                no_ws=bool(args.no_ws),
            )
        eprint(
            f"discovered {len(rows)} markets across {len(hits)} series "
            f"in {elapsed:.1f}s; opening browser ({args.kalshi_env}/{mode})"
        )
        return run_browser(
            seed_series=seed_series,
            game_code=game_code,
            rows=rows,
            args=args,
            ws_url=str(args.ws_url),
            watch_limit=int(args.watch_limit),
            log_raw=bool(args.log_raw),
            no_ws=bool(args.no_ws),
        )

    if args.json and not args.watch:
        payload = {
            "seed_series": seed_series,
            "game_code": game_code,
            "league_prefix": league,
            "kalshi_env": args.kalshi_env,
            "mode": mode.lower().replace("-", "_"),
            "api_host": args.api_host,
            "ws_url": args.ws_url,
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

    if args.watch:
        return run_watch(
            rows,
            args=args,
            ws_url=str(args.ws_url),
            watch_limit=int(args.watch_limit),
            print_every=float(args.print_every),
            log_raw=bool(args.log_raw),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
