from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from .artifacts import ArtifactStore
from .config import AppConfig
from .db import IssueStore
from .models import Issue
from .orchestrator import Orchestrator, ProcessResult


SLOT_REFILL_POLL_SECONDS = 1.0


def process_batch(
    store: IssueStore,
    artifacts: ArtifactStore,
    config: AppConfig,
    concurrency: int,
    stop_event: threading.Event | None = None,
) -> list[ProcessResult]:
    concurrency = max(1, concurrency)
    results: list[ProcessResult] = []
    active_issue_ids: set[int] = set()
    futures: dict[Future[ProcessResult], int] = {}
    monitored_pull_requests = False
    if not _stop_requested(stop_event):
        _recover_interrupted_runs(store, artifacts, config)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        while True:
            if not futures and not _stop_requested(stop_event):
                _recover_interrupted_runs(store, artifacts, config)
                if not monitored_pull_requests:
                    results.extend(_monitor_pull_requests(store, artifacts, config))
                    monitored_pull_requests = True
            if not _stop_requested(stop_event):
                _refill_slots(executor, store, artifacts, config, concurrency, futures, active_issue_ids, stop_event)
            if not futures:
                return results

            refill_timeout = SLOT_REFILL_POLL_SECONDS if len(futures) < concurrency else None
            completed, _ = wait(
                futures.keys(),
                timeout=refill_timeout,
                return_when=FIRST_COMPLETED,
            )
            if not completed:
                continue
            for future in completed:
                issue_id = futures.pop(future)
                active_issue_ids.remove(issue_id)
                try:
                    result = future.result()
                except RuntimeError:
                    continue
                if result is not None:
                    results.append(result)


def run_worker_loop(
    store: IssueStore,
    artifacts: ArtifactStore,
    config: AppConfig,
    interval_seconds: int,
    concurrency: int,
    stop_event: threading.Event,
    on_result: Callable[[ProcessResult], None] | None = None,
) -> None:
    interval_seconds = max(0, interval_seconds)
    concurrency = max(1, concurrency)
    while not stop_event.is_set():
        results = process_batch(store, artifacts, config, concurrency, stop_event=stop_event)
        for result in results:
            if on_result is not None:
                on_result(result)
        if stop_event.is_set():
            break
        stop_event.wait(interval_seconds)


def _refill_slots(
    executor: ThreadPoolExecutor,
    store: IssueStore,
    artifacts: ArtifactStore,
    config: AppConfig,
    concurrency: int,
    futures: dict[Future[ProcessResult], int],
    active_issue_ids: set[int],
    stop_event: threading.Event | None = None,
) -> None:
    while len(futures) < concurrency:
        if _stop_requested(stop_event):
            return
        slots = concurrency - len(futures)
        candidates = _ready_candidates(store, concurrency, slots, active_issue_ids)
        if not candidates:
            return
        scheduled = False
        for issue in candidates:
            if _stop_requested(stop_event):
                return
            if len(futures) >= concurrency:
                return
            if issue.id in active_issue_ids:
                continue
            active_issue_ids.add(issue.id)
            future = executor.submit(
                Orchestrator(store, artifacts, config).process_issue,
                issue.id,
                None,
            )
            futures[future] = issue.id
            scheduled = True
        if not scheduled:
            return


def _ready_candidates(
    store: IssueStore,
    concurrency: int,
    slots: int,
    active_issue_ids: set[int],
) -> list[Issue]:
    candidate_limit = max(slots + len(active_issue_ids) + 1, concurrency * 2)
    candidates = store.list_next_ready_issues(candidate_limit, exclude_issue_ids=active_issue_ids)
    return [issue for issue in candidates if issue.id not in active_issue_ids][:slots]


def _recover_interrupted_runs(store: IssueStore, artifacts: ArtifactStore, config: AppConfig) -> None:
    recover = getattr(Orchestrator(store, artifacts, config), "recover_interrupted_runs", None)
    if recover is not None:
        recover()


def _monitor_pull_requests(store: IssueStore, artifacts: ArtifactStore, config: AppConfig) -> list[ProcessResult]:
    monitor = getattr(Orchestrator(store, artifacts, config), "monitor_pull_requests", None)
    if monitor is None:
        return []
    return monitor()


def _stop_requested(stop_event: threading.Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()
