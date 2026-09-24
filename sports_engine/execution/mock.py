"""Dry-run backend. Records intended orders. Never talks to Kalshi."""

from __future__ import annotations

from ..models import ArmedBet, GameState, SimulatedOrder


class MockExecutionBackend:
    def __init__(self) -> None:
        self.submitted: list[SimulatedOrder] = []

    def submit(self, bet: ArmedBet, game_state: GameState) -> SimulatedOrder:
        order = SimulatedOrder(
            market_id=bet.market_id,
            side=bet.side,
            quantity=bet.quantity,
            price_cents=bet.max_buy_cents,
            armed_id=bet.armed_id,
            dry_run=True,
            note=f"mock {bet.side.value} {bet.market_id} x{bet.quantity} (score {game_state.away_score}-{game_state.home_score})",
        )
        self.submitted.append(order)
        return order
