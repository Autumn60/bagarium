"""In-memory job queue for the web UI: jobs run one after another in a worker thread."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

MAX_ERRORS = 50
MAX_HISTORY = 50
ACTIVE = ("queued", "running")


class Cancelled(Exception):
    pass


class Job:
    """Handle given to a job's target to report progress and check for cancellation."""

    def __init__(self, queue: "JobQueue", job_id: int, kind: str, label: str,
                 target: Callable[["Job"], dict | None], reserved: Iterable[Path], info: dict):
        self._queue = queue
        self._cancel = threading.Event()
        self.target = target
        self.reserved = {Path(p) for p in reserved}
        self.state: dict = {
            "id": job_id, "kind": kind, "label": label, "state": "queued", "cancelling": False,
            "queued_at": _now(), "started_at": None, "finished_at": None, "error": None,
            "done": 0, "total": 0, "current": None, "stats": {}, "errors": [], "results": [], **info,
        }

    @property
    def id(self) -> int:
        return self.state["id"]

    def update(self, **fields) -> None:
        with self._queue._lock:
            self.state.update(fields)

    def bump(self, key: str, n: int = 1) -> None:
        with self._queue._lock:
            stats = self.state["stats"]
            stats[key] = stats.get(key, 0) + n

    def set_stats(self, **values: int) -> None:
        with self._queue._lock:
            self.state["stats"].update(values)

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def snapshot(self) -> dict:
        s = dict(self.state)
        s["stats"] = dict(s["stats"])
        s["errors"] = list(s["errors"])
        s["results"] = list(s["results"])
        return s


class JobQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._jobs: dict[int, Job] = {}
        self._next_id = 1
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------ submitting

    def submit(self, kind: str, label: str, target: Callable[[Job], dict | None],
               reserved: Iterable[Path] = (), **info) -> int:
        """Queue a job. ``reserved`` are output paths it will create (see ``reserved_outputs``)."""
        with self._lock:
            job = Job(self, self._next_id, kind, label, target, reserved, info)
            self._jobs[job.id] = job
            self._next_id += 1
            self._trim_history()
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="bagarium-jobs", daemon=True)
                self._worker.start()
            self._wakeup.notify()
            return job.id

    def cancel(self, job_id: int) -> bool:
        """Drop a queued job, or ask a running one to stop. Returns False if it already finished."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state["state"] not in ACTIVE:
                return False
            if job.state["state"] == "queued":
                job.state.update(state="cancelled", finished_at=_now())
            else:
                job.state["cancelling"] = True
                job._cancel.set()
            return True

    def clear_finished(self) -> None:
        with self._lock:
            for job_id in [i for i, j in self._jobs.items() if j.state["state"] not in ACTIVE]:
                del self._jobs[job_id]

    # ------------------------------------------------------------ inspecting

    def get(self, job_id: int) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.snapshot() if job else None

    def list(self) -> list[dict]:
        """Running first, then queued in run order, then finished (newest first)."""
        with self._lock:
            jobs = [j.snapshot() for j in self._jobs.values()]
        order = {"running": 0, "queued": 1}
        active = sorted((j for j in jobs if j["state"] in ACTIVE), key=lambda j: (order[j["state"]], j["id"]))
        finished = sorted((j for j in jobs if j["state"] not in ACTIVE), key=lambda j: -j["id"])
        return active + finished

    def active_count(self) -> int:
        with self._lock:
            return sum(j.state["state"] in ACTIVE for j in self._jobs.values())

    def reserved_outputs(self) -> set[Path]:
        """Outputs that queued or running jobs are going to create."""
        with self._lock:
            return {p for j in self._jobs.values() if j.state["state"] in ACTIVE for p in j.reserved}

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until nothing is queued or running (for tests and scripts)."""
        import time

        deadline = time.monotonic() + timeout
        while self.active_count():
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    # ------------------------------------------------------------ worker

    def _work(self) -> None:
        while True:
            with self._lock:
                while (job := self._next_queued()) is None:
                    self._wakeup.wait()
                job.state.update(state="running", started_at=_now())
            state, extra = "completed", {}
            try:
                extra = job.target(job) or {}
            except Cancelled:
                state = "cancelled"
            except Exception as e:  # surface unexpected failures in the UI
                state, extra = "failed", {"error": f"{type(e).__name__}: {e}"}
            with self._lock:
                job.state.update(state=state, cancelling=False, current=None, finished_at=_now(), **extra)

    def _next_queued(self) -> Job | None:
        return next((j for j in self._jobs.values() if j.state["state"] == "queued"), None)

    def _trim_history(self) -> None:
        finished = [i for i, j in self._jobs.items() if j.state["state"] not in ACTIVE]
        for job_id in finished[:-MAX_HISTORY] if len(finished) > MAX_HISTORY else []:
            del self._jobs[job_id]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
