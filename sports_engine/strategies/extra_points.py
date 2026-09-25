"""PAT / 2PT: extra points after a TD, then any newly cleared totals."""

from __future__ import annotations

from typing import Any, Sequence

from ..catalog import (
    NCAAF_GAME_TOTAL_SERIES,
    NCAAF_H_TEAM_TOTAL_SERIES,
    NCAAF_H_TOTAL_SERIES,
    NCAAF_Q_TOTAL_SERIES,
    NCAAF_TEAM_TOTAL_SERIES,
    NFL_GAME_TOTAL_SERIES,
    NFL_H_TEAM_TOTAL_SERIES,
    NFL_H_TOTAL_SERIES,
    NFL_TEAM_TOTAL_SERIES,
    Q_TOTAL_SERIES,
    is_college_rows,
    market_id,
    overs_cleared_by,
    row_floor,
)
from ..models import BetSide, CandidateBet, CandidateStatus, GameState


def extra_points_for(extra: str | None) -> int:
    if extra == "pat":
        return 1
    if extra == "2pt":
        return 2
    return 0


def preview_extra_overs(
    rows: Sequence[Any],
    game_state: GameState,
    team: str,
    add_points: int,
    *,
    sent: set[str] | None = None,
) -> list[CandidateBet]:
    if add_points <= 0:
        return []
    sent_ids = {s.upper() for s in (sent or set())}
    college = is_college_rows(rows)
    half = 1 if game_state.quarter <= 2 else 2
    q_series = (NCAAF_Q_TOTAL_SERIES if college else Q_TOTAL_SERIES).get(int(game_state.quarter))
    h_series = (NCAAF_H_TOTAL_SERIES if college else NFL_H_TOTAL_SERIES).get(half)
    ht_series = (NCAAF_H_TEAM_TOTAL_SERIES if college else NFL_H_TEAM_TOTAL_SERIES).get(half)
    game_series = NCAAF_GAME_TOTAL_SERIES if college else NFL_GAME_TOTAL_SERIES
    team_series = NCAAF_TEAM_TOTAL_SERIES if college else NFL_TEAM_TOTAL_SERIES
    out: list[CandidateBet] = []

    def add(row: Any, reason: str) -> None:
        if row is None:
            return
        mid = market_id(row)
        if not mid or mid in sent_ids:
            return
        if any(c.market_id == mid for c in out):
            return
        fl = row_floor(row)
        out.append(
            CandidateBet(
                strategy_id="extra_points",
                market_id=mid,
                side=BetSide.YES,
                reason=reason if fl is None else f"{reason} {fl}",
                trigger="extra",
                suggested_quantity=1,
                status=CandidateStatus.ELIGIBLE,
            )
        )

    q_before = float(game_state.points_this_quarter)
    if q_series:
        for row in overs_cleared_by(
            rows, q_series, before=q_before, after=q_before + add_points, sent=sent_ids
        ):
            add(row, f"YES — Q{game_state.quarter} over")
    h_before = float(game_state.points_this_half())
    if h_series:
        label = "1H" if half == 1 else "2H"
        for row in overs_cleared_by(
            rows, h_series, before=h_before, after=h_before + add_points, sent=sent_ids
        ):
            add(row, f"YES — {label} over")
    th_before = float(game_state.team_points_this_half(team))
    if ht_series:
        label = "1H" if half == 1 else "2H"
        for row in overs_cleared_by(
            rows,
            ht_series,
            before=th_before,
            after=th_before + add_points,
            game_code=game_state.game_code,
            team=team,
            sent=sent_ids,
        ):
            add(row, f"YES — {team} {label} over")
    g_before = float(game_state.away_score + game_state.home_score)
    for row in overs_cleared_by(
        rows, game_series, before=g_before, after=g_before + add_points, sent=sent_ids
    ):
        add(row, "YES — game over")
    stats = game_state.team_stats(team)
    t_before = float(stats.score) if stats is not None else 0.0
    for row in overs_cleared_by(
        rows,
        team_series,
        before=t_before,
        after=t_before + add_points,
        game_code=game_state.game_code,
        team=team,
        sent=sent_ids,
    ):
        add(row, f"YES — {team} game over")
    return out
