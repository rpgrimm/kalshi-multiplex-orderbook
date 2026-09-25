"""Minimal CLI vertical slice. Mock execution only — never Kalshi."""

from __future__ import annotations

import argparse

from .models import EventType, GameEvent
from .session import SportsSession


def run_engine_demo(args: argparse.Namespace, *, game_code: str, seed_series: str) -> int:
    session = SportsSession(game_code=game_code, away="AWAY", home="HOME")
    print(f"engine-demo  env={getattr(args, 'kalshi_env', '?')}  game={seed_series}-{game_code}")
    print("backend=MockExecutionBackend  (no Kalshi orders)")
    print()

    event = GameEvent(
        type=EventType.TOUCHDOWN,
        team="HOME",
        player="example.scorer",
        quarter=1,
        clock="12:34",
        source="engine-demo",
        raw="demo touchdown",
        payload={"points": 6},
    )
    candidates = session.ingest(event)
    state = session.state()
    print("1. event ingested:", event.type.value, event.team, f"+{event.points()}")
    print(f"2. game state: Q{state.quarter} {state.away} {state.away_score}-{state.home_score} {state.home} tds_home={state.home_stats.touchdowns}")
    print(f"3. candidates ({len(candidates)}) — not executable:")
    if not candidates:
        print("   (none)")
        return 1
    for cand in candidates:
        print(f"   {cand.candidate_id}  {cand.side.value} {cand.market_id}  {cand.reason}")

    before_exec = session.run_execution()
    print(f"4. execution on unarmed candidates: sent={len(before_exec)} (must be 0)")

    armed = session.arm(candidates[0].candidate_id, quantity=int(getattr(args, "count_yes", None) or getattr(args, "count", 1) or 1))
    print(f"5. armed {armed.armed_id}  status={armed.status.value}  qty={armed.quantity}")

    sent = session.run_execution()
    print(f"6. execution after arm: sent={len(sent)}")
    for order in sent:
        print(f"   {order.note}")

    replayed = session.replay()
    same = (
        replayed.away_score == state.away_score
        and replayed.home_score == state.home_score
        and replayed.home_stats.touchdowns == state.home_stats.touchdowns
    )
    print(f"7. replay from event store matches state: {same}")
    print(f"8. history events: {len(session.store.events())}")
    return 0 if sent and same and not before_exec else 1
