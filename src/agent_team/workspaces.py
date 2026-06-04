from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .artifacts import ArtifactStore
from .models import Issue, utc_now_iso
from .pull_requests import (
    PullRequestError,
    PullRequestRemote,
    PullRequestRequest,
    PullRequestResult,
    PullRequestStatusSnapshot,
    SAFE_PULL_REQUEST_DESCRIPTION,
    create_or_get_pull_request,
    parse_pull_request_remote,
    pull_request_remote_from_metadata,
)

_REMOTE_GIT_COMMAND_TIMEOUT_SECONDS = 120.0
_REMOTE_GIT_NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}


class WorkspaceError(Exception):
    """Raised when an isolated issue workspace cannot be prepared safely."""


@dataclass(frozen=True)
class WorkspaceMergeResult:
    status: str
    summary: str
    target_branch: str | None
    worktree_head: str | None
    merge_commit: str | None
    worktree_commit: str | None = None
    conflict_files: tuple[str, ...] = ()
    cleanup_removed: bool = False
    pr_provider: str | None = None
    pr_remote_name: str | None = None
    pr_source_branch: str | None = None
    pr_url: str | None = None
    pr_id: str | None = None
    pr_number: int | None = None
    pr_status: str | None = None
    pr_is_existing: bool = False

    def artifact_markdown(self) -> str:
        lines = [
            "# Merge Result",
            "",
            f"- Status: `{self.status}`",
            f"- Summary: {self.summary}",
        ]
        if self.target_branch:
            lines.append(f"- Target branch: `{self.target_branch}`")
        if self.worktree_head:
            lines.append(f"- Worktree HEAD: `{self.worktree_head}`")
        if self.worktree_commit:
            lines.append(f"- Worktree commit created: `{self.worktree_commit}`")
        if self.merge_commit:
            lines.append(f"- Merge commit: `{self.merge_commit}`")
        if self.status == "pull_request" or self.pr_provider or self.pr_url:
            if self.pr_provider:
                lines.append(f"- Pull request provider: `{self.pr_provider}`")
            if self.pr_remote_name:
                lines.append(f"- Pull request remote: `{self.pr_remote_name}`")
            if self.pr_source_branch:
                lines.append(f"- Pull request source branch: `{self.pr_source_branch}`")
            if self.pr_url:
                lines.append(f"- Pull request URL: {self.pr_url}")
            if self.pr_number is not None:
                lines.append(f"- Pull request number: `{self.pr_number}`")
            elif self.pr_id:
                lines.append(f"- Pull request id: `{self.pr_id}`")
            if self.pr_status:
                lines.append(f"- Pull request status: `{self.pr_status}`")
            lines.append(f"- Existing pull request reused: `{str(self.pr_is_existing).lower()}`")
        lines.append(f"- Worktree removed: `{str(self.cleanup_removed).lower()}`")
        if self.status == "conflicts" and self.pr_url:
            lines.extend(
                [
                    "",
                    "## Hosted pull request conflict",
                    "",
                    "The hosted pull request provider reported merge conflicts. The orchestrator recreated the "
                    "isolated issue worktree from the PR branch and merged the latest target branch so conflict "
                    "markers can be resolved locally before validation, review, and merge approval run again.",
                ]
            )
        if self.conflict_files:
            lines.extend(["", "## Conflicted files", ""])
            lines.extend(f"- `{path}`" for path in self.conflict_files)
            lines.extend(
                [
                    "",
                    "Resolve these conflicts in the isolated workspace. A successful conflict-resolution run "
                    "will be committed before validation.",
                ]
            )
        recommendation_by_status = {
            "conflicts": "ready_for_merge_conflict_resolution",
            "pull_request": "awaiting_pr_closure",
            "target_synced": "ready_for_validation",
        }
        recommendation = recommendation_by_status.get(self.status, "done")
        lines.extend(["", f"Recommendation: `{recommendation}`"])
        return "\n".join(lines)


@dataclass(frozen=True)
class WorkspaceSourceSyncResult:
    status: str
    summary: str
    target_branch: str | None
    old_source_head: str | None
    new_source_head: str | None
    worktree_head: str | None
    sync_commit: str | None = None
    conflict_files: tuple[str, ...] = ()
    synced_at: str = ""

    def artifact_markdown(self) -> str:
        lines = [
            "# Workspace Source Sync",
            "",
            f"- Status: `{self.status}`",
            f"- Summary: {self.summary}",
        ]
        if self.target_branch:
            lines.append(f"- Source branch: `{self.target_branch}`")
        if self.old_source_head:
            lines.append(f"- Previous source HEAD: `{self.old_source_head}`")
        if self.new_source_head:
            lines.append(f"- Current source HEAD: `{self.new_source_head}`")
        if self.worktree_head:
            lines.append(f"- Worktree HEAD before sync: `{self.worktree_head}`")
        if self.sync_commit:
            lines.append(f"- Source sync commit: `{self.sync_commit}`")
        if self.synced_at:
            lines.append(f"- Synced at: {self.synced_at}")
        if self.conflict_files:
            lines.extend(["", "## Conflicted files", ""])
            lines.extend(f"- `{path}`" for path in self.conflict_files)
            lines.extend(
                [
                    "",
                    "These conflicts happened while merging the recorded source branch into the isolated "
                    "workspace after review requested implementation rework. Resolve conflict markers in the "
                    "isolated workspace before continuing the rework loop.",
                    "",
                    "If prior review findings still require code changes after resolving these conflict markers, "
                    "recommend `ready_for_implementation`; otherwise recommend `ready_for_validation`.",
                ]
            )
        recommendation = (
            "ready_for_merge_conflict_resolution" if self.status == "conflicts" else "ready_for_implementation"
        )
        lines.extend(["", f"Recommendation: `{recommendation}`"])
        return "\n".join(lines)


@dataclass(frozen=True)
class WorkspaceMergeRecovery:
    next_phase: str
    run_status: str
    summary: str
    artifact_markdown: str


@dataclass(frozen=True)
class _MergePreflightResult:
    status: str
    target_branch: str
    merge_branch: str
    worktree_head: str
    integrated_head: str | None
    worktree_commit: str | None
    conflict_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SelectedRemote:
    remote: PullRequestRemote
    push_url: str


@dataclass(frozen=True)
class WorkspaceResetResult:
    removed_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceInfo:
    issue_id: int
    original_repo_path: Path
    source_root: Path
    source_git_common_dir: Path
    relative_subpath: str
    worktree_root: Path
    workspace_repo_path: Path
    source_branch: str | None
    source_head: str
    created_at: str

    def to_metadata(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "original_repo_path": str(self.original_repo_path),
            "source_root": str(self.source_root),
            "source_git_common_dir": str(self.source_git_common_dir),
            "relative_subpath": self.relative_subpath,
            "worktree_root": str(self.worktree_root),
            "workspace_repo_path": str(self.workspace_repo_path),
            "source_branch": self.source_branch,
            "source_head": self.source_head,
            "created_at": self.created_at,
        }

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> "WorkspaceInfo":
        return cls(
            issue_id=int(metadata["issue_id"]),
            original_repo_path=Path(str(metadata["original_repo_path"])),
            source_root=Path(str(metadata["source_root"])),
            source_git_common_dir=Path(str(metadata["source_git_common_dir"])),
            relative_subpath=str(metadata.get("relative_subpath") or ""),
            worktree_root=Path(str(metadata["worktree_root"])),
            workspace_repo_path=Path(str(metadata["workspace_repo_path"])),
            source_branch=metadata.get("source_branch"),
            source_head=str(metadata["source_head"]),
            created_at=str(metadata["created_at"]),
        )


@dataclass(frozen=True)
class _WorkspacePaths:
    repo_path: Path
    source_root: Path
    source_git_common_dir: Path
    relative_subpath: str
    worktree_root: Path
    workspace_repo_path: Path


