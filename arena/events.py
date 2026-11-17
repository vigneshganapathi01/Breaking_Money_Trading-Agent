"""In-process event bus for the live debate dashboard.

The debate engine publishes events (trigger, each agent reply, judge verdict,
engine result, research, market updates). The control panel streams them to
the browser with Server-Sent Events. Events are also persisted in SQLite by
the debate engine, so the dashboard can rebuild history after a restart.
"""
from __future__ import annotations

import collections
import threading
from datetime import datetime


class EventBus:
    def __init__(self, maxlen: int = 1000):
        self._events: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self._cond = threading.Condition()
        self._seq = 0

    def publish(self, kind: str, data: dict, ts: datetime | None = None) -> dict:
        with self._cond:
            self._seq += 1
            ev = {"seq": self._seq, "kind": kind, "ts": (ts or datetime.now()).isoformat(timespec="seconds"), "data": data}
            self._events.append(ev)
            self._cond.notify_all()
            return ev

    def since(self, seq: int) -> list[dict]:
        with self._cond:
            return [e for e in self._events if e["seq"] > seq]

    def wait(self, seq: int, timeout: float) -> list[dict]:
        """Block until there are events newer than `seq` or the timeout passes."""
        with self._cond:
            if self._seq <= seq:
                self._cond.wait(timeout)
            return [e for e in self._events if e["seq"] > seq]

    @property
    def last_seq(self) -> int:
        return self._seq


BUS = EventBus()
