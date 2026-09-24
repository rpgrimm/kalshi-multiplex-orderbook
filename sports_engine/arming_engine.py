"""Candidates are observations. Armed bets are explicit permission."""

from __future__ import annotations

from .models import ArmedBet, ArmedStatus, CandidateBet


class ArmingEngine:
    def __init__(self) -> None:
        self._candidates: dict[str, CandidateBet] = {}
        self._armed: dict[str, ArmedBet] = {}

    def observe(self, candidates: list[CandidateBet]) -> None:
        """Replace the latest candidate snapshot. Does not arm anything."""
        self._candidates = {c.candidate_id: c for c in candidates}

    def candidates(self) -> list[CandidateBet]:
        return list(self._candidates.values())

    def armed_bets(self) -> list[ArmedBet]:
        return [b for b in self._armed.values() if b.status is not ArmedStatus.CANCELLED]

    def arm(
        self,
        candidate_id: str,
        *,
        quantity: int | None = None,
        max_buy_cents: int | None = None,
        min_sell_cents: int | None = None,
    ) -> ArmedBet:
        cand = self._candidates.get(candidate_id)
        if cand is None:
            raise KeyError(f"unknown candidate {candidate_id}")
        for existing in self.armed_bets():
            if existing.market_id == cand.market_id and existing.side == cand.side:
                return existing
        qty = int(quantity if quantity is not None else (cand.suggested_quantity or 1))
        bet = ArmedBet(
            candidate_id=cand.candidate_id,
            strategy_id=cand.strategy_id,
            market_id=cand.market_id,
            side=cand.side,
            quantity=qty,
            trigger=cand.trigger,
            max_buy_cents=max_buy_cents if max_buy_cents is not None else cand.suggested_price_cents,
            min_sell_cents=min_sell_cents,
            status=ArmedStatus.ARMED,
        )
        self._armed[bet.armed_id] = bet
        return bet

    def disarm(self, armed_id: str) -> ArmedBet:
        bet = self._armed.get(armed_id)
        if bet is None:
            raise KeyError(f"unknown armed bet {armed_id}")
        bet.status = ArmedStatus.CANCELLED
        return bet

    def get(self, armed_id: str) -> ArmedBet | None:
        return self._armed.get(armed_id)

    def drop_stale(self, valid_market_ids: set[str]) -> list[ArmedBet]:
        dropped: list[ArmedBet] = []
        for bet in self.armed_bets():
            if bet.market_id not in valid_market_ids:
                bet.status = ArmedStatus.CANCELLED
                dropped.append(bet)
        return dropped
