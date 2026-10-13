"""Background job runner for the web UI (one job at a time: scan or extract)."""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable

MAX_ERRORS = 50


class Cancelled(Exception):
    pass


class JobRunner:
    """Runs one job at a time in a thread and exposes its progress.

    A job target receives the runner and reports through ``update`` / ``bump``,
    calls ``check_cancelled`` regularly, and may return extra state fields.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict = {"running": False, "kind": None}

    def status(self) -> dict:
        with self._lock:
            s = dict(self._state)
            s["stats"] = dict(s.get("stats") or {})
            s["errors"] = list(s.get("errors") or [])
            s["results"] = list(s.get("results") or [])
            return s

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state["running"]

    def start(self, kind: str, label: str, target: Callable[["JobRunner"], dict | None], **info) -> bool:
        """Start ``target`` in the background. Returns False if a job is already running."""
        with self._lock:
            if self._state["running"]:
                return False
            self._cancel.clear()
            self._state = {
                "kind": kind, "label": label, "running": True, "cancelling": False,
                "started_at": _now(), "finished_at": None, "outcome": None, "error": None,
                "done": 0, "total": 0, "current": None, "stats": {}, "errors": [], **info,
            }
        self._thread = threading.Thread(target=self._run, args=(target,), name=f"bagarium-{kind}", daemon=True)
        self._thread.start()
        return True

    def update(self, **fields) -> None:
        with self._lock:
            self._state.update(fields)

    def bump(self, key: str, n: int = 1) -> None:
        with self._lock:
            stats = self._state["stats"]
            stats[key] = stats.get(key, 0) + n

    def set_stats(self, **values: int) -> None:
        with self._lock:
            self._state["stats"].update(values)

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        with self._lock:
            if self._state["running"]:
                self._state["cancelling"] = True
                self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self, target: Callable[["JobRunner"], dict | None]) -> None:
        outcome, extra = "completed", {}
        try:
            extra = target(self) or {}
        except Cancelled:
            outcome = "cancelled"
        except Exception as e:  # surface unexpected failures in the UI
            outcome = "failed"
            extra = {"error": f"{type(e).__name__}: {e}"}
        with self._lock:
            self._state.update(running=False, cancelling=False, current=None,
                               finished_at=_now(), outcome=outcome, **extra)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
