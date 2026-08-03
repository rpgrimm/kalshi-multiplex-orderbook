"""
Series ticker -> market ticker discovery.

The default path uses kalshi_python_sync because you already have that working.
There is also a small REST fallback for cases where you want fewer dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Dict, Iterable, List, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_REST_HOST = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass(frozen=True)
class MarketInfo:
    ticker: str
    title: str = ""
    event_ticker: str = ""
    status: str = ""
    close_time: str = ""
    raw: Dict[str, Any] | None = None


def parse_statuses(status: str | Sequence[str] | None) -> set[str]:
    """
    Convert 'active,open' or ['active', 'open'] into {'active', 'open'}.

    Pass None, '', or 'all' to accept all statuses.
    """
    if status is None:
        return set()

    if isinstance(status, str):
        if status.strip().lower() in ("", "all", "*"):
            return set()
        parts = [p.strip().lower() for p in status.split(",")]
    else:
        parts = [str(p).strip().lower() for p in status]

    return {p for p in parts if p}


def _market_to_info(market: Any) -> MarketInfo:
    """
    Convert either an SDK model or a plain dict into MarketInfo.
    """
    if hasattr(market, "model_dump"):
        raw = market.model_dump()
    elif isinstance(market, dict):
        raw = dict(market)
    else:
        raw = {
            "ticker": getattr(market, "ticker", ""),
            "title": getattr(market, "title", ""),
            "event_ticker": getattr(market, "event_ticker", ""),
            "status": getattr(market, "status", ""),
            "close_time": str(getattr(market, "close_time", "") or ""),
        }

    return MarketInfo(
        ticker=str(raw.get("ticker", "") or "").upper(),
        title=str(raw.get("title", "") or ""),
        event_ticker=str(raw.get("event_ticker", "") or ""),
        status=str(raw.get("status", "") or ""),
        close_time=str(raw.get("close_time", "") or ""),
        raw=raw,
    )


def get_markets_for_series_sdk(
    series_ticker: str,
    *,
    statuses: str | Sequence[str] | None = "active,open",
    rest_host: str = DEFAULT_REST_HOST,
    api_key_id: str | None = None,
    private_key_file: str | None = None,
) -> list[MarketInfo]:
    """
    Discover markets in a series using kalshi_python_sync.

    This mirrors your get_markets(series_ticker=...) prototype.
    """
    try:
        from kalshi_python_sync import Configuration, KalshiClient
    except ImportError as exc:
        raise RuntimeError(
            "kalshi_python_sync is not installed. Install it or use discovery='rest'."
        ) from exc

    api_key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID")
    private_key_file = private_key_file or os.environ.get("KALSHI_PRIVATE_KEY_FILE")

    if not api_key_id:
        raise RuntimeError("Missing KALSHI_API_KEY_ID")
    if not private_key_file:
        raise RuntimeError("Missing KALSHI_PRIVATE_KEY_FILE")

    with open(os.path.expanduser(private_key_file), "r", encoding="utf-8") as f:
        private_key_pem = f.read()

    config = Configuration(host=rest_host)
    config.api_key_id = api_key_id
    config.private_key_pem = private_key_pem
    client = KalshiClient(config)

    resp = client.get_markets(series_ticker=series_ticker.upper())
    wanted = parse_statuses(statuses)

    out: list[MarketInfo] = []
    for market in getattr(resp, "markets", []):
        info = _market_to_info(market)
        if not info.ticker:
            continue
        if wanted and info.status.lower() not in wanted:
            continue
        out.append(info)

    return out


def _get_json(url: str, timeout: float = 10.0) -> Dict[str, Any]:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kx-orderbook-prototype/0.1",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_markets_for_series_rest(
    series_ticker: str,
    *,
    statuses: str | Sequence[str] | None = "open",
    rest_host: str = DEFAULT_REST_HOST,
    limit: int = 200,
    max_pages: int = 20,
    timeout: float = 10.0,
) -> list[MarketInfo]:
    """
    Discover markets in a series using REST.

    Note: some hosts/status names have changed over time. The SDK path is
    usually safer if your installed SDK is already working.
    """
    status_set = parse_statuses(statuses)

    # The REST API accepts one status at a time. If user asked active,open,
    # query each separately and de-duplicate.
    status_queries = list(status_set) if status_set else [None]

    seen: set[str] = set()
    out: list[MarketInfo] = []

    for status in status_queries:
        cursor = None
        for _ in range(max_pages):
            params = {
                "series_ticker": series_ticker.upper(),
                "limit": str(limit),
            }
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor

            url = f"{rest_host.rstrip('/')}/markets?{urlencode(params)}"
            data = _get_json(url, timeout=timeout)

            for raw_market in data.get("markets", []):
                info = _market_to_info(raw_market)
                if not info.ticker or info.ticker in seen:
                    continue
                seen.add(info.ticker)
                out.append(info)

            cursor = data.get("cursor")
            if not cursor:
                break

    return out


def get_markets_for_series(
    series_ticker: str,
    *,
    discovery: str = "sdk",
    statuses: str | Sequence[str] | None = "active,open",
    rest_host: str = DEFAULT_REST_HOST,
) -> list[MarketInfo]:
    """
    Discover markets for a Kalshi series ticker.

    discovery:
        'sdk'  - use kalshi_python_sync
        'rest' - use urllib REST calls
    """
    discovery = discovery.lower().strip()
    if discovery == "sdk":
        return get_markets_for_series_sdk(
            series_ticker,
            statuses=statuses,
            rest_host=rest_host,
        )
    if discovery == "rest":
        return get_markets_for_series_rest(
            series_ticker,
            statuses=statuses,
            rest_host=rest_host,
        )
    raise ValueError(f"Unknown discovery mode: {discovery!r}")


def market_tickers(markets: Iterable[MarketInfo]) -> list[str]:
    """Convenience: extract ticker strings from MarketInfo objects."""
    return [m.ticker for m in markets]
