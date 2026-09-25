"""Quarter end: winner/tie, spreads, and Q totals. No execution."""

from __future__ import annotations

from typing import Any, Sequence

from ..catalog import (
    NCAAF_Q_SPREAD_SERIES,
    NCAAF_Q_TOTAL_SERIES,
    NCAAF_Q_WINNER_SERIES,
    NFL_Q_SPREAD_SERIES,
    NFL_Q_WINNER_SERIES,
    Q_TOTAL_SERIES,
    is_college_rows,
    market_id,
    row_floor,
    row_series,
    team_abbrev_from_row,
)
from ..models import BetSide, CandidateBet, CandidateStatus, EventType, GameState


def preview_qend(
    rows: Sequence[Any],
    game_state: GameState,
    *,
    sent: set[str] | None = None,
    ended_quarter: int | None = None,
    away_this_q: int | None = None,
    home_this_q: int | None = None,
) -> list[CandidateBet]:
    ended = int(ended_quarter if ended_quarter is not None else game_state.quarter)
    away_q = (
        int(away_this_q)
        if away_this_q is not None
        else int(game_state.away_score - game_state.away_score_at_q_start)
    )
    home_q = (
        int(home_this_q)
        if home_this_q is not None
        else int(game_state.home_score - game_state.home_score_at_q_start)
    )
    sent_ids = {s.upper() for s in (sent or set())}
    college = is_college_rows(rows)
    winners = (NCAAF_Q_WINNER_SERIES if college else NFL_Q_WINNER_SERIES).get(ended)
    spreads = (NCAAF_Q_SPREAD_SERIES if college else NFL_Q_SPREAD_SERIES).get(ended)
    totals = (NCAAF_Q_TOTAL_SERIES if college else Q_TOTAL_SERIES).get(ended)
    away, home = game_state.away, game_state.home
    if away_q > home_q:
        winner, loser, margin = away, home, away_q - home_q
    elif home_q > away_q:
        winner, loser, margin = home, away, home_q - away_q
    else:
        winner, loser, margin = None, None, 0
    total = away_q + home_q
    out: list[CandidateBet] = []

    def add(row: Any, side: BetSide, reason: str) -> None:
        if row is None:
            return
        mid = market_id(row)
        if not mid or mid in sent_ids:
            return
        if any(c.market_id == mid and c.side == side for c in out):
            return
        out.append(
            CandidateBet(
                strategy_id="quarter_end",
                market_id=mid,
                side=side,
                reason=reason,
                trigger="qend",
                suggested_quantity=1,
                status=CandidateStatus.ELIGIBLE,
            )
        )

    if winners:
        for row in rows:
            if row_series(row) != winners:
                continue
            mid = market_id(row)
            suffix = mid.rsplit("-", 1)[-1]
            team = team_abbrev_from_row(row, game_state.game_code)
            if suffix == "TIE" or (team is None and "TIE" in suffix):
                add(
                    row,
                    BetSide.YES if winner is None else BetSide.NO,
                    "YES — Q tie" if winner is None else "NO — Q tie",
                )
            elif team == winner:
                add(row, BetSide.YES, f"YES — {team} wins Q{ended}")
            elif team in {away, home}:
                add(row, BetSide.NO, f"NO — {team} wins Q{ended}")

    if spreads:
        for row in rows:
            if row_series(row) != spreads:
                continue
            team = team_abbrev_from_row(row, game_state.game_code)
            floor = row_floor(row)
            if team is None or floor is None:
                continue
            if winner is not None and team == winner and margin > floor:
                add(row, BetSide.YES, f"YES — {team} Q{ended} by {floor}+")
            else:
                add(row, BetSide.NO, f"NO — {team} Q{ended} by {floor}+")

    if totals:
        for row in rows:
            if row_series(row) != totals:
                continue
            floor = row_floor(row)
            if floor is None:
                continue
            if total > floor:
                add(row, BetSide.YES, f"YES — Q{ended} over {floor}")
            else:
                add(row, BetSide.NO, f"NO — Q{ended} over {floor}")
    return out


class QuarterEndStrategy:
    id = "quarter_end"

    def __init__(self, rows: Sequence[Any]) -> None:
        self.rows = list(rows)

    def evaluate(self, game_state: GameState) -> list[CandidateBet]:
        event = game_state.last_event
        if event is None or event.type is not EventType.QUARTER:
            return []
        if not event.payload.get("end"):
            return []
        return preview_qend(
            self.rows,
            game_state,
            ended_quarter=int(event.payload.get("ended_quarter") or game_state.quarter),
            away_this_q=int(event.payload.get("away_this_q") or 0),
            home_this_q=int(event.payload.get("home_this_q") or 0),
        )
