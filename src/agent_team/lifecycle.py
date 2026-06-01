from __future__ import annotations

import uuid
from dataclasses import dataclass

from .artifacts import ArtifactStore
from .config import AppConfig
from .db import IssueStore
from .models import HumanInputRequest, Issue
from .workspaces import WorkspaceManager


DEFAULT_STOP_MESSAGE = "Issue stopped by manager"


@dataclass(frozen=True)
class ResetIssueResult:
    issue_id: int
    prior_phase: str
    issue: Issue
    deleted_runs: int
    deleted_events: int
    deleted_artifacts: int
    removed_workspace_paths: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DeleteIssueResult:
    issue_id: int
    prior_phase: str
    deleted_runs: int
    deleted_events: int
    deleted_artifacts: int
    removed_workspace_paths: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class StopIssueResult:
    issue_id: int
    prior_phase: str
    issue: Issue
    stopped_human_input_request: HumanInputRequest | None = None


def stop_issue(
    config: AppConfig,
    store: IssueStore,
    artifacts: ArtifactStore,
    issue_id: int,
    message: str | None = None,
    *,
    stopped_by: str = "cli",
) -> StopIssueResult:
    from .orchestrator import Orchestrator

    Orchestrator(store, artifacts, config).recover_interrupted_issue(issue_id)
    stop_message = (message or "").strip() or DEFAULT_STOP_MESSAGE
    stopped = store.stop_issue(issue_id, stop_message, stopped_by=stopped_by)
    if stopped.stopped_human_input_request is not None:
        artifacts.append_human_input_stop(stopped.stopped_human_input_request)
        artifacts.write_human_input_summary(issue_id, store.list_human_input_requests(issue_id))
    artifacts.write_issue_snapshot(stopped.issue)
    return StopIssueResult(
        issue_id=stopped.issue_id,
        prior_phase=stopped.prior_phase,
        issue=stopped.issue,
        stopped_human_input_request=stopped.stopped_human_input_request,
    )


def reset_issue_to_draft(
    config: AppConfig,
    store: IssueStore,
    artifacts: ArtifactStore,
    issue_id: int,
    message: str | None = None,
) -> ResetIssueResult:
    owner = f"reset-to-draft:{uuid.uuid4()}"
    prior = store.begin_reset_issue(issue_id, owner, config.lock_ttl_seconds)

    workspace_result = WorkspaceManager(
        config.worktrees_dir,
        artifacts,
        config.locks_dir or config.home / "locks",
    ).reset_issue_workspace(prior)
    deleted_artifacts = artifacts.reset_issue_artifacts(issue_id)
    reset_message = _reset_message(prior, message)
    issue, deleted_runs, deleted_events = store.complete_reset_issue_to_draft(issue_id, owner, reset_message)
    artifacts.write_issue_snapshot(issue)

    return ResetIssueResult(
        issue_id=issue_id,
        prior_phase=prior.phase,
        issue=issue,
        deleted_runs=deleted_runs,
        deleted_events=deleted_events,
        deleted_artifacts=deleted_artifacts,
        removed_workspace_paths=workspace_result.removed_paths,
        warnings=workspace_result.warnings,
    )


def delete_issue(
    config: AppConfig,
    store: IssueStore,
    artifacts: ArtifactStore,
    issue_id: int,
    message: str | None = None,
) -> DeleteIssueResult:
    owner = f"delete-issue:{uuid.uuid4()}"
    prior = store.begin_delete_issue(issue_id, owner, config.lock_ttl_seconds)

    try:
        workspace_result = WorkspaceManager(
            config.worktrees_dir,
            artifacts,
            config.locks_dir or config.home / "locks",
        ).reset_issue_workspace(prior)
        deleted_artifacts = artifacts.delete_issue_artifacts(issue_id)
        deleted_runs, deleted_events = store.complete_delete_issue(issue_id, owner)
    except Exception:
        store.release_lock(issue_id, owner)
        raise

    return DeleteIssueResult(
        issue_id=issue_id,
        prior_phase=prior.phase,
        deleted_runs=deleted_runs,
        deleted_events=deleted_events,
        deleted_artifacts=deleted_artifacts,
        removed_workspace_paths=workspace_result.removed_paths,
        warnings=workspace_result.warnings,
    )


def _reset_message(prior: Issue, message: str | None) -> str:
    cleaned = (message or "").strip()
    if cleaned:
        return f"{cleaned} (prior phase: {prior.phase})"
    return f"Reset issue {prior.id} to draft from {prior.phase}"
