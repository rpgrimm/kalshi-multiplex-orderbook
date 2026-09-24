"""Browse-side helpers: build a session and turn a play line into events.

Does not submit Kalshi orders. Arming is explicit here after candidates appear.
"""

from __future__ import annotations

from typing import Any, Sequence

from .catalog import (
    find_player_first_td,
    football_team,
    player_last_names,
    team_abbrev_from_row,
    teams_from_game_code,
    unique_player_rows,
)
from .models import CandidateBet, EventType, GameEvent
from .play_protocol import PlayQuery
from .session import SportsSession
from .strategies.quarter_end import QuarterEndStrategy
from .strategies.td_cluster import TdClusterStrategy
from .strategy_engine import StrategyEngine


def make_browse_session(game_code: str, rows: Sequence[Any]) -> SportsSession:
    away, home = teams_from_game_code(game_code)
    return SportsSession(
        game_code=game_code,
        away=away,
        home=home,
        strategy_engine=StrategyEngine(
            [TdClusterStrategy(rows), QuarterEndStrategy(rows)]
        ),
    )


def format_candidates(cands: list[CandidateBet]) -> str:
    if not cands:
        return "no candidates"
    bits = [f"{c.side.value.upper()} {c.market_id} ({c.reason})" for c in cands]
    return f"{len(cands)} candidates (not sent): " + " · ".join(bits)


def ingest_td_play(
    session: SportsSession,
    rows: Sequence[Any],
    query: PlayQuery,
    *,
    quantity: int = 1,
    auto_arm: bool = True,
) -> tuple[list[CandidateBet], str]:
    tokens = list(query.name_tokens)
    hits, display = unique_player_rows(rows, tokens)
    if not display:
        return [], f"no unique player for {' '.join(tokens)}"
    first = find_player_first_td(rows, tokens)
    trigger = first or (hits[0] if hits else None)
    team = team_abbrev_from_row(trigger, session.state().game_code) if trigger is not None else None
    team_key = football_team(trigger) if trigger is not None else None
    event = GameEvent(
        type=EventType.TOUCHDOWN,
        team=team,
        player=display,
        quarter=session.state().quarter,
        payload={"intent": query.intent, "points": 6, "team_key": team_key or ""},
        source="browse-filter",
        raw=query.raw,
    )
    cands = session.ingest(event)
    armed_n = 0
    if auto_arm:
        for cand in cands:
            session.arm(cand.candidate_id, quantity=quantity)
            armed_n += 1
    msg = format_candidates(cands)
    if auto_arm:
        msg += f" · armed {armed_n} (not sent)"
    return cands, msg


def ingest_quarter_end(session: SportsSession, *, quantity: int = 1, auto_arm: bool = True) -> tuple[list[CandidateBet], str]:
    st = session.state()
    event = GameEvent(
        type=EventType.QUARTER,
        quarter=st.quarter,
        payload={"end": True},
        source="timekeeping",
        raw="qend",
    )
    cands = session.ingest(event)
    if auto_arm:
        for cand in cands:
            session.arm(cand.candidate_id, quantity=quantity)
    st2 = session.state()
    extra = f"now Q{st2.quarter} {st2.away} {st2.away_score}-{st2.home_score} {st2.home}"
    return cands, format_candidates(cands) + " · " + extra


def ingest_score(session: SportsSession, team: str, points: int) -> str:
    event = GameEvent(
        type=EventType.SCORE,
        team=team.upper(),
        payload={"set": int(points)},
        source="timekeeping",
        raw=f"{team} {points}",
    )
    session.ingest(event)
    st = session.state()
    return f"score {st.away} {st.away_score}-{st.home_score} {st.home}"


def parse_time_line(text: str, session: SportsSession) -> tuple[str, str] | None:
    """Return (kind, rest) for timekeeping: qend | q N | TEAM N."""
    raw = str(text or "").strip().lower()
    if not raw:
        return None
    if raw in {"qend", "endq", "end"}:
        return "qend", raw
    parts = raw.replace("=", " ").split()
    if len(parts) == 1 and parts[0] in {"q1", "q2", "q3", "q4"}:
        return "quarter", parts[0][-1]
    if len(parts) == 2 and parts[0] in {"q", "quarter"} and parts[1].isdigit():
        return "quarter", parts[1]
    if len(parts) == 2 and parts[1].isdigit():
        team = parts[0].upper()
        st = session.state()
        if team in {st.away, st.home}:
            return "score", f"{team} {parts[1]}"
    return None


def catalog_player_names(rows: Sequence[Any]) -> list[str]:
    return player_last_names(rows)
