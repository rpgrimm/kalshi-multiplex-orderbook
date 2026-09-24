"""Evaluate configured strategies. Never arms or executes."""

from __future__ import annotations

from collections.abc import Sequence

from .models import CandidateBet, GameState
from .strategies.base import Strategy


class StrategyEngine:
    def __init__(self, strategies: Sequence[Strategy] | None = None) -> None:
        self._strategies: list[Strategy] = list(strategies or [])

    def add(self, strategy: Strategy) -> None:
        self._strategies.append(strategy)

    def evaluate(self, game_state: GameState) -> list[CandidateBet]:
        out: list[CandidateBet] = []
        for strategy in self._strategies:
            out.extend(strategy.evaluate(game_state))
        return out
