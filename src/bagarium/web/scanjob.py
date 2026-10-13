"""Background scan runner for the web UI (one scan at a time)."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from .. import catalog, db

MAX_ERRORS = 50


class ScanCancelled(Exception):
    pass


class ScanJob:
    def __init__(self, db_path: str | Path | None):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict = {"running": False, "finished_at": None}

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "errors": list(self._state.get("errors", []))}

    @property
    def running(self) -> bool:
        with self._lock:
            return self._state["running"]

    def start(self, roots: list[str], force: bool = False) -> bool:
        """Start a scan in the background. Returns False if one is already running."""
        with self._lock:
            if self._state["running"]:
                return False
            self._cancel.clear()
            self._state = {
                "running": True, "cancelling": False, "roots": roots, "force": force,
                "started_at": _now(), "finished_at": None,
                "done": 0, "total": 0, "current": None,
                "added": 0, "updated": 0, "moved": 0, "unchanged": 0, "missing": 0,
                "error_count": 0, "errors": [], "outcome": None,
            }
        self._thread = threading.Thread(target=self._run, args=(roots, force), name="bagarium-scan", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        with self._lock:
            if self._state["running"]:
                self._state["cancelling"] = True
                self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _on_bag(self, path: Path, status: str, done: int, total: int) -> None:
        with self._lock:
            s = self._state
            s["done"], s["total"], s["current"] = done, total, str(path)
            if status == "error":
                s["error_count"] += 1
            elif status in ("added", "updated", "moved", "unchanged"):
                s[status] += 1
        if self._cancel.is_set():
            raise ScanCancelled()

    def _run(self, roots: list[str], force: bool) -> None:
        outcome, missing, errors = "completed", 0, None
        conn = None
        try:
            conn = db.connect(self.db_path)
            result = catalog.scan(conn, roots, force=force, on_bag=self._on_bag)
            missing, errors = result.missing, result.errors
        except ScanCancelled:
            outcome = "cancelled"
        except Exception as e:  # surface unexpected failures in the UI
            outcome = "failed"
            errors = [("", f"{type(e).__name__}: {e}")]
        finally:
            if conn is not None:
                conn.close()
        with self._lock:
            s = self._state
            s.update(running=False, cancelling=False, current=None, finished_at=_now(), outcome=outcome, missing=missing)
            if errors is not None:  # not available when the scan was cancelled
                s["errors"] = [{"path": str(p), "message": m} for p, m in errors[:MAX_ERRORS]]
                s["error_count"] = len(errors)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
