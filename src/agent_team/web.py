from __future__ import annotations

import json
import signal
import secrets
import threading
import urllib.parse
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .artifacts import ArtifactStore
from .blocked_summary import summarize_blocked_reason
from .config import AppConfig
from .db import IssueStore
from .lifecycle import delete_issue, reset_issue_to_draft, stop_issue
from .models import HumanInputRequest, Issue, utc_now_iso
from .orchestrator import Orchestrator, ProcessResult
from .state_machine import (
    READY_PHASES,
    RUNNING_PHASES,
    allowed_transitions,
    ready_phase_for_agent_phase,
    runnable_phase_for,
)
from . import web_resources
from .web_errors import WebError
from .web_html import (
    _bootstrap_scripts,
    _controls_signature,
    _csrf_field_html,
    _esc,
    _format_bytes,
    _merge_branch_hint,
    _repo_hidden_input,
    _repo_selector_html,
    _shorten,
    _workspace_metadata_rows,
    phase_option_label,
)
from .web_jobs import WebJob, WebJobManager
from .web_models import IssueMetadataForm, RepoContext, RuntimeInfo
from .web_pages import render_dashboard_body, render_issue_detail_body
from .web_routing import (
    artifact_route as _artifact_route,
    context_url as _context_url,
    issue_url as _issue_url,
    parse_issue_id as _parse_issue_id,
    path_parts as _path_parts,
    single as _single,
    split_path as _split_path,
)
from .web_security import allowed_hosts as _allowed_hosts, validate_web_bind as _validate_web_bind
from .worker import run_worker_loop


MAX_POST_BYTES = 1_000_000
LOG_TAIL_BYTES = 24_000
BLOCKED_ARTIFACT_READ_CHARS = 4_000
BLOCKED_ARTIFACT_EXCERPT_CHARS = 700
SYNOPSIS_ARTIFACT_READ_CHARS = 4_000
CLOSED_SYNOPSIS_EXCERPT_CHARS = 700
SERVER_THREAD_JOIN_SECONDS = 5.0
WORKER_THREAD_JOIN_SECONDS = 5.0
SUBMIT_DRAFT_FOR_RESEARCH_MESSAGE = "Submitted draft for research"


def _vscode_file_uri(path: Any) -> str | None:
    return _vscode_workspace_uri(path)


def _vscode_workspace_uri(path: Any, wsl_distro: str | None = None) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None

    normalized = path.replace("\\", "/")
    is_posix_or_unc = normalized.startswith("/")
    is_posix = is_posix_or_unc and not normalized.startswith("//")
    is_windows_drive = (
        len(normalized) >= 3
        and normalized[0].isalpha()
        and normalized[1:3] == ":/"
    )
    if not (is_posix_or_unc or is_windows_drive):
        return None

    distro = wsl_distro.strip() if isinstance(wsl_distro, str) else ""
    if distro and is_posix and not is_windows_drive:
        encoded_distro = urllib.parse.quote(distro, safe="")
        encoded_path = urllib.parse.quote(normalized, safe="/")
        return urllib.parse.urlunsplit(
            ("vscode", "vscode-remote", f"/wsl+{encoded_distro}{encoded_path}", "", "")
        )

    encoded_path = urllib.parse.quote(normalized, safe="/:")
    return urllib.parse.urlunsplit(("vscode", "file", encoded_path, "", ""))


