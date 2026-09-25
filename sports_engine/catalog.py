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
# Kalshi NCAAF suffixes are 2–4 letters (MISS/FLA, ALA/UGA, …).
NCAA_TEAM_ABBREVS = frozenset(
    {
        "ALA", "APP", "ARK", "ARMY", "AUB", "BAY", "BC", "BSU", "BYU", "CAL",
        "CCU", "CHAR", "CIN", "CLEM", "COLO", "CONN", "DUKE", "ECU", "FAU",
        "FIU", "FLA", "FRES", "FSU", "GASO", "GAST", "GT", "HOU", "ILL", "IND",
        "IOWA", "ISU", "JMU", "KSU", "LIB", "LOU", "LSU", "LT", "MD", "MEM",
        "MIA", "MICH", "MINN", "MISS", "MIZZ", "MRSH", "MSU", "MTSU", "NAVY",
        "NCST", "ND", "NEB", "NMSU", "NW", "ODU", "OKLA", "OKST", "ORE", "ORST",
        "OSU", "OU", "PITT", "PSU", "PUR", "RICE", "RUT", "SC", "SDSU", "SMU",
        "STAN", "SYR", "TAM", "TCU", "TEMP", "TENN", "TEX", "TLSA", "TROY",
        "TTU", "TULN", "UAB", "UCF", "UCLA", "UGA", "UK", "UNC", "UNLV", "UNT",
        "USA", "USC", "USF", "USM", "UTAH", "UTEP", "VT", "WAKE", "WASH",
        "WISC", "WKU", "WSU", "WYO",
    }
)
TEAM_ABBREVS = NFL_TEAM_ABBREVS | NCAA_TEAM_ABBREVS
Q_TOTAL_SERIES = {
    1: "KXNFL1QTOTAL",
    2: "KXNFL2QTOTAL",
    3: "KXNFL3QTOTAL",
    4: "KXNFL4QTOTAL",
}
NCAAF_Q_TOTAL_SERIES = {
    1: "KXNCAAF1QTOTAL",
    2: "KXNCAAF2QTOTAL",
    3: "KXNCAAF3QTOTAL",
    4: "KXNCAAF4QTOTAL",
}
NCAAF_H_TOTAL_SERIES = {
    1: "KXNCAAF1HTOTAL",
    2: "KXNCAAF2HTOTAL",
}
NCAAF_H_TEAM_TOTAL_SERIES = {
    1: "KXNCAAF1HTEAMTOTAL",
    2: "KXNCAAF2HTEAMTOTAL",
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
    for a_len in (4, 3, 2):
        b_len = len(blob) - a_len
        if b_len not in (2, 3, 4):
            continue
        a, b = blob[:a_len], blob[a_len:]
        if a in TEAM_ABBREVS and b in TEAM_ABBREVS:
            found.append((a, b))
    if found:
        found.sort(key=lambda pair: -(len(pair[0]) + len(pair[1])))
        return found[0]
    return "AWAY", "HOME"


def is_college_rows(rows: Sequence[Any]) -> bool:
    return any(str(row_series(row)).startswith("KXNCAAF") for row in rows)


def teams_from_rows(rows: Sequence[Any], game_code: str) -> tuple[str, str]:
    away, home = teams_from_game_code(game_code)
    if away != "AWAY" and home != "HOME":
        return away, home
    skip = {"NONE", "TIE", "Y", "N"}
    codes: list[str] = []
    for row in rows:
        if row_series(row) not in {"KXNCAAFGAME", "KXNFLGAME", "KXNCAAFFIRSTTDTEAM"}:
            continue
        suffix = ticker_parts(str(getattr(row, "ticker", "") or ""))[2]
        if not suffix or suffix in skip or suffix in codes:
            continue
        codes.append(suffix)
        if len(codes) == 2:
            return codes[0], codes[1]
    return away, home


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
    for blob in (raw, raw.get("custom"), raw.get("custom_strike")):
        if not isinstance(blob, dict):
            continue
        for key in ("football_team", "team", "nfl_team"):
            val = blob.get(key)
            if val:
                return str(val).strip().lower()
    return None


def same_team(a: Any, b: Any, game_code: str = "") -> bool:
    ta, tb = football_team(a), football_team(b)
    if ta and tb:
        return ta == tb
    if game_code:
        ca = team_abbrev_from_row(a, game_code)
        cb = team_abbrev_from_row(b, game_code)
        if ca and cb:
            return ca == cb
    return False


def row_floor(row: Any) -> float | None:
    raw = getattr(row, "raw", None)
    if isinstance(raw, dict):
        val = raw.get("floor_strike")
        if val is not None and val != "":
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    title = row_title(row)
    plus = re.search(r"(\d+)\+", title)
    if plus:
        return int(plus.group(1)) - 0.5
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


def find_qb_pass_td(
    rows: Sequence[Any],
    trigger_row: Any,
    floor: float,
    game_code: str = "",
) -> Any | None:
    hits = [
        row
        for row in rows
        if row_series(row) == "KXNFLPASSTDS"
        and floor_equals(row, floor)
        and same_team(trigger_row, row, game_code)
    ]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        named = [row for row in hits if "pass" in row_title(row).lower()]
        if len(named) == 1:
            return named[0]
    return None


def resolve_game_team(token: str, away: str, home: str) -> str | None:
    """Map `m` / `mia` onto this game's abbreviations. Unique prefix only."""
    tok = str(token or "").strip().upper()
    if not tok:
        return None
    teams = [str(away or "").upper(), str(home or "").upper()]
    teams = [t for t in teams if t]
    exact = [t for t in teams if t == tok]
    if len(exact) == 1:
        return exact[0]
    prefixes = [t for t in teams if t.startswith(tok)]
    if len(prefixes) == 1:
        return prefixes[0]
    return None


def tab_complete_team(text: str, teams: Sequence[str]) -> tuple[str, list[str]]:
    from .play_protocol import last_token, RESERVED

    prefix, tok = last_token(text)
    if tok.lower() in {r.lower() for r in RESERVED}:
        return text, []
    needle = tok.lower()
    labels = []
    seen: set[str] = set()
    for team in teams:
        label = str(team or "").lower()
        if not label or label in seen:
            continue
        if not needle or label.startswith(needle):
            seen.add(label)
            labels.append(label)
    if not tok:
        return text, labels
    if len(labels) == 1:
        return prefix + labels[0], labels
    return text, labels


def ncaaf_first_td_rows(rows: Sequence[Any]) -> list[Any]:
    return [row for row in rows if row_series(row) == "KXNCAAFFIRSTTDTEAM"]


def ncaaf_dst_td_row(rows: Sequence[Any]) -> Any | None:
    hits = [row for row in rows if row_series(row) == "KXNCAAFDSTTD"]
    return hits[0] if len(hits) == 1 else None


def find_ncaaf_team_rec_td(
    rows: Sequence[Any],
    team: str,
    floor: float,
    game_code: str,
) -> Any | None:
    want = str(team or "").upper()
    hits = [
        row
        for row in rows
        if row_series(row) == "KXNCAAFTEAMRECTD"
        and floor_equals(row, floor)
        and team_abbrev_from_row(row, game_code) == want
    ]
    return hits[0] if len(hits) == 1 else None


def overs_cleared_by(
    rows: Sequence[Any],
    series: str,
    *,
    before: float,
    after: float,
    game_code: str = "",
    team: str | None = None,
    sent: set[str] | None = None,
) -> list[Any]:
    """YES overs with floor in (before, after]."""
    skip = {s.upper() for s in (sent or set())}
    want_team = str(team or "").upper() or None
    out: list[Any] = []
    for row in rows:
        if row_series(row) != str(series).upper():
            continue
        mid = market_id(row)
        if mid in skip:
            continue
        fl = row_floor(row)
        if fl is None or not (float(before) < fl <= float(after)):
            continue
        if want_team and team_abbrev_from_row(row, game_code) != want_team:
            continue
        out.append(row)
    return out


def find_team_fg(
    rows: Sequence[Any],
    team: str,
    floor: float,
    game_code: str,
) -> Any | None:
    want = str(team or "").upper()
    hits = [
        row
        for row in rows
        if row_series(row) == "KXNFLFG"
        and floor_equals(row, floor)
        and team_abbrev_from_row(row, game_code) == want
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
