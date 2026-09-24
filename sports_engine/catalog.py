"""Map GameState / play tokens onto discovered Kalshi rows. No orders."""

from __future__ import annotations

import re
from typing import Any, Sequence

_GAME_DATE_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{2})([A-Z]+)$")
NFL_TEAM_ABBREVS = frozenset(
    {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
        "DET", "GB", "HOU", "IND", "JAC", "JAX", "KC", "LA", "LAC", "LAR", "LV",
        "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
        "TEN", "WAS", "WSH",
    }
)
Q_TOTAL_SERIES = {
    1: "KXNFL1QTOTAL",
    2: "KXNFL2QTOTAL",
    3: "KXNFL3QTOTAL",
    4: "KXNFL4QTOTAL",
}
SKIP_TITLE = ("D/ST", "No Touchdown")


def ticker_parts(ticker: str) -> tuple[str, str, str]:
    parts = [p for p in str(ticker or "").upper().split("-") if p]
    if len(parts) < 2:
        return str(ticker or "").upper(), "", ""
    return parts[0], parts[1], "-".join(parts[2:])


def teams_from_game_code(game_code: str) -> tuple[str, str]:
    text = str(game_code or "").upper().strip()
    matched = _GAME_DATE_RE.match(text)
    blob = matched.group(2) if matched else text
    found: list[tuple[str, str]] = []
    for a_len in (2, 3):
        b_len = len(blob) - a_len
        if b_len not in (2, 3):
            continue
        a, b = blob[:a_len], blob[a_len:]
        if a in NFL_TEAM_ABBREVS and b in NFL_TEAM_ABBREVS:
            found.append((a, b))
    if found:
        found.sort(key=lambda pair: -(len(pair[0]) + len(pair[1])))
        return found[0]
    return "AWAY", "HOME"


def row_series(row: Any) -> str:
    series = str(getattr(row, "series_ticker", "") or "").upper()
    if series:
        return series
    return ticker_parts(str(getattr(row, "ticker", "") or ""))[0]


def row_title(row: Any) -> str:
    return str(getattr(row, "title", "") or getattr(row, "yes_sub_title", "") or "")


def player_title_name(row: Any) -> str:
    head = row_title(row).split(":")[0].strip()
    if not head or any(s in head for s in SKIP_TITLE):
        return ""
    return head


def player_last_names(rows: Sequence[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        series = row_series(row)
        if series not in {"KXNFLFIRSTTD", "KXNFLTD", "KXNFLPASSTDS"}:
            continue
        name = player_title_name(row)
        if not name:
            continue
        last = name.split()[-1]
        key = last.lower()
        if key not in seen:
            seen.add(key)
            out.append(last)
    return sorted(out, key=str.lower)


def football_team(row: Any) -> str | None:
    raw = getattr(row, "raw", None)
    if not isinstance(raw, dict):
        return None
    custom = raw.get("custom")
    if not isinstance(custom, dict):
        return None
    val = custom.get("football_team")
    if not val:
        return None
    return str(val).strip().lower()


def same_team(a: Any, b: Any) -> bool:
    ta, tb = football_team(a), football_team(b)
    if ta and tb:
        return ta == tb
    return False


def row_floor(row: Any) -> float | None:
    raw = getattr(row, "raw", None)
    if not isinstance(raw, dict):
        return None
    val = raw.get("floor_strike")
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def floor_equals(row: Any, expected: float) -> bool:
    got = row_floor(row)
    return got is not None and abs(got - float(expected)) < 1e-9


def _name_match(row: Any, name_tokens: Sequence[str]) -> bool:
    name = player_title_name(row).lower().split()
    if not name or not name_tokens:
        return False

    def ok(q: str) -> bool:
        ql = q.lower()
        return any(n == ql or (len(ql) >= 2 and n.startswith(ql)) for n in name)

    return all(ok(q) for q in name_tokens)


def unique_player_rows(rows: Sequence[Any], name_tokens: Sequence[str]) -> tuple[list[Any], str]:
    hits = [
        row
        for row in rows
        if row_series(row) in {"KXNFLFIRSTTD", "KXNFLTD", "KXNFLPASSTDS"}
        and _name_match(row, name_tokens)
    ]
    names = sorted({player_title_name(r) for r in hits if player_title_name(r)})
    if len(names) != 1:
        return [], ""
    return hits, names[0]


def find_player_first_td(rows: Sequence[Any], name_tokens: Sequence[str]) -> Any | None:
    hits = [
        row
        for row in rows
        if row_series(row) == "KXNFLFIRSTTD" and _name_match(row, name_tokens)
        and not any(s in row_title(row) for s in SKIP_TITLE)
    ]
    return hits[0] if len(hits) == 1 else None


def find_player_td_ladder(rows: Sequence[Any], name_tokens: Sequence[str], floor: float) -> Any | None:
    hits = [
        row
        for row in rows
        if row_series(row) == "KXNFLTD"
        and _name_match(row, name_tokens)
        and floor_equals(row, floor)
    ]
    return hits[0] if len(hits) == 1 else None


def find_qb_pass_td(rows: Sequence[Any], trigger_row: Any, floor: float) -> Any | None:
    hits = [
        row
        for row in rows
        if row_series(row) == "KXNFLPASSTDS"
        and floor_equals(row, floor)
        and same_team(trigger_row, row)
    ]
    return hits[0] if len(hits) == 1 else None


def find_q_total(rows: Sequence[Any], quarter: int, floor: float = 6.5) -> Any | None:
    series = Q_TOTAL_SERIES.get(int(quarter))
    if not series:
        return None
    hits = [row for row in rows if row_series(row) == series and floor_equals(row, floor)]
    return hits[0] if len(hits) == 1 else None


def q_totals(rows: Sequence[Any], quarter: int) -> list[Any]:
    series = Q_TOTAL_SERIES.get(int(quarter))
    if not series:
        return []
    return [row for row in rows if row_series(row) == series]


def market_id(row: Any) -> str:
    return str(getattr(row, "ticker", "") or "").upper()


def team_abbrev_from_row(row: Any, game_code: str) -> str | None:
    suffix = ticker_parts(str(getattr(row, "ticker", "") or ""))[2]
    away, home = teams_from_game_code(game_code)
    hits = [code for code in (away, home) if suffix.startswith(code)]
    if not hits:
        return None
    return max(hits, key=len)
