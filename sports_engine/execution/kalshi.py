"""Placeholder Kalshi backend. Not wired; refuses to send orders."""

from __future__ import annotations

from ..models import ArmedBet, GameState, SimulatedOrder


class KalshiExecutionBackend:
    def submit(self, bet: ArmedBet, game_state: GameState) -> SimulatedOrder:
        raise RuntimeError(
            "KalshiExecutionBackend is not implemented; sports_engine defaults "
            "to MockExecutionBackend so no real orders are sent"
        )
