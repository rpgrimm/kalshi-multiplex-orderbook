"""TD play preview: First TD (if none yet) + player ladder + optional QB + this-Q 6.5.

Uses *current* GameState (before the TD is applied). Does not arm or execute.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..catalog import (
    find_player_first_td,
    find_player_td_ladder,
    find_qb_pass_td,
    find_q_total,
    football_team,
    market_id,
    unique_player_rows,
)
from ..models import BetSide, CandidateBet, CandidateStatus, EventType, GameState


def _player_tds(game_state: GameState, display: str) -> int:
    key = display.lower()
    if key in game_state.players:
        return game_state.players[key].total_td
    last = display.split()[-1].lower()
    for player in game_state.players.values():
        if player.name.lower().split()[-1] == last:
            return player.total_td
    return 0


def preview_td_cluster(
    rows: Sequence[Any],
    game_state: GameState,
    name_tokens: Sequence[str],
    intent: str | None,
) -> list[CandidateBet]:
    """Candidates for the *next* TD. Score/TDs must not have been applied yet."""
    hits, display = unique_player_rows(rows, name_tokens)
    if not display:
        return []
    first = find_player_first_td(rows, name_tokens)
    trigger = first or (hits[0] if hits else None)
    player_next = _player_tds(game_state, display) + 1
    team_key = football_team(trigger) if trigger is not None else None
    team_rec_now = game_state.team_rec_tds.get(str(team_key or "").lower(), 0)
    team_rec_next = team_rec_now + 1
    out: list[CandidateBet] = []

    def add(row: Any, reason: str) -> None:
        if row is None:
            return
        out.append(
            CandidateBet(
                strategy_id="td_cluster",
                market_id=market_id(row),
                side=BetSide.YES,
                reason=reason,
                trigger=f"td:{intent or 'unknown'}",
                suggested_quantity=1,
                status=CandidateStatus.ELIGIBLE,
            )
        )

    if game_state.game_tds == 0 and first is not None:
        add(first, f"first TD of game ({display})")
    ladder = find_player_td_ladder(rows, name_tokens, 0.5 + float(player_next - 1))
    add(ladder, f"{display} {player_next}+ TDs ({intent or 'td'})")
    if intent == "receiving" and trigger is not None:
        qb = find_qb_pass_td(rows, trigger, 0.5 + float(team_rec_next - 1))
        add(qb, f"same-team QB {team_rec_next}+ pass TD")
    qrow = find_q_total(rows, game_state.quarter, 6.5)
    add(qrow, f"Q{game_state.quarter} over 6.5")
    return out


class TdClusterStrategy:
    id = "td_cluster"

    def __init__(self, rows: Sequence[Any]) -> None:
        self.rows = list(rows)

    def evaluate(self, game_state: GameState) -> list[CandidateBet]:
        """After a TD event is applied, describe that play (engine-demo path)."""
        event = game_state.last_event
        if event is None or event.type is not EventType.TOUCHDOWN:
            return []
        intent = str(event.payload.get("intent") or "") or None
        tokens = [t for t in str(event.player or "").lower().split() if t]
        if not tokens:
            return []
        # State already includes this TD; preview is defined on *next* TD, so
        # rewind counts via a shallow copy of the relevant fields.
        rewind = game_state.snapshot()
        stats = rewind.team_stats(event.team)
        if stats is not None and stats.touchdowns:
            stats.touchdowns -= 1
        key = (event.player or "").lower()
        if key in rewind.players:
            player = rewind.players[key]
            if intent == "rush":
                player.rush_td = max(0, player.rush_td - 1)
            else:
                player.rec_td = max(0, player.rec_td - 1)
        tk = str(event.payload.get("team_key") or "").lower()
        if tk and intent != "rush":
            rewind.team_rec_tds[tk] = max(0, rewind.team_rec_tds.get(tk, 0) - 1)
        return preview_td_cluster(self.rows, rewind, tokens, intent)
