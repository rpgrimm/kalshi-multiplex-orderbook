from __future__ import annotations

from typing import Protocol

from ..models import ArmedBet, GameState, SimulatedOrder


class ExecutionBackend(Protocol):
    def submit(self, bet: ArmedBet, game_state: GameState) -> SimulatedOrder:
        ...
