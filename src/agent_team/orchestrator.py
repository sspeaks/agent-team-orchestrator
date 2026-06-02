from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .artifacts import ArtifactStore
from .blocked_summary import summarize_blocked_reason
from .config import AppConfig
from .db import IssueStore, RecoveryResult
from .locks import is_definitely_dead_same_host_owner, make_lock_owner
from .models import AgentResult, Issue
from .runners.base import AgentRunner
from .runners.copilot_cli import PHASE_RECOMMENDATIONS, CopilotCliRunner
from .runners.dry_run import DryRunRunner
from .state_machine import default_next_phase, runnable_phase_for, running_phase_for
from .workspaces import WorkspaceError, WorkspaceInfo, WorkspaceManager, WorkspaceSourceSyncResult


@dataclass(frozen=True)
class ProcessResult:
    issue_id: int
    run_id: str
    phase: str
    status: str
    next_phase: str | None
    summary: str
    artifact_path: Path | None


class Orchestrator:
    def __init__(
        self,
        store: IssueStore,
        artifacts: ArtifactStore,
        config: AppConfig,
        runner: AgentRunner | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.config = config
        self.runner = runner or build_runner(config)
        self.owner = make_lock_owner(self.runner.name)

    def process_next(self, repo_path: str | None = None) -> ProcessResult | None:
        self.recover_interrupted_runs()
        for _ in range(3):
            issue = self.store.find_next_ready_issue(repo_path=repo_path)
            if issue is None:
                return None
            phase = runnable_phase_for(issue.phase)
            if phase is None:
                return None
            try:
                return self.process_issue(issue.id, phase)
            except RuntimeError:
                continue
        return None

    def recover_interrupted_runs(self) -> list[RecoveryResult]:
        merge_results = self._recover_interrupted_merges()
        source_sync_results = self._recover_interrupted_review_source_syncs()
        results = self.store.recover_interrupted_runs(
            is_lock_reclaimable=is_definitely_dead_same_host_owner,
            terminal_next_phase_resolver=self._terminal_next_phase_from_artifact,
        )
        all_results = [*merge_results, *source_sync_results, *results]
        self._record_recoveries(all_results)
        return all_results

    def recover_interrupted_issue(self, issue_id: int) -> RecoveryResult | None:
        issue = self.store.get_issue(issue_id)
        if issue.phase == "merging":
            result = self._recover_interrupted_merge_issue(issue)
        elif issue.phase == "reviewing":
            result = self._recover_interrupted_review_source_sync_issue(issue)
            if result is None:
                result = self.store.recover_interrupted_issue(
                    issue_id,
                    is_lock_reclaimable=is_definitely_dead_same_host_owner,
                    terminal_next_phase_resolver=self._terminal_next_phase_from_artifact,
                )
        else:
            result = self.store.recover_interrupted_issue(
                issue_id,
                is_lock_reclaimable=is_definitely_dead_same_host_owner,
                terminal_next_phase_resolver=self._terminal_next_phase_from_artifact,
            )
        if result is not None:
            self._record_recoveries([result])
        return result

    def process_issue(self, issue_id: int, phase: str | None = None) -> ProcessResult:
        self.recover_interrupted_issue(issue_id)
        issue = self.store.get_issue(issue_id)
        runnable = runnable_phase_for(issue.phase)
        if phase is None:
            if runnable is None:
                raise ValueError(f"Issue {issue.id} is not in a runnable phase: {issue.phase}")
            phase = runnable
        if phase != runnable:
            raise ValueError(f"Issue {issue.id} phase {issue.phase!r} is not ready for agent phase {phase!r}")

        run_id = str(uuid.uuid4())
        if not self.store.acquire_lock(
            issue.id,
            self.owner,
            self.config.lock_ttl_seconds,
            run_id,
            expected_phase=issue.phase,
            mark_scheduled=True,
        ):
            raise RuntimeError(f"Issue {issue.id} is locked by another worker")

        artifact_path: Path | None = None
        try:
            running_phase = running_phase_for(phase)
            self.store.transition_issue(issue.id, running_phase, run_id, f"Starting {phase}")
            issue = self.store.get_issue(issue.id)
            runner_name = "workspace-merge" if phase == "merge" else self.runner.name
            self.store.create_run(run_id, issue.id, phase, runner_name)
            self.artifacts.write_issue_snapshot(issue)
            self.artifacts.archive_phase_artifact_before_run(issue.id, phase, run_id)
            log_path = self.artifacts.start_run_log(issue.id, phase, run_id, runner_name)

            with _IssueLockHeartbeat(
                self.store,
                issue.id,
                self.owner,
                run_id,
                self.config.lock_ttl_seconds,
                expected_phase=running_phase,
            ):
                workspace_info: WorkspaceInfo | None = None
                runner_invoked = False
                if phase == "merge":
                    result = self._run_merge(issue)
                else:
                    try:
                        workspace_info = self._workspace_for_phase(phase, issue)
                    except WorkspaceError as exc:
                        result = self._blocked_workspace_result(str(exc))
                    else:
                        context = self._build_context(phase, issue, log_path, workspace_info)
                        runner_invoked = True
                        result = self.runner.run(phase, issue, context)
            if not self.store.refresh_run_lock(issue.id, self.owner, run_id, self.config.lock_ttl_seconds):
                raise RuntimeError(f"Run {run_id} is no longer current for issue {issue.id}")
            if result.raw_stdout or result.raw_stderr:
                self.artifacts.finish_run_log(log_path, result.raw_stdout, result.raw_stderr)
            elif self.runner.name != "copilot-cli" or not runner_invoked:
                self.artifacts.finish_run_log(log_path)
            try:
                artifact_path = self.artifacts.write_phase_artifact(issue.id, phase, run_id, result.artifact_markdown)
            except OSError as exc:
                message = f"Phase artifact persistence failed: {exc}"
                result = AgentResult(
                    status="blocked",
                    summary=message,
                    artifact_markdown="",
                    suggested_next_phase="blocked",
                    error=message,
                    blocked_summary=summarize_blocked_reason(message),
                )
                artifact_path = None
            next_phase = result.suggested_next_phase or default_next_phase(phase)
            if result.status == "requeued" and result.suggested_next_phase is not None:
                next_phase = result.suggested_next_phase
            elif result.status != "success":
                next_phase = "blocked"
            human_input_request = None
            final_status = result.status
            final_summary = result.summary
            final_error = result.error
            final_blocked_summary = result.blocked_summary
            if result.status == "success" and next_phase == "awaiting_human_input":
                try:
                    human_input_request = CopilotCliRunner._human_input_request_from_artifact(
                        phase,
                        result.artifact_markdown,
                    )
                except ValueError as exc:
                    final_status = "blocked"
                    final_summary = f"Invalid human input request: {exc}"
                    final_error = final_summary
                    final_blocked_summary = summarize_blocked_reason(final_summary)
                    next_phase = "blocked"
            if phase == "plan" and result.status == "success" and next_phase == "awaiting_plan_approval":
                self.artifacts.clear_plan_rejection_context(issue.id)
            workspace_source_sync: dict[str, object] | None = None
            if self._should_sync_source_before_rework(phase, workspace_info, final_status, next_phase):
                try:
                    sync_result = WorkspaceManager(
                        self.config.worktrees_dir,
                        self.artifacts,
                        self.config.locks_dir or self.config.home / "locks",
                    ).sync_source_into_workspace(issue, workspace_info)
                except WorkspaceError as exc:
                    final_status = "blocked"
                    final_summary = f"Workspace source sync failed: {exc}"
                    final_error = final_summary
                    final_blocked_summary = summarize_blocked_reason(final_summary)
                    next_phase = "blocked"
                    human_input_request = None
                    workspace_source_sync = {
                        "status": "blocked",
                        "summary": final_summary,
                    }
                    artifact_path = self.artifacts.write_phase_artifact(
                        issue.id,
                        phase,
                        run_id,
                        self._source_sync_blocked_artifact(phase, final_summary, result.artifact_markdown),
                    )
                else:
                    workspace_source_sync = self._source_sync_history(sync_result)
                    if sync_result.status == "conflicts":
                        try:
                            self.artifacts.archive_phase_artifact_before_run(issue.id, "merge", run_id)
                            sync_artifact_path = self.artifacts.write_phase_artifact(
                                issue.id,
                                "merge",
                                run_id,
                                sync_result.artifact_markdown(),
                            )
                        except OSError as exc:
                            final_status = "blocked"
                            final_summary = f"Workspace source sync conflict artifact persistence failed: {exc}"
                            final_error = final_summary
                            final_blocked_summary = summarize_blocked_reason(final_summary)
                            next_phase = "blocked"
                            workspace_source_sync["artifact_error"] = str(exc)
                            artifact_path = self.artifacts.write_phase_artifact(
                                issue.id,
                                phase,
                                run_id,
                                self._source_sync_blocked_artifact(phase, final_summary, result.artifact_markdown),
                            )
                        else:
                            workspace_source_sync["artifact_path"] = str(sync_artifact_path)
                            next_phase = "ready_for_merge_conflict_resolution"
            workspace_commit = None
            if self._should_commit_phase_snapshot(phase, workspace_info, final_status, next_phase):
                try:
                    workspace_commit = WorkspaceManager(self.config.worktrees_dir, self.artifacts).commit_phase_snapshot(
                        issue,
                        workspace_info,
                        phase=phase,
                        run_id=run_id,
                        summary=final_summary,
                        artifact_markdown=result.artifact_markdown,
                        next_phase=next_phase,
                    )
                except WorkspaceError as exc:
                    final_status = "blocked"
                    final_summary = f"Workspace snapshot commit failed: {exc}"
                    final_error = final_summary
                    final_blocked_summary = summarize_blocked_reason(final_summary)
                    next_phase = "blocked"
                    human_input_request = None
                    artifact_path = self.artifacts.write_phase_artifact(
                        issue.id,
                        phase,
                        run_id,
                        self._snapshot_blocked_artifact(phase, final_summary),
                    )
            history_event = {
                "run_id": run_id,
                "phase": phase,
                "status": final_status,
                "summary": final_summary,
                "artifact_path": str(artifact_path) if artifact_path is not None else None,
                "log_path": str(log_path),
            }
            if human_input_request is not None:
                history_event["human_input_request"] = {
                    "requested_by_phase": human_input_request.requested_by_phase,
                    "resume_phase": human_input_request.resume_phase,
                    "question": human_input_request.question,
                    "rationale": human_input_request.rationale,
                    "requested_decision": human_input_request.requested_decision,
                    "options": list(human_input_request.options),
                    "context": human_input_request.context,
                }
            if workspace_info is not None:
                history_event.update(
                    {
                        "workspace_root": str(workspace_info.worktree_root),
                        "workspace_repo_path": str(workspace_info.workspace_repo_path),
                        "source_repo_path": str(workspace_info.original_repo_path),
                    }
                )
            if workspace_commit is not None:
                history_event["workspace_commit"] = workspace_commit
            if workspace_source_sync is not None:
                history_event["workspace_source_sync"] = workspace_source_sync
            if next_phase == "blocked":
                final_blocked_summary = final_blocked_summary or summarize_blocked_reason(
                    result.artifact_markdown or final_error or result.error or final_summary
                )
                history_event["blocked_summary"] = final_blocked_summary
            self.artifacts.append_history(issue.id, history_event)
            if human_input_request is not None:
                created_request = self.store.complete_run_and_request_human_input(
                    run_id,
                    issue.id,
                    final_summary,
                    str(artifact_path),
                    human_input_request,
                )
                self.artifacts.append_human_input_request(created_request)
                self.artifacts.write_human_input_summary(
                    issue.id,
                    self.store.list_human_input_requests(issue.id),
                )
                updated_issue = self.store.get_issue(issue.id)
            else:
                self.store.complete_run(
                    run_id,
                    issue.id,
                    final_status,
                    final_summary,
                    str(artifact_path) if artifact_path is not None else None,
                    final_error,
                    next_phase=next_phase,
                )

                updated_issue = self.store.transition_issue(
                    issue.id,
                    next_phase,
                    run_id,
                    final_summary,
                    blocked_summary=final_blocked_summary,
                )
            self.artifacts.write_issue_snapshot(updated_issue)
            return ProcessResult(
                issue_id=issue.id,
                run_id=run_id,
                phase=phase,
                status=final_status,
                next_phase=next_phase,
                summary=final_summary,
                artifact_path=artifact_path,
            )
        finally:
            self.store.release_lock(issue.id, self.owner, run_id)

    def _recover_interrupted_merges(self) -> list[RecoveryResult]:
        results: list[RecoveryResult] = []
        for issue in self.store.list_issues("open"):
            if issue.phase != "merging":
                continue
            result = self._recover_interrupted_merge_issue(issue)
            if result is not None:
                results.append(result)
        return results

    def _recover_interrupted_merge_issue(self, issue: Issue) -> RecoveryResult | None:
        issue = self.store.claim_interrupted_merge_recovery(
            issue.id,
            self.owner,
            self.config.lock_ttl_seconds,
            is_lock_reclaimable=is_definitely_dead_same_host_owner,
        )
        if issue is None:
            return None
        manager = WorkspaceManager(
            self.config.worktrees_dir,
            self.artifacts,
            self.config.locks_dir or self.config.home / "locks",
        )
        lease = _MergeRecoveryLease(
            self.store,
            issue.id,
            self.owner,
            issue.current_run_id,
            self.config.lock_ttl_seconds,
        )
        with lease:
            recovery = manager.recover_interrupted_merge(issue)
        issue = self.store.refresh_issue_lock(
            issue.id,
            self.owner,
            self.config.lock_ttl_seconds,
            expected_phase="merging",
            expected_run_id=issue.current_run_id,
        )
        if issue is None:
            return None
        artifact_path: Path | None = None
        if issue.current_run_id:
            artifact_path = self.artifacts.write_phase_artifact(
                issue.id,
                "merge",
                issue.current_run_id,
                recovery.artifact_markdown,
            )
        return self.store.recover_interrupted_merge(
            issue.id,
            next_phase=recovery.next_phase,
            run_status=recovery.run_status,
            summary=recovery.summary,
            artifact_path=str(artifact_path) if artifact_path is not None else None,
            is_lock_reclaimable=is_definitely_dead_same_host_owner,
            claimed_owner=self.owner,
            claimed_run_id=issue.current_run_id,
        )

    def _recover_interrupted_review_source_syncs(self) -> list[RecoveryResult]:
        results: list[RecoveryResult] = []
        for issue in self.store.list_issues("open"):
            if issue.phase != "reviewing":
                continue
            result = self._recover_interrupted_review_source_sync_issue(issue)
            if result is not None:
                results.append(result)
        return results

    def _recover_interrupted_review_source_sync_issue(self, issue: Issue) -> RecoveryResult | None:
        manager = WorkspaceManager(
            self.config.worktrees_dir,
            self.artifacts,
            self.config.locks_dir or self.config.home / "locks",
        )
        if not manager.has_interrupted_source_sync_conflicts(issue):
            return None
        issue = self.store.claim_interrupted_review_source_sync_recovery(
            issue.id,
            self.owner,
            self.config.lock_ttl_seconds,
            is_lock_reclaimable=is_definitely_dead_same_host_owner,
        )
        if issue is None:
            return None
        lease = _ReviewSourceSyncRecoveryLease(
            self.store,
            issue.id,
            self.owner,
            issue.current_run_id,
            self.config.lock_ttl_seconds,
        )
        with lease:
            recovery = manager.recover_interrupted_source_sync(issue)
        issue = self.store.refresh_issue_lock(
            issue.id,
            self.owner,
            self.config.lock_ttl_seconds,
            expected_phase="reviewing",
            expected_run_id=issue.current_run_id,
        )
        if issue is None:
            return None
        if recovery is None:
            self.store.release_lock(issue.id, self.owner, issue.current_run_id)
            return None
        artifact_path: Path | None = None
        if issue.current_run_id:
            self.artifacts.archive_phase_artifact_before_run(issue.id, "merge", issue.current_run_id)
            artifact_path = self.artifacts.write_phase_artifact(
                issue.id,
                "merge",
                issue.current_run_id,
                recovery.artifact_markdown,
            )
        return self.store.recover_interrupted_review_source_sync(
            issue.id,
            next_phase=recovery.next_phase,
            run_status=recovery.run_status,
            summary=recovery.summary,
            artifact_path=str(artifact_path) if artifact_path is not None else None,
            is_lock_reclaimable=is_definitely_dead_same_host_owner,
            claimed_owner=self.owner,
            claimed_run_id=issue.current_run_id,
        )

    def _record_recoveries(self, results: list[RecoveryResult]) -> None:
        for result in results:
            event = {
                "run_id": result.run_id,
                "phase": result.agent_phase or result.previous_phase,
                "status": result.action,
                "summary": result.summary,
                "recovered_from": result.previous_phase,
                "recovered_to": result.next_phase,
            }
            if result.run_id and result.agent_phase:
                log_path = self.artifacts.run_log_path(result.issue_id, result.agent_phase, result.run_id)
                if log_path.exists():
                    event["log_path"] = str(log_path)
            self.artifacts.append_history(result.issue_id, event)
            self.artifacts.write_issue_snapshot(self.store.get_issue(result.issue_id))

    def _terminal_next_phase_from_artifact(self, run) -> str | None:
        if str(run["status"]) != "success":
            return "blocked"
        phase = str(run["phase"])
        if phase == "merge":
            return default_next_phase(phase)
        if str(run["runner"]) == "dry-run":
            return default_next_phase(phase)
        if phase not in PHASE_RECOMMENDATIONS:
            return None
        artifact_path_text = run["artifact_path"]
        path = Path(str(artifact_path_text)) if artifact_path_text else None
        if path is None or not path.is_file():
            path = self.artifacts.phase_artifact_path(int(run["issue_id"]), phase)
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        artifact = CopilotCliRunner._strip_run_header(content)
        return CopilotCliRunner._recommended_next_phase(phase, artifact)

    def _build_context(
        self,
        phase: str,
        issue: Issue,
        log_path: Path | None = None,
        workspace_info: WorkspaceInfo | None = None,
    ) -> dict[str, str]:
        prompt_path = Path(__file__).parent / "prompts" / f"{phase}.md"
        prompt_template = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""
        context = {
            "prompt_template": prompt_template,
            "artifacts_dir": str(self.artifacts.issue_dir(issue.id)),
            "phase_artifact": str(self.artifacts.phase_artifact_path(issue.id, phase)),
            "run_log": str(log_path) if log_path is not None else "",
            "workspace_repo_path": "",
            "workspace_root": "",
            "source_repo_path": issue.repo_path or "",
        }
        if workspace_info is not None:
            context.update(
                {
                    "workspace_repo_path": str(workspace_info.workspace_repo_path),
                    "workspace_root": str(workspace_info.worktree_root),
                    "source_repo_path": str(workspace_info.original_repo_path),
                }
            )
        return context

    def _workspace_for_phase(self, phase: str, issue: Issue) -> WorkspaceInfo | None:
        if self.runner.name != "copilot-cli" or not issue.repo_path:
            return None
        if phase in {"research", "plan"}:
            return None
        manager = WorkspaceManager(self.config.worktrees_dir, self.artifacts)
        if phase == "implementation":
            return manager.prepare(issue)
        if phase in {"validation", "review", "merge_conflict_resolution"}:
            return manager.existing(issue)
        return None

    def _run_merge(self, issue: Issue) -> AgentResult:
        try:
            merge_result = WorkspaceManager(
                self.config.worktrees_dir,
                self.artifacts,
                self.config.locks_dir or self.config.home / "locks",
            ).merge_and_cleanup(issue)
        except WorkspaceError as exc:
            return self._blocked_workspace_result(str(exc))
        next_phase = "ready_for_merge_conflict_resolution" if merge_result.status == "conflicts" else "done"
        return AgentResult(
            status="success",
            summary=merge_result.summary,
            artifact_markdown=merge_result.artifact_markdown(),
            suggested_next_phase=next_phase,
        )

    @staticmethod
    def _should_sync_source_before_rework(
        phase: str,
        workspace_info: WorkspaceInfo | None,
        final_status: str,
        next_phase: str | None,
    ) -> bool:
        return (
            phase == "review"
            and workspace_info is not None
            and final_status == "success"
            and next_phase == "ready_for_implementation"
        )

    @staticmethod
    def _source_sync_history(result: WorkspaceSourceSyncResult) -> dict[str, object]:
        return {
            "status": result.status,
            "summary": result.summary,
            "target_branch": result.target_branch,
            "old_source_head": result.old_source_head,
            "new_source_head": result.new_source_head,
            "worktree_head": result.worktree_head,
            "sync_commit": result.sync_commit,
            "conflict_files": list(result.conflict_files),
        }

    @staticmethod
    def _source_sync_blocked_artifact(phase: str, message: str, original_artifact_markdown: str) -> str:
        title = phase.replace("_", " ").title()
        sections = [f"# {title} Blocked During Source Sync", "", message]
        if original_artifact_markdown.strip():
            sections.extend(["", "## Original review artifact", "", original_artifact_markdown.strip()])
        sections.extend(["", f"Blocked summary: {summarize_blocked_reason(message)}", "Recommendation: `blocked`"])
        return "\n".join(sections)

    @staticmethod
    def _blocked_workspace_result(message: str) -> AgentResult:
        return AgentResult(
            status="blocked",
            summary=message,
            artifact_markdown=(
                f"{message}\n\n"
                f"Blocked summary: {summarize_blocked_reason(message)}\n"
                "Recommendation: `blocked`"
            ),
            suggested_next_phase="blocked",
            error=message,
            blocked_summary=summarize_blocked_reason(message),
        )

    @staticmethod
    def _should_commit_phase_snapshot(
        phase: str,
        workspace_info: WorkspaceInfo | None,
        final_status: str,
        next_phase: str | None,
    ) -> bool:
        return (
            workspace_info is not None
            and final_status == "success"
            and (
                (phase == "implementation" and next_phase == "ready_for_validation")
                or (
                    phase == "merge_conflict_resolution"
                    and next_phase in {"ready_for_validation", "ready_for_implementation"}
                )
            )
        )

    @staticmethod
    def _snapshot_blocked_artifact(phase: str, message: str) -> str:
        title = phase.replace("_", " ").title()
        return f"# {title} Blocked\n\n{message}\n\nBlocked summary: {summarize_blocked_reason(message)}\nRecommendation: `blocked`"


def build_runner(config: AppConfig) -> AgentRunner:
    if config.runner == "dry-run":
        return DryRunRunner()
    if config.runner == "copilot-cli":
        return CopilotCliRunner(
            command=config.copilot_command,
            timeout_seconds=config.runner_timeout_seconds,
            extra_args=config.copilot_args,
            plugin_dir=config.copilot_plugin_dir,
            permission_mode=config.copilot_permission_mode,
        )
    raise ValueError(f"Unknown runner: {config.runner}")


class _IssueLockHeartbeat:
    def __init__(
        self,
        store: IssueStore,
        issue_id: int,
        owner: str,
        run_id: str | None,
        ttl_seconds: int,
        *,
        expected_phase: str,
        thread_name: str | None = None,
    ) -> None:
        self.store = store
        self.issue_id = issue_id
        self.owner = owner
        self.run_id = run_id
        self.ttl_seconds = max(1, ttl_seconds)
        self.expected_phase = expected_phase
        self.thread_name = thread_name or f"agent-team-lock-heartbeat-{issue_id}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_IssueLockHeartbeat":
        interval = max(0.1, min(5.0, self.ttl_seconds / 3))
        self._thread = threading.Thread(
            target=self._refresh_until_stopped,
            args=(interval,),
            name=self.thread_name,
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _refresh_until_stopped(self, interval: float) -> None:
        while not self._stop.wait(interval):
            refreshed = self.store.refresh_issue_lock(
                self.issue_id,
                self.owner,
                self.ttl_seconds,
                expected_phase=self.expected_phase,
                expected_run_id=self.run_id,
            )
            if refreshed is None:
                self._stop.set()
                return


class _MergeRecoveryLease(_IssueLockHeartbeat):
    def __init__(
        self,
        store: IssueStore,
        issue_id: int,
        owner: str,
        run_id: str | None,
        ttl_seconds: int,
    ) -> None:
        super().__init__(
            store,
            issue_id,
            owner,
            run_id,
            ttl_seconds,
            expected_phase="merging",
            thread_name=f"agent-team-merge-recovery-lease-{issue_id}",
        )


class _ReviewSourceSyncRecoveryLease(_IssueLockHeartbeat):
    def __init__(
        self,
        store: IssueStore,
        issue_id: int,
        owner: str,
        run_id: str | None,
        ttl_seconds: int,
    ) -> None:
        super().__init__(
            store,
            issue_id,
            owner,
            run_id,
            ttl_seconds,
            expected_phase="reviewing",
            thread_name=f"agent-team-review-source-sync-recovery-lease-{issue_id}",
        )
