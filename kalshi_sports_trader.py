#!/usr/bin/env python3
"""kalshi_sports_trader.py — sports game market tools (slice 1: list related markets).

Take a game/event id such as:

    kxnflgame-26sep13atlpit

and print markets associated with that game across related Kalshi series.

Sports props for one game are split across many series that share the same
game code (e.g. KXNFLSPREAD-26SEP13ATLPIT, KXNFLREC-26SEP13ATLPIT). Querying
only the KXNFLGAME event ticker is not enough.

Examples:
    ./kalshi_sports_trader.py kxnflgame-26sep13atlpit
    ./kalshi_sports_trader.py KXNFLGAME-26SEP13ATLPIT --status all
    ./kalshi_sports_trader.py https://kalshi.com/markets/kxnflgame/.../kxnflgame-26sep13atlpit

No orders in this slice. Public REST discovery only.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_REST_HOST = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "kalshi-sports-trader/0.1"

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
)

NFL_PRIORITY_SERIES = [
    "KXNFLGAME",
    "KXNFLSPREAD",
    "KXNFLTOTAL",
    "KXNFLTEAMTOTAL",
    "KXNFL1H",
    "KXNFL1HSPREAD",
    "KXNFL1HTOTAL",
    "KXNFL1HTEAMTOTAL",
    "KXNFL2H",
    "KXNFL2HSPREAD",
    "KXNFL2HTOTAL",
    "KXNFLANYTD",
    "KXNFLFIRSTTD",
    "KXNFLFIRSTTDTEAM",
    "KXNFLPASSYDS",
    "KXNFLPASSTDS",
    "KXNFLPASSATT",
    "KXNFLPASSCOMP",
    "KXNFLRSHYDS",
    "KXNFLRSHATT",
    "KXNFLREC",
    "KXNFLRECYDS",
    "KXNFLRRYDS",
    "KXNFLMOSTRECYDS",
    "KXNFLMOSTRSHYDS",
    "KXNFLTOTALTD",
    "KXNFLGAMESPECIALS",
    "KXNFLGAMETD",
    "KXNFLGAMEFG",
    "KXNFLGAMESACK",
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

    if "://" in text or text.count("/") >= 1 and " " not in text:
        path = urllib.parse.urlparse(text).path if "://" in text else text
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError(f"could not parse game id from URL path: {raw!r}")
        text = parts[-1]

    text = urllib.parse.unquote(text)
    text = re.sub(r"\s+", "", text).upper().strip().strip("-")
    pieces = [p for p in text.split("-") if p]
    # Drop trailing market leg if pasted full market ticker, e.g. ...-ATL
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


def fetch_markets_for_event(
    host: str,
    event_ticker: str,
    *,
    statuses: set[str],
    page_limit: int = 200,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
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
) -> list[str]:
    if explicit_series:
        return sorted({s.upper() for s in explicit_series if s.strip()})

    found = list_series_tickers(host, league_prefix)
    cands: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = s.upper()
        if s in seen:
            return
        seen.add(s)
        cands.append(s)

    add(seed_series)
    if league_prefix == "KXNFL":
        for s in NFL_PRIORITY_SERIES:
            add(s)
    for s in found:
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
    pause_s: float = 0.05,
) -> tuple[list[MarketRow], dict[str, int], list[str]]:
    by_ticker: dict[str, MarketRow] = {}
    hits: dict[str, int] = {}
    errors: list[str] = []

    for series in candidate_series:
        event_ticker = f"{series}-{game_code}".upper()
        try:
            raw_markets = fetch_markets_for_event(host, event_ticker, statuses=statuses)
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


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="List Kalshi sports markets associated with a game id (slice 1)."
    )
    p.add_argument(
        "game",
        help="Game/event id or URL tail, e.g. kxnflgame-26sep13atlpit",
    )
    p.add_argument(
        "--host",
        default=DEFAULT_REST_HOST,
        help=f"REST host (default {DEFAULT_REST_HOST})",
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
        help="Only probe these series tickers (repeatable). Default: auto league set.",
    )
    p.add_argument(
        "--include-season-long",
        action="store_true",
        help="Do not filter season-long series from auto candidates.",
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
        "--json",
        action="store_true",
        help="Emit JSON instead of text table.",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress stderr progress.",
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

    candidates = build_candidate_series(
        args.host,
        league_prefix=league,
        seed_series=seed_series,
        explicit_series=args.series or None,
        include_season_long=bool(args.include_season_long),
    )
    if args.max_series and args.max_series > 0:
        candidates = candidates[: args.max_series]
    eprint(f"probing {len(candidates)} series")

    rows, hits, errors = discover_game_markets(
        args.host,
        game_code=game_code,
        statuses=statuses,
        candidate_series=candidates,
        pause_s=max(0.0, float(args.pause)),
    )
    elapsed = time.time() - t0

    if args.json:
        payload = {
            "seed_series": seed_series,
            "game_code": game_code,
            "league_prefix": league,
            "market_count": len(rows),
            "series_hits": hits,
            "errors": errors,
            "elapsed_seconds": round(elapsed, 3),
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

    if not rows:
        eprint("no markets found")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
