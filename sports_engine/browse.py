"""Browse-side helpers: draft a TD play, then confirm to apply stats.

Arming a draft does not change GameState and does not send Kalshi orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from .strategies.td_cluster import TdClusterStrategy, preview_td_cluster
from .strategy_engine import StrategyEngine


@dataclass
class DraftPlay:
    player: str
    name_tokens: tuple[str, ...]
    kind: str
    intent: str | None = None
    armed_ids: list[str] = field(default_factory=list)
    team: str | None = None
    team_key: str = ""
    display: str = ""


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


def format_candidates(cands: list[CandidateBet], *, armed: bool = False) -> str:
    if not cands:
        return "no candidates"
    bits = [f"{c.side.value.upper()} {c.market_id} ({c.reason})" for c in cands]
    verb = "armed (Enter confirms / sends)" if armed else "candidates (not sent)"
    return f"{len(cands)} {verb}: " + " · ".join(bits)


def _event_fields(session: SportsSession, rows: Sequence[Any], name_tokens: Sequence[str]) -> tuple[str, str | None, str]:
    hits, display = unique_player_rows(rows, name_tokens)
    first = find_player_first_td(rows, name_tokens)
    trigger = first or (hits[0] if hits else None)
    team = team_abbrev_from_row(trigger, session.state().game_code) if trigger is not None else None
    team_key = football_team(trigger) if trigger is not None else None
    return display, team, team_key or ""


def arm_td_draft(
    session: SportsSession,
    rows: Sequence[Any],
    query: PlayQuery,
    *,
    quantity: int = 1,
    previous: DraftPlay | None = None,
    intent: str | None = None,
) -> tuple[DraftPlay | None, list[CandidateBet], str]:
    """Build/rebuild a draft from current state. Does not apply the TD."""
    tokens = list(query.name_tokens)
    display, team, team_key = _event_fields(session, rows, tokens)
    if not display:
        return previous, [], f"no unique player for {' '.join(tokens)}"
    use_intent = intent if intent is not None else query.intent
    if previous is not None:
        for armed_id in previous.armed_ids:
            try:
                session.arming.disarm(armed_id)
            except KeyError:
                pass
    cands = preview_td_cluster(rows, session.state(), tokens, use_intent)
    session.arming.observe(cands)
    armed_ids: list[str] = []
    for cand in cands:
        bet = session.arm(cand.candidate_id, quantity=quantity)
        armed_ids.append(bet.armed_id)
    draft = DraftPlay(
        player=display.split()[-1],
        name_tokens=tuple(tokens),
        kind="td",
        intent=use_intent,
        armed_ids=armed_ids,
        team=team,
        team_key=team_key,
        display=display,
    )
    hint = "type re for QB pass · ru for rush · Enter confirms"
    if use_intent == "receiving":
        hint = "QB pass armed · Enter confirms send"
    elif use_intent == "rush":
        hint = "rush (no QB) · Enter confirms send"
    return draft, cands, format_candidates(cands, armed=True) + " · " + hint


def apply_td_draft(session: SportsSession, draft: DraftPlay) -> str:
    """Record the TD in GameState after the operator confirms. No extra candidates."""
    event = GameEvent(
        type=EventType.TOUCHDOWN,
        team=draft.team,
        player=draft.display,
        quarter=session.state().quarter,
        payload={"intent": draft.intent or "receiving", "points": 6, "team_key": draft.team_key},
        source="browse-confirm",
        raw=f"{draft.player} td {draft.intent or ''}".strip(),
    )
    session.ingest(event, evaluate=False)
    st = session.state()
    return (
        f"recorded {draft.display} TD · "
        f"Q{st.quarter} {st.away} {st.away_score}-{st.home_score} {st.home} · "
        f"game TDs {st.game_tds}"
    )


def ingest_td_play(
    session: SportsSession,
    rows: Sequence[Any],
    query: PlayQuery,
    *,
    quantity: int = 1,
    auto_arm: bool = True,
) -> tuple[list[CandidateBet], str]:
    """Test helper: preview + optional arm, still does not apply stats."""
    draft, cands, msg = arm_td_draft(
        session, rows, query, quantity=quantity, intent=query.intent
    )
    if not auto_arm and draft is not None:
        for armed_id in draft.armed_ids:
            try:
                session.arming.disarm(armed_id)
            except KeyError:
                pass
        return cands, format_candidates(cands)
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
    session.ingest(event, evaluate=False)
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
