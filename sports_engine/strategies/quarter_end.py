"""Quarter end → BUY NO on overs that did not hit. No execution."""

from __future__ import annotations

from typing import Any, Sequence

from ..catalog import market_id, q_totals, row_floor
from ..models import BetSide, CandidateBet, CandidateStatus, EventType, GameState


class QuarterEndStrategy:
    id = "quarter_end_no"

    def __init__(self, rows: Sequence[Any]) -> None:
        self.rows = list(rows)

    def evaluate(self, game_state: GameState) -> list[CandidateBet]:
        event = game_state.last_event
        if event is None or event.type is not EventType.QUARTER:
            return []
        if not event.payload.get("end"):
            return []
        ended = int(event.payload.get("ended_quarter") or game_state.quarter)
        points = int(event.payload.get("points_this_q") or 0)
        away_q = int(event.payload.get("away_this_q") or 0)
        home_q = int(event.payload.get("home_this_q") or 0)
        out: list[CandidateBet] = []
        for row in q_totals(self.rows, ended):
            floor = row_floor(row)
            if floor is None:
                continue
            if points > floor:
                continue
            out.append(
                CandidateBet(
                    strategy_id=self.id,
                    market_id=market_id(row),
                    side=BetSide.NO,
                    reason=f"Q{ended} ended {points} pts; over {floor} missed",
                    trigger="quarter_end",
                    suggested_quantity=1,
                    status=CandidateStatus.ELIGIBLE,
                )
            )
        # Bookkeeping note for a shutout this quarter (no extra market required).
        if away_q == 0 or home_q == 0:
            quiet = []
            if away_q == 0:
                quiet.append(game_state.away)
            if home_q == 0:
                quiet.append(game_state.home)
            if quiet and not out:
                out.append(
                    CandidateBet(
                        strategy_id=self.id,
                        market_id=f"EXAMPLE-Q{ended}-NOSCORE",
                        side=BetSide.NO,
                        reason=f"Q{ended}: {', '.join(quiet)} scored 0 (no matching total market)",
                        trigger="quarter_end",
                        suggested_quantity=1,
                        status=CandidateStatus.ELIGIBLE,
                    )
                )
        return out
