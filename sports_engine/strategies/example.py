"""Trivial example: any score makes a YES candidate on a fake market.

Not a real Kalshi strategy. Exists to prove candidate != armed != executed.
"""

from __future__ import annotations

from ..models import BetSide, CandidateBet, CandidateStatus, GameState


class ScoreOccurredStrategy:
    id = "example_score_occurred"
    market_id = "EXAMPLE-SCORE-OCCURRED"

    def evaluate(self, game_state: GameState) -> list[CandidateBet]:
        total = int(game_state.away_score) + int(game_state.home_score)
        if total <= 0:
            return []
        return [
            CandidateBet(
                strategy_id=self.id,
                market_id=self.market_id,
                side=BetSide.YES,
                reason=f"score is {game_state.away} {game_state.away_score}-{game_state.home_score} {game_state.home}",
                trigger="total_score > 0",
                suggested_quantity=1,
                status=CandidateStatus.ELIGIBLE,
            )
        ]
