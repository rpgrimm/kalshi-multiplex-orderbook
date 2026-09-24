"""Rebuild GameState only by applying GameEvents."""

from __future__ import annotations

from collections.abc import Sequence

from .models import EventType, GameEvent, GameState, PlayerTdStat, TeamStats


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
        if event.clock is not None:
            state.clock = event.clock
        if event.type is EventType.POSSESSION and event.team:
            state.possession = event.team.upper()

        if event.type is EventType.SCORE:
            stats = state.team_stats(event.team)
            if stats is not None and "set" in event.payload:
                stats.score = max(0, int(event.payload.get("set") or 0))
        elif event.type is EventType.QUARTER and event.payload.get("end"):
            away_q = state.away_score - state.away_score_at_q_start
            home_q = state.home_score - state.home_score_at_q_start
            event.payload["ended_quarter"] = state.quarter
            event.payload["away_this_q"] = away_q
            event.payload["home_this_q"] = home_q
            event.payload["points_this_q"] = away_q + home_q
            if state.quarter < 4:
                state.quarter += 1
            state.away_score_at_q_start = state.away_score
            state.home_score_at_q_start = state.home_score
        elif event.type is EventType.QUARTER and event.quarter is not None:
            q = max(1, min(4, int(event.quarter)))
            state.quarter = q
            state.away_score_at_q_start = state.away_score
            state.home_score_at_q_start = state.home_score
        elif event.quarter is not None:
            state.quarter = int(event.quarter)

        stats = state.team_stats(event.team)
        if stats is not None and event.type is not EventType.SCORE:
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

        if event.type is EventType.TOUCHDOWN and event.player:
            key = event.player.lower()
            player = state.players.get(key)
            if player is None:
                player = PlayerTdStat(name=event.player)
                state.players[key] = player
            intent = str(event.payload.get("intent") or "receiving")
            if intent == "rush":
                player.rush_td += 1
            else:
                player.rec_td += 1
                team_key = str(event.payload.get("team_key") or event.team or "").lower()
                if team_key:
                    state.team_rec_tds[team_key] = state.team_rec_tds.get(team_key, 0) + 1

        state.event_count += 1
        state.last_event_id = event.event_id
        state.last_event = event
        return self.get_state()

    def replay(self, events: Sequence[GameEvent]) -> GameState:
        self.reset()
        for event in events:
            self.apply_event(event)
        return self.get_state()