class AgentTeamWebApp:
    def __init__(
        self,
        config: AppConfig,
        max_workers: int = 1,
        allow_remote: bool = False,
        runtime_info: RuntimeInfo | None = None,
    ) -> None:
        self.config = config
        self.allow_remote = allow_remote
        web_workers = max(1, max_workers)
        self.runtime_info = runtime_info or RuntimeInfo(web_workers=web_workers)
        self.store = IssueStore(config.db_path)
        self.store.init_schema()
        self.artifacts = ArtifactStore(config.artifacts_dir)
        Orchestrator(self.store, self.artifacts, config).recover_interrupted_runs()
        self.jobs = WebJobManager(config, max_workers=web_workers)
        self.csrf_token = secrets.token_urlsafe(32)

    def build_server(self, host: str, port: int) -> ThreadingHTTPServer:
        return ThreadingHTTPServer((host, port), self.handler_class())

    def shutdown(self) -> None:
        self.jobs.shutdown()

    def handler_class(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AgentTeamWeb/0.1"

            def do_GET(self) -> None:
                app.handle_request(self, "GET")

            def do_POST(self) -> None:
                app.handle_request(self, "POST")

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler

    def handle_request(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        try:
            self._validate_host(handler)
            if method == "POST":
                self._validate_origin(handler)
                self._handle_post(handler)
            elif method == "GET":
                self._handle_get(handler)
            else:
                raise WebError(HTTPStatus.METHOD_NOT_ALLOWED, f"Unsupported method: {method}")
        except WebError as exc:
            self._send_error_page(handler, exc.status, exc.message)
        except KeyError as exc:
            self._send_error_page(handler, HTTPStatus.NOT_FOUND, _clean_exception_message(exc))
        except ValueError as exc:
            self._send_error_page(handler, HTTPStatus.BAD_REQUEST, str(exc))

    def _handle_get(self, handler: BaseHTTPRequestHandler) -> None:
        path, query = _split_path(handler.path)
        if path == "/static/app.js":
            self._send_static(handler, web_resources.app_js(), "application/javascript; charset=utf-8")
            return
        if path == "/static/styles.css":
            self._send_static(handler, web_resources.styles_css(), "text/css; charset=utf-8")
            return
        if path == "/api/dashboard":
            self._send_json(handler, self._dashboard_payload(self._repo_context(query)))
            return
        parts = _path_parts(path)
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "jobs":
            job = self.jobs.get(parts[2])
            if job is None:
                raise WebError(HTTPStatus.NOT_FOUND, f"Job not found: {parts[2]}")
            self._send_json(handler, _job_payload(job))
            return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "issues":
            issue_id = _parse_issue_id(parts[2])
            self._send_json(handler, self._issue_payload(issue_id, self._repo_context(query)))
            return
        if len(parts) == 5 and parts[0] == "api" and parts[1] == "issues" and parts[3] == "logs" and parts[4] == "current":
            issue_id = _parse_issue_id(parts[2])
            self._send_json(handler, self._current_log_payload(issue_id, self._repo_context(query)))
            return
        if path == "/":
            self._send_page(handler, "Dashboard", self._render_dashboard(query))
            return
        if path == "/issues":
            self._send_page(handler, "Issues", self._render_issue_list(query))
            return
        if path == "/issues/new":
            self._send_page(handler, "New issue", self._render_new_issue(query))
            return
        if path.startswith("/artifacts/"):
            issue_id, relative_path = _artifact_route(path)
            self.store.get_issue(issue_id)
            self._send_page(handler, "Artifact", self._render_artifact_page(issue_id, relative_path, query))
            return

        if len(parts) == 3 and parts[0] == "issues" and parts[2] == "edit":
            issue_id = _parse_issue_id(parts[1])
            self._send_page(handler, "Edit issue", self._render_issue_edit(issue_id, query))
            return

        if len(parts) == 2 and parts[0] == "issues":
            issue_id = _parse_issue_id(parts[1])
            self._send_page(handler, "Issue", self._render_issue_detail(issue_id, query))
            return
        raise WebError(HTTPStatus.NOT_FOUND, f"Unknown path: {path}")

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        path, query = _split_path(handler.path)
        context = self._repo_context(query)
        form = self._parse_form(handler)
        self._validate_csrf(form)
        if path == "/issues":
            issue = self._create_issue(form, context)
            self._redirect(handler, _context_url(f"/issues/{issue.id}", context, flash=f"Created issue {issue.id}"))
            return
        if path == "/actions/run-next":
            job = self.jobs.submit_run_next(context.repo_path)
            self._redirect(
                handler,
                _context_url("/", context, flash="Queued run for next ready issue", job=job.id),
            )
            return

        parts = _path_parts(path)
        if len(parts) == 4 and parts[0] == "issues" and parts[2] == "actions":
            issue_id = _parse_issue_id(parts[1])
            action = parts[3]
            if action == "run":
                Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue_id)
                issue = self.store.get_issue(issue_id)
                issue = self._ensure_no_active_lock(issue, "run this issue")
                if runnable_phase_for(issue.phase) is None:
                    raise WebError(HTTPStatus.BAD_REQUEST, f"Issue {issue.id} is not in a runnable phase: {issue.phase}")
                job = self.jobs.submit_run_issue(issue_id, context.repo_path)
                self._redirect(
                    handler,
                    _context_url(f"/issues/{issue_id}", context, flash=f"Queued run for issue {issue_id}", job=job.id),
                )
                return
            if action == "edit":
                issue = self.store.get_issue(issue_id)
                self._ensure_no_active_lock(issue, "edit this draft")
                self._ensure_no_active_job(issue_id, "edit this draft")
                metadata = self._parse_issue_metadata(form)
                title = "" if metadata.description != issue.description else None
                updated = self.store.update_draft_issue(
                    issue_id,
                    title=title,
                    description=metadata.description,
                    repo_path=metadata.repo_path,
                    priority=metadata.priority,
                    tags=metadata.tags,
                )
                self.artifacts.write_issue_snapshot(updated)
                self._redirect(handler, _context_url(f"/issues/{issue_id}", context, flash="Draft issue edited"))
                return
            if action == "submit-for-research":
                issue = self.store.get_issue(issue_id)
                if issue.status != "open" or issue.phase != "draft":
                    raise WebError(HTTPStatus.BAD_REQUEST, f"Issue {issue.id} is not an open draft")
                issue = self._ensure_no_active_lock(issue, "submit this draft for research")
                self._ensure_no_active_job(issue_id, "submit this draft for research")
                if issue.status != "open" or issue.phase != "draft":
                    raise WebError(HTTPStatus.BAD_REQUEST, f"Issue {issue.id} is not an open draft")
                message = form.get("message", SUBMIT_DRAFT_FOR_RESEARCH_MESSAGE).strip()
                updated = self.store.transition_issue(
                    issue_id,
                    "needs_research",
                    None,
                    message or SUBMIT_DRAFT_FOR_RESEARCH_MESSAGE,
                )
                self.artifacts.write_issue_snapshot(updated)
                self._redirect(
                    handler,
                    _context_url(f"/issues/{issue_id}", context, flash=SUBMIT_DRAFT_FOR_RESEARCH_MESSAGE),
                )
                return
            if action == "approve-plan":
                issue = self.store.get_issue(issue_id)
                issue = self._ensure_no_active_lock(issue, "approve a plan")
                if issue.phase != "awaiting_plan_approval":
                    raise WebError(
                        HTTPStatus.BAD_REQUEST,
                        f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_plan_approval'",
                    )
                message = form.get("message", "Human approved the plan for implementation").strip()
                updated = self.store.transition_issue(
                    issue_id,
                    "ready_for_implementation",
                    None,
                    message or "Human approved the plan for implementation",
                )
                self.artifacts.clear_plan_rejection_context(issue_id)
                self.artifacts.write_issue_snapshot(updated)
                self._redirect(handler, _context_url(f"/issues/{issue_id}", context, flash="Plan approved"))
                return
            if action == "reject-plan":
                issue = self.store.get_issue(issue_id)
                issue = self._ensure_no_active_lock(issue, "reject a plan")
                if issue.phase != "awaiting_plan_approval":
                    raise WebError(
                        HTTPStatus.BAD_REQUEST,
                        f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_plan_approval'",
                    )
                feedback = form.get("feedback", "").strip()
                if not feedback:
                    raise WebError(HTTPStatus.BAD_REQUEST, "feedback is required")
                self.artifacts.save_prior_plan(issue.id)
                self.artifacts.write_plan_feedback(issue.id, feedback)
                updated = self.store.reject_plan(issue_id, feedback)
                self.artifacts.write_issue_snapshot(updated)
                self._redirect(
                    handler,
                    _context_url(f"/issues/{issue_id}", context, flash="Plan rejected; returned to planning"),
                )
                return
            if action == "answer-human-input":
                issue = self.store.get_issue(issue_id)
                issue = self._ensure_no_active_lock(issue, "answer human input")
                self._ensure_no_active_job(issue_id, "answer human input")
                if issue.phase != "awaiting_human_input":
                    raise WebError(
                        HTTPStatus.BAD_REQUEST,
                        f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_human_input'",
                    )
                answer = form.get("answer", "").strip()
                if not answer:
                    raise WebError(HTTPStatus.BAD_REQUEST, "answer is required")
                updated, request = self.store.answer_human_input_request(issue_id, answer, answered_by="web")
                self.artifacts.append_human_input_answer(request)
                self.artifacts.write_human_input_summary(issue_id, self.store.list_human_input_requests(issue_id))
                self.artifacts.write_issue_snapshot(updated)
                self._redirect(
                    handler,
                    _context_url(
                        f"/issues/{issue_id}",
                        context,
                        flash=f"Human input answered; resumed at {updated.phase}",
                    ),
                )
                return
            if action == "stop":
                issue = self.store.get_issue(issue_id)
                issue = self._ensure_no_active_lock(issue, "stop this issue")
                self._ensure_no_active_job(issue_id, "stop this issue")
                message = form.get("message", "").strip() or None
                result = stop_issue(self.config, self.store, self.artifacts, issue.id, message, stopped_by="web")
                self._redirect(
                    handler,
                    _context_url(
                        f"/issues/{issue_id}",
                        context,
                        flash=f"Issue {issue_id} stopped at {result.issue.phase}",
                    ),
                )
                return
            if action == "approve-merge":
                issue = self.store.get_issue(issue_id)
                issue = self._ensure_no_active_lock(issue, "approve a merge")
                if issue.phase != "awaiting_merge_approval":
                    raise WebError(
                        HTTPStatus.BAD_REQUEST,
                        f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_merge_approval'",
                    )
                branch = form.get("branch", "").strip() or None
                message = form.get("message", "Human approved worktree merge and cleanup").strip()
                message = message or "Human approved worktree merge and cleanup"
                self.artifacts.write_merge_request(issue.id, target_branch=branch, message=message)
                updated = self.store.transition_issue(issue_id, "ready_for_merge", None, message)
                self.artifacts.write_issue_snapshot(updated)
                self._redirect(handler, _context_url(f"/issues/{issue_id}", context, flash="Merge approved"))
                return
            if action == "reset-to-draft":
                issue = self.store.get_issue(issue_id)
                issue = self._ensure_no_active_lock(issue, "reset this issue")
                self._ensure_no_active_job(issue_id, "reset this issue")
                expected = f"RESET {issue_id}"
                confirmation = form.get("confirmation", "").strip()
                if confirmation != expected:
                    raise WebError(HTTPStatus.BAD_REQUEST, f"confirmation must be exactly {expected!r}")
                message = form.get("message", "").strip() or None
                result = reset_issue_to_draft(self.config, self.store, self.artifacts, issue_id, message)
                flash = (
                    f"Reset issue {issue_id} to draft; deleted {result.deleted_runs} runs, "
                    f"{result.deleted_events} events, and {result.deleted_artifacts} artifact/log entries"
                )
                self._redirect(handler, _context_url(f"/issues/{issue_id}", context, flash=flash))
                return
            if action == "delete":
                issue = self.store.get_issue(issue_id)
                self._ensure_no_active_lock(issue, "delete this issue")
                self._ensure_no_active_job(issue_id, "delete this issue")
                expected = f"DELETE {issue_id}"
                confirmation = form.get("confirmation", "").strip()
                if confirmation != expected:
                    raise WebError(HTTPStatus.BAD_REQUEST, f"confirmation must be exactly {expected!r}")
                self._ensure_no_active_job(issue_id, "delete this issue")
                message = form.get("message", "").strip() or None
                result = delete_issue(self.config, self.store, self.artifacts, issue_id, message)
                self.jobs.forget_jobs_for_issue(issue_id)
                flash = (
                    f"Deleted issue {issue_id}; removed {result.deleted_runs} runs, "
                    f"{result.deleted_events} events, and {result.deleted_artifacts} artifact/log entries"
                )
                self._redirect(handler, _context_url("/issues", context, flash=flash))
                return
            if action == "transition":
                issue = self.store.get_issue(issue_id)
                issue = self._ensure_no_active_lock(issue, "manually transition an issue")
                if issue.phase == "awaiting_human_input":
                    raise WebError(
                        HTTPStatus.BAD_REQUEST,
                        "Issue is awaiting human input; use the answer-human-input action to resume",
                    )
                next_phase = form.get("next_phase", "").strip()
                if not next_phase:
                    raise WebError(HTTPStatus.BAD_REQUEST, "next_phase is required")
                if next_phase == "awaiting_human_input":
                    raise WebError(
                        HTTPStatus.BAD_REQUEST,
                        "Cannot manually transition to awaiting_human_input; "
                        "run the relevant phase so an agent can create a structured human-input request",
                    )
                original_phase = issue.phase
                message = form.get("message", "").strip() or None
                if issue.phase == "awaiting_plan_approval" and next_phase == "ready_for_plan" and message:
                    self.artifacts.save_prior_plan(issue.id)
                    self.artifacts.write_plan_feedback(issue.id, message)
                    updated = self.store.reject_plan(issue_id, message)
                else:
                    updated = self.store.transition_issue(issue_id, next_phase, None, message)
                    if original_phase == "blocked":
                        if message:
                            self.artifacts.write_unblock_context(updated.id, updated.phase, message)
                        else:
                            self.artifacts.clear_unblock_context(updated.id)
                    if updated.phase == "ready_for_implementation":
                        self.artifacts.clear_plan_rejection_context(issue_id)
                self.artifacts.write_issue_snapshot(updated)
                self._redirect(handler, _context_url(f"/issues/{issue_id}", context, flash=f"Transitioned to {next_phase}"))
                return
        raise WebError(HTTPStatus.NOT_FOUND, f"Unknown path: {path}")

    def _create_issue(self, form: dict[str, str], context: RepoContext):
        metadata = self._parse_issue_metadata(form, default_repo_path=context.repo_path)
        ready = "ready" in form
        issue = self.store.create_issue(
            title=None,
            description=metadata.description,
            repo_path=metadata.repo_path,
            priority=metadata.priority,
            tags=metadata.tags,
            ready=ready,
        )
        self.artifacts.write_issue_snapshot(issue)
        return issue

    def _parse_issue_metadata(
        self,
        form: dict[str, str],
        *,
        default_repo_path: str | None = None,
    ) -> IssueMetadataForm:
        description = form.get("description", "").strip()
        if not description:
            raise WebError(HTTPStatus.BAD_REQUEST, "description is required")
        try:
            priority = int(form.get("priority", "3") or "3")
        except ValueError as exc:
            raise WebError(HTTPStatus.BAD_REQUEST, "priority must be an integer") from exc
        repo_path = form.get("repo_path", "").strip() or default_repo_path
        if not repo_path:
            raise WebError(HTTPStatus.BAD_REQUEST, "target repo is required")
        tags = form.get("tags", "").strip() or None
        return IssueMetadataForm(description, repo_path, priority, tags)

    def _parse_form(self, handler: BaseHTTPRequestHandler) -> dict[str, str]:
        length_header = handler.headers.get("Content-Length", "0")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise WebError(HTTPStatus.BAD_REQUEST, "Invalid Content-Length") from exc
        if length > MAX_POST_BYTES:
            raise WebError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Form body is too large")
        content_type = handler.headers.get("Content-Type", "")
        if length > 0 and not content_type.startswith("application/x-www-form-urlencoded"):
            raise WebError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Only application/x-www-form-urlencoded forms are supported")
        try:
            body = handler.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebError(HTTPStatus.BAD_REQUEST, "Form body must be UTF-8") from exc
        parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def _validate_csrf(self, form: dict[str, str]) -> None:
        token = form.get("_csrf_token", "")
        if not secrets.compare_digest(token, self.csrf_token):
            raise WebError(HTTPStatus.FORBIDDEN, "Invalid CSRF token")

    def _ensure_no_active_lock(self, issue: Issue, action: str) -> Issue:
        if issue.current_run_id is not None or issue.lock_expires_at is not None:
            Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)
            issue = self.store.get_issue(issue.id)
        if issue.current_run_id is not None:
            raise WebError(HTTPStatus.CONFLICT, f"Cannot {action} while issue {issue.id} has an active run")
        lock_expires_at = issue.lock_expires_at
        if lock_expires_at is not None and lock_expires_at >= utc_now_iso():
            raise WebError(HTTPStatus.CONFLICT, f"Cannot {action} while issue {issue.id} has an active run")
        return issue

    def _ensure_no_active_job(self, issue_id: int, action: str) -> None:
        for job in self.jobs.list_jobs(issue_id):
            if job.status in {"queued", "running"}:
                raise WebError(HTTPStatus.CONFLICT, f"Cannot {action} while queued browser action {job.id} is {job.status}")

    def _repo_context(self, query: dict[str, list[str]]) -> RepoContext:
        selected = _single(query.get("repo")).strip() or None
        known_repos = self.store.list_known_repos()
        if selected is not None and selected not in known_repos:
            known_repos = [*known_repos, selected]
        return RepoContext(selected, known_repos)

    def _jobs_for_context(self, context: RepoContext) -> list[WebJob]:
        jobs = self.jobs.list_jobs()
        if context.repo_path is None:
            return jobs
        filtered: list[WebJob] = []
        for job in jobs:
            if job.repo_path == context.repo_path:
                filtered.append(job)
                continue
            if job.issue_id is None:
                continue
            try:
                issue = self.store.get_issue(job.issue_id)
            except KeyError:
                continue
            if issue.repo_path == context.repo_path:
                filtered.append(job)
        return filtered

    def _dashboard_payload(self, context: RepoContext) -> dict[str, Any]:
        data = self.store.dashboard_summary(repo_path=context.repo_path)
        phase_counts = [_row_payload(row) for row in data["phase_counts"]]
        open_total = sum(int(row["count"]) for row in phase_counts if row["status"] == "open")
        recent_completed_runs = [_row_payload(row) for row in data["recent_completed_runs"]]
        recently_merged = [_row_payload(row) for row in data["recently_merged"]]
        bucket_counts = data["manager_bucket_counts"]
        summary = {
            "active_work": len(data["active_work"]),
            "approval_needed": bucket_counts["approval_needed"],
            "human_input_needed": bucket_counts["human_input_needed"],
            "draft": bucket_counts["draft"],
            "blocked": bucket_counts["blocked"],
            "ready": bucket_counts["ready"],
            "open_total": open_total,
            "recent_completions": len(recent_completed_runs),
            "recently_merged": len(recently_merged),
        }
        return {
            "generated_at": utc_now_iso(),
            "runtime": _runtime_payload(self.runtime_info),
            "summary": summary,
            "phase_counts": phase_counts,
            "active_work": [_row_payload(row) for row in data["active_work"]],
            "approval_issues": [_row_payload(row) for row in data["approval_issues"]],
            "awaiting_plan_approval": [_row_payload(row) for row in data["awaiting_plan_approval"]],
            "awaiting_merge_approval": [_row_payload(row) for row in data["awaiting_merge_approval"]],
            "human_input_needed": [_row_payload(row) for row in data["human_input_needed"]],
            "draft_issues": [_row_payload(row) for row in data["draft_issues"]],
            "blocked_issues": [_row_payload(row) for row in data["blocked_issues"]],
            "ready_issues": [_row_payload(row) for row in data["ready_issues"]],
            "recent_completed_runs": recent_completed_runs,
            "recently_merged": recently_merged,
            "recent_runs": [_row_payload(row) for row in data["recent_runs"]],
            "recent_events": [_row_payload(row) for row in data["recent_events"]],
            "open_issues": [_row_payload(row) for row in data["open_issues"]],
            "jobs": [_job_payload(job) for job in self._jobs_for_context(context)],
        }

    def _issue_payload(self, issue_id: int, repo_context: RepoContext | None = None) -> dict[str, Any]:
        issue = self.store.get_issue(issue_id)
        runs = self.store.list_runs(issue.id)
        events = self.store.list_events(issue.id)
        human_input_requests = self.store.list_human_input_requests(issue.id)
        pending_human_input = next((request for request in human_input_requests if request.status == "pending"), None)
        jobs = self.jobs.list_jobs(issue.id)
        active_jobs = [job for job in jobs if job.status in {"queued", "running"}]
        artifacts = self._artifact_metadata_payload(issue.id, repo_context)
        blocked_reason = _blocked_reason_payload(issue, runs, events, artifacts, self._blocked_artifact_excerpt)
        manager_controls = self._manager_controls_payload(issue, repo_context, blocked_reason)
        merged_metadata = self.artifacts.read_merged_workspace_metadata(issue.id) if issue.status == "closed" else None
        return {
            "generated_at": utc_now_iso(),
            "issue": _issue_payload(issue),
            "active_lock": _active_lock_payload(issue),
            "jobs": [_job_payload(job) for job in jobs],
            "active_job": _job_payload(active_jobs[0]) if active_jobs else None,
            "next_action": _next_manager_action(issue, active_jobs, blocked_reason),
            "blocked_reason": blocked_reason,
            "closed_synopsis": _closed_synopsis_payload(
                issue,
                runs,
                artifacts,
                merged_metadata,
                self._synopsis_artifact_excerpt,
            ),
            "human_input": {
                "pending": _human_input_request_payload(pending_human_input) if pending_human_input else None,
                "requests": [_human_input_request_payload(request) for request in human_input_requests],
            },
            "csrf_token": self.csrf_token,
            "manager_controls": manager_controls,
            "manager_controls_signature": _controls_signature(manager_controls),
            "phase_timeline": _phase_timeline(issue.phase, artifacts),
            "recent_runs": [_row_payload(row) for row in reversed(runs[-10:])],
            "recent_events": [_row_payload(row) for row in reversed(events[-15:])],
            "artifacts": artifacts,
            "current_log": self._selected_log_payload(issue, runs, artifacts),
        }

    def _current_log_payload(
        self,
        issue_id: int,
        repo_context: RepoContext | None = None,
        artifact_payload: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        issue = self.store.get_issue(issue_id)
        runs = self.store.list_runs(issue.id)
        artifacts = artifact_payload if artifact_payload is not None else self._artifact_metadata_payload(issue.id, repo_context)
        selected = self._selected_log_payload(issue, runs, artifacts)
        if selected and selected["exists"]:
            try:
                tail = self.artifacts.read_issue_artifact_tail(issue.id, str(selected["relative_path"]), max_bytes=LOG_TAIL_BYTES)
            except (FileNotFoundError, ValueError):
                selected = _missing_log_payload(selected)
            else:
                selected = {
                    **selected,
                    "content": tail.content,
                    "size_bytes": tail.size_bytes,
                    "modified_at": tail.modified_at,
                    "truncated": tail.truncated,
                }
        elif selected:
            selected = _missing_log_payload(selected)
        else:
            selected = {
                "exists": False,
                "label": "No run log yet",
                "relative_path": None,
                "url": None,
                "kind": "log",
                "size_bytes": 0,
                "modified_at": None,
                "run_id": None,
                "phase": None,
                "status": None,
                "source": "none",
                "content": "",
                "truncated": False,
            }
        return {"generated_at": utc_now_iso(), "issue_id": issue.id, "log": selected}

    def _artifact_metadata_payload(self, issue_id: int, repo_context: RepoContext | None = None) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for metadata in self.artifacts.list_issue_artifact_metadata(issue_id):
            artifact_path = f"/artifacts/{issue_id}/{urllib.parse.quote(metadata.relative_path, safe='/')}"
            artifacts.append(
                {
                    "label": metadata.label,
                    "relative_path": metadata.relative_path,
                    "url": _context_url(artifact_path, repo_context),
                    "kind": metadata.kind,
                    "size_bytes": metadata.size_bytes,
                    "modified_at": metadata.modified_at,
                }
            )
        return artifacts

    def _blocked_artifact_excerpt(self, issue_id: int, relative_path: str) -> str | None:
        try:
            content = self.artifacts.read_issue_artifact(issue_id, relative_path, max_chars=BLOCKED_ARTIFACT_READ_CHARS)
        except (FileNotFoundError, ValueError):
            return None
        return _artifact_reason_excerpt(content)

    def _synopsis_artifact_excerpt(self, issue_id: int, relative_path: str) -> str | None:
        try:
            content = self.artifacts.read_issue_artifact(issue_id, relative_path, max_chars=SYNOPSIS_ARTIFACT_READ_CHARS)
        except (FileNotFoundError, ValueError):
            return None
        return _artifact_synopsis_excerpt(content)

    def _selected_log_payload(
        self,
        issue: Issue,
        runs: list[sqlite3.Row],
        artifact_payload: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        artifacts = {
            artifact["relative_path"]: artifact
            for artifact in (artifact_payload if artifact_payload is not None else self._artifact_metadata_payload(issue.id))
        }
        running_to_agent = {running: agent for agent, running in RUNNING_PHASES.items()}
        run_by_id = {str(row["id"]): row for row in runs}

        if issue.current_run_id:
            agent_phase = running_to_agent.get(issue.phase)
            run = run_by_id.get(issue.current_run_id)
            phase = agent_phase or (str(run["phase"]) if run is not None else None)
            if phase:
                return _log_payload_from_metadata(
                    f"logs/{phase}-{issue.current_run_id}.md",
                    artifacts,
                    run=run,
                    run_id=issue.current_run_id,
                    phase=phase,
                    source="current_run",
                )

        for run in sorted(runs, key=lambda row: str(row["started_at"]), reverse=True):
            relative_path = f"logs/{run['phase']}-{run['id']}.md"
            if relative_path in artifacts:
                return _log_payload_from_metadata(relative_path, artifacts, run=run, source="latest_run")

        log_artifacts = [artifact for artifact in artifacts.values() if artifact["kind"] == "log"]
        if not log_artifacts:
            return None
        latest = sorted(log_artifacts, key=lambda artifact: str(artifact["modified_at"]), reverse=True)[0]
        return _log_payload_from_metadata(str(latest["relative_path"]), artifacts, run=None, source="latest_log")

    def _manager_controls_payload(
        self,
        issue: Issue,
        repo_context: RepoContext | None = None,
        blocked_reason: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        controls: list[dict[str, Any]] = []
        runnable = runnable_phase_for(issue.phase)
        if runnable is not None:
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/run", repo_context),
                    "method": "post",
                    "button": f"Run {runnable}",
                    "fields": [],
                }
            )
        if issue.phase == "awaiting_plan_approval":
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/approve-plan", repo_context),
                    "method": "post",
                    "button": "Approve plan",
                    "fields": [
                        {
                            "type": "input",
                            "name": "message",
                            "value": "Human approved the plan for implementation",
                        }
                    ],
                }
            )
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/reject-plan", repo_context),
                    "method": "post",
                    "button": "Reject plan",
                    "class_name": "stack",
                    "fields": [
                        {
                            "type": "textarea",
                            "name": "feedback",
                            "label": "Rejection feedback",
                            "rows": 5,
                            "required": True,
                            "placeholder": "Explain what needs to change in the plan.",
                        }
                    ],
                }
            )
        if issue.phase == "awaiting_merge_approval":
            workspace_metadata = self.artifacts.read_workspace_metadata(issue.id)
            branch_hint = _merge_branch_hint(
                self.artifacts.read_merge_request(issue.id),
                workspace_metadata,
            )
            vscode_uri = _vscode_workspace_uri(
                workspace_metadata.get("workspace_repo_path") if workspace_metadata else None,
                self.config.vscode_wsl_distro,
            )
            if vscode_uri:
                controls.append(
                    {
                        "action": vscode_uri,
                        "href": vscode_uri,
                        "method": "get",
                        "kind": "link",
                        "button": "Open in VS Code",
                    }
                )
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/approve-merge", repo_context),
                    "method": "post",
                    "button": "Approve merge",
                    "class_name": "stack",
                    "fields": [
                        {
                            "type": "input",
                            "name": "branch",
                            "label": "Target branch",
                            "value": branch_hint,
                            "placeholder": "main",
                        },
                        {
                            "type": "input",
                            "name": "message",
                            "value": "Human approved worktree merge and cleanup",
                        },
                    ],
                }
            )
        if issue.phase == "awaiting_human_input":
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/answer-human-input", repo_context),
                    "method": "post",
                    "button": "Answer human input",
                    "class_name": "stack",
                    "fields": [
                        {
                            "type": "textarea",
                            "name": "answer",
                            "label": "Answer",
                            "rows": 6,
                            "required": True,
                            "placeholder": "Provide the decision, approval, or context the agent requested.",
                        }
                    ],
                }
            )
        if issue.status == "open" and issue.phase not in {"draft", "blocked"}:
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/stop", repo_context),
                    "method": "post",
                    "button": "Stop issue",
                    "group": "primary",
                    "class_name": "stack",
                    "fields": [
                        {
                            "type": "textarea",
                            "name": "message",
                            "label": "Stop reason",
                            "rows": 3,
                            "placeholder": "Optional reason; defaults to a manager stop note.",
                        }
                    ],
                }
            )
        if issue.status == "open" and issue.phase == "draft":
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/submit-for-research", repo_context),
                    "method": "post",
                    "button": "Submit for research",
                    "group": "primary",
                    "fields": [
                        {
                            "type": "hidden",
                            "name": "message",
                            "value": SUBMIT_DRAFT_FOR_RESEARCH_MESSAGE,
                        }
                    ],
                }
            )
            edit_url = _context_url(f"/issues/{issue.id}/edit", repo_context)
            controls.append(
                {
                    "action": edit_url,
                    "href": edit_url,
                    "method": "get",
                    "kind": "link",
                    "button": "Edit draft",
                }
            )
        suggested_transition = _blocked_suggested_transition(blocked_reason)
        if issue.phase == "blocked" and suggested_transition is not None:
            agent_phase = suggested_transition["agent_phase"]
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/transition", repo_context),
                    "method": "post",
                    "button": f"Retry {agent_phase}",
                    "group": "primary",
                    "fields": [
                        {
                            "type": "hidden",
                            "name": "next_phase",
                            "value": suggested_transition["ready_phase"],
                        },
                        {
                            "type": "hidden",
                            "name": "message",
                            "value": f"Retrying {agent_phase} after blocked run",
                        },
                    ],
                }
            )
        if issue.status == "open" and issue.phase != "draft":
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/reset-to-draft", repo_context),
                    "method": "post",
                    "button": "Reset to draft (destructive)",
                    "class_name": "stack",
                    "fields": [
                        {
                            "type": "input",
                            "name": "confirmation",
                            "label": "Type RESET {id} to confirm".format(id=issue.id),
                            "placeholder": f"RESET {issue.id}",
                            "required": True,
                        },
                        {
                            "type": "input",
                            "name": "message",
                            "label": "Reset message",
                            "placeholder": "Optional reset reason",
                        },
                    ],
                }
            )
        controls.append(
            {
                "action": _context_url(f"/issues/{issue.id}/actions/delete", repo_context),
                "method": "post",
                "button": "Delete issue (irreversible)",
                "class_name": "stack",
                "fields": [
                    {
                        "type": "input",
                        "name": "confirmation",
                        "label": "Type DELETE {id} to confirm".format(id=issue.id),
                        "placeholder": f"DELETE {issue.id}",
                        "required": True,
                    },
                    {
                        "type": "input",
                        "name": "message",
                        "label": "Delete message",
                        "placeholder": "Optional delete reason",
                    },
                ],
            }
        )
        transitions = (
            ()
            if issue.phase == "awaiting_human_input"
            else tuple(phase for phase in allowed_transitions(issue.phase) if phase != "awaiting_human_input")
        )
        if transitions:
            controls.append(
                {
                    "action": _context_url(f"/issues/{issue.id}/actions/transition", repo_context),
                    "method": "post",
                    "button": "Transition",
                    "fields": [
                        {
                            "type": "select",
                            "name": "next_phase",
                            "label": "Transition to",
                            "options": [{"value": phase, "label": phase_option_label(phase)} for phase in transitions],
                        },
                        {
                            "type": "input",
                            "name": "message",
                            "placeholder": "Optional message",
                        },
                    ],
                }
            )
        return controls

    def _render_dashboard(self, query: dict[str, list[str]]) -> str:
        context = self._repo_context(query)
        payload = self._dashboard_payload(context)
        flash, job = self._flash_context(query)
        run_next_url = _context_url("/actions/run-next", context)
        body = render_dashboard_body(
            payload,
            context,
            self._csrf_field(),
            run_next_url,
            self._jobs_for_context(context),
        )
        return self._layout(
            "Dashboard",
            flash,
            job,
            body,
            {
                "page": "dashboard",
                "dashboard_api_url": _context_url("/api/dashboard", context),
                "repo": context.repo_path,
            },
            repo_context=context,
        )

    def _render_issue_list(self, query: dict[str, list[str]]) -> str:
        context = self._repo_context(query)
        flash, job = self._flash_context(query)
        status = _single(query.get("status")).strip() or None
        issues = self.store.list_issues(status, repo_path=context.repo_path)
        options = [("", "All"), ("open", "Open"), ("closed", "Closed")]
        option_html = "".join(
            f'<option value="{_esc(value)}"{" selected" if value == (status or "") else ""}>{_esc(label)}</option>'
            for value, label in options
        )
        repo_hidden = _repo_hidden_input(context)
        create_url = _context_url("/issues/new", context)
        rows = "".join(
            "<tr>"
            f'<td><a href="{_esc(_issue_url(issue.id, context))}">#{issue.id}</a></td>'
            f"<td>{_esc(issue.status)}</td>"
            f"<td>{_esc(issue.phase)}</td>"
            f"<td>P{issue.priority}</td>"
            f"<td>{_esc(issue.title)}</td>"
            f"<td>{_esc(issue.updated_at)}</td>"
            "</tr>"
            for issue in issues
        )
        table = (
            "<table><thead><tr><th>Issue</th><th>Status</th><th>Phase</th><th>Priority</th><th>Title</th><th>Updated</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            if rows
            else "<p>No issues match this filter.</p>"
        )
        return self._layout(
            "Issues",
            flash,
            job,
            f"""
            <section>
              <h2>Issues</h2>
              <p><a class="button" href="{_esc(create_url)}">Create issue</a></p>
              <form method="get" action="/issues" class="inline">
                {repo_hidden}
                <label>Status <select name="status">{option_html}</select></label>
                <button type="submit">Filter</button>
              </form>
              {table}
            </section>
            """,
            repo_context=context,
        )

    def _render_new_issue(self, query: dict[str, list[str]]) -> str:
        context = self._repo_context(query)
        flash, job = self._flash_context(query)
        repo_value = context.repo_path or ""
        form_action = _context_url("/issues", context)
        return self._layout(
            "New issue",
            flash,
            job,
            f"""
            <section>
              <h2>Create issue</h2>
              <form method="post" action="{_esc(form_action)}" class="stack">
                {self._csrf_field()}
                <label>Description <textarea name="description" rows="8" required></textarea></label>
                <label>Target repo <input name="repo_path" value="{_esc(repo_value)}" placeholder="/path/to/repo" required></label>
                <label>Priority <input name="priority" type="number" value="3" min="1"></label>
                <label>Tags <input name="tags" placeholder="comma,separated,tags"></label>
                <label><input name="ready" type="checkbox" value="1" checked> Make runnable now</label>
                <button type="submit">Create issue</button>
              </form>
            </section>
            """,
            repo_context=context,
        )

    def _render_issue_edit(self, issue_id: int, query: dict[str, list[str]]) -> str:
        context = self._repo_context(query)
        flash, job = self._flash_context(query)
        issue = self.store.get_issue(issue_id)
        if issue.status != "open" or issue.phase != "draft":
            raise WebError(
                HTTPStatus.BAD_REQUEST,
                f"Issue {issue.id} is in phase {issue.phase!r} with status {issue.status!r}, not an open draft",
            )
        form_action = _context_url(f"/issues/{issue.id}/actions/edit", context)
        issue_url = _issue_url(issue.id, context)
        return self._layout(
            f"Edit issue {issue.id}",
            flash,
            job,
            f"""
            <section>
              <p><a href="{_esc(issue_url)}">Back to issue #{issue.id}</a></p>
              <h2>Edit draft issue</h2>
              <p><strong>Title:</strong> {_esc(issue.title)}</p>
              <form method="post" action="{_esc(form_action)}" class="stack">
                {self._csrf_field()}
                <label>Description <textarea name="description" rows="8" required>{_esc(issue.description)}</textarea></label>
                <label>Target repo <input name="repo_path" value="{_esc(issue.repo_path or "")}" placeholder="/path/to/repo" required></label>
                <label>Priority <input name="priority" type="number" value="{issue.priority}" min="1"></label>
                <label>Tags <input name="tags" value="{_esc(issue.tags or "")}" placeholder="comma,separated,tags"></label>
                <button type="submit">Save draft</button>
              </form>
            </section>
            """,
            repo_context=context,
        )

    def _render_issue_detail(self, issue_id: int, query: dict[str, list[str]]) -> str:
        context = self._repo_context(query)
        issue = self.store.get_issue(issue_id)
        payload = self._issue_payload(issue.id, context)
        flash, job = self._flash_context(query)
        workspace_rows = _workspace_metadata_rows(self.artifacts.read_workspace_metadata(issue.id))
        artifacts_payload = payload["artifacts"]
        log_payload = self._current_log_payload(issue.id, context, artifacts_payload)["log"]
        plan_review = self._render_plan_review(issue, artifacts_payload)
        artifacts = self._render_artifacts(artifacts_payload)
        body = render_issue_detail_body(
            issue,
            payload,
            workspace_rows,
            log_payload,
            plan_review,
            artifacts,
            self.csrf_token,
        )
        return self._layout(
            f"Issue {issue.id}",
            flash,
            job,
            body,
            {
                "page": "issue",
                "issue_id": issue.id,
                "csrf_token": self.csrf_token,
                "issue_api_url": _context_url(f"/api/issues/{issue.id}", context),
                "log_api_url": _context_url(f"/api/issues/{issue.id}/logs/current", context),
                "repo": context.repo_path,
            },
            repo_context=context,
        )

    def _render_plan_review(self, issue: Issue, artifacts: list[dict[str, Any]]) -> str:
        if issue.phase != "awaiting_plan_approval":
            return ""

        plan_artifact = next(
            (
                artifact
                for artifact in artifacts
                if artifact["relative_path"] == "plan.md"
            ),
            None,
        )
        if plan_artifact is None:
            return self._render_missing_plan_review()

        try:
            content = self.artifacts.read_issue_artifact(issue.id, "plan.md")
        except (FileNotFoundError, ValueError):
            return self._render_missing_plan_review()

        return f"""
              <div class="panel priority plan-review-panel">
                <h2>Plan review</h2>
                <p class="muted">
                  {_format_bytes(int(plan_artifact["size_bytes"]))} &middot; modified {_esc(plan_artifact["modified_at"])}
                  &middot; <a href="{_esc(plan_artifact["url"])}">Open full plan artifact</a>
                </p>
                <pre class="plan-review-content">{_esc(content)}</pre>
              </div>
        """

    def _render_missing_plan_review(self) -> str:
        return """
              <div class="panel attention plan-review-panel">
                <h2>Plan review</h2>
                <p>This issue is awaiting plan approval, but the plan artifact is not available yet.</p>
                <p class="muted">Approval and rejection controls are still available below.</p>
              </div>
        """

    def _render_artifacts(self, artifacts: list[dict[str, Any]]) -> str:
        if not artifacts:
            return "<p>No artifacts or logs yet.</p>"
        rows = "".join(
            "<tr>"
            f"<td>{_esc(artifact['kind'])}</td>"
            f'<td><a href="{_esc(artifact["url"])}">{_esc(artifact["label"])}</a></td>'
            f"<td>{_esc(artifact['relative_path'])}</td>"
            f"<td>{_format_bytes(int(artifact['size_bytes']))}</td>"
            f"<td>{_esc(artifact['modified_at'])}</td>"
            "</tr>"
            for artifact in artifacts
        )
        return (
            "<table><thead><tr><th>Kind</th><th>Artifact</th><th>Path</th><th>Size</th><th>Modified</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    def _render_artifact_page(self, issue_id: int, relative_path: str, query: dict[str, list[str]]) -> str:
        context = self._repo_context(query)
        flash, job = self._flash_context(query)
        content = self.artifacts.read_issue_artifact(issue_id, relative_path)
        return self._layout(
            "Artifact",
            flash,
            job,
            f"""
            <section>
              <p><a href="{_esc(_issue_url(issue_id, context))}">Back to issue #{issue_id}</a></p>
              <h2>{_esc(relative_path)}</h2>
              <pre>{_esc(content)}</pre>
            </section>
            """,
            repo_context=context,
        )

    def _layout(
        self,
        title: str,
        flash: str | None,
        job: WebJob | None,
        body: str,
        bootstrap: dict[str, Any] | None = None,
        repo_context: RepoContext | None = None,
    ) -> str:
        context = repo_context or RepoContext(None, self.store.list_known_repos())
        dashboard_url = _context_url("/", context)
        issues_url = _context_url("/issues", context)
        create_url = _context_url("/issues/new", context)
        notices = []
        if flash:
            notices.append(f'<div class="notice">{_esc(flash)}</div>')
        if job:
            notices.append(
                f'<div class="notice">Queued browser action <code>{_esc(job.id[:8])}</code>: {_esc(job.status)} - {_esc(job.message)}</div>'
            )
        scripts = _bootstrap_scripts(bootstrap)
        repo_selector = _repo_selector_html(context)
        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{_esc(title)} - Agent Team</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <header>
    <h1>Agent Team Orchestrator</h1>
    <div class="header-row">
      <nav>
        <a href="{_esc(dashboard_url)}">Dashboard</a>
        <a href="{_esc(issues_url)}">Issues</a>
        <a href="{_esc(create_url)}">Create issue</a>
      </nav>
      {repo_selector}
    </div>
  </header>
  <main>
    {"".join(notices)}
    {body}
  </main>
  {scripts}
</body>
</html>"""

    def _csrf_field(self) -> str:
        return _csrf_field_html(self.csrf_token)

    def _send_page(self, handler: BaseHTTPRequestHandler, title: str, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = body.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        handler.wfile.write(payload)

    def _send_json(self, handler: BaseHTTPRequestHandler, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=True, sort_keys=True).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.wfile.write(payload)

    def _send_static(self, handler: BaseHTTPRequestHandler, content: str, content_type: str) -> None:
        payload = content.encode("utf-8")
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        handler.wfile.write(payload)

    def _send_error_page(self, handler: BaseHTTPRequestHandler, status: HTTPStatus, message: str) -> None:
        body = self._layout(
            f"{status.value} {status.phrase}",
            None,
            None,
            f"<section><h2>{status.value} {status.phrase}</h2><p>{_esc(message)}</p></section>",
        )
        self._send_page(handler, f"{status.value} {status.phrase}", body, status)

    def _redirect(self, handler: BaseHTTPRequestHandler, location: str) -> None:
        handler.send_response(HTTPStatus.SEE_OTHER)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _flash_context(self, query: dict[str, list[str]]) -> tuple[str | None, WebJob | None]:
        flash = _single(query.get("flash")).strip() or None
        job = self.jobs.get(_single(query.get("job")).strip() or None)
        return flash, job

    def _validate_host(self, handler: BaseHTTPRequestHandler) -> None:
        host = handler.headers.get("Host")
        if self.allow_remote:
            return
        if not host or host.lower() not in _allowed_hosts(handler):
            raise WebError(HTTPStatus.FORBIDDEN, "Host header is not allowed for this local web interface")

    def _validate_origin(self, handler: BaseHTTPRequestHandler) -> None:
        for header_name in ("Origin", "Referer"):
            value = handler.headers.get(header_name)
            if not value:
                continue
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise WebError(HTTPStatus.FORBIDDEN, f"{header_name} is not allowed for this local web interface")
            if self.allow_remote:
                # Remote opt-in relaxes the localhost allow-list, but POSTs still must be same-origin.
                request_host = handler.headers.get("Host")
                if not request_host or parsed.netloc.lower() != request_host.lower():
                    raise WebError(HTTPStatus.FORBIDDEN, f"{header_name} is not allowed for this local web interface")
                continue
            if parsed.netloc.lower() not in _allowed_hosts(handler):
                raise WebError(HTTPStatus.FORBIDDEN, f"{header_name} is not allowed for this local web interface")


def serve_web(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    workers: int = 1,
    unsafe_allow_remote: bool = False,
) -> int:
    _validate_web_bind(host, unsafe_allow_remote)
    web_workers = max(1, workers)
    app = AgentTeamWebApp(
        config,
        max_workers=web_workers,
        allow_remote=unsafe_allow_remote,
        runtime_info=RuntimeInfo(web_workers=web_workers),
    )
    server = app.build_server(host, port)
    display_host = _display_host(host)
    print(f"Serving agent-team web at http://{display_host}:{server.server_port}")
    print("This local control interface is not authenticated; keep it bound to localhost unless you add protection.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping agent-team web")
    finally:
        app.shutdown()
        server.server_close()
    return 0


def serve_web_and_worker(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    web_workers: int = 1,
    worker_concurrency: int = 1,
    interval_seconds: int = 60,
    unsafe_allow_remote: bool = False,
    stop_event: threading.Event | None = None,
    on_started: Callable[[ThreadingHTTPServer], None] | None = None,
    install_signal_handlers: bool = True,
) -> int:
    _validate_web_bind(host, unsafe_allow_remote)
    web_workers = max(1, web_workers)
    worker_concurrency = max(1, worker_concurrency)
    interval_seconds = max(0, interval_seconds)
    service_stop = stop_event or threading.Event()
    app = AgentTeamWebApp(
        config,
        max_workers=web_workers,
        allow_remote=unsafe_allow_remote,
        runtime_info=RuntimeInfo(
            mode="serve",
            web_workers=web_workers,
            worker_concurrency=worker_concurrency,
            worker_interval_seconds=interval_seconds,
        ),
    )
    server = app.build_server(host, port)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def record_error(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)
        service_stop.set()

    def run_server() -> None:
        try:
            server.serve_forever()
        except BaseException as exc:
            record_error(exc)

    def run_worker() -> None:
        try:
            run_worker_loop(
                app.store,
                app.artifacts,
                config,
                interval_seconds=interval_seconds,
                concurrency=worker_concurrency,
                stop_event=service_stop,
                on_result=_print_worker_result,
            )
        except BaseException as exc:
            record_error(exc)

    previous_handlers: list[tuple[signal.Signals, signal.Handlers]] = []

    def request_shutdown(signum: int, _frame: object) -> None:
        print(f"\nStopping agent-team serve after signal {signum}")
        service_stop.set()

    if install_signal_handlers and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers.append((sig, signal.getsignal(sig)))
            signal.signal(sig, request_shutdown)

    web_thread = threading.Thread(target=run_server, name="agent-team-web-server")
    worker_thread = threading.Thread(target=run_worker, name="agent-team-worker-loop")
    display_host = _display_host(host)
    try:
        web_thread.start()
        worker_thread.start()
        print(f"Serving agent-team web at http://{display_host}:{server.server_port}")
        print(
            "Autonomous worker loop enabled: "
            f"concurrency {worker_concurrency}, interval {interval_seconds}s; queued browser actions {web_workers}."
        )
        print("This local control interface is not authenticated; keep it bound to localhost unless you add protection.")
        if on_started is not None:
            on_started(server)
        while not service_stop.wait(0.2):
            pass
    finally:
        service_stop.set()
        server.shutdown()
        app.shutdown()
        web_thread.join(SERVER_THREAD_JOIN_SECONDS)
        worker_wait_reported = False
        while worker_thread.is_alive():
            worker_thread.join(WORKER_THREAD_JOIN_SECONDS)
            if worker_thread.is_alive() and not worker_wait_reported:
                print("Worker loop is waiting for active issue runs to finish before process exit.")
                worker_wait_reported = True
        server.server_close()
        for sig, previous in reversed(previous_handlers):
            signal.signal(sig, previous)

    with errors_lock:
        if errors:
            raise errors[0]
    return 0


def _display_host(host: str) -> str:
    return "localhost" if host in {"", "0.0.0.0", "::"} else host


def _print_worker_result(result: ProcessResult) -> None:
    print(f"Issue {result.issue_id} {result.phase} -> {result.next_phase}")


def _clean_exception_message(exc: KeyError) -> str:
    text = str(exc)
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == "'" else text


def _row_payload(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)


def _issue_payload(issue: Issue) -> dict[str, Any]:
    return {
        "id": issue.id,
        "title": issue.title,
        "description": issue.description,
        "source": issue.source,
        "external_id": issue.external_id,
        "repo_path": issue.repo_path,
        "phase": issue.phase,
        "status": issue.status,
        "priority": issue.priority,
        "tags": issue.tags,
        "lock_owner": issue.lock_owner,
        "lock_expires_at": issue.lock_expires_at,
        "current_run_id": issue.current_run_id,
        "blocked_summary": issue.blocked_summary,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
    }


def _human_input_request_payload(request: HumanInputRequest) -> dict[str, Any]:
    return {
        "id": request.id,
        "issue_id": request.issue_id,
        "run_id": request.run_id,
        "requested_by_phase": request.requested_by_phase,
        "resume_phase": request.resume_phase,
        "question": request.question,
        "rationale": request.rationale,
        "requested_decision": request.requested_decision,
        "options": list(request.options),
        "context": request.context,
        "status": request.status,
        "created_at": request.created_at,
        "answered_at": request.answered_at,
        "answer": request.answer,
        "answered_by": request.answered_by,
    }


def _job_payload(job: WebJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "action": job.action,
        "issue_id": job.issue_id,
        "repo_path": job.repo_path,
        "status": job.status,
        "message": job.message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _runtime_payload(runtime: RuntimeInfo) -> dict[str, Any]:
    return {
        "mode": runtime.mode,
        "web_workers": runtime.web_workers,
        "worker_concurrency": runtime.worker_concurrency,
        "worker_interval_seconds": runtime.worker_interval_seconds,
    }


def _active_lock_payload(issue: Issue) -> dict[str, Any] | None:
    if issue.lock_expires_at is None or issue.lock_expires_at < utc_now_iso():
        return None
    return {
        "owner": issue.lock_owner,
        "expires_at": issue.lock_expires_at,
        "run_id": issue.current_run_id,
    }


def _closed_synopsis_payload(
    issue: Issue,
    runs: list[Any],
    artifacts: list[dict[str, Any]],
    merged_metadata: dict[str, Any] | None,
    artifact_excerpt_reader: Callable[[int, str], str | None] | None = None,
) -> dict[str, Any] | None:
    if issue.status != "closed":
        return None

    artifacts_by_path = {str(artifact["relative_path"]): artifact for artifact in artifacts}
    latest_merge = _latest_successful_merge_run(runs)
    purpose_source = "issue"
    purpose_excerpt = None
    if "plan.md" in artifacts_by_path and artifact_excerpt_reader is not None:
        purpose_excerpt = artifact_excerpt_reader(issue.id, "plan.md")
        if purpose_excerpt:
            purpose_source = "plan.md"
    if not purpose_excerpt:
        purpose_excerpt = _shorten((issue.description or issue.title).strip(), CLOSED_SYNOPSIS_EXCERPT_CHARS)

    change_artifact = None
    change_excerpt = None
    for relative_path in ("implementation.md", "review.md"):
        if relative_path not in artifacts_by_path or artifact_excerpt_reader is None:
            continue
        change_excerpt = artifact_excerpt_reader(issue.id, relative_path)
        if change_excerpt:
            change_artifact = relative_path
            break

    merge_excerpt = None
    if "merge.md" in artifacts_by_path and artifact_excerpt_reader is not None:
        merge_excerpt = artifact_excerpt_reader(issue.id, "merge.md")
    merge_summary = str(latest_merge["summary"] or "") if latest_merge is not None else ""
    merge_summary = merge_summary or merge_excerpt

    completed_at = None
    if latest_merge is not None:
        completed_at = latest_merge["completed_at"] or latest_merge["started_at"]
    if not completed_at and merged_metadata:
        completed_at = merged_metadata.get("merged_at")
    completed_at = completed_at or issue.updated_at

    fallback = "No detailed closed synopsis is available. Review the issue description, runs, and artifacts."
    summary = purpose_excerpt or merge_summary or fallback
    if summary == fallback:
        purpose_source = "fallback"

    run_id = str(latest_merge["id"]) if latest_merge is not None else None
    return {
        "source": purpose_source,
        "headline": summary,
        "summary": summary,
        "change_source": change_artifact,
        "change_excerpt": change_excerpt,
        "merge_summary": merge_summary,
        "run_id": run_id,
        "completed_at": completed_at,
        "merged_at": merged_metadata.get("merged_at") if merged_metadata else None,
        "target_branch": merged_metadata.get("merge_target_branch") if merged_metadata else None,
        "merge_commit": merged_metadata.get("merge_commit") if merged_metadata else None,
        "worktree_commit": merged_metadata.get("worktree_commit") if merged_metadata else None,
        "links": _closed_synopsis_links(artifacts_by_path, run_id),
    }


def _latest_successful_merge_run(runs: list[Any]) -> Any | None:
    merge_runs = [run for run in runs if run["phase"] == "merge" and run["status"] == "success"]
    if not merge_runs:
        return None
    return sorted(
        merge_runs,
        key=lambda run: (
            str(run["completed_at"] or run["started_at"]),
            str(run["started_at"]),
            str(run["id"]),
        ),
        reverse=True,
    )[0]


def _closed_synopsis_links(artifacts_by_path: dict[str, dict[str, Any]], run_id: str | None) -> list[dict[str, Any]]:
    candidates = ["plan.md", "implementation.md", "review.md", "merge.md"]
    if run_id:
        candidates.append(f"logs/merge-{run_id}.md")
    candidates.append("workspace.merged.json")
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for relative_path in candidates:
        if relative_path in seen:
            continue
        seen.add(relative_path)
        metadata = artifacts_by_path.get(relative_path)
        if metadata is None:
            continue
        links.append(_closed_synopsis_link(metadata))
    return links


def _closed_synopsis_link(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": metadata["label"],
        "url": metadata["url"],
        "relative_path": metadata["relative_path"],
        "kind": metadata["kind"],
        "size_bytes": metadata["size_bytes"],
        "modified_at": metadata["modified_at"],
    }


def _next_manager_action(
    issue: Issue,
    active_jobs: list[WebJob],
    blocked_reason: dict[str, Any] | None = None,
) -> str:
    if active_jobs:
        return f"Queued browser action is {active_jobs[0].status}; watch the live activity and log."
    if _active_lock_payload(issue) is not None or issue.phase in set(RUNNING_PHASES.values()):
        return "Agent is working now; watch the live activity and current log."
    runnable = runnable_phase_for(issue.phase)
    if runnable is not None:
        return f"Ready to run the {runnable} agent."
    if issue.phase == "draft":
        return "Draft backlog: edit it as needed, then submit it for research when it is ready for agents."
    if issue.phase == "awaiting_plan_approval":
        return "Review the plan, then approve it or send feedback."
    if issue.phase == "awaiting_human_input":
        return "Answer the pending human-input request so agents can resume."
    if issue.phase == "awaiting_merge_approval":
        return "Review the worktree merge request, then approve or send it back."
    if issue.phase == "blocked":
        suggested_transition = _blocked_suggested_transition(blocked_reason)
        if suggested_transition is not None:
            return (
                f"Blocked: retry the {suggested_transition['agent_phase']} phase from the primary control, "
                "or inspect the highlighted reason first."
            )
        return (
            "Blocked: no automatic retry target was found. Review the highlighted reason, then use advanced phase "
            "override if you know where to resume."
        )
    if issue.phase == "done":
        return "Done: no manager action needed."
    return "Monitor this phase for the next transition."


def _phase_timeline(current_phase: str, artifacts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    conflict_artifact_key = "merge_conflict_resolution"
    conflict_phases = ("ready_for_merge_conflict_resolution", "resolving_merge_conflicts")
    workflow = [
        ("Draft", None, ("draft",)),
        ("Research", "research", ("needs_research", "researching")),
        ("Plan", "plan", ("ready_for_plan", "planning", "awaiting_plan_approval")),
        ("Human input", "human_input", ("awaiting_human_input",)),
        ("Implementation", "implementation", ("ready_for_implementation", "implementing")),
        ("Validation", "validation", ("ready_for_validation", "validating")),
        ("Review", "review", ("ready_for_review", "reviewing", "awaiting_merge_approval")),
        ("Merge", "merge", ("ready_for_merge", "merging")),
        (
            "Conflict resolution",
            conflict_artifact_key,
            conflict_phases,
        ),
        ("Done", None, ("done",)),
    ]
    artifact_keys = {artifact_key for _label, artifact_key, _phases in workflow if artifact_key is not None}
    phase_artifacts: dict[str, dict[str, Any]] = {}
    for artifact in artifacts or []:
        if artifact.get("kind") not in {"phase", "human_input"}:
            continue
        relative_path = artifact.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path.endswith(".md"):
            continue
        artifact_key = relative_path[:-3]
        if artifact_key not in artifact_keys or relative_path != f"{artifact_key}.md":
            continue
        phase_artifacts[artifact_key] = {
            "label": artifact.get("label") or f"{artifact_key} artifact",
            "relative_path": relative_path,
            "url": artifact.get("url"),
        }

    include_conflict_resolution = current_phase in conflict_phases or conflict_artifact_key in phase_artifacts
    visible_workflow = [
        step for step in workflow if step[1] != conflict_artifact_key or include_conflict_resolution
    ]

    def step_payload(label: str, status: str, phases: tuple[str, ...], artifact_key: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"label": label, "status": status, "phases": list(phases)}
        if artifact_key is not None and artifact_key in phase_artifacts:
            payload["artifact"] = phase_artifacts[artifact_key]
        return payload

    if current_phase == "blocked":
        return [{"label": "Blocked", "status": "attention", "phases": ["blocked"]}] + [
            step_payload(label, "pending", phases, artifact_key) for label, artifact_key, phases in visible_workflow
        ]

    current_index = next(
        (index for index, (_label, _artifact_key, phases) in enumerate(visible_workflow) if current_phase in phases),
        None,
    )
    steps: list[dict[str, Any]] = []
    for index, (label, artifact_key, phases) in enumerate(visible_workflow):
        if current_index is None:
            status = "pending"
        elif index < current_index:
            status = "done"
        elif index == current_index:
            status = "current"
        else:
            status = "pending"
        steps.append(step_payload(label, status, phases, artifact_key))
    return steps


def _blocked_reason_payload(
    issue: Issue,
    runs: list[Any],
    events: list[Any],
    artifacts: list[dict[str, Any]],
    artifact_excerpt_reader: Callable[[int, str], str | None] | None = None,
) -> dict[str, Any] | None:
    if issue.phase != "blocked":
        return None

    artifacts_by_path = {str(artifact["relative_path"]): artifact for artifact in artifacts}
    runs_by_id = {str(run["id"]): run for run in runs}
    transition = _latest_blocked_transition_event(events)
    suggested_transition = _blocked_suggested_transition_payload(issue, runs, transition, runs_by_id)

    if transition is not None:
        transition_run_id = transition["run_id"]
        if transition_run_id is None:
            return _finalize_blocked_reason_payload(issue, _blocked_transition_payload(issue, transition), suggested_transition)
        run = runs_by_id.get(str(transition_run_id))
        if run is not None and str(run["status"]) != "success":
            return _finalize_blocked_reason_payload(
                issue,
                _blocked_run_payload(issue, run, artifacts_by_path, artifact_excerpt_reader),
                suggested_transition,
            )
        return _finalize_blocked_reason_payload(issue, _blocked_transition_payload(issue, transition), suggested_transition)

    for run in reversed(runs):
        status = str(run["status"])
        if status == "success":
            continue
        return _finalize_blocked_reason_payload(
            issue,
            _blocked_run_payload(issue, run, artifacts_by_path, artifact_excerpt_reader),
            suggested_transition,
        )

    return _finalize_blocked_reason_payload(
        issue,
        {
            "source": "fallback",
            "title": "No blocked reason recorded",
            "headline": "No blocked reason was recorded.",
            "summary": "No blocked reason was recorded. Review recent activity, runs, logs, and artifacts.",
            "error": None,
            "run_id": None,
            "phase": issue.phase,
            "status": issue.status,
            "started_at": None,
            "completed_at": issue.updated_at,
            "artifact": None,
            "log": None,
        },
        suggested_transition,
    )


def _finalize_blocked_reason_payload(
    issue: Issue,
    payload: dict[str, Any],
    suggested_transition: dict[str, Any] | None,
) -> dict[str, Any]:
    original_summary = str(payload.get("summary") or payload.get("headline") or payload.get("error") or "").strip()
    stored_summary = issue.blocked_summary
    if (
        stored_summary
        and payload.get("artifact_excerpt")
        and _is_generic_blocked_run_reason(stored_summary, "", str(payload.get("phase") or ""))
    ):
        stored_summary = None
    summary_source = stored_summary or original_summary
    concise_summary = summarize_blocked_reason(summary_source)
    if original_summary and original_summary != concise_summary:
        payload["technical_summary"] = original_summary
    else:
        payload["technical_summary"] = None
    payload["blocked_summary"] = concise_summary
    payload["headline"] = concise_summary
    payload["summary"] = concise_summary
    payload["suggested_transition"] = suggested_transition
    return payload


def _blocked_suggested_transition_payload(
    issue: Issue,
    runs: list[Any],
    transition: Any | None,
    runs_by_id: dict[str, Any],
) -> dict[str, Any] | None:
    if issue.phase != "blocked":
        return None

    run = None
    if transition is not None:
        if transition["run_id"] is None:
            return None
        run = runs_by_id.get(str(transition["run_id"]))
    else:
        for candidate in reversed(runs):
            if str(candidate["status"]) == "success":
                continue
            run = candidate
            break
    if run is None:
        return None

    agent_phase = str(run["phase"])
    ready_phase = ready_phase_for_agent_phase(agent_phase)
    if ready_phase is None or ready_phase not in allowed_transitions("blocked"):
        return None
    return {
        "agent_phase": agent_phase,
        "ready_phase": ready_phase,
        "label": phase_option_label(ready_phase),
        "run_id": str(run["id"]),
    }


def _blocked_suggested_transition(blocked_reason: dict[str, Any] | None) -> dict[str, Any] | None:
    if not blocked_reason:
        return None
    suggested_transition = blocked_reason.get("suggested_transition")
    return suggested_transition if isinstance(suggested_transition, dict) else None


def _blocked_run_payload(
    issue: Issue,
    run: Any,
    artifacts_by_path: dict[str, dict[str, Any]],
    artifact_excerpt_reader: Callable[[int, str], str | None] | None,
) -> dict[str, Any]:
    phase = str(run["phase"])
    run_id = str(run["id"])
    status = str(run["status"])
    run_summary = str(run["summary"] or "")
    error = str(run["error"] or "")
    artifact_metadata = artifacts_by_path.get(f"{phase}.md")
    artifact_excerpt = _blocked_artifact_excerpt(issue.id, artifact_metadata, artifact_excerpt_reader)
    summary = run_summary or error or f"{phase} run ended with {status}"
    display_error = error or None
    if artifact_excerpt and _is_generic_blocked_run_reason(run_summary, error, phase):
        summary = artifact_excerpt
        if _is_copilot_recommended_blocked_text(error, phase):
            display_error = None
    return {
        "source": "run",
        "title": f"Blocked by {phase} run",
        "headline": summary,
        "summary": summary,
        "run_summary": run_summary or None,
        "error": display_error,
        "run_id": run_id,
        "phase": phase,
        "status": status,
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "artifact_excerpt": artifact_excerpt,
        "artifact": _blocked_reason_link(artifact_metadata, "Open blocked artifact"),
        "log": _blocked_reason_link(artifacts_by_path.get(f"logs/{phase}-{run_id}.md"), "Open run log"),
    }


def _blocked_transition_payload(issue: Issue, transition: Any) -> dict[str, Any]:
    message = str(transition["message"] or "")
    return {
        "source": "manual_transition" if transition["run_id"] is None else "transition",
        "title": "Manual block" if transition["run_id"] is None else "Blocked transition",
        "headline": message or "Issue was moved to blocked.",
        "summary": message,
        "error": None,
        "run_id": transition["run_id"],
        "phase": issue.phase,
        "status": issue.status,
        "started_at": None,
        "completed_at": transition["created_at"],
        "artifact_excerpt": None,
        "artifact": None,
        "log": None,
    }


def _latest_blocked_transition_event(events: list[Any]) -> Any | None:
    for event in reversed(events):
        if event["event_type"] == "issue.transitioned":
            return event
    return None


def _blocked_artifact_excerpt(
    issue_id: int,
    artifact_metadata: dict[str, Any] | None,
    artifact_excerpt_reader: Callable[[int, str], str | None] | None,
) -> str | None:
    if artifact_metadata is None or artifact_excerpt_reader is None:
        return None
    return artifact_excerpt_reader(issue_id, str(artifact_metadata["relative_path"]))


def _is_generic_blocked_run_reason(summary: str, error: str, phase: str) -> bool:
    return (
        not summary.strip()
        or _is_generic_copilot_blocked_text(summary, phase)
        or _is_generic_copilot_blocked_text(error, phase)
    )


def _is_generic_copilot_blocked_text(text: str, phase: str) -> bool:
    normalized = text.strip().lower()
    prefix = f"copilot cli {phase} "
    return (
        normalized.startswith(f"{prefix}recommended blocked")
        or normalized.startswith(f"{prefix}did not provide a valid recommendation")
        or normalized.startswith(f"{prefix}provided invalid recommendation")
    )


def _is_copilot_recommended_blocked_text(text: str, phase: str) -> bool:
    normalized = text.strip().lower()
    return normalized.startswith(f"copilot cli {phase} recommended blocked")


def _artifact_synopsis_excerpt(markdown: str) -> str | None:
    preferred = _artifact_preferred_section_excerpt(markdown)
    if preferred:
        return preferred
    return _artifact_leading_synopsis_excerpt(markdown)


def _artifact_preferred_section_excerpt(markdown: str) -> str | None:
    preferred_headings = {
        "executive summary",
        "summary",
        "summary of changes",
        "implementation summary",
        "review summary",
        "merge summary",
        "changes",
        "what changed",
    }
    collected: list[str] = []
    collecting = False
    for raw_line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        heading = _artifact_heading_text(line)
        if heading in preferred_headings:
            collecting = True
            collected = []
            continue
        if collecting and _is_synopsis_section_boundary(line):
            break
        if collecting:
            collected.append(line)
    return _synopsis_lines_excerpt(collected)


def _artifact_leading_synopsis_excerpt(markdown: str) -> str | None:
    lines: list[str] = []
    for raw_line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if _is_artifact_noise_line(line):
            continue
        if _is_synopsis_section_boundary(line):
            if lines:
                break
            continue
        lines.append(line)
    return _synopsis_lines_excerpt(lines)


def _synopsis_lines_excerpt(lines: list[str]) -> str | None:
    cleaned: list[str] = []
    for line in lines:
        if not line:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        if _is_artifact_noise_line(line):
            continue
        cleaned.append(line)

    paragraphs: list[str] = []
    current: list[str] = []
    for line in cleaned:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))

    excerpt = "\n\n".join(paragraphs).strip()
    if not excerpt:
        return None
    return _shorten(excerpt, CLOSED_SYNOPSIS_EXCERPT_CHARS)


def _is_synopsis_section_boundary(line: str) -> bool:
    if not line:
        return False
    if line.startswith("#"):
        return True
    heading = _artifact_heading_text(line)
    return heading in {
        "executive summary",
        "summary",
        "summary of changes",
        "implementation summary",
        "review summary",
        "merge summary",
        "changes",
        "what changed",
        "proposed approach",
        "files changed",
        "tests/checks run",
        "deviations from the plan",
        "remaining risks",
        "recommendation",
        "required human approvals",
    }


def _artifact_heading_text(line: str) -> str:
    return line.lstrip("#").strip().lstrip("0123456789. )").strip().lower().lstrip("*_").rstrip("*_").strip()


def _artifact_reason_excerpt(markdown: str) -> str | None:
    lines: list[str] = []
    for raw_line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if _is_artifact_noise_line(line):
            continue
        if _is_artifact_section_heading(line):
            continue
        lines.append(line)

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))

    excerpt = "\n\n".join(paragraphs).strip()
    if not excerpt:
        return None
    return _shorten(excerpt, BLOCKED_ARTIFACT_EXCERPT_CHARS)


def _is_artifact_noise_line(line: str) -> bool:
    lowered = line.lower()
    heading = line.lstrip("#").strip().lstrip("0123456789. )").strip().lower().lstrip("*_").strip()
    return (
        lowered.startswith("<!--")
        or lowered.startswith("recommendation:")
        or heading.startswith("recommendation:")
    )


def _is_artifact_section_heading(line: str) -> bool:
    stripped = line.lstrip("#").strip()
    normalized = stripped.lstrip("0123456789. )").strip().lower()
    return normalized in {
        "summary",
        "summary of changes",
        "blocked reason",
        "blocker",
        "critical findings",
        "important findings",
        "minor findings",
        "missing tests or documentation",
        "files changed",
        "tests/checks run",
        "deviations from the plan",
        "remaining risks",
    }


def _blocked_reason_link(metadata: dict[str, Any] | None, label: str) -> dict[str, Any] | None:
    if metadata is None or not metadata.get("url"):
        return None
    return {
        "label": label,
        "url": metadata["url"],
        "relative_path": metadata["relative_path"],
        "kind": metadata["kind"],
        "size_bytes": metadata["size_bytes"],
        "modified_at": metadata["modified_at"],
    }


def _log_payload_from_metadata(
    relative_path: str,
    artifacts: dict[str, dict[str, Any]],
    *,
    run: sqlite3.Row | None,
    source: str,
    run_id: str | None = None,
    phase: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    metadata = artifacts.get(relative_path)
    payload = dict(
        metadata
        or {
            "label": relative_path,
            "relative_path": relative_path,
            "url": None,
            "kind": "log",
            "size_bytes": 0,
            "modified_at": None,
        }
    )
    payload.update(
        {
            "exists": metadata is not None,
            "run_id": str(run["id"]) if run is not None else run_id,
            "phase": str(run["phase"]) if run is not None else phase,
            "status": str(run["status"]) if run is not None else status,
            "source": source,
        }
    )
    return payload


def _missing_log_payload(log: dict[str, Any]) -> dict[str, Any]:
    return {
        **log,
        "exists": False,
        "url": None,
        "size_bytes": 0,
        "modified_at": None,
        "content": "",
        "truncated": False,
    }
