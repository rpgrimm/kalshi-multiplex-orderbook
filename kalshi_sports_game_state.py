#!/usr/bin/env python3
"""In-game sports operator state: quarter, score, player TDs, armed bets.

Typing `watson td re` / `watson td ru` records a play and arms the next
YES legs. `[` `]` set quarter; ending a quarter proposes BUY NO on that
quarter's over-totals that did not hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from kalshi_sports_packages import (
    parse_filter_query,
    same_team_as_trigger,
    team_codes_from_game_code,
    team_key,
    ticker_parts,
)

SKIP_NAME_TOKENS = frozenset(
    {"td", "touchdown", "touchdowns", "jr", "jr.", "sr", "ii", "iii", "iv", "d/st", "dst"}
)
Q_TOTAL_SERIES = {
    1: "KXNFL1QTOTAL",
    2: "KXNFL2QTOTAL",
    3: "KXNFL3QTOTAL",
    4: "KXNFL4QTOTAL",
}


def _norm_tokens(text: str) -> list[str]:
    cleaned = (
        str(text or "")
        .lower()
        .replace("'", "")
        .replace(".", " ")
        .replace(":", " ")
        .replace("-", " ")
    )
    return [tok for tok in cleaned.split() if tok]


def player_title_tokens(row: Any) -> list[str]:
    title = str(getattr(row, "title", "") or getattr(row, "yes_sub_title", "") or "")
    head = title.split(":")[0]
    return [tok for tok in _norm_tokens(head) if tok not in SKIP_NAME_TOKENS]


def row_series(row: Any) -> str:
    series = str(getattr(row, "series_ticker", "") or "").upper()
    if series:
        return series
    ticker = str(getattr(row, "ticker", "") or "")
    return ticker_parts(ticker)[0]


def _raw_floor(row: Any) -> float | None:
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
    got = _raw_floor(row)
    if got is None:
        return False
    return abs(got - float(expected)) < 1e-9


def is_skippable_td_row(row: Any) -> bool:
    title = str(getattr(row, "title", "") or "")
    return "D/ST" in title or "No Touchdown" in title


@dataclass
class PlayerStat:
    name: str
    key: str
    rec_td: int = 0
    rush_td: int = 0

    @property
    def total_td(self) -> int:
        return int(self.rec_td) + int(self.rush_td)

    def label(self) -> str:
        bits = []
        if self.rec_td:
            bits.append(f"{self.rec_td}re")
        if self.rush_td:
            bits.append(f"{self.rush_td}ru")
        return f"{self.name} {' '.join(bits)}".strip()


@dataclass
class ArmedBet:
    ticker: str
    title: str
    side: str  # buy_yes | buy_no
    reason: str
    player: str = ""
    series: str = ""


@dataclass
class GameState:
    game_code: str
    away: str
    home: str
    quarter: int = 1
    away_score: int = 0
    home_score: int = 0
    away_score_at_q_start: int = 0
    home_score_at_q_start: int = 0
    players: dict[str, PlayerStat] = field(default_factory=dict)
    game_tds: int = 0
    team_rec_tds: dict[str, int] = field(default_factory=dict)
    armed: list[ArmedBet] = field(default_factory=list)
    sent_tickers: set[str] = field(default_factory=set)
    play_armed_for_query: bool = False
    score_set_for_query: bool = False
    plays: list[str] = field(default_factory=list)

    @classmethod
    def from_game_code(cls, game_code: str) -> "GameState":
        codes = team_codes_from_game_code(game_code)
        away = codes[0] if len(codes) > 0 else "AWAY"
        home = codes[1] if len(codes) > 1 else "HOME"
        return cls(game_code=str(game_code or "").upper(), away=away, home=home)

    @property
    def points_this_quarter(self) -> int:
        return (self.away_score + self.home_score) - (
            self.away_score_at_q_start + self.home_score_at_q_start
        )

    def header_line(self) -> str:
        q = f"Q{self.quarter}"
        score = f"{self.away} {self.away_score}-{self.home_score} {self.home}"
        qpts = f"thisQ {self.points_this_quarter}"
        stats = " · ".join(p.label() for p in list(self.players.values())[-4:])
        stats_bit = f" | {stats}" if stats else ""
        return f"{q}  {score}  {qpts}  | armed {len(self.armed)}{stats_bit}"

    def set_quarter(self, quarter: int) -> str:
        q = max(1, min(4, int(quarter)))
        if q == self.quarter:
            return f"already {self.header_line()}"
        self.quarter = q
        self.away_score_at_q_start = self.away_score
        self.home_score_at_q_start = self.home_score
        return f"quarter set → {self.header_line()}"

    def shift_quarter(self, delta: int) -> str:
        return self.set_quarter(self.quarter + int(delta))

    def set_score(self, team: str, points: int) -> str:
        team_u = str(team or "").upper()
        pts = max(0, int(points))
        if team_u == self.away:
            self.away_score = pts
        elif team_u == self.home:
            self.home_score = pts
        else:
            return f"unknown team {team_u} (game is {self.away}@{self.home})"
        return f"score {self.away} {self.away_score}-{self.home_score} {self.home}"

    def _player(self, key: str, name: str) -> PlayerStat:
        if key not in self.players:
            self.players[key] = PlayerStat(name=name, key=key)
        return self.players[key]

    def record_td(self, *, key: str, name: str, intent: str, team_key: str | None) -> PlayerStat:
        player = self._player(key, name)
        if intent == "rush":
            player.rush_td += 1
        else:
            player.rec_td += 1
            if team_key:
                self.team_rec_tds[team_key] = self.team_rec_tds.get(team_key, 0) + 1
        self.game_tds += 1
        self.plays.append(f"Q{self.quarter} {name} {intent} TD")
        return player

    def has_armed_or_sent(self, ticker: str) -> bool:
        t = ticker.upper()
        if t in self.sent_tickers:
            return True
        return any(b.ticker.upper() == t for b in self.armed)

    def arm(self, bet: ArmedBet) -> bool:
        if self.has_armed_or_sent(bet.ticker):
            return False
        self.armed.append(bet)
        return True

    def drop_armed_at(self, index: int) -> ArmedBet | None:
        if 0 <= index < len(self.armed):
            return self.armed.pop(index)
        return None

    def mark_sent(self, ticker: str) -> None:
        self.sent_tickers.add(str(ticker).upper())
        self.armed = [b for b in self.armed if b.ticker.upper() != str(ticker).upper()]


def parse_score_command(text: str, state: GameState) -> tuple[str, int] | None:
    """`atl 7` / `gb=14` — team must be this game's away/home."""
    market, intent = parse_filter_query(text)
    if intent is not None:
        return None
    teams = {state.away.lower(), state.home.lower()}

    def _pair(tok_team: str, tok_pts: str) -> tuple[str, int] | None:
        tok_team = tok_team.replace("=", "")
        tok_pts = tok_pts.replace("=", "")
        if tok_team not in teams:
            return None
        if not tok_pts.isdigit():
            return None
        return tok_team.upper(), int(tok_pts)

    if len(market) == 1 and "=" in market[0]:
        team_tok, _, pts_tok = market[0].partition("=")
        return _pair(team_tok, pts_tok)
    if len(market) != 2:
        return None
    a, b = market[0], market[1]
    for team_tok, pts_tok in ((a, b), (b, a)):
        got = _pair(team_tok, pts_tok)
        if got:
            return got
    return None


