#!/usr/bin/env python3
"""Engine-boundary tests. No network, no Kalshi."""

from __future__ import annotations

import unittest

from sports_engine.arming_engine import ArmingEngine
from sports_engine.event_store import EventStore
from sports_engine.execution.kalshi import KalshiExecutionBackend
from sports_engine.execution.mock import MockExecutionBackend
from sports_engine.execution_engine import ExecutionEngine
from sports_engine.game_state_engine import GameStateEngine
from sports_engine.models import ArmedStatus, BetSide, EventType, GameEvent
from sports_engine.session import SportsSession
from sports_engine.strategies.example import ScoreOccurredStrategy
from sports_engine.strategy_engine import StrategyEngine


def td(team: str = "HOME") -> GameEvent:
    return GameEvent(type=EventType.TOUCHDOWN, team=team, quarter=1, payload={"points": 6})


class TestSportsEngine(unittest.TestCase):
    def test_event_updates_score_and_replay_matches(self) -> None:
        store = EventStore()
        engine = GameStateEngine(game_code="26SEP24ATLGB", away="ATL", home="GB")
        e1 = store.append(td("HOME"))
        e2 = store.append(GameEvent(type=EventType.FIELD_GOAL, team="AWAY", payload={"points": 3}))
        engine.apply_event(e1)
        engine.apply_event(e2)
        live = engine.get_state()
        self.assertEqual(live.home_score, 6)
        self.assertEqual(live.away_score, 3)
        self.assertEqual(live.home_stats.touchdowns, 1)
        self.assertEqual(live.away_stats.field_goals, 1)

        other = GameStateEngine(game_code="26SEP24ATLGB", away="ATL", home="GB")
        replayed = other.replay(store.events())
        self.assertEqual(replayed.home_score, live.home_score)
        self.assertEqual(replayed.away_score, live.away_score)
        self.assertEqual(replayed.home_stats.touchdowns, live.home_stats.touchdowns)
        self.assertEqual(replayed.event_count, 2)

    def test_strategy_emits_candidate_without_executing(self) -> None:
        state_engine = GameStateEngine()
        strategies = StrategyEngine([ScoreOccurredStrategy()])
        mock = MockExecutionBackend()
        execution = ExecutionEngine(backend=mock)

        self.assertEqual(strategies.evaluate(state_engine.get_state()), [])
        state_engine.apply_event(td("HOME"))
        cands = strategies.evaluate(state_engine.get_state())
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].side, BetSide.YES)
        self.assertEqual(cands[0].market_id, "EXAMPLE-SCORE-OCCURRED")

        sent = execution.evaluate([], state_engine.get_state())
        self.assertEqual(sent, [])
        self.assertEqual(mock.submitted, [])

    def test_arming_is_explicit_and_execution_ignores_unarmed(self) -> None:
        session = SportsSession()
        cands = session.ingest(td("HOME"))
        self.assertEqual(len(cands), 1)
        self.assertEqual(session.arming.armed_bets(), [])
        self.assertEqual(session.run_execution(), [])
        if session.mock is not None:
            self.assertEqual(session.mock.submitted, [])

        armed = session.arm(cands[0].candidate_id, quantity=2)
        self.assertEqual(armed.status, ArmedStatus.ARMED)
        self.assertEqual(armed.quantity, 2)
        self.assertEqual(armed.candidate_id, cands[0].candidate_id)

        sent = session.run_execution()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].market_id, "EXAMPLE-SCORE-OCCURRED")
        self.assertEqual(sent[0].quantity, 2)
        self.assertTrue(sent[0].dry_run)
        self.assertEqual(armed.status, ArmedStatus.ORDER_SENT)
        if session.mock is not None:
            self.assertEqual(len(session.mock.submitted), 1)

    def test_kalshi_backend_refuses(self) -> None:
        session = SportsSession()
        cands = session.ingest(td("HOME"))
        armed = session.arm(cands[0].candidate_id)
        engine = ExecutionEngine(backend=KalshiExecutionBackend())
        with self.assertRaises(RuntimeError):
            engine.evaluate([armed], session.state())

    def test_disarm_prevents_execution(self) -> None:
        arming = ArmingEngine()
        session = SportsSession(arming=arming)
        cands = session.ingest(td("HOME"))
        armed = session.arm(cands[0].candidate_id)
        session.arming.disarm(armed.armed_id)
        self.assertEqual(session.run_execution(), [])


if __name__ == "__main__":
    unittest.main()
