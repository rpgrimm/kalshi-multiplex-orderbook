"""Shared dataclasses for the sports engine pipeline."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    TOUCHDOWN = "touchdown"
    FIELD_GOAL = "field_goal"
    EXTRA_POINT = "extra_point"
    SAFETY = "safety"
    TURNOVER = "turnover"
    SACK = "sack"
    RUSH = "rush"
    PASS = "pass"
    POSSESSION = "possession"
    QUARTER = "quarter"
    CLOCK = "clock"
    SCORE = "score"
    OTHER = "other"


class BetSide(str, Enum):
    YES = "yes"
    NO = "no"


class CandidateStatus(str, Enum):
    WATCHING = "watching"
    ELIGIBLE = "eligible"


class ArmedStatus(str, Enum):
    INACTIVE = "inactive"
    WATCHING = "watching"
    ELIGIBLE = "eligible"
    ARMED = "armed"
    TRIGGERED = "triggered"
    ORDER_SENT = "order_sent"
    PARTIAL = "partial"
    FILLED = "filled"
    MISSED = "missed"
    CANCELLED = "cancelled"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class GameEvent:
    """Something that happened. New types can use EventType.OTHER + payload."""

    type: EventType = EventType.OTHER
    team: str | None = None
    player: str | None = None
    quarter: int | None = None
    clock: str | None = None
    ts: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    raw: str | None = None
    event_id: str = field(default_factory=lambda: _new_id("evt"))
    seq: int | None = None

    def points(self) -> int:
        if "points" in self.payload:
            try:
                return int(self.payload["points"])
            except (TypeError, ValueError):
                return 0
        if self.type is EventType.TOUCHDOWN:
            return 6
        if self.type is EventType.FIELD_GOAL:
            return 3
        if self.type is EventType.EXTRA_POINT:
            return 1
        if self.type is EventType.SAFETY:
            return 2
        return 0


@dataclass
class TeamStats:
    score: int = 0
    touchdowns: int = 0
    field_goals: int = 0
    turnovers: int = 0
    sacks: int = 0
    rush_attempts: int = 0
    rush_yards: int = 0
    pass_completions: int = 0
    pass_yards: int = 0


@dataclass
class PlayerTdStat:
    name: str
    rec_td: int = 0
    rush_td: int = 0

    @property
    def total_td(self) -> int:
        return int(self.rec_td) + int(self.rush_td)


@dataclass
class GameState:
    game_code: str = ""
    away: str = "AWAY"
    home: str = "HOME"
    quarter: int = 1
    clock: str | None = None
    possession: str | None = None
    away_stats: TeamStats = field(default_factory=TeamStats)
    home_stats: TeamStats = field(default_factory=TeamStats)
    event_count: int = 0
    last_event_id: str | None = None
    last_event: GameEvent | None = None
    players: dict[str, PlayerTdStat] = field(default_factory=dict)
    team_rec_tds: dict[str, int] = field(default_factory=dict)
    away_score_at_q_start: int = 0
    home_score_at_q_start: int = 0
    away_score_at_half: int = 0
    home_score_at_half: int = 0

    @property
    def game_tds(self) -> int:
        return int(self.away_stats.touchdowns) + int(self.home_stats.touchdowns)

    @property
    def points_this_quarter(self) -> int:
        return (self.away_score + self.home_score) - (
            self.away_score_at_q_start + self.home_score_at_q_start
        )

    def points_this_half(self) -> int:
        if self.quarter <= 2:
            return self.away_score + self.home_score
        return (self.away_score + self.home_score) - (
            self.away_score_at_half + self.home_score_at_half
        )

    def team_points_this_half(self, team: str | None) -> int:
        stats = self.team_stats(team)
        if stats is None:
            return 0
        if self.quarter <= 2:
            return int(stats.score)
        half = (
            self.away_score_at_half
            if str(team or "").upper() in {self.away.upper(), "AWAY"}
            else self.home_score_at_half
        )
        return int(stats.score) - int(half)

    @property
    def away_score(self) -> int:
        return self.away_stats.score

    @property
    def home_score(self) -> int:
        return self.home_stats.score

    def snapshot(self) -> "GameState":
        """Independent copy for replay comparisons."""
        import copy

        return copy.deepcopy(self)

    def team_stats(self, team: str | None) -> TeamStats | None:
        if not team:
            return None
        key = team.upper()
        if key in {self.away.upper(), "AWAY"}:
            return self.away_stats
        if key in {self.home.upper(), "HOME"}:
            return self.home_stats
        return None


@dataclass
class CandidateBet:
    """A strategy thought this might be a trade. Not permission to spend."""

    strategy_id: str
    market_id: str
    side: BetSide
    reason: str
    trigger: str = ""
    suggested_price_cents: int | None = None
    suggested_quantity: int | None = None
    status: CandidateStatus = CandidateStatus.ELIGIBLE
    candidate_id: str = field(default_factory=lambda: _new_id("cand"))


@dataclass
class ArmedBet:
    """Explicit permission to execute if trigger + constraints hold."""

    candidate_id: str
    strategy_id: str
    market_id: str
    side: BetSide
    quantity: int
    trigger: str = ""
    max_buy_cents: int | None = None
    min_sell_cents: int | None = None
    armed_ts: float = field(default_factory=time.time)
    status: ArmedStatus = ArmedStatus.ARMED
    armed_id: str = field(default_factory=lambda: _new_id("arm"))
    last_result: str | None = None


@dataclass
class SimulatedOrder:
    """What a backend would have submitted. No network."""

    market_id: str
    side: BetSide
    quantity: int
    price_cents: int | None
    armed_id: str
    dry_run: bool = True
    note: str = ""