def parse_play_command(text: str) -> tuple[list[str], str] | None:
    """Return (player_tokens, intent) when query looks like `{player} td ru|re`."""
    market, intent = parse_filter_query(text)
    if intent not in {"rush", "receiving"}:
        return None
    if "td" not in market and "touchdown" not in market:
        return None
    player = [tok for tok in market if tok not in SKIP_NAME_TOKENS]
    if not player:
        return None
    return player, intent


def _name_match(name_toks: Sequence[str], query_toks: Sequence[str]) -> bool:
    def tok_ok(q: str) -> bool:
        return any(n == q or (len(q) >= 3 and n.startswith(q)) for n in name_toks)

    return all(tok_ok(q) for q in query_toks)


def find_player_rows(
    rows: Sequence[Any], player_tokens: Sequence[str]
) -> tuple[list[Any], str, str | None, str]:
    """Rows whose player name contains all tokens.

    Returns (rows, display_name, team_key, error).
    """
    tokens = [str(t).lower() for t in player_tokens if t]
    if not tokens:
        return [], "", None, "no player tokens"
    hits: list[Any] = []
    for row in rows:
        if is_skippable_td_row(row):
            continue
        series = row_series(row)
        if series not in {"KXNFLFIRSTTD", "KXNFLTD", "KXNFLPASSTDS"}:
            continue
        name_toks = player_title_tokens(row)
        if not name_toks:
            continue
        if _name_match(name_toks, tokens):
            hits.append(row)
    if not hits:
        return [], "", None, f"no player match for {' '.join(tokens)}"
    names = sorted({" ".join(player_title_tokens(r)).title() for r in hits})
    if len(names) > 1:
        return [], "", None, "ambiguous player: " + ", ".join(names[:4])
    name = names[0]
    team = None
    for row in hits:
        team = team_key(row)
        if team:
            break
    return hits, name, team, ""


