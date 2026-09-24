"""On-disk catalog cache so browse/scripts can skip live REST discovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

CACHE_ROOT = Path.home() / ".local" / "share" / "kalshi-multiplex-orderbook" / "market-cache"


def default_cache_path(game_code: str, kalshi_env: str = "prod") -> Path:
    env = str(kalshi_env or "prod").lower()
    code = str(game_code or "game").upper()
    return CACHE_ROOT / f"{env}-{code}.json"


def row_to_record(row: Any) -> dict[str, Any]:
    raw = getattr(row, "raw", None)
    if isinstance(raw, dict) and raw.get("ticker"):
        return raw
    return {
        "ticker": str(getattr(row, "ticker", "") or ""),
        "title": str(getattr(row, "title", "") or ""),
        "event_ticker": str(getattr(row, "event_ticker", "") or ""),
        "series_ticker": str(getattr(row, "series_ticker", "") or ""),
        "status": str(getattr(row, "status", "") or ""),
        "yes_sub_title": str(getattr(row, "yes_sub_title", "") or ""),
        "no_sub_title": str(getattr(row, "no_sub_title", "") or ""),
        "custom": (raw or {}).get("custom") if isinstance(raw, dict) else None,
        "floor_strike": (raw or {}).get("floor_strike") if isinstance(raw, dict) else None,
    }


def save_market_cache(
    path: str | Path,
    *,
    rows: Sequence[Any],
    game_code: str,
    seed_series: str,
    kalshi_env: str,
) -> Path:
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kalshi_env": kalshi_env,
        "game_code": game_code,
        "seed_series": seed_series,
        "market_count": len(rows),
        "markets": [row_to_record(r) for r in rows],
    }
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def load_market_cache(path: str | Path) -> dict[str, Any]:
    src = Path(path).expanduser()
    data = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "markets" not in data:
        raise ValueError(f"not a market cache: {src}")
    return data


def clear_market_cache(path: str | Path) -> bool:
    src = Path(path).expanduser()
    if not src.is_file():
        return False
    src.unlink()
    return True
