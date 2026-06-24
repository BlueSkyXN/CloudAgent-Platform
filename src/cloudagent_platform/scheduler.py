from __future__ import annotations

import threading
from typing import Protocol

from .config import DEFAULT_MAX_JSON_BYTES
from .utils import now_iso


class SchedulerStore(Protocol):
    path: str

    def close(self) -> None:
        ...

    def requeue_expired_runs(self, request_id: str) -> int:
        ...

    def due_jobs(self) -> list[dict[str, object]]:
        ...

    def enqueue_job(self, job_id: str, request_id: str, source: str = "manual") -> dict[str, object]:
        ...


class Runtime:
    def __init__(
        self,
        store: SchedulerStore,
        auth_token: str,
        cors_origins: set[str] | None = None,
        max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
    ) -> None:
        self.store = store
        self.auth_token = auth_token
        self.cors_origins = cors_origins or set()
        self.max_json_bytes = max_json_bytes
        self.started_at = now_iso()
        self._stop = threading.Event()
        self._scheduler = threading.Thread(target=self.scheduler_loop, daemon=True)
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self._scheduler.join(timeout=2)
        self.store.close()

    def scheduler_loop(self) -> None:
        while not self._stop.wait(1):
            try:
                self.store.requeue_expired_runs(request_id="scheduler")
            except Exception:
                continue
            for job in self.store.due_jobs():
                try:
                    self.store.enqueue_job(str(job["id"]), request_id="scheduler", source="schedule")
                except Exception:
                    # Keep the prototype scheduler alive; detailed failure events come later.
                    continue
