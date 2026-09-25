"""Thin wiring for the CLI demo. Not a god-object: engines stay injectable."""

from __future__ import annotations

from .arming_engine import ArmingEngine
from .event_store import EventStore
from .execution.mock import MockExecutionBackend
from .execution_engine import ExecutionEngine
from .game_state_engine import GameStateEngine
from collections.abc import Sequence

from dataclasses import dataclass, field

from .models import ArmedBet, CandidateBet, GameEvent, GameState, SimulatedOrder
from .strategies.example import ScoreOccurredStrategy
from .strategy_engine import StrategyEngine


@dataclass
class PlayRecord:
    kind: str
    label: str
    n_events: int
    sent: list[dict] = field(default_factory=list)
    pending_extra_before: str | None = None


class SportsSession:
    def __init__(
        self,
        *,
        game_code: str = "",
        away: str = "AWAY",
        home: str = "HOME",
        store: EventStore | None = None,
        state_engine: GameStateEngine | None = None,
        strategy_engine: StrategyEngine | None = None,
        arming: ArmingEngine | None = None,
        execution: ExecutionEngine | None = None,
    ) -> None:
        self.store = store or EventStore()
        self.state_engine = state_engine or GameStateEngine(
            game_code=game_code, away=away, home=home
        )
        self.strategy_engine = strategy_engine or StrategyEngine([ScoreOccurredStrategy()])
        self.arming = arming or ArmingEngine()
        backend = MockExecutionBackend()
        self.execution = execution or ExecutionEngine(backend=backend)
        self.mock = backend if isinstance(self.execution.backend, MockExecutionBackend) else None
        self.sent_markets: set[str] = set()
        self.pending_extra_team: str | None = None
        self.plays: list[PlayRecord] = []

    def mark_sent(self, tickers: Sequence[str]) -> None:
        for t in tickers:
            if t:
                self.sent_markets.add(str(t).upper())

    def record_play(
        self,
        *,
        kind: str,
        label: str,
        n_events: int,
        sent: Sequence[dict] | None = None,
        pending_extra_before: str | None = None,
    ) -> PlayRecord:
        play = PlayRecord(
            kind=kind,
            label=label,
            n_events=max(0, int(n_events)),
            sent=[dict(x) for x in (sent or [])],
            pending_extra_before=pending_extra_before,
        )
        self.plays.append(play)
        return play

    def rebuild_sent_markets(self) -> None:
        self.sent_markets = {
            str(leg.get("ticker") or "").upper()
            for play in self.plays
            for leg in play.sent
            if leg.get("ticker")
        }

    def rollback_last_play(self) -> PlayRecord | None:
        if not self.plays:
            return None
        play = self.plays.pop()
        if play.n_events:
            self.store.pop_last(play.n_events)
        self.replay()
        self.pending_extra_team = play.pending_extra_before
        self.rebuild_sent_markets()
        return play

    def ingest(self, event: GameEvent, *, evaluate: bool = True) -> list[CandidateBet]:
        stored = self.store.append(event)
        self.state_engine.apply_event(stored)
        if not evaluate:
            return []
        candidates = self.strategy_engine.evaluate(self.state_engine.get_state())
        self.arming.observe(candidates)
        return candidates

    def state(self) -> GameState:
        return self.state_engine.get_state()

    def replay(self) -> GameState:
        return self.state_engine.replay(self.store.events())

    def arm(
        self,
        candidate_id: str,
        *,
        quantity: int | None = None,
        max_buy_cents: int | None = None,
        min_sell_cents: int | None = None,
    ) -> ArmedBet:
        return self.arming.arm(
            candidate_id,
            quantity=quantity,
            max_buy_cents=max_buy_cents,
            min_sell_cents=min_sell_cents,
        )

    def run_execution(self) -> list[SimulatedOrder]:
        return self.execution.evaluate(self.arming.armed_bets(), self.state_engine.get_state())
