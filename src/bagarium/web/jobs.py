"""In-memory job queue for the web UI.

Jobs run in lanes: each lane has its own worker threads and runs its jobs in
order. File operations (scan, extract, move, import) share the "main" lane and
run one at a time; video encoding has its own lane so it neither blocks them
nor waits for them, and runs a few jobs in parallel.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

MAX_ERRORS = 50
MAX_HISTORY = 50
ACTIVE = ("queued", "running")


def default_lanes() -> dict[str, int]:
    video = os.environ.get("BAGARIUM_VIDEO_WORKERS")
    return {"main": 1, "video": int(video) if video else max(1, min(3, (os.cpu_count() or 4) // 4))}


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
    def __init__(self, lanes: dict[str, int] | None = None) -> None:
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._jobs: dict[int, Job] = {}
        self._next_id = 1
        self.lanes = lanes or default_lanes()
        self._workers: dict[str, list[threading.Thread]] = {}

    # ------------------------------------------------------------ submitting

    def submit(self, kind: str, label: str, target: Callable[[Job], dict | None],
               reserved: Iterable[Path] = (), lane: str = "main", **info) -> int:
        """Queue a job in ``lane``. ``reserved`` are output paths it will create (see ``reserved_outputs``)."""
        if lane not in self.lanes:
            raise ValueError(f"unknown lane: {lane}")
        with self._lock:
            job = Job(self, self._next_id, kind, label, target, reserved, {**info, "lane": lane})
            self._jobs[job.id] = job
            self._next_id += 1
            self._trim_history()
            if lane not in self._workers:
                self._workers[lane] = [
                    threading.Thread(target=self._work, args=(lane,), name=f"bagarium-{lane}-{n}", daemon=True)
                    for n in range(self.lanes[lane])
                ]
                for worker in self._workers[lane]:
                    worker.start()
            self._wakeup.notify_all()
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

    def _work(self, lane: str) -> None:
        while True:
            with self._lock:
                while (job := self._next_queued(lane)) is None:
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

    def _next_queued(self, lane: str) -> Job | None:
        return next((j for j in self._jobs.values()
                     if j.state["state"] == "queued" and j.state["lane"] == lane), None)

    def _trim_history(self) -> None:
        finished = [i for i, j in self._jobs.items() if j.state["state"] not in ACTIVE]
        for job_id in finished[:-MAX_HISTORY] if len(finished) > MAX_HISTORY else []:
            del self._jobs[job_id]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
