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
