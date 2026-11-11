"""In-memory job status for loop background runners."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

JobState = Literal["idle", "running", "waiting_hitl", "done", "error"]


@dataclass
class JobStatus:
    loop_id: str
    state: JobState = "idle"
    message: str = ""
    error: str | None = None
    log_tail: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append_log(self, line: str) -> None:
        with self.lock:
            self.log_tail.append(line)

    def set(self, state: JobState, message: str = "", error: str | None = None) -> None:
        with self.lock:
            self.state = state
            if message:
                self.message = message
            self.error = error
            if message:
                self.log_tail.append(message)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "loop_id": self.loop_id,
                "job_state": self.state,
                "message": self.message,
                "error": self.error,
                "log_tail": list(self.log_tail),
            }


_jobs: dict[str, JobStatus] = {}
_jobs_lock = threading.Lock()
_running_threads: dict[str, threading.Thread] = {}


def get_or_create_job(loop_id: str) -> JobStatus:
    with _jobs_lock:
        if loop_id not in _jobs:
            _jobs[loop_id] = JobStatus(loop_id=loop_id)
        return _jobs[loop_id]


def get_job(loop_id: str) -> JobStatus | None:
    with _jobs_lock:
        return _jobs.get(loop_id)


def is_busy(loop_id: str) -> bool:
    t = _running_threads.get(loop_id)
    return t is not None and t.is_alive()


def spawn(loop_id: str, target, *args, **kwargs) -> bool:
    """Start background thread if not already running. Returns False if busy."""
    if is_busy(loop_id):
        return False

    def _wrap() -> None:
        try:
            target(*args, **kwargs)
        finally:
            with _jobs_lock:
                _running_threads.pop(loop_id, None)

    t = threading.Thread(target=_wrap, name=f"loop-{loop_id}", daemon=True)
    with _jobs_lock:
        _running_threads[loop_id] = t
    t.start()
    return True
