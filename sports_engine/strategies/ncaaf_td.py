"""College team TD cluster. No player props.

A TD is 6: arm first-team YES/NO, Q/H overs the +6 newly clears, optional
D/ST, and receiving ladder from 2+.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..catalog import (
    NCAAF_H_TEAM_TOTAL_SERIES,
    NCAAF_H_TOTAL_SERIES,
    NCAAF_Q_TOTAL_SERIES,
    find_ncaaf_team_rec_td,
    market_id,
    ncaaf_dst_td_row,
    ncaaf_first_td_rows,
    overs_cleared_by,
    resolve_game_team,
    team_abbrev_from_row,
)
from ..models import BetSide, CandidateBet, CandidateStatus, GameState


def preview_ncaaf_td(
    rows: Sequence[Any],
    game_state: GameState,
    team_token: str,
    intent: str | None,
    *,
    sent: set[str] | None = None,
) -> list[CandidateBet]:
    team = resolve_game_team(team_token, game_state.away, game_state.home)
    if not team:
        return []
    sent_ids = {s.upper() for s in (sent or set())}
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
                strategy_id="ncaaf_td",
                market_id=mid,
                side=side,
                reason=reason,
                trigger=f"td:{intent or 'offense'}",
                suggested_quantity=1,
                status=CandidateStatus.ELIGIBLE,
            )
        )

    if game_state.game_tds == 0:
        for row in ncaaf_first_td_rows(rows):
            suffix = team_abbrev_from_row(row, game_state.game_code)
            ticker = market_id(row)
            if ticker.endswith("-NONE") or suffix is None and "NONE" in ticker:
                add(row, BetSide.NO, "NO — no team scores a TD")
            elif suffix == team:
                add(row, BetSide.YES, f"YES — {team} scores first TD")
            elif suffix in {game_state.away, game_state.home}:
                add(row, BetSide.NO, f"NO — {suffix} scores first TD")

    if intent == "defense":
        add(ncaaf_dst_td_row(rows), BetSide.YES, "YES — D/ST touchdown")

    pts_q = float(game_state.points_this_quarter)
    after_q = pts_q + 6.0
    q_series = NCAAF_Q_TOTAL_SERIES.get(int(game_state.quarter))
    if q_series:
        for row in overs_cleared_by(
            rows, q_series, before=pts_q, after=after_q, sent=sent_ids
        ):
            add(row, BetSide.YES, f"YES — Q{game_state.quarter} over {row_floor_label(row)}")

    if game_state.quarter <= 2:
        half = 1
        pts_h = float(game_state.away_score + game_state.home_score)
        h_series = NCAAF_H_TOTAL_SERIES.get(half)
        if h_series:
            for row in overs_cleared_by(
                rows, h_series, before=pts_h, after=pts_h + 6.0, sent=sent_ids
            ):
                add(row, BetSide.YES, f"YES — 1H over {row_floor_label(row)}")
        ht_series = NCAAF_H_TEAM_TOTAL_SERIES.get(half)
        stats = game_state.team_stats(team)
        team_h = float(stats.score) if stats is not None else 0.0
        if ht_series:
            for row in overs_cleared_by(
                rows,
                ht_series,
                before=team_h,
                after=team_h + 6.0,
                game_code=game_state.game_code,
                team=team,
                sent=sent_ids,
            ):
                add(row, BetSide.YES, f"YES — {team} 1H over {row_floor_label(row)}")

    if intent == "receiving":
        rec_now = game_state.team_rec_tds.get(team.lower(), 0)
        rec_next = rec_now + 1
        if rec_next >= 2:
            rec_row = find_ncaaf_team_rec_td(
                rows, team, 0.5 + float(rec_next - 1), game_state.game_code
            )
            add(rec_row, BetSide.YES, f"YES — {team} {rec_next}+ receiving TDs")

    return out


def row_floor_label(row: Any) -> str:
    from ..catalog import row_floor

    fl = row_floor(row)
    if fl is None:
        return "?"
    return str(fl)
