from __future__ import annotations

import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from .artifacts import ArtifactStore
from .config import AppConfig
from .db import IssueStore
from .models import utc_now_iso
from .orchestrator import Orchestrator, ProcessResult


@dataclass
class QueuedAction:
    id: str
    action: str
    issue_id: int | None
    status: str
    message: str
    created_at: str
    updated_at: str
    repo_path: str | None = None


WebJob = QueuedAction


class QueuedActionManager:
    def __init__(self, config: AppConfig, max_workers: int = 1, max_history: int = 100) -> None:
        self.config = config
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="agent-team-web")
        self._lock = threading.Lock()
        self._max_history = max(1, max_history)
        self._jobs: dict[str, QueuedAction] = {}
        self._futures: dict[str, Future[object]] = {}

    def submit_run_next(self, repo_path: str | None = None) -> QueuedAction:
        return self._submit("Run next ready issue", None, repo_path, lambda: _run_next_issue(self.config, repo_path))

    def submit_run_issue(self, issue_id: int, repo_path: str | None = None) -> QueuedAction:
        return self._submit(f"Run issue {issue_id}", issue_id, repo_path, lambda: _run_issue(self.config, issue_id))

    def get(self, job_id: str | None) -> QueuedAction | None:
        if not job_id:
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, issue_id: int | None = None) -> list[QueuedAction]:
        with self._lock:
            jobs = list(self._jobs.values())
        if issue_id is not None:
            jobs = [job for job in jobs if job.issue_id == issue_id]
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    def forget_jobs_for_issue(self, issue_id: int) -> None:
        with self._lock:
            job_ids = [
                job.id
                for job in self._jobs.values()
                if job.issue_id == issue_id and job.status in {"succeeded", "failed"}
            ]
            for job_id in job_ids:
                self._jobs.pop(job_id, None)
                self._futures.pop(job_id, None)

    def wait_for_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if all(job.status in {"succeeded", "failed"} for job in self._jobs.values()):
                    return True
            time.sleep(0.01)
        return False

    def shutdown(self) -> None:
        if sys.version_info >= (3, 9):
            self._executor.shutdown(wait=False, cancel_futures=True)
        else:
            self._executor.shutdown(wait=False)

    def _submit(
        self,
        action: str,
        issue_id: int | None,
        repo_path: str | None,
        run: Callable[[], object],
    ) -> QueuedAction:
        job = QueuedAction(
            id=str(uuid.uuid4()),
            action=action,
            issue_id=issue_id,
            status="queued",
            message="Queued",
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            repo_path=repo_path,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._prune_locked()
        future = self._executor.submit(self._execute, job.id, run)
        with self._lock:
            self._futures[job.id] = future
            self._prune_locked()
        return job

    def _execute(self, job_id: str, run: Callable[[], object]) -> object:
        self._update(job_id, "running", "Running")
        try:
            result = run()
        except Exception as exc:
            self._update(job_id, "failed", str(exc))
            raise
        self._update(job_id, "succeeded", job_result_message(result))
        return result

    def _update(self, job_id: str, status: str, message: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = status
            job.message = message
            job.updated_at = utc_now_iso()
            self._prune_locked()

    def _prune_locked(self) -> None:
        overflow = len(self._jobs) - self._max_history
        if overflow <= 0:
            return
        completed = sorted(
            (job for job in self._jobs.values() if job.status in {"succeeded", "failed"}),
            key=lambda job: job.updated_at,
        )
        for job in completed:
            if overflow <= 0:
                break
            future = self._futures.get(job.id)
            if future is None or not future.done():
                continue
            self._jobs.pop(job.id, None)
            self._futures.pop(job.id, None)
            overflow -= 1


WebJobManager = QueuedActionManager


def run_next_issue(config: AppConfig, repo_path: str | None = None) -> ProcessResult | None:
    store = IssueStore(config.db_path)
    store.init_schema()
    artifacts = ArtifactStore(config.artifacts_dir)
    return Orchestrator(store, artifacts, config).process_next(repo_path=repo_path)


def run_issue(config: AppConfig, issue_id: int) -> ProcessResult:
    store = IssueStore(config.db_path)
    store.init_schema()
    artifacts = ArtifactStore(config.artifacts_dir)
    return Orchestrator(store, artifacts, config).process_issue(issue_id)


def job_result_message(result: object) -> str:
    if result is None:
        return "No ready issues."
    phase = getattr(result, "phase", None)
    next_phase = getattr(result, "next_phase", None)
    status = getattr(result, "status", None)
    issue_id = getattr(result, "issue_id", None)
    if issue_id is not None and phase is not None:
        return f"Issue {issue_id} {phase} completed with {status}; next phase: {next_phase}"
    return "Completed"


_run_next_issue = run_next_issue
_run_issue = run_issue
_job_result_message = job_result_message
