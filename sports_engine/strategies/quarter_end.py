"""Quarter end: Q winner/spreads/totals. Q2 also settles 1H; Q4 settles 2H."""

from __future__ import annotations

from typing import Any, Sequence

from ..catalog import (
    NCAAF_H_SPREAD_SERIES,
    NCAAF_H_TEAM_TOTAL_SERIES,
    NCAAF_H_TOTAL_SERIES,
    NCAAF_H_WINNER_SERIES,
    NCAAF_Q_SPREAD_SERIES,
    NCAAF_Q_TOTAL_SERIES,
    NCAAF_Q_WINNER_SERIES,
    NFL_H_SPREAD_SERIES,
    NFL_H_TEAM_TOTAL_SERIES,
    NFL_H_TOTAL_SERIES,
    NFL_H_WINNER_SERIES,
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


def _margin(away_pts: int, home_pts: int, away: str, home: str) -> tuple[str | None, str | None, int]:
    if away_pts > home_pts:
        return away, home, away_pts - home_pts
    if home_pts > away_pts:
        return home, away, home_pts - away_pts
    return None, None, 0


def _settle_period(
    rows: Sequence[Any],
    game_state: GameState,
    *,
    away_pts: int,
    home_pts: int,
    winner_series: str | None,
    spread_series: str | None,
    total_series: str | None,
    team_total_series: str | None,
    label: str,
    sent_ids: set[str],
    out: list[CandidateBet],
) -> None:
    away, home = game_state.away, game_state.home
    winner, _loser, margin = _margin(away_pts, home_pts, away, home)
    total = away_pts + home_pts
    team_pts = {away: away_pts, home: home_pts}

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

    if winner_series:
        for row in rows:
            if row_series(row) != winner_series:
                continue
            mid = market_id(row)
            suffix = mid.rsplit("-", 1)[-1]
            team = team_abbrev_from_row(row, game_state.game_code)
            if suffix == "TIE" or (team is None and "TIE" in suffix):
                add(
                    row,
                    BetSide.YES if winner is None else BetSide.NO,
                    f"YES — {label} tie" if winner is None else f"NO — {label} tie",
                )
            elif team == winner:
                add(row, BetSide.YES, f"YES — {team} wins {label}")
            elif team in {away, home}:
                add(row, BetSide.NO, f"NO — {team} wins {label}")

    if spread_series:
        for row in rows:
            if row_series(row) != spread_series:
                continue
            team = team_abbrev_from_row(row, game_state.game_code)
            floor = row_floor(row)
            if team is None or floor is None:
                continue
            if winner is not None and team == winner and margin > floor:
                add(row, BetSide.YES, f"YES — {team} {label} by {floor}+")
            else:
                add(row, BetSide.NO, f"NO — {team} {label} by {floor}+")

    if total_series:
        for row in rows:
            if row_series(row) != total_series:
                continue
            floor = row_floor(row)
            if floor is None:
                continue
            if total > floor:
                add(row, BetSide.YES, f"YES — {label} over {floor}")
            else:
                add(row, BetSide.NO, f"NO — {label} over {floor}")

    if team_total_series:
        for row in rows:
            if row_series(row) != team_total_series:
                continue
            team = team_abbrev_from_row(row, game_state.game_code)
            floor = row_floor(row)
            if team is None or floor is None:
                continue
            pts = team_pts.get(team, 0)
            if pts > floor:
                add(row, BetSide.YES, f"YES — {team} {label} over {floor}")
            else:
                add(row, BetSide.NO, f"NO — {team} {label} over {floor}")


def preview_qend(
    rows: Sequence[Any],
    game_state: GameState,
    *,
    sent: set[str] | None = None,
    ended_quarter: int | None = None,
    away_this_q: int | None = None,
    home_this_q: int | None = None,
    away_this_h: int | None = None,
    home_this_h: int | None = None,
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
    out: list[CandidateBet] = []
    _settle_period(
        rows,
        game_state,
        away_pts=away_q,
        home_pts=home_q,
        winner_series=(NCAAF_Q_WINNER_SERIES if college else NFL_Q_WINNER_SERIES).get(ended),
        spread_series=(NCAAF_Q_SPREAD_SERIES if college else NFL_Q_SPREAD_SERIES).get(ended),
        total_series=(NCAAF_Q_TOTAL_SERIES if college else Q_TOTAL_SERIES).get(ended),
        team_total_series=None,
        label=f"Q{ended}",
        sent_ids=sent_ids,
        out=out,
    )
    half = 1 if ended == 2 else 2 if ended == 4 else None
    if half is not None:
        if away_this_h is not None and home_this_h is not None:
            away_h, home_h = int(away_this_h), int(home_this_h)
        elif half == 1:
            away_h, home_h = int(game_state.away_score), int(game_state.home_score)
        else:
            away_h = int(game_state.away_score - game_state.away_score_at_half)
            home_h = int(game_state.home_score - game_state.home_score_at_half)
        label = "1H" if half == 1 else "2H"
        _settle_period(
            rows,
            game_state,
            away_pts=away_h,
            home_pts=home_h,
            winner_series=(NCAAF_H_WINNER_SERIES if college else NFL_H_WINNER_SERIES).get(half),
            spread_series=(NCAAF_H_SPREAD_SERIES if college else NFL_H_SPREAD_SERIES).get(half),
            total_series=(NCAAF_H_TOTAL_SERIES if college else NFL_H_TOTAL_SERIES).get(half),
            team_total_series=(
                NCAAF_H_TEAM_TOTAL_SERIES if college else NFL_H_TEAM_TOTAL_SERIES
            ).get(half),
            label=label,
            sent_ids=sent_ids,
            out=out,
        )
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
            away_this_h=event.payload.get("away_this_h"),
            home_this_h=event.payload.get("home_this_h"),
        )