def _player_key(rows: Sequence[Any], name: str) -> str:
    for row in rows:
        raw = getattr(row, "raw", None)
        if isinstance(raw, dict):
            custom = raw.get("custom")
            if isinstance(custom, dict) and custom.get("football_player"):
                return str(custom.get("football_player")).lower()
    return name.lower()


def _pick_unique(rows: Sequence[Any], *, series: str, floor: float | None, same_team_as: Any | None) -> Any | None:
    hits: list[Any] = []
    for row in rows:
        if row_series(row) != series:
            continue
        if floor is not None and not floor_equals(row, floor):
            continue
        if same_team_as is not None and not same_team_as_trigger(same_team_as, row):
            continue
        hits.append(row)
    if len(hits) == 1:
        return hits[0]
    return None


def resolve_play_bets(
    *,
    state: GameState,
    rows: Sequence[Any],
    player_tokens: Sequence[str],
    intent: str,
) -> tuple[list[ArmedBet], str]:
    """Record the TD then arm the next YES legs. Does not submit."""
    player_rows, name, team, err = find_player_rows(rows, player_tokens)
    if err or not name:
        return [], err or "no player match"

    trigger_row = next((r for r in player_rows if row_series(r) == "KXNFLFIRSTTD"), player_rows[0])
    key = _player_key(player_rows, name)
    before_game = state.game_tds
    before_player = state.players[key].total_td if key in state.players else 0
    before_team_rec = state.team_rec_tds.get(team or "", 0) if team else 0

    state.record_td(key=key, name=name, intent=intent, team_key=team if intent != "rush" else None)

    bets: list[ArmedBet] = []

    def _arm_row(row: Any, reason: str) -> None:
        ticker = str(getattr(row, "ticker", "") or "").upper()
        if not ticker:
            return
        title = str(getattr(row, "title", "") or getattr(row, "yes_sub_title", "") or ticker)
        bet = ArmedBet(
            ticker=ticker,
            title=title,
            side="buy_yes",
            reason=reason,
            player=name,
            series=row_series(row),
        )
        if state.arm(bet):
            bets.append(bet)

    if before_game == 0:
        first = next((r for r in player_rows if row_series(r) == "KXNFLFIRSTTD"), None)
        if first is not None:
            _arm_row(first, f"first TD of game ({intent})")

    td_floor = 0.5 + float(before_player)  # 1st player TD → 1+ (0.5); 2nd → 2+ (1.5)
    player_td = None
    for row in rows:
        if row_series(row) != "KXNFLTD":
            continue
        if is_skippable_td_row(row):
            continue
        if not _name_match(player_title_tokens(row), player_tokens):
            continue
        if floor_equals(row, td_floor):
            player_td = row
            break
    if player_td is not None:
        n = before_player + 1
        _arm_row(player_td, f"{name} {n}+ TDs ({intent})")

    if intent != "rush":
        pass_floor = 0.5 + float(before_team_rec)
        qb = _pick_unique(
            rows,
            series="KXNFLPASSTDS",
            floor=pass_floor,
            same_team_as=trigger_row,
        )
        if qb is not None:
            n = before_team_rec + 1
            _arm_row(qb, f"same-team QB {n}+ pass TD (receiving)")

    q_series = Q_TOTAL_SERIES.get(state.quarter)
    if q_series:
        q_row = _pick_unique(rows, series=q_series, floor=6.5, same_team_as=None)
        if q_row is not None:
            _arm_row(q_row, f"Q{state.quarter} over 6.5 (once)")

    if not bets:
        return [], f"recorded {name} {intent} TD; no new markets to arm"
    return bets, f"ARMED {name} {intent} TD #{before_player + 1}: {len(bets)} new bets"


def resolve_end_quarter_no_bets(*, state: GameState, rows: Sequence[Any]) -> list[ArmedBet]:
    """BUY NO on this quarter's over-totals that did not occur."""
    series = Q_TOTAL_SERIES.get(state.quarter)
    if not series:
        return []
    points = state.points_this_quarter
    bets: list[ArmedBet] = []
    for row in rows:
        if row_series(row) != series:
            continue
        floor = _raw_floor(row)
        if floor is None:
            continue
        # Over X.5 occurs iff points > X.5.
        if points > floor:
            continue
        ticker = str(getattr(row, "ticker", "") or "").upper()
        if not ticker or state.has_armed_or_sent(ticker):
            continue
        title = str(getattr(row, "title", "") or ticker)
        bet = ArmedBet(
            ticker=ticker,
            title=title,
            side="buy_no",
            reason=f"Q{state.quarter} ended with {points} pts; over {floor} did not hit",
            series=series,
        )
        if state.arm(bet):
            bets.append(bet)
    return bets
