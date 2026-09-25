"""Team FG play: next KXNFLFG rung (1+, 2+, …). No Q 6.5 — an FG is 3."""

from __future__ import annotations

from typing import Any, Sequence

from ..catalog import find_team_fg, market_id, resolve_game_team
from ..models import BetSide, CandidateBet, CandidateStatus, EventType, GameState


def preview_fg_ladder(
    rows: Sequence[Any],
    game_state: GameState,
    team_token: str,
) -> list[CandidateBet]:
    team = resolve_game_team(team_token, game_state.away, game_state.home)
    if not team:
        return []
    stats = game_state.team_stats(team)
    made = int(stats.field_goals) if stats is not None else 0
    nxt = made + 1
    row = find_team_fg(rows, team, 0.5 + float(nxt - 1), game_state.game_code)
    if row is None:
        return []
    return [
        CandidateBet(
            strategy_id="fg_ladder",
            market_id=market_id(row),
            side=BetSide.YES,
            reason=f"{team} {nxt}+ FGs",
            trigger="fg",
            suggested_quantity=1,
            status=CandidateStatus.ELIGIBLE,
        )
    ]


class FgLadderStrategy:
    id = "fg_ladder"

    def __init__(self, rows: Sequence[Any]) -> None:
        self.rows = list(rows)

    def evaluate(self, game_state: GameState) -> list[CandidateBet]:
        event = game_state.last_event
        if event is None or event.type is not EventType.FIELD_GOAL:
            return []
        team = str(event.team or "")
        rewind = game_state.snapshot()
        stats = rewind.team_stats(team)
        if stats is not None and stats.field_goals:
            stats.field_goals -= 1
        return preview_fg_ladder(self.rows, rewind, team)
