"""Append-only GameEvent history. In-memory for the skeleton."""

from __future__ import annotations

from .models import GameEvent


class EventStore:
    def __init__(self) -> None:
        self._events: list[GameEvent] = []

    def append(self, event: GameEvent) -> GameEvent:
        event.seq = len(self._events) + 1
        self._events.append(event)
        return event

    def events(self) -> list[GameEvent]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()

    def pop_last(self, n: int = 1) -> list[GameEvent]:
        n = max(0, int(n))
        if n <= 0 or not self._events:
            return []
        take = min(n, len(self._events))
        popped = self._events[-take:]
        del self._events[-take:]
        return popped