class WorkspaceManager:
    def __init__(
        self,
        worktrees_dir: Path,
        artifacts: ArtifactStore,
        locks_dir: Path | None = None,
        *,
        merge_mode: str = "auto",
        pr_remote: str | None = None,
        pr_branch_prefix: str = "agent-team/issue-",
    ) -> None:
        self.worktrees_dir = worktrees_dir
        self.artifacts = artifacts
        self.locks_dir = locks_dir
        self.merge_mode = _normalize_merge_mode(merge_mode)
        self.pr_remote = _clean_optional_string(pr_remote)
        self.pr_branch_prefix = pr_branch_prefix

    def prepare(self, issue: Issue) -> WorkspaceInfo:
        paths = self._workspace_paths(issue)
        info = self._existing_from_metadata(issue, paths)
        if info is not None:
            return info

        if paths.worktree_root.exists():
            return self._recover_orphaned_worktree(issue, paths)
        self._ensure_clean_source(paths.source_root)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._git(paths.source_root, "worktree", "add", "--detach", str(paths.worktree_root), "HEAD")
        if not paths.workspace_repo_path.is_dir():
            raise WorkspaceError(
                f"Isolated workspace subdirectory does not exist for issue {issue.id}: {paths.workspace_repo_path}"
            )

        info = WorkspaceInfo(
            issue_id=issue.id,
            original_repo_path=paths.repo_path,
            source_root=paths.source_root,
            source_git_common_dir=paths.source_git_common_dir,
            relative_subpath=paths.relative_subpath,
            worktree_root=paths.worktree_root.resolve(),
            workspace_repo_path=paths.workspace_repo_path.resolve(),
            source_branch=self._source_branch(paths.source_root),
            source_head=self._git(paths.source_root, "rev-parse", "HEAD"),
            created_at=utc_now_iso(),
        )
        self.artifacts.write_workspace_metadata(issue.id, info.to_metadata())
        return info

    def existing(self, issue: Issue) -> WorkspaceInfo:
        paths = self._workspace_paths(issue)
        info = self._existing_from_metadata(issue, paths)
        if info is None:
            raise WorkspaceError(f"Workspace metadata is missing for issue {issue.id}")
        return info

    def sync_source_into_workspace(self, issue: Issue, info: WorkspaceInfo) -> WorkspaceSourceSyncResult:
        self._validate_merge_workspace(issue, info)
        metadata = self._workspace_metadata_for_update(issue, info)
        old_source_head = _clean_optional_string(metadata.get("last_source_sync_head")) or info.source_head
        synced_at = utc_now_iso()
        branch = info.source_branch
        if not branch:
            result = WorkspaceSourceSyncResult(
                status="skipped",
                summary=f"Skipped source sync for issue {issue.id}; workspace was created from detached source HEAD.",
                target_branch=None,
                old_source_head=old_source_head,
                new_source_head=None,
                worktree_head=self._git(info.worktree_root, "rev-parse", "HEAD"),
                synced_at=synced_at,
            )
            self._write_source_sync_metadata(issue.id, metadata, result)
            return result

        with self._repo_lock(info):
            return self._sync_source_into_workspace_locked(issue, info, metadata, branch, old_source_head, synced_at)

    def prepare_pull_request_conflict_workspace(
        self,
        issue: Issue,
        pull_request_metadata: dict[str, Any],
        status_snapshot: PullRequestStatusSnapshot,
    ) -> WorkspaceMergeResult:
        try:
            info = WorkspaceInfo.from_metadata(pull_request_metadata)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(
                f"Pull request metadata is invalid for issue {issue.id}: "
                f"{self.artifacts.pull_request_metadata_path(issue.id)}"
            ) from exc
        self._validate_pull_request_repair_context(issue, info)
        with self._repo_lock_for_common_dir(info.source_git_common_dir):
            return self._prepare_pull_request_conflict_workspace_locked(
                issue,
                info,
                pull_request_metadata,
                status_snapshot,
            )

    def recover_interrupted_source_sync(self, issue: Issue) -> WorkspaceMergeRecovery | None:
        try:
            metadata = self.artifacts.read_workspace_metadata(issue.id)
        except (OSError, ValueError):
            return None
        if metadata is None:
            return None
        try:
            info = WorkspaceInfo.from_metadata(metadata)
        except (KeyError, TypeError, ValueError):
            return None
        try:
            self._validate_merge_workspace(issue, info)
        except WorkspaceError:
            return None

        with self._repo_lock(info):
            conflict_files = self._conflicted_files(info.worktree_root)
            if not conflict_files:
                return None
            old_source_head = _clean_optional_string(metadata.get("last_source_sync_previous_head")) or info.source_head
            new_source_head = _clean_optional_string(metadata.get("last_source_sync_head"))
            if new_source_head is None and info.source_branch and self._git_check(
                info.source_root, "rev-parse", "--verify", info.source_branch
            ):
                new_source_head = self._git(info.source_root, "rev-parse", info.source_branch)
            result = WorkspaceSourceSyncResult(
                status="conflicts",
                summary=(
                    f"Recovered interrupted source-sync conflicts for issue {issue.id}; "
                    "AI resolution is required before implementation rework can continue."
                ),
                target_branch=info.source_branch,
                old_source_head=old_source_head,
                new_source_head=new_source_head,
                worktree_head=self._git(info.worktree_root, "rev-parse", "HEAD"),
                conflict_files=conflict_files,
                synced_at=utc_now_iso(),
            )
            self._write_source_sync_metadata(issue.id, metadata, result)
            return WorkspaceMergeRecovery(
                "ready_for_merge_conflict_resolution",
                "interrupted",
                result.summary,
                result.artifact_markdown(),
            )

    def has_interrupted_source_sync_conflicts(self, issue: Issue) -> bool:
        try:
            metadata = self.artifacts.read_workspace_metadata(issue.id)
        except (OSError, ValueError):
            return False
        if metadata is None:
            return False
        try:
            info = WorkspaceInfo.from_metadata(metadata)
        except (KeyError, TypeError, ValueError):
            return False
        try:
            self._validate_merge_workspace(issue, info)
        except WorkspaceError:
            return False
        return bool(self._conflicted_files(info.worktree_root))

    def commit_phase_snapshot(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        *,
        phase: str,
        run_id: str,
        summary: str,
        artifact_markdown: str,
        next_phase: str,
    ) -> str | None:
        subject, body = self._phase_snapshot_commit_message(
            issue,
            phase,
            run_id,
            summary,
            artifact_markdown,
            next_phase,
        )
        return self._commit_workspace_changes(
            info.worktree_root,
            subject,
            body,
            allow_empty=self._merge_head_exists(info.worktree_root),
        )

    def merge_and_cleanup(self, issue: Issue, target_branch: str | None = None) -> WorkspaceMergeResult:
        metadata = self.artifacts.read_workspace_metadata(issue.id)
        if metadata is None:
            if not issue.repo_path:
                raise WorkspaceError(
                    f"Workspace metadata is missing for issue {issue.id}, and the issue has no target repo."
                )
            raise WorkspaceError(f"Workspace metadata is missing for issue {issue.id}")
        try:
            info = WorkspaceInfo.from_metadata(metadata)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(
                f"Workspace metadata is invalid for issue {issue.id}: "
                f"{self.artifacts.workspace_metadata_path(issue.id)}"
            ) from exc

        self._validate_merge_workspace(issue, info)
        merge_request = self.artifacts.read_merge_request(issue.id) or {}
        requested_branch = target_branch or _clean_optional_string(merge_request.get("target_branch"))
        approval_message = _clean_optional_string(merge_request.get("message"))
        requested_mode = _normalize_merge_mode(_clean_optional_string(merge_request.get("mode")) or self.merge_mode)
        requested_remote = _clean_optional_string(merge_request.get("remote_name")) or self.pr_remote
        branch = requested_branch or info.source_branch
        if not branch:
            raise WorkspaceError(
                "Workspace was created from a detached source HEAD; approve merge with an explicit target branch."
            )

        with self._repo_lock(info):
            return self._merge_and_cleanup_locked(
                issue,
                info,
                branch,
                approval_message,
                requested_mode,
                requested_remote,
            )

    def recover_interrupted_merge(self, issue: Issue, target_branch: str | None = None) -> WorkspaceMergeRecovery:
        try:
            merged_metadata = self.artifacts.read_merged_workspace_metadata(issue.id)
        except (OSError, ValueError) as exc:
            return _merge_recovery_blocked(
                f"Merged workspace metadata is unreadable for issue {issue.id}: "
                f"{self.artifacts.merged_workspace_metadata_path(issue.id)}\n{exc}"
            )
        if merged_metadata is not None:
            return self._recover_merged_metadata(issue, merged_metadata)

        try:
            metadata = self.artifacts.read_workspace_metadata(issue.id)
        except (OSError, ValueError) as exc:
            return _merge_recovery_blocked(
                f"Workspace metadata is unreadable for issue {issue.id}: "
                f"{self.artifacts.workspace_metadata_path(issue.id)}\n{exc}"
            )
        if metadata is not None and _is_hosted_pull_request_repair_metadata(metadata):
            return self._recover_workspace_metadata(issue, metadata, target_branch)

        try:
            pull_request_metadata = self.artifacts.read_pull_request_metadata(issue.id)
        except (OSError, ValueError) as exc:
            return _merge_recovery_blocked(
                f"Pull request metadata is unreadable for issue {issue.id}: "
                f"{self.artifacts.pull_request_metadata_path(issue.id)}\n{exc}"
            )
        if pull_request_metadata is not None:
            return self._recover_pull_request_metadata(issue, pull_request_metadata)

        if metadata is None:
            if not issue.repo_path:
                return _merge_recovery_blocked(
                    f"Merge recovery could not find workspace metadata for issue {issue.id}, "
                    "and the issue has no target repo."
                )
            return _merge_recovery_blocked(
                f"Merge recovery could not find workspace metadata for issue {issue.id}."
            )

        try:
            info = WorkspaceInfo.from_metadata(metadata)
        except (KeyError, TypeError, ValueError) as exc:
            return _merge_recovery_blocked(
                f"Workspace metadata is invalid for issue {issue.id}: {self.artifacts.workspace_metadata_path(issue.id)}"
            )

        try:
            self._validate_merge_workspace(issue, info)
        except WorkspaceError as exc:
            return _merge_recovery_blocked(str(exc))

        merge_request = self.artifacts.read_merge_request(issue.id) or {}
        requested_branch = target_branch or _clean_optional_string(merge_request.get("target_branch"))
        branch = requested_branch or info.source_branch
        if not branch:
            return _merge_recovery_blocked(
                "Workspace was created from a detached source HEAD; approve merge with an explicit target branch."
            )

        with self._repo_lock(info):
            try:
                latest_merged_metadata = self.artifacts.read_merged_workspace_metadata(issue.id)
            except (OSError, ValueError) as exc:
                return _merge_recovery_blocked(
                    f"Merged workspace metadata is unreadable for issue {issue.id}: "
                    f"{self.artifacts.merged_workspace_metadata_path(issue.id)}\n{exc}"
                )
            if latest_merged_metadata is not None:
                try:
                    latest_info = WorkspaceInfo.from_metadata(latest_merged_metadata)
                except (KeyError, TypeError, ValueError) as exc:
                    return _merge_recovery_blocked(
                        f"Merged workspace metadata is invalid for issue {issue.id}: "
                        f"{self.artifacts.merged_workspace_metadata_path(issue.id)}"
                    )
                return self._recover_merged_metadata_locked(issue, latest_info, latest_merged_metadata)
            if _is_hosted_pull_request_repair_metadata(metadata):
                return self._recover_interrupted_merge_locked(issue, info, branch)
            try:
                latest_pull_request_metadata = self.artifacts.read_pull_request_metadata(issue.id)
            except (OSError, ValueError) as exc:
                return _merge_recovery_blocked(
                    f"Pull request metadata is unreadable for issue {issue.id}: "
                    f"{self.artifacts.pull_request_metadata_path(issue.id)}\n{exc}"
                )
            if latest_pull_request_metadata is not None:
                return self._recover_pull_request_metadata_locked(issue, info, latest_pull_request_metadata)
            return self._recover_interrupted_merge_locked(issue, info, branch)

    def _recover_workspace_metadata(
        self,
        issue: Issue,
        metadata: dict[str, Any],
        target_branch: str | None,
    ) -> WorkspaceMergeRecovery:
        try:
            info = WorkspaceInfo.from_metadata(metadata)
        except (KeyError, TypeError, ValueError) as exc:
            return _merge_recovery_blocked(
                f"Workspace metadata is invalid for issue {issue.id}: {self.artifacts.workspace_metadata_path(issue.id)}"
            )

        try:
            self._validate_merge_workspace(issue, info)
        except WorkspaceError as exc:
            return _merge_recovery_blocked(str(exc))

        merge_request = self.artifacts.read_merge_request(issue.id) or {}
        requested_branch = target_branch or _clean_optional_string(merge_request.get("target_branch"))
        branch = requested_branch or info.source_branch
        if not branch:
            return _merge_recovery_blocked(
                "Workspace was created from a detached source HEAD; approve merge with an explicit target branch."
            )

        with self._repo_lock(info):
            try:
                latest_merged_metadata = self.artifacts.read_merged_workspace_metadata(issue.id)
            except (OSError, ValueError) as exc:
                return _merge_recovery_blocked(
                    f"Merged workspace metadata is unreadable for issue {issue.id}: "
                    f"{self.artifacts.merged_workspace_metadata_path(issue.id)}\n{exc}"
                )
            if latest_merged_metadata is not None:
                try:
                    latest_info = WorkspaceInfo.from_metadata(latest_merged_metadata)
                except (KeyError, TypeError, ValueError) as exc:
                    return _merge_recovery_blocked(
                        f"Merged workspace metadata is invalid for issue {issue.id}: "
                        f"{self.artifacts.merged_workspace_metadata_path(issue.id)}"
                    )
                return self._recover_merged_metadata_locked(issue, latest_info, latest_merged_metadata)
            try:
                latest_metadata = self.artifacts.read_workspace_metadata(issue.id)
            except (OSError, ValueError) as exc:
                return _merge_recovery_blocked(
                    f"Workspace metadata is unreadable for issue {issue.id}: "
                    f"{self.artifacts.workspace_metadata_path(issue.id)}\n{exc}"
                )
            if latest_metadata is not None:
                try:
                    latest_info = WorkspaceInfo.from_metadata(latest_metadata)
                except (KeyError, TypeError, ValueError) as exc:
                    return _merge_recovery_blocked(
                        f"Workspace metadata is invalid for issue {issue.id}: "
                        f"{self.artifacts.workspace_metadata_path(issue.id)}"
                    )
                latest_branch = requested_branch or latest_info.source_branch
                if not latest_branch:
                    return _merge_recovery_blocked(
                        "Workspace was created from a detached source HEAD; approve merge with an explicit target branch."
                    )
                return self._recover_interrupted_merge_locked(issue, latest_info, latest_branch)
            try:
                latest_pull_request_metadata = self.artifacts.read_pull_request_metadata(issue.id)
            except (OSError, ValueError) as exc:
                return _merge_recovery_blocked(
                    f"Pull request metadata is unreadable for issue {issue.id}: "
                    f"{self.artifacts.pull_request_metadata_path(issue.id)}\n{exc}"
                )
            if latest_pull_request_metadata is not None:
                return self._recover_pull_request_metadata_locked(issue, info, latest_pull_request_metadata)
            return _merge_recovery_blocked(f"Merge recovery could not find workspace metadata for issue {issue.id}.")

    def _recover_merged_metadata(
        self,
        issue: Issue,
        merged_metadata: dict[str, Any],
    ) -> WorkspaceMergeRecovery:
        try:
            info = WorkspaceInfo.from_metadata(merged_metadata)
        except (KeyError, TypeError, ValueError) as exc:
            return _merge_recovery_blocked(
                f"Merged workspace metadata is invalid for issue {issue.id}: "
                f"{self.artifacts.merged_workspace_metadata_path(issue.id)}"
            )
        with self._repo_lock(info):
            return self._recover_merged_metadata_locked(issue, info, merged_metadata)

    def _recover_pull_request_metadata(
        self,
        issue: Issue,
        pull_request_metadata: dict[str, Any],
    ) -> WorkspaceMergeRecovery:
        try:
            info = WorkspaceInfo.from_metadata(pull_request_metadata)
        except (KeyError, TypeError, ValueError) as exc:
            return _merge_recovery_blocked(
                f"Pull request metadata is invalid for issue {issue.id}: "
                f"{self.artifacts.pull_request_metadata_path(issue.id)}"
            )
        with self._repo_lock(info):
            return self._recover_pull_request_metadata_locked(issue, info, pull_request_metadata)

    def _recover_merged_metadata_locked(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        merged_metadata: dict[str, Any],
    ) -> WorkspaceMergeRecovery:
        cleanup_removed = bool(merged_metadata.get("cleanup_removed"))
        if not cleanup_removed:
            if info.worktree_root.exists():
                try:
                    self._ensure_clean_repo(info.worktree_root, "issue worktree")
                    self._git(info.source_root, "worktree", "remove", str(info.worktree_root))
                except WorkspaceError as exc:
                    return _merge_recovery_blocked(f"Merge cleanup recovery could not remove worktree: {exc}")
            merged_metadata = {**merged_metadata, "cleanup_removed": True}
            self.artifacts.write_merged_workspace_metadata(issue.id, merged_metadata)
        self.artifacts.delete_workspace_metadata(issue.id)
        self._delete_internal_merge_branches(issue.id, info.source_root, force=True)
        merge_commit = _clean_optional_string(merged_metadata.get("merge_commit"))
        target_branch = _clean_optional_string(merged_metadata.get("merge_target_branch"))
        suffix = f" at {merge_commit[:12]}" if merge_commit else ""
        branch_text = f" into {target_branch}" if target_branch else ""
        result = WorkspaceMergeResult(
            status="merged",
            summary=f"Recovered completed merge for issue {issue.id}{branch_text}{suffix}.",
            target_branch=target_branch,
            worktree_head=_clean_optional_string(merged_metadata.get("worktree_head")),
            merge_commit=merge_commit,
            worktree_commit=_clean_optional_string(merged_metadata.get("worktree_commit")),
            cleanup_removed=True,
        )
        return WorkspaceMergeRecovery("done", "success", result.summary, result.artifact_markdown())

    def _recover_pull_request_metadata_locked(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        pull_request_metadata: dict[str, Any],
    ) -> WorkspaceMergeRecovery:
        cleanup_removed = bool(pull_request_metadata.get("cleanup_removed"))
        if not cleanup_removed:
            if info.worktree_root.exists():
                try:
                    self._ensure_clean_repo(info.worktree_root, "issue worktree")
                    self._git(info.source_root, "worktree", "remove", str(info.worktree_root))
                except WorkspaceError as exc:
                    return _merge_recovery_blocked(f"Pull request cleanup recovery could not remove worktree: {exc}")
            pull_request_metadata = {**pull_request_metadata, "cleanup_removed": True}
            self.artifacts.write_pull_request_metadata(issue.id, pull_request_metadata)
        self.artifacts.delete_workspace_metadata(issue.id)
        self._delete_internal_merge_branches(issue.id, info.source_root, force=True)
        result = self._pull_request_result_from_metadata(
            issue,
            pull_request_metadata,
            summary_prefix="Recovered completed pull request finalization",
        )
        return WorkspaceMergeRecovery("awaiting_pr_closure", "success", result.summary, result.artifact_markdown())

    def _recover_interrupted_merge_locked(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        branch: str,
    ) -> WorkspaceMergeRecovery:
        try:
            self._ensure_clean_repo(info.source_root, "source repo")
        except WorkspaceError as exc:
            return _merge_recovery_blocked(str(exc))
        try:
            branch = self._validate_local_target_branch(info.source_root, branch)
        except WorkspaceError as exc:
            return _merge_recovery_blocked(str(exc))

        conflict_files = self._conflicted_files(info.worktree_root)
        if conflict_files:
            result = WorkspaceMergeResult(
                status="conflicts",
                summary=f"Recovered interrupted merge conflicts for issue {issue.id}; AI resolution is required.",
                target_branch=branch,
                worktree_head=self._git(info.worktree_root, "rev-parse", "HEAD"),
                merge_commit=None,
                conflict_files=conflict_files,
                cleanup_removed=False,
            )
            return WorkspaceMergeRecovery(
                "ready_for_merge_conflict_resolution",
                "interrupted",
                result.summary,
                result.artifact_markdown(),
            )

        worktree_head = self._git(info.worktree_root, "rev-parse", "HEAD")
        if worktree_head != info.source_head and self._git_check(
            info.source_root,
            "merge-base",
            "--is-ancestor",
            worktree_head,
            branch,
        ):
            try:
                self._ensure_clean_repo(info.worktree_root, "issue worktree")
            except WorkspaceError as exc:
                return _merge_recovery_blocked(
                    f"Source branch already contains committed worktree head, but cleanup found dirty worktree state: {exc}"
                )
            merge_commit = self._git(info.source_root, "rev-parse", branch)
            merged_metadata = {
                **info.to_metadata(),
                "cleanup_removed": False,
                "merge_commit": merge_commit,
                "merge_target_branch": branch,
                "merged_at": utc_now_iso(),
                "worktree_head": worktree_head,
                "worktree_commit": None,
            }
            self.artifacts.write_merged_workspace_metadata(issue.id, merged_metadata)
            self._git(info.source_root, "worktree", "remove", str(info.worktree_root))
            merged_metadata["cleanup_removed"] = True
            self.artifacts.write_merged_workspace_metadata(issue.id, merged_metadata)
            self.artifacts.delete_workspace_metadata(issue.id)
            result = WorkspaceMergeResult(
                status="merged",
                summary=f"Recovered already-merged issue {issue.id} worktree in {branch} at {merge_commit[:12]}.",
                target_branch=branch,
                worktree_head=worktree_head,
                merge_commit=merge_commit,
                cleanup_removed=True,
            )
            return WorkspaceMergeRecovery("done", "success", result.summary, result.artifact_markdown())

        return _merge_recovery_retry(
            f"Recovered interrupted merge for issue {issue.id} before source merge completed; retrying merge is safe."
        )

    def _merge_and_cleanup_locked(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        branch: str,
        approval_message: str | None,
        requested_mode: str,
        requested_remote: str | None,
    ) -> WorkspaceMergeResult:
        preflight = self._prepare_merge_in_worktree(issue, info, branch)
        if preflight.status == "conflicts":
            return WorkspaceMergeResult(
                status="conflicts",
                summary=(
                    "Merge conflicts require AI resolution before finalizing "
                    f"issue {issue.id} into {preflight.target_branch}."
                ),
                target_branch=preflight.target_branch,
                worktree_head=preflight.worktree_head,
                merge_commit=None,
                worktree_commit=preflight.worktree_commit,
                conflict_files=preflight.conflict_files,
                cleanup_removed=False,
            )

        assert preflight.integrated_head is not None
        final_mode, selected_remote = self._select_finalization_mode(info, requested_mode, requested_remote)
        if final_mode == "pull_request":
            if selected_remote is None:
                raise WorkspaceError("Pull request finalization requires a supported remote.")
            return self._finalize_pull_request(
                issue,
                info,
                preflight,
                selected_remote,
            )
        return self._finalize_local_merge(issue, info, preflight, approval_message)

    def _prepare_merge_in_worktree(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        branch: str,
    ) -> _MergePreflightResult:
        self._ensure_clean_repo(info.source_root, "source repo")
        branch = self._validate_local_target_branch(info.source_root, branch)
        worktree_commit = self._commit_dirty_worktree(issue, info)
        self._ensure_clean_repo(info.worktree_root, "issue worktree")
        worktree_head = self._git(info.worktree_root, "rev-parse", "HEAD")
        if worktree_head == info.source_head:
            raise WorkspaceError(
                f"Worktree has no commits beyond source HEAD {info.source_head[:12]}; nothing can be merged."
            )

        merge_branch = self._merge_branch_name(issue.id, worktree_head)
        self._checkout_internal_merge_branch(info, merge_branch, worktree_head)

        if not self._git_check(info.worktree_root, "merge-base", "--is-ancestor", branch, "HEAD"):
            try:
                self._git(
                    info.worktree_root,
                    "merge",
                    "--no-ff",
                    branch,
                    "-m",
                    f"Merge {branch} into issue {issue.id} workspace",
                )
            except WorkspaceError as exc:
                conflict_files = self._conflicted_files(info.worktree_root)
                if not conflict_files:
                    try:
                        self._git(info.worktree_root, "merge", "--abort")
                    except WorkspaceError as abort_exc:
                        raise WorkspaceError(f"{exc}\n\nAlso failed to abort workspace merge: {abort_exc}") from abort_exc
                    raise
                return _MergePreflightResult(
                    status="conflicts",
                    target_branch=branch,
                    merge_branch=merge_branch,
                    worktree_head=worktree_head,
                    integrated_head=None,
                    worktree_commit=worktree_commit,
                    conflict_files=conflict_files,
                )

        integrated_head = self._git(info.worktree_root, "rev-parse", "HEAD")
        self._ensure_clean_repo(info.worktree_root, "issue worktree")
        self._ensure_clean_repo(info.source_root, "source repo")
        return _MergePreflightResult(
            status="ready",
            target_branch=branch,
            merge_branch=merge_branch,
            worktree_head=worktree_head,
            integrated_head=integrated_head,
            worktree_commit=worktree_commit,
        )

    def _finalize_local_merge(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        preflight: _MergePreflightResult,
        approval_message: str | None,
    ) -> WorkspaceMergeResult:
        integrated_head = _require_string(preflight.integrated_head, "integrated worktree head")
        branch = preflight.target_branch
        merge_branch = preflight.merge_branch
        self._ensure_clean_repo(info.source_root, "source repo")
        self._git(info.source_root, "checkout", branch)

        if not self._git_check(info.source_root, "merge-base", "--is-ancestor", integrated_head, branch):
            try:
                self._git(
                    info.source_root,
                    "merge",
                    "--no-ff",
                    "--log",
                    "-m",
                    self._source_merge_message(issue, approval_message),
                    integrated_head,
                )
            except WorkspaceError as exc:
                try:
                    self._git(info.source_root, "merge", "--abort")
                except WorkspaceError as abort_exc:
                    raise WorkspaceError(f"{exc}\n\nAlso failed to abort source merge: {abort_exc}") from abort_exc
                raise

        merge_commit = self._git(info.source_root, "rev-parse", "HEAD")
        merged_metadata = {
            **info.to_metadata(),
            "cleanup_removed": False,
            "merge_commit": merge_commit,
            "merge_target_branch": branch,
            "merged_at": utc_now_iso(),
            "worktree_head": integrated_head,
            "worktree_commit": preflight.worktree_commit,
            "merge_branch": merge_branch,
        }
        self.artifacts.write_merged_workspace_metadata(issue.id, merged_metadata)
        self._git(info.source_root, "worktree", "remove", str(info.worktree_root))
        merged_metadata["cleanup_removed"] = True
        self.artifacts.write_merged_workspace_metadata(issue.id, merged_metadata)
        self.artifacts.delete_workspace_metadata(issue.id)
        self._delete_internal_merge_branches(issue.id, info.source_root, force=True)
        return WorkspaceMergeResult(
            status="merged",
            summary=f"Merged issue {issue.id} worktree into {branch} at {merge_commit[:12]}.",
            target_branch=branch,
            worktree_head=integrated_head,
            merge_commit=merge_commit,
            worktree_commit=preflight.worktree_commit,
            cleanup_removed=True,
        )

    def _sync_source_into_workspace_locked(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        metadata: dict[str, Any],
        branch: str,
        old_source_head: str | None,
        synced_at: str,
    ) -> WorkspaceSourceSyncResult:
        self._ensure_clean_repo(info.source_root, "source repo")
        self._ensure_clean_repo(info.worktree_root, "issue worktree")
        if not self._git_check(info.source_root, "rev-parse", "--verify", branch):
            raise WorkspaceError(f"Recorded source branch does not exist in source repo: {branch}")

        new_source_head = self._git(info.source_root, "rev-parse", branch)
        worktree_head = self._git(info.worktree_root, "rev-parse", "HEAD")
        if self._git_check(info.worktree_root, "merge-base", "--is-ancestor", branch, "HEAD"):
            result = WorkspaceSourceSyncResult(
                status="up_to_date",
                summary=f"Source branch {branch} is already included in issue {issue.id} workspace.",
                target_branch=branch,
                old_source_head=old_source_head,
                new_source_head=new_source_head,
                worktree_head=worktree_head,
                synced_at=synced_at,
            )
            self._write_source_sync_metadata(issue.id, metadata, result)
            return result

        try:
            self._git(
                info.worktree_root,
                "merge",
                "--no-ff",
                branch,
                "-m",
                f"Merge {branch} into issue {issue.id} workspace before implementation rework",
            )
        except WorkspaceError as exc:
            conflict_files = self._conflicted_files(info.worktree_root)
            if not conflict_files:
                try:
                    self._git(info.worktree_root, "merge", "--abort")
                except WorkspaceError as abort_exc:
                    raise WorkspaceError(f"{exc}\n\nAlso failed to abort workspace source sync: {abort_exc}") from abort_exc
                raise
            result = WorkspaceSourceSyncResult(
                status="conflicts",
                summary=(
                    f"Source sync conflicts require resolution before implementation rework for issue "
                    f"{issue.id}."
                ),
                target_branch=branch,
                old_source_head=old_source_head,
                new_source_head=new_source_head,
                worktree_head=worktree_head,
                conflict_files=conflict_files,
                synced_at=synced_at,
            )
            self._write_source_sync_metadata(issue.id, metadata, result)
            return result

        sync_commit = self._git(info.worktree_root, "rev-parse", "HEAD")
        self._ensure_clean_repo(info.worktree_root, "issue worktree")
        result = WorkspaceSourceSyncResult(
            status="synced",
            summary=f"Merged source branch {branch} into issue {issue.id} workspace at {sync_commit[:12]}.",
            target_branch=branch,
            old_source_head=old_source_head,
            new_source_head=new_source_head,
            worktree_head=worktree_head,
            sync_commit=sync_commit,
            synced_at=synced_at,
        )
        self._write_source_sync_metadata(issue.id, metadata, result)
        return result

    def _prepare_pull_request_conflict_workspace_locked(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        pull_request_metadata: dict[str, Any],
        status_snapshot: PullRequestStatusSnapshot,
    ) -> WorkspaceMergeResult:
        remote = self._pull_request_repair_remote(info, pull_request_metadata)
        source_branch = _clean_optional_string(status_snapshot.source_branch) or _clean_optional_string(
            pull_request_metadata.get("source_branch")
        )
        target_branch = _clean_optional_string(status_snapshot.target_branch) or _clean_optional_string(
            pull_request_metadata.get("target_branch")
        )
        if source_branch is None or target_branch is None:
            raise WorkspaceError("Pull request metadata is missing source or target branch for conflict repair.")
        self._validate_remote_branch_name(info.source_root, source_branch, "pull request source branch")
        self._validate_remote_branch_name(info.source_root, target_branch, "pull request target branch")
        head_ref, target_ref = self._pull_request_repair_refs(issue.id)
        self._git_remote(
            info.source_root,
            "fetch",
            "--no-tags",
            remote.remote_name,
            f"+refs/heads/{source_branch}:{head_ref}",
            f"+refs/heads/{target_branch}:{target_ref}",
        )
        fetched_head = self._git(info.source_root, "rev-parse", head_ref)
        if status_snapshot.head_sha and fetched_head != status_snapshot.head_sha:
            raise WorkspaceError(
                f"Provider reported PR head {status_snapshot.head_sha[:12]}, but fetching "
                f"{source_branch} produced {fetched_head[:12]}. Retry PR monitoring after the provider settles."
            )
        target_head = self._git(info.source_root, "rev-parse", target_ref)
        repair_info = WorkspaceInfo(
            issue_id=issue.id,
            original_repo_path=info.original_repo_path,
            source_root=info.source_root,
            source_git_common_dir=info.source_git_common_dir,
            relative_subpath=info.relative_subpath,
            worktree_root=info.worktree_root,
            workspace_repo_path=info.workspace_repo_path,
            source_branch=target_branch,
            source_head=target_head,
            created_at=utc_now_iso(),
        )
        self._recreate_pull_request_repair_worktree(issue, repair_info, head_ref)
        repair_metadata = {
            **repair_info.to_metadata(),
            "hosted_pull_request_conflict": True,
            "hosted_pull_request_provider": status_snapshot.provider,
            "hosted_pull_request_url": status_snapshot.url,
            "hosted_pull_request_source_branch": source_branch,
            "hosted_pull_request_target_branch": target_branch,
            "hosted_pull_request_head": fetched_head,
            "hosted_pull_request_target_head": target_head,
            "hosted_pull_request_detected_at": status_snapshot.checked_at,
        }
        self.artifacts.write_workspace_metadata(issue.id, repair_metadata)
        if self._git_check(repair_info.worktree_root, "merge-base", "--is-ancestor", target_ref, "HEAD"):
            synced_head = self._git(repair_info.worktree_root, "rev-parse", "HEAD")
            self.artifacts.write_workspace_metadata(
                issue.id,
                {**repair_metadata, "hosted_pull_request_target_sync_status": "already_included"},
            )
            return self._hosted_pull_request_result(
                issue,
                pull_request_metadata,
                status_snapshot,
                status="target_synced",
                summary=f"Hosted pull request for issue {issue.id} already includes latest target branch {target_branch}.",
                target_branch=target_branch,
                worktree_head=synced_head,
            )
        try:
            self._git(
                repair_info.worktree_root,
                "merge",
                "--no-ff",
                target_ref,
                "-m",
                f"Merge {target_branch} into hosted PR workspace for issue {issue.id}",
            )
        except WorkspaceError as exc:
            conflict_files = self._conflicted_files(repair_info.worktree_root)
            if not conflict_files:
                try:
                    self._git(repair_info.worktree_root, "merge", "--abort")
                except WorkspaceError as abort_exc:
                    raise WorkspaceError(f"{exc}\n\nAlso failed to abort hosted PR target merge: {abort_exc}") from abort_exc
                raise
            self.artifacts.write_workspace_metadata(
                issue.id,
                {
                    **repair_metadata,
                    "hosted_pull_request_target_sync_status": "conflicts",
                    "hosted_pull_request_conflict_files": list(conflict_files),
                },
            )
            return self._hosted_pull_request_result(
                issue,
                pull_request_metadata,
                status_snapshot,
                status="conflicts",
                summary=(
                    f"Hosted pull request for issue {issue.id} has merge conflicts with latest target branch "
                    f"{target_branch}; AI resolution is required."
                ),
                target_branch=target_branch,
                worktree_head=self._git(repair_info.worktree_root, "rev-parse", "HEAD"),
                conflict_files=conflict_files,
            )
        synced_head = self._git(repair_info.worktree_root, "rev-parse", "HEAD")
        self._ensure_clean_repo(repair_info.worktree_root, "issue worktree")
        self.artifacts.write_workspace_metadata(
            issue.id,
            {
                **repair_metadata,
                "hosted_pull_request_target_sync_status": "synced",
                "hosted_pull_request_target_sync_commit": synced_head,
            },
        )
        return self._hosted_pull_request_result(
            issue,
            pull_request_metadata,
            status_snapshot,
            status="target_synced",
            summary=(
                f"Merged latest target branch {target_branch} into hosted PR repair workspace for issue "
                f"{issue.id} at {synced_head[:12]}."
            ),
            target_branch=target_branch,
            worktree_head=synced_head,
        )

    def _finalize_pull_request(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        preflight: _MergePreflightResult,
        selected_remote: _SelectedRemote,
    ) -> WorkspaceMergeResult:
        integrated_head = _require_string(preflight.integrated_head, "integrated worktree head")
        source_branch = self._pull_request_branch(issue)
        self._push_pull_request_branch(issue, info, selected_remote, source_branch, integrated_head)
        body_path = self._write_pull_request_body(
            issue,
            preflight,
            selected_remote,
            source_branch,
            integrated_head,
        )
        request = PullRequestRequest(
            source_branch=source_branch,
            target_branch=preflight.target_branch,
            title=f"Issue {issue.id}: {_single_line(issue.title)}",
            body_path=body_path,
        )
        try:
            pr_result = create_or_get_pull_request(selected_remote.remote, request)
        except PullRequestError as exc:
            raise WorkspaceError(str(exc)) from exc
        metadata = self._pull_request_metadata(
            issue,
            info,
            preflight,
            selected_remote,
            pr_result,
            integrated_head,
        )
        self.artifacts.write_pull_request_metadata(issue.id, metadata)
        self._git(info.source_root, "worktree", "remove", str(info.worktree_root))
        metadata["cleanup_removed"] = True
        self.artifacts.write_pull_request_metadata(issue.id, metadata)
        self.artifacts.delete_workspace_metadata(issue.id)
        self._delete_internal_merge_branches(issue.id, info.source_root, force=True)
        return self._pull_request_result_from_metadata(issue, metadata)

    def _validate_local_target_branch(self, source_root: Path, branch: str) -> str:
        target = _clean_optional_string(branch)
        if target is None:
            raise WorkspaceError("Target branch is empty.")
        if target == "HEAD" or target.startswith("refs/") or "@{" in target:
            raise WorkspaceError(
                f"Target branch must be an existing local branch name, not a revision or full ref: {target}"
            )
        if not self._git_check(source_root, "check-ref-format", "--branch", target):
            raise WorkspaceError(f"Target branch is not a valid local branch name: {target}")
        if not self._git_check(source_root, "show-ref", "--verify", "--quiet", f"refs/heads/{target}"):
            raise WorkspaceError(f"Target branch is not an existing local branch in source repo: {target}")
        return target

    @staticmethod
    def _merge_branch_name(issue_id: int, worktree_head: str) -> str:
        return f"agent-team/issue-{issue_id}-merge-{worktree_head[:12]}"

    def _checkout_internal_merge_branch(
        self,
        info: WorkspaceInfo,
        merge_branch: str,
        worktree_head: str,
    ) -> None:
        ref = f"refs/heads/{merge_branch}"
        if self._git_check(info.source_root, "show-ref", "--verify", "--quiet", ref):
            existing_head = self._git(info.source_root, "rev-parse", ref)
            if existing_head != worktree_head:
                raise WorkspaceError(
                    f"Internal merge branch '{merge_branch}' already exists at {existing_head[:12]}, "
                    f"not prepared worktree head {worktree_head[:12]}. Refusing to reset it."
                )
            self._git(info.worktree_root, "checkout", merge_branch)
            return
        self._git(info.worktree_root, "checkout", "-b", merge_branch, worktree_head)

    @staticmethod
    def _is_internal_merge_branch(issue_id: int, merge_branch: str) -> bool:
        prefix = f"agent-team/issue-{issue_id}-merge-"
        suffix = merge_branch[len(prefix) :] if merge_branch.startswith(prefix) else ""
        return bool(re.fullmatch(r"[0-9a-f]{12}", suffix))

    def _select_finalization_mode(
        self,
        info: WorkspaceInfo,
        requested_mode: str,
        requested_remote: str | None,
    ) -> tuple[str, _SelectedRemote | None]:
        mode = _normalize_merge_mode(requested_mode)
        if mode == "local":
            return "local", None
        remote_names = self._remote_names(info.source_root)
        if not remote_names:
            if mode == "pull_request":
                raise WorkspaceError("Pull request finalization was requested, but the source repo has no remotes.")
            return "local", None
        selected_remote = self._select_pull_request_remote(info, requested_remote, remote_names)
        if selected_remote is not None:
            return "pull_request", selected_remote
        if requested_remote:
            raise WorkspaceError(
                f"Remote '{requested_remote}' does not use a supported pull request provider. "
                "Supported remote providers are GitHub and Azure DevOps Services; approve with --mode local "
                "to merge locally instead."
            )
        raise WorkspaceError(
            "The source repo has remotes, but none use a supported pull request provider. "
            "Supported remote providers are GitHub and Azure DevOps Services; approve with --mode local "
            "to merge locally instead."
        )

    def _select_pull_request_remote(
        self,
        info: WorkspaceInfo,
        requested_remote: str | None,
        remote_names: list[str],
    ) -> _SelectedRemote | None:
        names = [requested_remote] if requested_remote else remote_names
        validation_errors: list[str] = []
        for remote_name in names:
            if remote_name is None:
                continue
            if remote_name not in remote_names:
                raise WorkspaceError(f"Requested pull request remote does not exist: {remote_name}")
            remote_url = self._git(info.source_root, "remote", "get-url", remote_name)
            push_url = self._git(info.source_root, "remote", "get-url", "--push", remote_name)
            remote = parse_pull_request_remote(remote_name, remote_url)
            if remote is not None:
                try:
                    self._validate_pull_request_push_url(remote, push_url)
                except WorkspaceError as exc:
                    if requested_remote:
                        raise
                    validation_errors.append(str(exc))
                    continue
                return _SelectedRemote(remote=remote, push_url=push_url)
        if validation_errors:
            raise WorkspaceError(
                "The source repo has remotes that use supported pull request providers, but none have usable "
                f"push URLs. {' '.join(validation_errors)}"
            )
        return None

    def _validate_pull_request_push_url(self, remote: PullRequestRemote, push_url: str) -> None:
        if _remote_url_embeds_credentials(push_url):
            raise WorkspaceError(
                f"Remote '{remote.remote_name}' push URL embeds credentials. Refusing to pass "
                "credential-bearing remote URLs to git; use a Git credential helper, an authenticated SSH remote, "
                "or approve with --mode local to merge locally instead."
            )
        if _remote_url_has_query_or_fragment(push_url):
            raise WorkspaceError(
                f"Remote '{remote.remote_name}' push URL includes query or fragment components. Refusing to pass "
                "potentially credential-bearing remote URLs to git; use a Git credential helper, an authenticated "
                "SSH remote, or approve with --mode local to merge locally instead."
            )
        push_remote = parse_pull_request_remote(remote.remote_name, push_url)
        if push_remote is None:
            raise WorkspaceError(
                f"Remote '{remote.remote_name}' fetch URL resolves to {_pull_request_remote_label(remote)}, "
                "but its push URL does not resolve to a supported pull request provider repository. "
                "The fetch and push URLs must resolve to the same GitHub or Azure DevOps Services repository; "
                "approve with --mode local to merge locally instead."
            )
        if not _same_pull_request_repository(remote, push_remote):
            raise WorkspaceError(
                f"Remote '{remote.remote_name}' fetch URL resolves to {_pull_request_remote_label(remote)}, "
                f"but its push URL resolves to {_pull_request_remote_label(push_remote)}. "
                "Refusing to push issue work to a different repository; fix the remote push URL or approve "
                "with --mode local to merge locally instead."
            )

    def _remote_names(self, source_root: Path) -> list[str]:
        return [line for line in self._git(source_root, "remote").splitlines() if line]

    def _pull_request_branch(self, issue: Issue) -> str:
        branch = f"{self.pr_branch_prefix}{issue.id}"
        if not self._git_check(Path.cwd(), "check-ref-format", "--branch", branch):
            raise WorkspaceError(
                f"Configured pull request branch name is not a valid Git branch: {branch}. "
                "Check AGENT_TEAM_PR_BRANCH_PREFIX."
            )
        return branch

    def _push_pull_request_branch(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        selected_remote: _SelectedRemote,
        source_branch: str,
        integrated_head: str,
    ) -> None:
        remote_head = self._remote_branch_head(info.source_root, selected_remote.push_url, source_branch)
        if remote_head == integrated_head:
            return
        push_args = ["push"]
        if remote_head is None:
            push_args.append(f"--force-with-lease=refs/heads/{source_branch}:")
        else:
            prior_metadata = self._read_pull_request_metadata_for_push(issue.id)
            expected_heads = self._metadata_pr_branch_expected_heads(
                prior_metadata,
                selected_remote.remote,
                source_branch,
            )
            if not expected_heads:
                raise WorkspaceError(
                    f"Remote branch '{source_branch}' already exists on '{selected_remote.remote.remote_name}' "
                    f"at {remote_head[:12]}, not the prepared head {integrated_head[:12]}. "
                    "Refusing to overwrite it without existing pull_request.json ownership metadata for the "
                    "same provider repository and a recorded branch head."
                )
            expected_head = next((head for head in expected_heads if head == remote_head), None)
            if expected_head is None:
                expected_text = ", ".join(head[:12] for head in expected_heads)
                raise WorkspaceError(
                    f"Remote branch '{source_branch}' already exists on '{selected_remote.remote.remote_name}' "
                    f"at {remote_head[:12]}, but pull_request.json last recorded the orchestrator-owned head as "
                    f"{expected_text}. Refusing to overwrite remote branch changes made after PR finalization; "
                    "inspect the remote branch before retrying."
                )
            push_args.append(f"--force-with-lease=refs/heads/{source_branch}:{expected_head}")
        push_args.extend(
            [
                selected_remote.push_url,
                f"{integrated_head}:refs/heads/{source_branch}",
            ]
        )
        self._git_remote(info.source_root, *push_args)

    def _remote_branch_head(self, source_root: Path, remote_ref: str, branch: str) -> str | None:
        if _remote_url_embeds_credentials(remote_ref):
            raise WorkspaceError(
                "Refusing to pass a credential-bearing remote URL to git. Use a Git credential helper "
                "or an authenticated SSH remote instead."
            )
        if _remote_url_has_query_or_fragment(remote_ref):
            raise WorkspaceError(
                "Refusing to pass a remote URL with query or fragment components to git. Use a Git "
                "credential helper or an authenticated SSH remote instead."
            )
        output = self._git_remote(source_root, "ls-remote", "--heads", remote_ref, branch)
        for line in output.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == f"refs/heads/{branch}":
                return parts[0]
        return None

    def _read_pull_request_metadata_for_push(self, issue_id: int) -> dict[str, Any] | None:
        try:
            return self.artifacts.read_pull_request_metadata(issue_id)
        except (OSError, ValueError) as exc:
            raise WorkspaceError(
                f"Pull request metadata is unreadable for issue {issue_id}: "
                f"{self.artifacts.pull_request_metadata_path(issue_id)}\n{exc}"
            ) from exc

    @staticmethod
    def _metadata_owns_pr_branch(
        metadata: dict[str, Any] | None,
        remote: PullRequestRemote,
        source_branch: str,
    ) -> bool:
        if metadata is None:
            return False
        metadata_identity = _metadata_pull_request_remote_identity(metadata)
        return (
            _clean_optional_string(metadata.get("remote_name")) == remote.remote_name
            and _clean_optional_string(metadata.get("source_branch")) == source_branch
            and metadata_identity == _pull_request_remote_identity(remote)
        )

    @classmethod
    def _metadata_pr_branch_expected_heads(
        cls,
        metadata: dict[str, Any] | None,
        remote: PullRequestRemote,
        source_branch: str,
    ) -> tuple[str, ...]:
        if not cls._metadata_owns_pr_branch(metadata, remote, source_branch) or metadata is None:
            return ()
        heads: list[str] = []
        for key in ("head_commit", "worktree_head"):
            head = _clean_optional_string(metadata.get(key))
            if head is not None and head not in heads:
                heads.append(head)
        return tuple(heads)

    def _write_pull_request_body(
        self,
        issue: Issue,
        preflight: _MergePreflightResult,
        selected_remote: _SelectedRemote,
        source_branch: str,
        integrated_head: str,
    ) -> Path:
        path = self.artifacts.issue_dir(issue.id) / "pull_request_body.md"
        lines = [
            f"# Issue {issue.id}: {_single_line(issue.title)}",
            "",
            SAFE_PULL_REQUEST_DESCRIPTION,
            "",
            "## Agent-team finalization",
            "",
            f"- Source branch: `{source_branch}`",
            f"- Target branch: `{preflight.target_branch}`",
            f"- Head commit: `{integrated_head}`",
            f"- Provider: `{selected_remote.remote.provider}`",
            f"- Remote: `{selected_remote.remote.remote_name}`",
        ]
        if preflight.worktree_commit:
            lines.append(f"- Final workspace snapshot commit: `{preflight.worktree_commit}`")
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return path

    def _pull_request_metadata(
        self,
        issue: Issue,
        info: WorkspaceInfo,
        preflight: _MergePreflightResult,
        selected_remote: _SelectedRemote,
        pr_result: PullRequestResult,
        integrated_head: str,
    ) -> dict[str, Any]:
        return {
            **info.to_metadata(),
            "cleanup_removed": False,
            "finalized_at": utc_now_iso(),
            "mode": "pull_request",
            "provider": pr_result.provider,
            "remote_name": pr_result.remote_name,
            "remote_url": _redact_remote_url(selected_remote.remote.url),
            "remote_identity": list(_pull_request_remote_identity(selected_remote.remote)),
            "source_branch": pr_result.source_branch,
            "target_branch": pr_result.target_branch,
            "head_commit": integrated_head,
            "worktree_head": integrated_head,
            "worktree_commit": preflight.worktree_commit,
            "merge_branch": preflight.merge_branch,
            "title": pr_result.title,
            "url": pr_result.url,
            "id": pr_result.id,
            "number": pr_result.number,
            "pr_status": pr_result.status,
            "is_existing": pr_result.is_existing,
            "monitoring_enabled": True,
            "last_status_check_at": None,
            "last_status": pr_result.status,
            "last_merge_state": None,
            "last_head_commit": integrated_head,
            "last_is_open": None,
            "last_is_closed": None,
            "last_is_merged": None,
            "last_has_conflicts": None,
            "final_status": None,
            "closed_at": None,
            "merged_at": None,
            "conflict_detected_at": None,
            "conflict_comment_posted_at": None,
            "conflict_comment_error": None,
            "conflict_comment_id": None,
            "conflict_comment_url": None,
            "conflict_comment_key": f"issue-{issue.id}:{pr_result.provider}:{pr_result.id or pr_result.number or pr_result.source_branch}",
            "conflict_comment_marker": (
                f"<!-- agent-team-orchestrator-conflict:"
                f"issue-{issue.id}:{pr_result.provider}:{pr_result.id or pr_result.number or pr_result.source_branch} -->"
            ),
            "raw": pr_result.raw,
        }

    def _pull_request_result_from_metadata(
        self,
        issue: Issue,
        metadata: dict[str, Any],
        *,
        summary_prefix: str | None = None,
    ) -> WorkspaceMergeResult:
        url = _clean_optional_string(metadata.get("url"))
        source_branch = _clean_optional_string(metadata.get("source_branch"))
        target_branch = _clean_optional_string(metadata.get("target_branch"))
        head_commit = _clean_optional_string(metadata.get("head_commit")) or _clean_optional_string(
            metadata.get("worktree_head")
        )
        existing = bool(metadata.get("is_existing"))
        action = "Reused existing pull request" if existing else "Opened pull request"
        if summary_prefix:
            action = summary_prefix
        target_text = f" into {target_branch}" if target_branch else ""
        url_text = f": {url}" if url else "."
        summary = f"{action} for issue {issue.id}{target_text}{url_text}"
        return WorkspaceMergeResult(
            status="pull_request",
            summary=summary,
            target_branch=target_branch,
            worktree_head=head_commit,
            merge_commit=None,
            worktree_commit=_clean_optional_string(metadata.get("worktree_commit")),
            cleanup_removed=bool(metadata.get("cleanup_removed")),
            pr_provider=_clean_optional_string(metadata.get("provider")),
            pr_remote_name=_clean_optional_string(metadata.get("remote_name")),
            pr_source_branch=source_branch,
            pr_url=url,
            pr_id=_clean_optional_string(metadata.get("id")),
            pr_number=_clean_optional_int(metadata.get("number")),
            pr_status=_clean_optional_string(metadata.get("pr_status")),
            pr_is_existing=existing,
        )

    def reset_issue_workspace(self, issue: Issue) -> WorkspaceResetResult:
        candidates: list[Path] = []
        contexts: list[tuple[Path, Path]] = []
        warnings: list[str] = []

        try:
            metadata = self.artifacts.read_workspace_metadata(issue.id)
        except ValueError as exc:
            metadata = None
            warnings.append(str(exc))
        if metadata:
            worktree_root = _clean_optional_string(metadata.get("worktree_root"))
            if worktree_root:
                candidates.append(Path(worktree_root).expanduser())
            try:
                info = WorkspaceInfo.from_metadata(metadata)
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(
                    f"Workspace metadata is invalid for issue {issue.id}: "
                    f"{self.artifacts.workspace_metadata_path(issue.id)} ({exc})"
                )
            else:
                context = self._source_context(info.source_root)
                if context is not None:
                    contexts.append(context)

        if issue.repo_path:
            try:
                paths = self._workspace_paths(issue)
            except WorkspaceError as exc:
                warnings.append(f"Current repo workspace path could not be resolved: {exc}")
            else:
                candidates.append(paths.worktree_root)
                contexts.append((paths.source_root, paths.source_git_common_dir))

        if self.worktrees_dir.is_dir():
            candidates.extend(sorted(self.worktrees_dir.glob(f"issue-{issue.id}-*")))

        candidates = _unique_paths(candidates)
        removed: list[str] = []
        remaining = list(candidates)
        for source_root, source_git_common_dir in _unique_contexts(contexts):
            with self._repo_lock_for_common_dir(source_git_common_dir):
                for candidate in list(remaining):
                    safe_path = self._safe_reset_candidate(issue.id, candidate)
                    if self._remove_with_git(source_root, safe_path):
                        removed.append(str(safe_path))
                        remaining.remove(candidate)
                self._delete_reset_merge_branch(issue.id, source_root)
                try:
                    self._git(source_root, "worktree", "prune")
                except WorkspaceError as exc:
                    warnings.append(str(exc))

        for candidate in remaining:
            safe_path = self._safe_reset_candidate(issue.id, candidate)
            if self._remove_with_filesystem(safe_path):
                removed.append(str(safe_path))

        return WorkspaceResetResult(removed_paths=tuple(removed), warnings=tuple(warnings))

    def _workspace_metadata_for_update(self, issue: Issue, info: WorkspaceInfo) -> dict[str, Any]:
        try:
            metadata = self.artifacts.read_workspace_metadata(issue.id)
        except (OSError, ValueError) as exc:
            raise WorkspaceError(
                f"Workspace metadata is unreadable for issue {issue.id}: "
                f"{self.artifacts.workspace_metadata_path(issue.id)}"
            ) from exc
        if metadata is None:
            raise WorkspaceError(f"Workspace metadata is missing for issue {issue.id}")
        try:
            metadata_info = WorkspaceInfo.from_metadata(metadata)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(
                f"Workspace metadata is invalid for issue {issue.id}: "
                f"{self.artifacts.workspace_metadata_path(issue.id)}"
            ) from exc
        if metadata_info != info:
            raise WorkspaceError(
                f"Workspace metadata changed while preparing source sync for issue {issue.id}; rerun the phase."
            )
        return dict(metadata)

    def _write_source_sync_metadata(
        self,
        issue_id: int,
        metadata: dict[str, Any],
        result: WorkspaceSourceSyncResult,
    ) -> None:
        updated = {
            **metadata,
            "last_source_sync_at": result.synced_at,
            "last_source_sync_branch": result.target_branch,
            "last_source_sync_commit": result.sync_commit,
            "last_source_sync_conflict_files": list(result.conflict_files),
            "last_source_sync_head": result.new_source_head,
            "last_source_sync_previous_head": result.old_source_head,
            "last_source_sync_status": result.status,
        }
        self.artifacts.write_workspace_metadata(issue_id, updated)

    def _conflicted_files(self, repo_path: Path) -> tuple[str, ...]:
        return tuple(line for line in self._git(repo_path, "diff", "--name-only", "--diff-filter=U").splitlines() if line)

    def _commit_dirty_worktree(self, issue: Issue, info: WorkspaceInfo) -> str | None:
        subject, body = self._final_snapshot_commit_message(issue)
        return self._commit_workspace_changes(info.worktree_root, subject, body)

    def _commit_workspace_changes(
        self,
        repo_path: Path,
        subject: str,
        body: str,
        *,
        allow_empty: bool = False,
    ) -> str | None:
        if not self._git(repo_path, "status", "--porcelain") and not allow_empty:
            return None
        self._git(repo_path, "add", "-A")
        staged_is_empty = self._git_check(repo_path, "diff", "--cached", "--quiet")
        if staged_is_empty and not allow_empty:
            return None
        commit_args = ["commit"]
        if staged_is_empty:
            commit_args.append("--allow-empty")
        commit_args.extend(["-m", subject, "-m", body])
        self._git(repo_path, *commit_args)
        return self._git(repo_path, "rev-parse", "HEAD")

    @staticmethod
    def _phase_snapshot_commit_message(
        issue: Issue,
        phase: str,
        run_id: str,
        summary: str,
        artifact_markdown: str,
        next_phase: str,
    ) -> tuple[str, str]:
        description = _phase_snapshot_description(issue, phase, summary, artifact_markdown)
        subject = f"Issue {issue.id}: {_truncate_snapshot_subject_detail(description)}"
        body_lines = [
            f"Issue: {issue.id}",
            f"Phase: {phase}",
            f"Run ID: {run_id}",
            f"Summary: {description}",
        ]
        runner_summary = _clean_artifact_summary(summary)
        if runner_summary and runner_summary != description:
            body_lines.append(f"Runner Summary: {runner_summary}")
        body_lines.append(f"Next Phase: {next_phase}")
        body = "\n".join(body_lines)
        return subject, body

    @staticmethod
    def _final_snapshot_commit_message(issue: Issue) -> tuple[str, str]:
        subject = f"Issue {issue.id} final workspace snapshot: {_single_line(issue.title)}"
        body = "\n".join(
            [
                "Committed by agent-team orchestrator during merge because uncommitted issue-worktree changes remained.",
                "",
                f"Issue: {issue.id}",
                "Phase: merge",
                "Summary: Final safety-net commit for remaining workspace changes.",
            ]
        )
        return subject, body

    @staticmethod
    def _source_merge_message(issue: Issue, approval_message: str | None) -> str:
        lines = [f"Merge issue {issue.id}: {_single_line(issue.title)}"]
        if approval_message:
            lines.extend(["", f"Merge approval: {_single_line(approval_message)}"])
        return "\n".join(lines)

    def _merge_head_exists(self, repo_path: Path) -> bool:
        return self._git_check(repo_path, "rev-parse", "-q", "--verify", "MERGE_HEAD")

    @staticmethod
    def _resolve_requested_repo_path(issue: Issue) -> Path:
        if not issue.repo_path:
            raise WorkspaceError(f"Target repo path is required to prepare a workspace for issue {issue.id}")
        repo_path = Path(issue.repo_path).expanduser()
        if not repo_path.is_dir():
            raise WorkspaceError(f"Target repo path does not exist or is not a directory: {issue.repo_path}")
        return repo_path.resolve()

    def _workspace_paths(self, issue: Issue) -> _WorkspacePaths:
        repo_path = self._resolve_requested_repo_path(issue)
        source_root = self._git_path(repo_path, "rev-parse", "--show-toplevel")
        relative_subpath = self._git(repo_path, "rev-parse", "--show-prefix").rstrip("/")
        source_git_common_dir = self._git_common_dir(source_root)
        worktree_root = self.worktrees_dir / self._workspace_name(issue.id, source_root, source_git_common_dir)
        workspace_repo_path = worktree_root / relative_subpath if relative_subpath else worktree_root
        return _WorkspacePaths(
            repo_path=repo_path,
            source_root=source_root,
            source_git_common_dir=source_git_common_dir,
            relative_subpath=relative_subpath,
            worktree_root=worktree_root,
            workspace_repo_path=workspace_repo_path,
        )

    def _recover_orphaned_worktree(self, issue: Issue, paths: _WorkspacePaths) -> WorkspaceInfo:
        try:
            actual_root = self._git_path(paths.worktree_root, "rev-parse", "--show-toplevel")
        except WorkspaceError as exc:
            raise WorkspaceError(
                f"Workspace path already exists without metadata and is not a usable Git worktree for issue "
                f"{issue.id}: {paths.worktree_root}"
            ) from exc
        if actual_root != paths.worktree_root.resolve():
            raise WorkspaceError(
                f"Workspace path already exists without metadata and is not the expected Git worktree for issue "
                f"{issue.id}: {paths.worktree_root}"
            )
        if self._git_common_dir(paths.worktree_root) != paths.source_git_common_dir:
            raise WorkspaceError(
                f"Workspace path already exists without metadata but belongs to a different Git repository for issue "
                f"{issue.id}: {paths.worktree_root}"
            )
        if not paths.workspace_repo_path.is_dir():
            raise WorkspaceError(
                f"Workspace path already exists without metadata but the requested subdirectory is missing for issue "
                f"{issue.id}: {paths.workspace_repo_path}"
            )
        status = self._git(paths.worktree_root, "status", "--porcelain")
        if status:
            raise WorkspaceError(
                f"Workspace path exists without metadata and has local changes for issue {issue.id}. "
                f"Inspect {paths.worktree_root} before rerunning.\n{status}"
            )
        source_head = self._git(paths.source_root, "rev-parse", "HEAD")
        if not self._git_check(paths.worktree_root, "merge-base", "--is-ancestor", source_head, "HEAD"):
            raise WorkspaceError(
                f"Workspace path exists without metadata and cannot be related safely to source HEAD {source_head[:12]} "
                f"for issue {issue.id}: {paths.worktree_root}"
            )
        info = WorkspaceInfo(
            issue_id=issue.id,
            original_repo_path=paths.repo_path,
            source_root=paths.source_root,
            source_git_common_dir=paths.source_git_common_dir,
            relative_subpath=paths.relative_subpath,
            worktree_root=paths.worktree_root.resolve(),
            workspace_repo_path=paths.workspace_repo_path.resolve(),
            source_branch=self._source_branch(paths.source_root),
            source_head=source_head,
            created_at=utc_now_iso(),
        )
        self.artifacts.write_workspace_metadata(issue.id, info.to_metadata())
        return info

    def _existing_from_metadata(self, issue: Issue, paths: _WorkspacePaths) -> WorkspaceInfo | None:
        try:
            metadata = self.artifacts.read_workspace_metadata(issue.id)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc
        if metadata is None:
            return None
        try:
            info = WorkspaceInfo.from_metadata(metadata)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(
                f"Workspace metadata is invalid for issue {issue.id}: "
                f"{self.artifacts.workspace_metadata_path(issue.id)}"
            ) from exc
        self._validate_reusable_workspace(
            info,
            issue=issue,
            repo_path=paths.repo_path,
            source_root=paths.source_root,
            source_git_common_dir=paths.source_git_common_dir,
            relative_subpath=paths.relative_subpath,
            worktree_root=paths.worktree_root,
            workspace_repo_path=paths.workspace_repo_path,
        )
        return info

    def _validate_reusable_workspace(
        self,
        info: WorkspaceInfo,
        *,
        issue: Issue,
        repo_path: Path,
        source_root: Path,
        source_git_common_dir: Path,
        relative_subpath: str,
        worktree_root: Path,
        workspace_repo_path: Path,
    ) -> None:
        expected = WorkspaceInfo(
            issue_id=issue.id,
            original_repo_path=repo_path,
            source_root=source_root,
            source_git_common_dir=source_git_common_dir,
            relative_subpath=relative_subpath,
            worktree_root=worktree_root.resolve(),
            workspace_repo_path=workspace_repo_path.resolve(),
            source_branch=info.source_branch,
            source_head=info.source_head,
            created_at=info.created_at,
        )
        mismatches = [
            field_name
            for field_name in (
                "issue_id",
                "original_repo_path",
                "source_root",
                "source_git_common_dir",
                "relative_subpath",
                "worktree_root",
                "workspace_repo_path",
            )
            if getattr(info, field_name) != getattr(expected, field_name)
        ]
        if mismatches:
            fields = ", ".join(mismatches)
            raise WorkspaceError(
                f"Workspace metadata for issue {issue.id} does not match the requested repo ({fields}). "
                f"Inspect or remove {self.artifacts.workspace_metadata_path(issue.id)} before rerunning."
            )
        if not info.worktree_root.is_dir():
            raise WorkspaceError(f"Workspace worktree root is missing for issue {issue.id}: {info.worktree_root}")
        actual_root = self._git_path(info.worktree_root, "rev-parse", "--show-toplevel")
        if actual_root != info.worktree_root.resolve():
            raise WorkspaceError(
                f"Workspace path is not the expected Git worktree for issue {issue.id}: {info.worktree_root}"
            )
        if not info.workspace_repo_path.is_dir():
            raise WorkspaceError(
                f"Workspace repo path is missing for issue {issue.id}: {info.workspace_repo_path}"
            )

    def _validate_merge_workspace(self, issue: Issue, info: WorkspaceInfo) -> None:
        if info.issue_id != issue.id:
            raise WorkspaceError(f"Workspace metadata issue id does not match issue {issue.id}")
        if issue.repo_path and Path(issue.repo_path).expanduser().resolve() != info.original_repo_path:
            raise WorkspaceError(
                f"Workspace metadata for issue {issue.id} does not match the requested repo. "
                f"Inspect or remove {self.artifacts.workspace_metadata_path(issue.id)} before merging."
            )
        if not info.source_root.is_dir():
            raise WorkspaceError(f"Workspace source repo is missing for issue {issue.id}: {info.source_root}")
        if not info.worktree_root.is_dir():
            raise WorkspaceError(f"Workspace worktree root is missing for issue {issue.id}: {info.worktree_root}")
        actual_root = self._git_path(info.worktree_root, "rev-parse", "--show-toplevel")
        if actual_root != info.worktree_root.resolve():
            raise WorkspaceError(
                f"Workspace path is not the expected Git worktree for issue {issue.id}: {info.worktree_root}"
            )
        if not info.workspace_repo_path.is_dir():
            raise WorkspaceError(f"Workspace repo path is missing for issue {issue.id}: {info.workspace_repo_path}")

    def _validate_pull_request_repair_context(self, issue: Issue, info: WorkspaceInfo) -> None:
        if info.issue_id != issue.id:
            raise WorkspaceError(f"Pull request metadata issue id does not match issue {issue.id}")
        if issue.repo_path and Path(issue.repo_path).expanduser().resolve() != info.original_repo_path:
            raise WorkspaceError(
                f"Pull request metadata for issue {issue.id} does not match the requested repo. "
                f"Inspect or remove {self.artifacts.pull_request_metadata_path(issue.id)} before monitoring."
            )
        if not info.source_root.is_dir():
            raise WorkspaceError(f"Workspace source repo is missing for issue {issue.id}: {info.source_root}")
        if self._git_common_dir(info.source_root) != info.source_git_common_dir:
            raise WorkspaceError(
                f"Workspace source repo Git common directory changed for issue {issue.id}: {info.source_root}"
            )

    def _pull_request_repair_remote(
        self,
        info: WorkspaceInfo,
        pull_request_metadata: dict[str, Any],
    ) -> PullRequestRemote:
        try:
            metadata_remote = pull_request_remote_from_metadata(pull_request_metadata)
        except PullRequestError as exc:
            raise WorkspaceError(str(exc)) from exc
        remote_names = self._remote_names(info.source_root)
        if metadata_remote.remote_name not in remote_names:
            raise WorkspaceError(
                f"Recorded pull request remote no longer exists in source repo: {metadata_remote.remote_name}"
            )
        remote_url = self._git(info.source_root, "remote", "get-url", metadata_remote.remote_name)
        source_remote = parse_pull_request_remote(metadata_remote.remote_name, remote_url)
        if source_remote is None:
            raise WorkspaceError(
                f"Recorded pull request remote no longer uses a supported provider: {metadata_remote.remote_name}"
            )
        if not _same_pull_request_repository(metadata_remote, source_remote):
            raise WorkspaceError(
                f"Recorded pull request remote points to {_pull_request_remote_label(metadata_remote)}, "
                f"but source repo remote now points to {_pull_request_remote_label(source_remote)}."
            )
        return source_remote

    def _validate_remote_branch_name(self, source_root: Path, branch: str, label: str) -> None:
        if branch == "HEAD" or branch.startswith("refs/") or "@{" in branch:
            raise WorkspaceError(f"Recorded {label} is not a safe branch name: {branch}")
        if not self._git_check(source_root, "check-ref-format", "--branch", branch):
            raise WorkspaceError(f"Recorded {label} is not a valid Git branch name: {branch}")

    @staticmethod
    def _pull_request_repair_refs(issue_id: int) -> tuple[str, str]:
        prefix = f"refs/agent-team/issues/{issue_id}/hosted-pr"
        return f"{prefix}/head", f"{prefix}/target"

    def _recreate_pull_request_repair_worktree(self, issue: Issue, info: WorkspaceInfo, head_ref: str) -> None:
        expected_head = self._git(info.source_root, "rev-parse", head_ref)
        expected_target_head = info.source_head
        if info.worktree_root.exists() or self._is_registered_worktree(info.source_root, info.worktree_root):
            if not info.worktree_root.is_dir():
                raise WorkspaceError(f"Workspace worktree path is not a directory for issue {issue.id}: {info.worktree_root}")
            try:
                actual_root = self._git_path(info.worktree_root, "rev-parse", "--show-toplevel")
            except WorkspaceError as exc:
                raise WorkspaceError(
                    f"Workspace path exists but is not a usable Git worktree for issue {issue.id}: {info.worktree_root}"
                ) from exc
            if actual_root != info.worktree_root.resolve():
                raise WorkspaceError(f"Workspace path is not the expected Git worktree for issue {issue.id}: {info.worktree_root}")
            conflict_files = self._conflicted_files(info.worktree_root)
            if conflict_files:
                actual_head = self._git(info.worktree_root, "rev-parse", "HEAD")
                if actual_head != expected_head:
                    raise WorkspaceError(
                        f"Existing hosted PR repair workspace for issue {issue.id} is based on PR head "
                        f"{actual_head[:12]}, but the provider now reports {expected_head[:12]}. "
                        "Refusing to reuse stale conflict markers; inspect or reset the issue workspace before retrying."
                    )
                try:
                    metadata = self.artifacts.read_workspace_metadata(issue.id) or {}
                except (OSError, ValueError) as exc:
                    raise WorkspaceError(
                        f"Workspace metadata is unreadable for issue {issue.id}: "
                        f"{self.artifacts.workspace_metadata_path(issue.id)}\n{exc}"
                    ) from exc
                recorded_head = _clean_optional_string(metadata.get("hosted_pull_request_head"))
                if recorded_head is not None and recorded_head != expected_head:
                    raise WorkspaceError(
                        f"Existing hosted PR repair metadata for issue {issue.id} records PR head "
                        f"{recorded_head[:12]}, but the provider now reports {expected_head[:12]}. "
                        "Refusing to reuse stale conflict markers; inspect or reset the issue workspace before retrying."
                    )
                recorded_target_head = _clean_optional_string(metadata.get("hosted_pull_request_target_head"))
                if recorded_target_head != expected_target_head:
                    recorded_target = recorded_target_head[:12] if recorded_target_head else "missing"
                    raise WorkspaceError(
                        f"Existing hosted PR repair metadata for issue {issue.id} records target head "
                        f"{recorded_target}, but the provider target branch now resolves to {expected_target_head[:12]}. "
                        "Refusing to reuse stale conflict markers; inspect or reset the issue workspace before retrying."
                    )
                return
            self._ensure_clean_repo(info.worktree_root, "issue worktree")
            self._git(info.source_root, "worktree", "remove", str(info.worktree_root))
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._git(info.source_root, "worktree", "add", "--detach", str(info.worktree_root), head_ref)
        if not info.workspace_repo_path.is_dir():
            raise WorkspaceError(
                f"Isolated workspace subdirectory does not exist for issue {issue.id}: {info.workspace_repo_path}"
            )

    @staticmethod
    def _hosted_pull_request_result(
        issue: Issue,
        pull_request_metadata: dict[str, Any],
        status_snapshot: PullRequestStatusSnapshot,
        *,
        status: str,
        summary: str,
        target_branch: str | None,
        worktree_head: str | None,
        conflict_files: tuple[str, ...] = (),
    ) -> WorkspaceMergeResult:
        return WorkspaceMergeResult(
            status=status,
            summary=summary,
            target_branch=target_branch,
            worktree_head=worktree_head,
            merge_commit=None,
            conflict_files=conflict_files,
            cleanup_removed=False,
            pr_provider=status_snapshot.provider,
            pr_remote_name=_clean_optional_string(pull_request_metadata.get("remote_name")),
            pr_source_branch=_clean_optional_string(pull_request_metadata.get("source_branch")),
            pr_url=status_snapshot.url or _clean_optional_string(pull_request_metadata.get("url")),
            pr_id=_clean_optional_string(pull_request_metadata.get("id")),
            pr_number=_clean_optional_int(pull_request_metadata.get("number")),
            pr_status=status_snapshot.status,
            pr_is_existing=bool(pull_request_metadata.get("is_existing")),
        )

    def _ensure_clean_source(self, source_root: Path) -> None:
        status = self._git(source_root, "status", "--porcelain")
        if status:
            raise WorkspaceError(
                "Target repo has uncommitted or untracked changes; commit or stash them before creating "
                f"an isolated workspace: {source_root}"
            )

    def _ensure_clean_repo(self, repo_path: Path, label: str) -> None:
        status = self._git(repo_path, "status", "--porcelain")
        if status:
            raise WorkspaceError(f"{label.capitalize()} has uncommitted or untracked changes: {repo_path}\n{status}")

    def _git_common_dir(self, source_root: Path) -> Path:
        raw_common_dir = Path(self._git(source_root, "rev-parse", "--git-common-dir"))
        common_dir = raw_common_dir if raw_common_dir.is_absolute() else source_root / raw_common_dir
        return common_dir.resolve()

    @staticmethod
    def _source_branch(source_root: Path) -> str | None:
        branch = WorkspaceManager._git(source_root, "rev-parse", "--abbrev-ref", "HEAD")
        return None if branch == "HEAD" else branch

    @staticmethod
    def _workspace_name(issue_id: int, source_root: Path, source_git_common_dir: Path) -> str:
        digest = hashlib.sha1(f"{source_root}\0{source_git_common_dir}".encode("utf-8")).hexdigest()[:12]
        return f"issue-{issue_id}-{digest}"

    @contextlib.contextmanager
    def _repo_lock(self, info: WorkspaceInfo):
        with self._repo_lock_for_common_dir(info.source_git_common_dir):
            yield

    @contextlib.contextmanager
    def _repo_lock_for_common_dir(self, source_git_common_dir: Path):
        if self.locks_dir is None:
            yield
            return
        try:
            import fcntl
        except ImportError:
            yield
            return

        self.locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.locks_dir / f"repo-{self._repo_lock_key(source_git_common_dir)}.lock"
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _source_context(self, source_root: Path) -> tuple[Path, Path] | None:
        if not source_root.is_dir():
            return None
        try:
            root = self._git_path(source_root, "rev-parse", "--show-toplevel")
            return root, self._git_common_dir(root)
        except WorkspaceError:
            return None

    def _safe_reset_candidate(self, issue_id: int, path: Path) -> Path:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            raise WorkspaceError(f"Workspace reset candidate is not an absolute path: {candidate}")
        worktrees_root = self.worktrees_dir.resolve()
        expected_prefix = f"issue-{issue_id}-"
        if candidate == worktrees_root:
            raise WorkspaceError(f"Refusing to remove worktrees root during reset: {candidate}")
        if candidate.parent.resolve() != worktrees_root:
            if not _is_relative_to(candidate.parent.resolve(), worktrees_root):
                raise WorkspaceError(f"Refusing to consider workspace outside worktrees directory: {candidate}")
            raise WorkspaceError(
                f"Refusing to remove non-root workspace path for issue {issue_id}; "
                f"expected an immediate child of {worktrees_root}: {candidate}"
            )
        if not candidate.name.startswith(expected_prefix):
            raise WorkspaceError(
                f"Refusing to remove workspace that is not owned by issue {issue_id}: {candidate}"
            )
        if candidate.is_symlink():
            return candidate
        if candidate.exists():
            if not _is_relative_to(candidate.resolve(), worktrees_root):
                raise WorkspaceError(f"Refusing to remove workspace outside worktrees directory: {candidate}")
            return candidate
        if not _is_relative_to(candidate.parent.resolve(), worktrees_root):
            raise WorkspaceError(f"Refusing to consider workspace outside worktrees directory: {candidate}")
        return candidate

    def _remove_with_git(self, source_root: Path, path: Path) -> bool:
        if path.is_symlink():
            return False
        registered = self._is_registered_worktree(source_root, path)
        if not path.exists() and not registered:
            return False
        try:
            self._git(source_root, "worktree", "remove", "--force", "--force", str(path))
        except WorkspaceError as exc:
            if registered:
                raise WorkspaceError(f"Unable to remove registered Git worktree during reset: {path}\n{exc}") from exc
            return False
        return True

    def _is_registered_worktree(self, source_root: Path, path: Path) -> bool:
        candidate = path.resolve()
        output = self._git(source_root, "worktree", "list", "--porcelain")
        for line in output.splitlines():
            if not line.startswith("worktree "):
                continue
            registered = Path(line[len("worktree ") :]).expanduser().resolve()
            if registered == candidate:
                return True
        return False

    def _remove_with_filesystem(self, path: Path) -> bool:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return True
        if path.is_dir():
            if not _is_relative_to(path.resolve(), self.worktrees_dir.resolve()):
                raise WorkspaceError(f"Refusing to remove workspace outside worktrees directory: {path}")
            shutil.rmtree(path)
            return True
        return False

    def _delete_reset_merge_branch(self, issue_id: int, source_root: Path) -> None:
        self._delete_internal_merge_branches(issue_id, source_root, force=True)

    def _delete_internal_merge_branches(self, issue_id: int, source_root: Path, *, force: bool) -> None:
        if not source_root.is_dir():
            return
        output = self._git(
            source_root,
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/agent-team/issue-{issue_id}-merge-*",
        )
        for merge_branch in output.splitlines():
            if self._is_internal_merge_branch(issue_id, merge_branch):
                self._git(source_root, "branch", "-D" if force else "-d", merge_branch)

    @staticmethod
    def _repo_lock_key(source_git_common_dir: Path) -> str:
        return hashlib.sha1(str(source_git_common_dir.resolve()).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _git_path(repo_path: Path, *args: str) -> Path:
        return Path(WorkspaceManager._git(repo_path, *args)).resolve()

    @staticmethod
    def _git(repo_path: Path, *args: str) -> str:
        return WorkspaceManager._run_git(repo_path, *args)

    @staticmethod
    def _git_remote(repo_path: Path, *args: str) -> str:
        return WorkspaceManager._run_git(
            repo_path,
            *args,
            noninteractive=True,
            timeout_seconds=_REMOTE_GIT_COMMAND_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _run_git(
        repo_path: Path,
        *args: str,
        noninteractive: bool = False,
        timeout_seconds: float | None = None,
    ) -> str:
        command = ["git", "-C", str(repo_path), *args]
        env = None
        stdin = None
        if noninteractive:
            env = os.environ.copy()
            env.update(_REMOTE_GIT_NONINTERACTIVE_ENV)
            stdin = subprocess.DEVNULL
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                stdin=stdin,
                env=env,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            rendered_args = " ".join(_redact_git_text(arg) for arg in args)
            raise WorkspaceError(
                f"Git remote operation timed out after {timeout_seconds:g} seconds for {repo_path}: "
                f"{rendered_args}. Verify remote access and credentials are configured non-interactively "
                "before retrying."
            ) from exc
        except FileNotFoundError as exc:
            raise WorkspaceError("Git executable was not found while preparing an isolated workspace") from exc
        if completed.returncode != 0:
            detail = _redact_git_text((completed.stderr or completed.stdout or "").strip())
            if args[:2] == ("rev-parse", "--show-toplevel"):
                raise WorkspaceError(f"Target repo path is not inside a Git worktree: {repo_path}")
            rendered_args = " ".join(_redact_git_text(arg) for arg in args)
            raise WorkspaceError(f"Git command failed for {repo_path}: {rendered_args}\n{detail}".rstrip())
        return completed.stdout.strip()

    @staticmethod
    def _git_check(repo_path: Path, *args: str) -> bool:
        command = ["git", "-C", str(repo_path), *args]
        try:
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
        except FileNotFoundError as exc:
            raise WorkspaceError("Git executable was not found while preparing an isolated workspace") from exc
        return completed.returncode == 0


def _clean_optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _normalize_merge_mode(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    if normalized not in {"auto", "local", "pull_request"}:
        raise WorkspaceError(f"Merge mode must be one of: auto, local, pull_request (got {value!r})")
    return normalized


def _require_string(value: str | None, label: str) -> str:
    cleaned = _clean_optional_string(value)
    if cleaned is None:
        raise WorkspaceError(f"Missing {label}")
    return cleaned


def _same_pull_request_repository(left: PullRequestRemote, right: PullRequestRemote) -> bool:
    return _pull_request_remote_identity(left) == _pull_request_remote_identity(right)


def _metadata_pull_request_remote_identity(metadata: dict[str, Any]) -> tuple[str, ...] | None:
    identity = metadata.get("remote_identity")
    if isinstance(identity, (list, tuple)):
        parts: list[str] = []
        for part in identity:
            cleaned = _clean_optional_string(part)
            if cleaned is None:
                return None
            parts.append(_identity_part(cleaned))
        if parts:
            return tuple(parts)

    remote_url = _clean_optional_string(metadata.get("remote_url"))
    if remote_url is None:
        return None
    remote_name = _clean_optional_string(metadata.get("remote_name")) or "origin"
    remote = parse_pull_request_remote(remote_name, remote_url)
    if remote is None:
        return None
    provider = _clean_optional_string(metadata.get("provider"))
    remote_identity = _pull_request_remote_identity(remote)
    if provider is not None and _identity_part(provider) != remote_identity[0]:
        return None
    return remote_identity


def _pull_request_remote_identity(remote: PullRequestRemote) -> tuple[str, ...]:
    if remote.provider == "github":
        return (
            remote.provider,
            _identity_part(remote.owner),
            _identity_part(remote.repo),
        )
    if remote.provider == "azure-devops":
        return (
            remote.provider,
            _identity_part(remote.org),
            _identity_part(remote.project),
            _identity_part(remote.repo),
        )
    return (remote.provider, _identity_part(remote.url))


def _identity_part(value: str | None) -> str:
    return (value or "").strip().casefold()


def _pull_request_remote_label(remote: PullRequestRemote) -> str:
    if remote.provider == "github":
        owner = remote.owner or "unknown-owner"
        return f"GitHub {owner}/{remote.repo}"
    if remote.provider == "azure-devops":
        org = remote.org or "unknown-org"
        project = remote.project or "unknown-project"
        return f"Azure DevOps Services {org}/{project}/{remote.repo}"
    return f"{remote.provider} {_redact_remote_url(remote.url)}"


def _single_line(value: str) -> str:
    return " ".join(value.split()) or "untitled"


_SNAPSHOT_SUBJECT_DETAIL_LIMIT = 72

_PHASE_SNAPSHOT_SECTION_PRIORITY = {
    "implementation": (
        "summary of changes",
        "summary",
        "implementation",
        "result",
    ),
    "merge_conflict_resolution": (
        "resolution strategy",
        "conflicted files resolved",
        "summary of changes",
        "summary",
        "implementation",
        "result",
    ),
}

_KNOWN_ARTIFACT_SECTION_TITLES = {
    "summary of changes",
    "summary",
    "implementation",
    "result",
    "resolution strategy",
    "conflicted files resolved",
    "files changed",
    "tests checks run",
    "tests run",
    "checks run",
    "deviations from the plan",
    "deviations",
    "remaining risks",
    "recommendation",
    "human input request",
}


def _phase_snapshot_description(issue: Issue, phase: str, summary: str, artifact_markdown: str) -> str:
    artifact_summary = _artifact_summary_for_phase(phase, artifact_markdown)
    for candidate in (
        artifact_summary,
        _clean_artifact_summary(summary),
        _clean_artifact_summary(issue.title),
    ):
        if candidate:
            return candidate
    return "Workspace snapshot"


def _artifact_summary_for_phase(phase: str, artifact_markdown: str) -> str | None:
    sections = _artifact_sections(artifact_markdown)
    for title in _PHASE_SNAPSHOT_SECTION_PRIORITY.get(phase, _PHASE_SNAPSHOT_SECTION_PRIORITY["implementation"]):
        summary = sections.get(title)
        if summary:
            return summary
    if sections:
        return None
    return _first_useful_artifact_paragraph(artifact_markdown.splitlines())


def _artifact_sections(markdown: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current_title: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_lines
        if current_title is not None and current_title not in sections:
            summary = _first_useful_artifact_paragraph(current_lines)
            if summary:
                sections[current_title] = summary
        current_lines = []

    for line in markdown.splitlines():
        title = _artifact_section_title(line)
        if title is not None:
            flush()
            current_title = title
            continue
        if current_title is not None:
            current_lines.append(line)
    flush()
    return sections


def _artifact_section_title(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or _is_artifact_comment(stripped):
        return None
    if _is_recommendation_line(stripped):
        return "recommendation"

    title = stripped
    heading_match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", stripped)
    if heading_match:
        title = heading_match.group(1)
    else:
        numbered_match = re.match(r"^\d+[\.)]\s+(.+?)\s*:?\s*$", stripped)
        if numbered_match:
            title = numbered_match.group(1)
        elif stripped.endswith(":"):
            title = stripped[:-1]

    normalized = _normalize_artifact_section_title(title)
    if normalized in _KNOWN_ARTIFACT_SECTION_TITLES:
        return normalized
    return None


def _normalize_artifact_section_title(value: str) -> str:
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = value.replace("*", "").replace("_", " ")
    return " ".join(re.sub(r"[^0-9A-Za-z]+", " ", value).lower().split())


def _first_useful_artifact_paragraph(lines: list[str]) -> str | None:
    paragraph: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if (
            _is_artifact_comment(stripped)
            or _is_recommendation_line(stripped)
            or _artifact_section_title(stripped)
            or re.match(r"^#{1,6}\s+", stripped)
        ):
            continue
        cleaned = _clean_artifact_summary(stripped)
        if cleaned:
            paragraph.append(cleaned)
    if not paragraph:
        return None
    return _clean_artifact_summary(" ".join(paragraph))


def _clean_artifact_summary(value: str) -> str:
    without_comments = re.sub(r"<!--.*?-->", " ", value)
    without_inline_markers = re.sub(r"`([^`]+)`", r"\1", without_comments)
    text = _single_line(without_inline_markers)
    text = re.sub(r"^(?:[-*+]|\d+[\.)])\s+", "", text)
    text = text.replace("**", "").replace("__", "").strip("#*_ \t-")
    if not text or text == "untitled" or _is_recommendation_line(text):
        return ""
    return " ".join(text.split())


def _is_artifact_comment(line: str) -> bool:
    return line.startswith("<!--")


def _is_recommendation_line(line: str) -> bool:
    stripped = re.sub(r"^\d+[\.)]\s+", "", line.strip())
    stripped = stripped.strip("*_` ")
    return bool(re.match(r"recommendation\b", stripped, flags=re.IGNORECASE))


def _redact_remote_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    hostname = parsed.hostname
    if hostname is None:
        return value
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _remote_url_embeds_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    if parsed.password is not None:
        return True
    return parsed.scheme.lower() in {"http", "https"} and parsed.username is not None


def _remote_url_has_query_or_fragment(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(parsed.query or parsed.fragment)


_CREDENTIAL_URL_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9+.-]*://)([^/\s:@]+(?::[^/\s@]*)?@)",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)([^\s,;]+)")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|pat|secret|sig|token"
    r")(\s*[:=]\s*)([^\s&#;,]+)"
)
_GH_TOKEN_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9_]{20,})\b")


def _redact_git_text(value: str) -> str:
    redacted = _CREDENTIAL_URL_RE.sub(r"\1[redacted]@", value)
    redacted = _AUTH_HEADER_RE.sub(r"\1[redacted]", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[redacted]", redacted)
    return _GH_TOKEN_RE.sub("[redacted]", redacted)


def _truncate_snapshot_subject_detail(value: str) -> str:
    detail = _clean_artifact_summary(value) or "Workspace snapshot"
    if len(detail) <= _SNAPSHOT_SUBJECT_DETAIL_LIMIT:
        return detail
    limit = _SNAPSHOT_SUBJECT_DETAIL_LIMIT - 3
    boundary = detail.rfind(" ", 0, limit + 1)
    if boundary < 32:
        boundary = limit
    return detail[:boundary].rstrip(" ,.;:-") + "..."


def _merge_recovery_blocked(message: str) -> WorkspaceMergeRecovery:
    return WorkspaceMergeRecovery(
        next_phase="blocked",
        run_status="blocked",
        summary=message,
        artifact_markdown=f"# Merge Recovery\n\n{message}\n\nRecommendation: `blocked`",
    )


def _is_hosted_pull_request_repair_metadata(metadata: dict[str, Any] | None) -> bool:
    return bool(metadata and metadata.get("hosted_pull_request_conflict"))


def _merge_recovery_retry(message: str) -> WorkspaceMergeRecovery:
    return WorkspaceMergeRecovery(
        next_phase="ready_for_merge",
        run_status="interrupted",
        summary=message,
        artifact_markdown=f"# Merge Recovery\n\n{message}\n\nRecommendation: `ready_for_merge`",
    )


def _is_relative_to(path: Path, base_dir: Path) -> bool:
    try:
        path.relative_to(base_dir)
    except ValueError:
        return False
    return True


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path.expanduser())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path.expanduser())
    return unique


def _unique_contexts(contexts: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[Path, Path]] = []
    for source_root, source_git_common_dir in contexts:
        key = (str(source_root), str(source_git_common_dir))
        if key in seen:
            continue
        seen.add(key)
        unique.append((source_root, source_git_common_dir))
    return unique
