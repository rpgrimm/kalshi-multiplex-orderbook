"""Pluggable strategy interface. Implementations must not execute orders."""

from __future__ import annotations

from typing import Protocol

from ..models import CandidateBet, GameState


class Strategy(Protocol):
    id: str

    def evaluate(self, game_state: GameState) -> list[CandidateBet]:
        ...
