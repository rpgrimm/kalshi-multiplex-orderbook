"""Sports betting engines: events → state → candidates → arming → execution.

Invariant: stats describe reality; strategies interpret; arming grants
permission; execution spends money. Strategy code never calls Kalshi.
"""

from .arming_engine import ArmingEngine
from .event_store import EventStore
from .execution_engine import ExecutionEngine
from .game_state_engine import GameStateEngine
from .models import (
    ArmedBet,
    ArmedStatus,
    BetSide,
    CandidateBet,
    CandidateStatus,
    EventType,
    GameEvent,
    GameState,
    SimulatedOrder,
)
from .session import SportsSession
from .strategy_engine import StrategyEngine

__all__ = [
    "ArmingEngine",
    "ArmedBet",
    "ArmedStatus",
    "BetSide",
    "CandidateBet",
    "CandidateStatus",
    "EventStore",
    "EventType",
    "ExecutionEngine",
    "GameEvent",
    "GameState",
    "GameStateEngine",
    "SimulatedOrder",
    "SportsSession",
    "StrategyEngine",
]
