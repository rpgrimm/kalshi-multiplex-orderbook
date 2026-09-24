"""TD play → First TD (if first of game) + player ladder + optional QB pass + this-Q 6.5.

Does not arm or execute. Catalog lookup only.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..catalog import (
    find_player_first_td,
    find_player_td_ladder,
    find_qb_pass_td,
    find_q_total,
    market_id,
    unique_player_rows,
)
from ..models import BetSide, CandidateBet, CandidateStatus, EventType, GameState


class TdClusterStrategy:
    id = "td_cluster"

    def __init__(self, rows: Sequence[Any]) -> None:
        self.rows = list(rows)

    def evaluate(self, game_state: GameState) -> list[CandidateBet]:
        event = game_state.last_event
        if event is None or event.type is not EventType.TOUCHDOWN:
            return []
        intent = str(event.payload.get("intent") or "")
        tokens = [t for t in str(event.player or "").lower().split() if t]
        if not tokens:
            return []
        _hits, display = unique_player_rows(self.rows, tokens)
        name_tokens = tokens
        first = find_player_first_td(self.rows, name_tokens)
        trigger = first or (_hits[0] if _hits else None)
        out: list[CandidateBet] = []

        def add(row: Any, reason: str) -> None:
            if row is None:
                return
            out.append(
                CandidateBet(
                    strategy_id=self.id,
                    market_id=market_id(row),
                    side=BetSide.YES,
                    reason=reason,
                    trigger=f"td:{intent}",
                    suggested_quantity=1,
                    status=CandidateStatus.ELIGIBLE,
                )
            )

        # After apply, this TD is already counted.
        player_tds = 0
        key = (event.player or "").lower()
        if key in game_state.players:
            player_tds = game_state.players[key].total_td
        game_tds = game_state.game_tds
        team_rec = 0
        if intent != "rush":
            tk = str(event.payload.get("team_key") or "").lower()
            if tk:
                team_rec = game_state.team_rec_tds.get(tk, 0)

        if game_tds == 1 and first is not None:
            add(first, f"first TD of game ({display or event.player})")
        if player_tds >= 1:
            ladder = find_player_td_ladder(self.rows, name_tokens, 0.5 + float(player_tds - 1))
            add(ladder, f"{display or event.player} {player_tds}+ TDs ({intent})")
        if intent == "receiving" and trigger is not None and team_rec >= 1:
            qb = find_qb_pass_td(self.rows, trigger, 0.5 + float(team_rec - 1))
            add(qb, f"same-team QB {team_rec}+ pass TD")
        qrow = find_q_total(self.rows, game_state.quarter, 6.5)
        add(qrow, f"Q{game_state.quarter} over 6.5")
        return out
