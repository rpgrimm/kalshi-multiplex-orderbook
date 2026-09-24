"""Evaluate armed bets only. No sports strategy and no candidate execution."""

from __future__ import annotations

from collections.abc import Sequence

from .execution.base import ExecutionBackend
from .execution.mock import MockExecutionBackend
from .models import ArmedBet, ArmedStatus, GameState, SimulatedOrder


class ExecutionEngine:
    def __init__(self, backend: ExecutionBackend | None = None) -> None:
        self.backend: ExecutionBackend = backend or MockExecutionBackend()

    def trigger_satisfied(self, bet: ArmedBet, game_state: GameState) -> bool:
        if bet.trigger == "total_score > 0":
            return (game_state.away_score + game_state.home_score) > 0
        if bet.trigger.startswith("td:") or bet.trigger == "quarter_end":
            return True
        if not bet.trigger or bet.trigger == "always":
            return True
        return False

    def evaluate(
        self,
        armed_bets: Sequence[ArmedBet],
        game_state: GameState,
    ) -> list[SimulatedOrder]:
        sent: list[SimulatedOrder] = []
        for bet in armed_bets:
            if bet.status is not ArmedStatus.ARMED:
                continue
            if not self.trigger_satisfied(bet, game_state):
                continue
            bet.status = ArmedStatus.TRIGGERED
            order = self.backend.submit(bet, game_state)
            bet.status = ArmedStatus.ORDER_SENT
            bet.last_result = order.note
            sent.append(order)
        return sent
