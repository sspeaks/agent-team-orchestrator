from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .models import Issue, utc_now_iso


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
        lines.append(f"- Worktree removed: `{str(self.cleanup_removed).lower()}`")
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
        recommendation = "ready_for_merge_conflict_resolution" if self.status == "conflicts" else "done"
        lines.extend(["", f"Recommendation: `{recommendation}`"])
        return "\n".join(lines)


@dataclass(frozen=True)
class WorkspaceMergeRecovery:
    next_phase: str
    run_status: str
    summary: str
    artifact_markdown: str


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
    def __init__(self, worktrees_dir: Path, artifacts: ArtifactStore, locks_dir: Path | None = None) -> None:
        self.worktrees_dir = worktrees_dir
        self.artifacts = artifacts
        self.locks_dir = locks_dir

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
        branch = requested_branch or info.source_branch
        if not branch:
            raise WorkspaceError(
                "Workspace was created from a detached source HEAD; approve merge with an explicit target branch."
            )

        with self._repo_lock(info):
            return self._merge_and_cleanup_locked(issue, info, branch, approval_message)

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
            return self._recover_interrupted_merge_locked(issue, info, branch)

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
        merge_branch = f"agent-team/issue-{issue.id}-merge"
        if info.source_root.is_dir() and self._git_check(info.source_root, "rev-parse", "--verify", merge_branch):
            try:
                self._git(info.source_root, "branch", "-d", merge_branch)
            except WorkspaceError:
                pass
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
        if not self._git_check(info.source_root, "rev-parse", "--verify", branch):
            return _merge_recovery_blocked(f"Target branch does not exist in source repo: {branch}")

        conflict_files = tuple(
            line
            for line in self._git(info.worktree_root, "diff", "--name-only", "--diff-filter=U").splitlines()
            if line
        )
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
    ) -> WorkspaceMergeResult:
        self._ensure_clean_repo(info.source_root, "source repo")
        if not self._git_check(info.source_root, "rev-parse", "--verify", branch):
            raise WorkspaceError(f"Target branch does not exist in source repo: {branch}")
        worktree_commit = self._commit_dirty_worktree(issue, info)
        self._ensure_clean_repo(info.worktree_root, "issue worktree")
        worktree_head = self._git(info.worktree_root, "rev-parse", "HEAD")
        if worktree_head == info.source_head:
            raise WorkspaceError(
                f"Worktree has no commits beyond source HEAD {info.source_head[:12]}; nothing can be merged."
            )

        merge_branch = f"agent-team/issue-{issue.id}-merge"
        self._git(info.worktree_root, "checkout", "-B", merge_branch, worktree_head)

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
                conflict_files = tuple(
                    line
                    for line in self._git(info.worktree_root, "diff", "--name-only", "--diff-filter=U").splitlines()
                    if line
                )
                if not conflict_files:
                    try:
                        self._git(info.worktree_root, "merge", "--abort")
                    except WorkspaceError as abort_exc:
                        raise WorkspaceError(f"{exc}\n\nAlso failed to abort workspace merge: {abort_exc}") from abort_exc
                    raise
                return WorkspaceMergeResult(
                    status="conflicts",
                    summary=f"Merge conflicts require AI resolution before merging issue {issue.id} into {branch}.",
                    target_branch=branch,
                    worktree_head=worktree_head,
                    merge_commit=None,
                    worktree_commit=worktree_commit,
                    conflict_files=conflict_files,
                    cleanup_removed=False,
                )

        integrated_head = self._git(info.worktree_root, "rev-parse", "HEAD")
        self._ensure_clean_repo(info.worktree_root, "issue worktree")
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
            "worktree_commit": worktree_commit,
        }
        self.artifacts.write_merged_workspace_metadata(issue.id, merged_metadata)
        self._git(info.source_root, "worktree", "remove", str(info.worktree_root))
        merged_metadata["cleanup_removed"] = True
        self.artifacts.write_merged_workspace_metadata(issue.id, merged_metadata)
        self.artifacts.delete_workspace_metadata(issue.id)
        if self._git_check(info.source_root, "rev-parse", "--verify", merge_branch):
            self._git(info.source_root, "branch", "-d", merge_branch)
        return WorkspaceMergeResult(
            status="merged",
            summary=f"Merged issue {issue.id} worktree into {branch} at {merge_commit[:12]}.",
            target_branch=branch,
            worktree_head=integrated_head,
            merge_commit=merge_commit,
            worktree_commit=worktree_commit,
            cleanup_removed=True,
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
        merge_branch = f"agent-team/issue-{issue_id}-merge"
        if self._git_check(source_root, "rev-parse", "--verify", merge_branch):
            self._git(source_root, "branch", "-D", merge_branch)

    @staticmethod
    def _repo_lock_key(source_git_common_dir: Path) -> str:
        return hashlib.sha1(str(source_git_common_dir.resolve()).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _git_path(repo_path: Path, *args: str) -> Path:
        return Path(WorkspaceManager._git(repo_path, *args)).resolve()

    @staticmethod
    def _git(repo_path: Path, *args: str) -> str:
        command = ["git", "-C", str(repo_path), *args]
        try:
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
        except FileNotFoundError as exc:
            raise WorkspaceError("Git executable was not found while preparing an isolated workspace") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            if args[:2] == ("rev-parse", "--show-toplevel"):
                raise WorkspaceError(f"Target repo path is not inside a Git worktree: {repo_path}")
            raise WorkspaceError(f"Git command failed for {repo_path}: {' '.join(args)}\n{detail}".rstrip())
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
