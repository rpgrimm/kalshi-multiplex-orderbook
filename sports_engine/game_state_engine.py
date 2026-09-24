"""Rebuild GameState only by applying GameEvents."""

from __future__ import annotations

from collections.abc import Sequence

from .models import EventType, GameEvent, GameState, TeamStats


class GameStateEngine:
    def __init__(self, *, game_code: str = "", away: str = "AWAY", home: str = "HOME") -> None:
        self._template = {"game_code": game_code, "away": away.upper(), "home": home.upper()}
        self._state = self._blank()

    def _blank(self) -> GameState:
        return GameState(
            game_code=self._template["game_code"],
            away=self._template["away"],
            home=self._template["home"],
            away_stats=TeamStats(),
            home_stats=TeamStats(),
        )

    def get_state(self) -> GameState:
        return self._state.snapshot()

    def reset(self) -> None:
        self._state = self._blank()

    def apply_event(self, event: GameEvent) -> GameState:
        state = self._state
        if event.quarter is not None:
            state.quarter = int(event.quarter)
        if event.clock is not None:
            state.clock = event.clock
        if event.type is EventType.POSSESSION and event.team:
            state.possession = event.team.upper()
        if event.type is EventType.QUARTER and event.quarter is not None:
            state.quarter = int(event.quarter)

        stats = state.team_stats(event.team)
        if stats is not None:
            pts = event.points()
            if pts:
                stats.score += pts
            if event.type is EventType.TOUCHDOWN:
                stats.touchdowns += 1
            elif event.type is EventType.FIELD_GOAL:
                stats.field_goals += 1
            elif event.type is EventType.TURNOVER:
                stats.turnovers += 1
            elif event.type is EventType.SACK:
                stats.sacks += 1
            elif event.type is EventType.RUSH:
                stats.rush_attempts += 1
                stats.rush_yards += int(event.payload.get("yards") or 0)
            elif event.type is EventType.PASS:
                if event.payload.get("complete", True):
                    stats.pass_completions += 1
                stats.pass_yards += int(event.payload.get("yards") or 0)

        state.event_count += 1
        state.last_event_id = event.event_id
        return self.get_state()

    def replay(self, events: Sequence[GameEvent]) -> GameState:
        self.reset()
        for event in events:
            self.apply_event(event)
        return self.get_state()
