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
    resolve_game_team,
    team_abbrev_from_row,
    teams_from_rows,
    unique_player_rows,
)
from .models import CandidateBet, EventType, GameEvent
from .play_protocol import PlayQuery
from .session import SportsSession
from .strategies.fg_ladder import FgLadderStrategy, preview_fg_ladder
from .strategies.extra_points import extra_points_for, preview_extra_overs
from .strategies.ncaaf_td import preview_ncaaf_td
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
    pat: bool = False
    college: bool = False


def make_browse_session(game_code: str, rows: Sequence[Any]) -> SportsSession:
    away, home = teams_from_rows(rows, game_code)
    return SportsSession(
        game_code=game_code,
        away=away,
        home=home,
        strategy_engine=StrategyEngine(
            [TdClusterStrategy(rows), FgLadderStrategy(rows), QuarterEndStrategy(rows)]
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
    if not team_key and team:
        team_key = team.lower()
    return display, team, team_key or ""


def arm_td_draft(
    session: SportsSession,
    rows: Sequence[Any],
    query: PlayQuery,
    *,
    quantity: int = 1,
    previous: DraftPlay | None = None,
    intent: str | None = None,
    include_pat: bool | None = None,
) -> tuple[DraftPlay | None, list[CandidateBet], str]:
    """Build/rebuild a draft from current state. Does not apply the TD."""
    tokens = list(query.name_tokens)
    display, team, team_key = _event_fields(session, rows, tokens)
    if not display:
        return previous, [], f"no unique player for {' '.join(tokens)}"
    use_intent = intent if intent is not None else query.intent
    use_pat = query.pat if include_pat is None else bool(include_pat)
    if previous is not None:
        for armed_id in previous.armed_ids:
            try:
                session.arming.disarm(armed_id)
            except KeyError:
                pass
    cands = preview_td_cluster(
        rows, session.state(), tokens, use_intent, include_pat=use_pat
    )
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
        pat=use_pat,
    )
    has_qb = any("PASSTDS" in c.market_id.upper() for c in cands)
    has_q = any("QTOTAL" in c.market_id.upper() for c in cands)
    bits = ["type re for QB · pat for Q 6.5 · Enter confirms"]
    if use_intent == "receiving":
        bits = ["QB pass armed" if has_qb else "QB pass NOT found"]
    elif use_intent == "rush":
        bits = ["rush (no QB)"]
    if use_pat:
        bits.append("Q 6.5 armed" if has_q else "Q 6.5 NOT found")
    bits.append("Enter confirms")
    return draft, cands, format_candidates(cands, armed=True) + " · " + " · ".join(bits)


def arm_ncaaf_td_draft(
    session: SportsSession,
    rows: Sequence[Any],
    query: PlayQuery,
    *,
    quantity: int = 1,
    previous: DraftPlay | None = None,
    intent: str | None = None,
) -> tuple[DraftPlay | None, list[CandidateBet], str]:
    st = session.state()
    token = query.name_tokens[0] if query.name_tokens else ""
    team = resolve_game_team(token, st.away, st.home)
    if not team:
        return previous, [], f"td team? {st.away.lower()} or {st.home.lower()}"
    use_intent = intent if intent is not None else query.intent
    if previous is not None:
        for armed_id in previous.armed_ids:
            try:
                session.arming.disarm(armed_id)
            except KeyError:
                pass
    cands = preview_ncaaf_td(
        rows, st, team, use_intent, sent=session.sent_markets
    )
    session.arming.observe(cands)
    armed_ids: list[str] = []
    for cand in cands:
        bet = session.arm(cand.candidate_id, quantity=quantity)
        armed_ids.append(bet.armed_id)
    draft = DraftPlay(
        player="",
        name_tokens=(team.lower(),),
        kind="ncaaf_td",
        intent=use_intent,
        armed_ids=armed_ids,
        team=team,
        team_key=team.lower(),
        display=team,
        college=True,
    )
    bits = ["offense"]
    if use_intent == "receiving":
        rec = st.team_rec_tds.get(team.lower(), 0) + 1
        bits = [f"{team.lower()} {rec} receiving td"]
    elif use_intent == "defense":
        bits = ["D/ST"]
    bits.append("Enter confirms")
    return draft, cands, format_candidates(cands, armed=True) + " · " + " · ".join(bits)


def arm_fg_draft(
    session: SportsSession,
    rows: Sequence[Any],
    query: PlayQuery,
    *,
    quantity: int = 1,
    previous: DraftPlay | None = None,
) -> tuple[DraftPlay | None, list[CandidateBet], str]:
    st = session.state()
    token = query.name_tokens[0] if query.name_tokens else ""
    team = resolve_game_team(token, st.away, st.home)
    if not team:
        return previous, [], f"fg team? {st.away.lower()} or {st.home.lower()}"
    if previous is not None:
        for armed_id in previous.armed_ids:
            try:
                session.arming.disarm(armed_id)
            except KeyError:
                pass
    cands = preview_fg_ladder(rows, st, team)
    session.arming.observe(cands)
    armed_ids: list[str] = []
    for cand in cands:
        bet = session.arm(cand.candidate_id, quantity=quantity)
        armed_ids.append(bet.armed_id)
    draft = DraftPlay(
        player="",
        name_tokens=(team.lower(),),
        kind="fg",
        armed_ids=armed_ids,
        team=team,
        team_key=team.lower(),
        display=team,
    )
    hint = "Enter confirms send" if cands else "no FG ladder market"
    return draft, cands, format_candidates(cands, armed=True) + f" · {team} FG · {hint}"


def arm_extra_draft(
    session: SportsSession,
    rows: Sequence[Any],
    extra: str,
    *,
    quantity: int = 1,
    previous: DraftPlay | None = None,
) -> tuple[DraftPlay | None, list[CandidateBet], str]:
    team = session.pending_extra_team
    if not team:
        return previous, [], "no TD waiting for PAT/2PT"
    add = extra_points_for(extra)
    if previous is not None:
        for armed_id in previous.armed_ids:
            try:
                session.arming.disarm(armed_id)
            except KeyError:
                pass
    cands = preview_extra_overs(
        rows, session.state(), team, add, sent=session.sent_markets
    )
    session.arming.observe(cands)
    armed_ids: list[str] = []
    for cand in cands:
        bet = session.arm(cand.candidate_id, quantity=quantity)
        armed_ids.append(bet.armed_id)
    draft = DraftPlay(
        player="",
        name_tokens=(),
        kind="extra",
        intent=extra,
        armed_ids=armed_ids,
        team=team,
        team_key=str(team).lower(),
        display=team,
    )
    label = "+1 PAT" if extra == "pat" else "+2 2PT"
    hint = "Enter confirms send" if cands else f"no new overs · Enter records {label}"
    return draft, cands, format_candidates(cands, armed=True) + f" · {team} {label} · {hint}"


def apply_extra_draft(session: SportsSession, draft: DraftPlay) -> str:
    extra = draft.intent or "pat"
    add = extra_points_for(extra)
    if add:
        session.ingest(
            GameEvent(
                type=EventType.EXTRA_POINT,
                team=draft.team,
                payload={"points": add, "extra": extra},
                source="browse-confirm",
                raw=extra,
            ),
            evaluate=False,
        )
    session.pending_extra_team = None
    st = session.state()
    label = "PAT" if extra == "pat" else "2PT"
    return (
        f"recorded {draft.team} {label} +{add} · "
        f"Q{st.quarter} {st.away} {st.away_score}-{st.home_score} {st.home}"
    )


def apply_extra_miss(session: SportsSession, extra: str) -> str:
    team = session.pending_extra_team
    session.ingest(
        GameEvent(
            type=EventType.OTHER,
            team=team,
            payload={"points": 0, "extra": extra},
            source="browse-confirm",
            raw=extra,
        ),
        evaluate=False,
    )
    session.pending_extra_team = None
    st = session.state()
    return (
        f"recorded {team or '?'} {extra} · "
        f"Q{st.quarter} {st.away} {st.away_score}-{st.home_score} {st.home}"
    )


def apply_fg_draft(session: SportsSession, draft: DraftPlay) -> str:
    event = GameEvent(
        type=EventType.FIELD_GOAL,
        team=draft.team,
        quarter=session.state().quarter,
        payload={"points": 3, "team_key": draft.team_key},
        source="browse-confirm",
        raw=f"fg {draft.team}",
    )
    session.ingest(event, evaluate=False)
    st = session.state()
    return (
        f"recorded {draft.team} FG · "
        f"Q{st.quarter} {st.away} {st.away_score}-{st.home_score} {st.home}"
    )


def apply_td_draft(session: SportsSession, draft: DraftPlay) -> str:
    """Record the TD in GameState after the operator confirms. No extra candidates."""
    event = GameEvent(
        type=EventType.TOUCHDOWN,
        team=draft.team,
        player=draft.display,
        quarter=session.state().quarter,
        payload={
            "intent": draft.intent or "receiving",
            "points": 6,
            "team_key": draft.team_key,
            "pat": draft.pat,
        },
        source="browse-confirm",
        raw=f"{draft.player} td {draft.intent or ''}".strip(),
    )
    session.ingest(event, evaluate=False)
    if draft.pat:
        session.ingest(
            GameEvent(
                type=EventType.EXTRA_POINT,
                team=draft.team,
                payload={"points": 1},
                source="browse-confirm",
                raw="pat",
            ),
            evaluate=False,
        )
    st = session.state()
    if draft.pat:
        session.pending_extra_team = None
    elif draft.team:
        session.pending_extra_team = draft.team
    extra = " + PAT" if draft.pat else ""
    rec_note = ""
    if draft.intent == "receiving" and draft.team:
        rec = st.team_rec_tds.get(str(draft.team).lower(), 0)
        rec_note = f" · {str(draft.team).lower()} {rec} receiving td"
    who = draft.display or draft.team or draft.player
    return (
        f"recorded {who} TD{extra}{rec_note} · "
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
