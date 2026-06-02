from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from .artifacts import ArtifactStore
from .config import AppConfig, ensure_home, load_config
from .dashboard import render_dashboard
from .db import IssueStore
from .lifecycle import DeleteIssueResult, ResetIssueResult, StopIssueResult, delete_issue, reset_issue_to_draft, stop_issue
from .orchestrator import Orchestrator
from .web import serve_web, serve_web_and_worker
from .worker import process_batch, run_worker_loop


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    ensure_home(config)
    store = IssueStore(config.db_path)
    store.init_schema()
    artifacts = ArtifactStore(config.artifacts_dir)

    if args.command == "init":
        print(f"Initialized agent-team state at {config.home}")
        return 0
    if args.command == "issue":
        return handle_issue(args, store, artifacts, config)
    if args.command == "run":
        result = Orchestrator(store, artifacts, config).process_issue(args.issue, args.phase)
        print_result(result)
        return 0
    if args.command == "worker":
        return handle_worker(args, store, artifacts, config)
    if args.command == "dashboard":
        print(render_dashboard(store))
        return 0
    if args.command == "web":
        host = _string_setting(args.host, config.web_host)
        port = _int_setting(args.port, config.web_port)
        web_workers = _positive_setting(args.web_workers, config.web_workers)
        unsafe_allow_remote = _bool_setting(args.unsafe_allow_remote, config.web_unsafe_allow_remote)
        return serve_web(config, host, port, web_workers, unsafe_allow_remote)
    if args.command == "serve":
        host = _string_setting(args.host, config.web_host)
        port = _int_setting(args.port, config.web_port)
        web_workers = _positive_setting(args.web_workers, config.web_workers)
        worker_concurrency = _positive_setting(args.worker_concurrency, config.worker_concurrency)
        interval_seconds = _non_negative_setting(args.interval, config.worker_interval_seconds)
        unsafe_allow_remote = _bool_setting(args.unsafe_allow_remote, config.web_unsafe_allow_remote)
        return serve_web_and_worker(
            config,
            host=host,
            port=port,
            web_workers=web_workers,
            worker_concurrency=worker_concurrency,
            interval_seconds=interval_seconds,
            unsafe_allow_remote=unsafe_allow_remote,
        )

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-team")
    parser.add_argument(
        "--config",
        help="Path to a JSONC config file. Must appear before the subcommand.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init")

    issue = sub.add_parser("issue")
    issue_sub = issue.add_subparsers(dest="issue_command", required=True)

    create = issue_sub.add_parser("create")
    create.add_argument("--title", help="Optional title override; generated from the description when omitted or blank.")
    create.add_argument("--description")
    create.add_argument("--description-file")
    create.add_argument("--repo", required=True, help="Required target repository path for the issue workspace.")
    create.add_argument("--priority", type=int, default=3)
    create.add_argument("--tags")
    create.add_argument("--ready", action="store_true", help="Create the issue in needs_research instead of draft.")

    edit = issue_sub.add_parser("edit")
    edit.add_argument("id", type=int)
    edit.add_argument("--title", help="Optional title override. Pass an empty string to regenerate from the description.")
    edit_description = edit.add_mutually_exclusive_group()
    edit_description.add_argument("--description")
    edit_description.add_argument("--description-file")
    edit_repo = edit.add_mutually_exclusive_group()
    edit_repo.add_argument("--repo")
    edit_repo.add_argument("--clear-repo", action="store_true")
    edit.add_argument("--priority", type=int)
    edit_tags = edit.add_mutually_exclusive_group()
    edit_tags.add_argument("--tags")
    edit_tags.add_argument("--clear-tags", action="store_true")

    list_cmd = issue_sub.add_parser("list")
    list_cmd.add_argument("--status")

    show = issue_sub.add_parser("show")
    show.add_argument("id", type=int)

    advance = issue_sub.add_parser("advance")
    advance.add_argument("id", type=int)
    advance.add_argument("--to", required=True)
    advance.add_argument("--message")

    approve = issue_sub.add_parser("approve-plan")
    approve.add_argument("id", type=int)
    approve.add_argument("--message", default="Human approved the plan for implementation")

    approve_merge = issue_sub.add_parser("approve-merge")
    approve_merge.add_argument("id", type=int)
    approve_merge.add_argument("--branch")
    approve_merge.add_argument(
        "--mode",
        choices=["auto", "local", "pull-request"],
        default=None,
        help="Finalize locally, by pull request, or auto-select based on remotes. Defaults to AGENT_TEAM_MERGE_MODE.",
    )
    approve_merge.add_argument("--remote", help="Optional remote name to use for pull-request mode.")
    approve_merge.add_argument("--message", default="Human approved worktree merge and cleanup")

    reset = issue_sub.add_parser("reset-to-draft")
    reset.add_argument("id", type=int)
    reset.add_argument("--message")

    stop = issue_sub.add_parser("stop")
    stop.add_argument("id", type=int)
    stop.add_argument("--message")

    delete = issue_sub.add_parser("delete")
    delete.add_argument("id", type=int)
    delete.add_argument("--message")
    delete.add_argument("--confirm", required=True, help='Must be exactly "DELETE <id>".')

    reject = issue_sub.add_parser("reject-plan")
    reject.add_argument("id", type=int)
    reject.add_argument("--feedback")
    reject.add_argument("--feedback-file")

    answer = issue_sub.add_parser("answer-human-input")
    answer.add_argument("id", type=int)
    answer_input = answer.add_mutually_exclusive_group()
    answer_input.add_argument("--answer")
    answer_input.add_argument("--answer-file")

    run = sub.add_parser("run")
    run.add_argument("--issue", type=int, required=True)
    run.add_argument(
        "--phase",
        choices=[
            "research",
            "plan",
            "implementation",
            "validation",
            "review",
            "merge",
            "merge_conflict_resolution",
        ],
    )

    sub.add_parser("dashboard")

    web = sub.add_parser("web")
    web.add_argument("--host", default=None)
    web.add_argument("--port", type=int, default=None)
    web.add_argument(
        "--web-workers",
        "--workers",
        dest="web_workers",
        type=int,
        default=None,
        help="Maximum queued web background jobs. --workers is kept as a compatibility alias.",
    )
    _add_unsafe_allow_remote_argument(web)

    serve = sub.add_parser("serve", help="Run the web UI and continuous ready-queue worker together.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument(
        "--web-workers",
        type=int,
        default=None,
        help="Maximum queued web background jobs.",
    )
    serve.add_argument(
        "--worker-concurrency",
        type=int,
        default=None,
        help="Maximum autonomous ready-queue issue runs.",
    )
    serve.add_argument("--interval", type=int, default=None, help="Seconds to sleep between idle worker batches.")
    _add_unsafe_allow_remote_argument(serve)

    worker = sub.add_parser("worker")
    worker_sub = worker.add_subparsers(dest="worker_command", required=True)
    once = worker_sub.add_parser("once", help="Drain ready work until the queue is idle.")
    once.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Maximum active issue runs while draining ready work.",
    )
    loop = worker_sub.add_parser("loop", help="Continuously drain ready work, then sleep when idle.")
    loop.add_argument("--interval", type=int, default=None)
    loop.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Maximum active issue runs while ready work exists.",
    )

    return parser


def handle_issue(
    args: argparse.Namespace,
    store: IssueStore,
    artifacts: ArtifactStore,
    config: AppConfig | None = None,
) -> int:
    if args.issue_command == "create":
        description = read_description(args.description, args.description_file)
        issue = store.create_issue(
            title=args.title,
            description=description,
            repo_path=_required_repo_path(args.repo),
            priority=args.priority,
            tags=args.tags,
            ready=args.ready,
        )
        artifacts.write_issue_snapshot(issue)
        print(f"Created issue {issue.id}: {issue.title}")
        return 0
    if args.issue_command == "edit":
        if not _has_issue_edit(args):
            raise ValueError("Provide at least one draft field to edit")
        if args.clear_repo:
            raise ValueError("target repo is required; --clear-repo is not supported")
        current = store.get_issue(args.id)
        description = read_optional_text(args.description, args.description_file)
        repo_path = _edited_optional_text(
            args.repo,
            args.clear_repo,
            current.repo_path,
            "repo",
            "clear-repo",
        )
        tags = _edited_optional_text(
            args.tags,
            args.clear_tags,
            current.tags,
            "tags",
            "clear-tags",
        )
        issue = store.update_draft_issue(
            args.id,
            title=args.title,
            description=description if description is not None else current.description,
            repo_path=repo_path,
            priority=args.priority if args.priority is not None else current.priority,
            tags=tags,
        )
        artifacts.write_issue_snapshot(issue)
        print(f"Issue {issue.id} edited")
        return 0
    if args.issue_command == "list":
        for issue in store.list_issues(args.status):
            print(f"{issue.id}\t{issue.status}\t{issue.phase}\tP{issue.priority}\t{issue.title}")
        return 0
    if args.issue_command == "show":
        issue = store.get_issue(args.id)
        print_issue(issue, store, artifacts)
        return 0
    if args.issue_command == "advance":
        issue = store.get_issue(args.id)
        if issue.phase == "awaiting_human_input":
            raise ValueError(
                f"Issue {issue.id} is awaiting human input; use 'agent-team issue answer-human-input' to resume"
            )
        if args.to == "awaiting_human_input":
            raise ValueError(
                "Cannot manually transition to awaiting_human_input; "
                "run the relevant phase so an agent can create a structured human-input request"
            )
        original_phase = issue.phase
        message = args.message.strip() if args.message else None
        if issue.phase == "awaiting_plan_approval" and args.to == "ready_for_plan" and message:
            artifacts.save_prior_plan(issue.id)
            artifacts.write_plan_feedback(issue.id, message)
            issue = store.reject_plan(args.id, message)
        else:
            issue = store.transition_issue(args.id, args.to, None, message)
            if original_phase == "blocked":
                if message:
                    artifacts.write_unblock_context(issue.id, issue.phase, message)
                else:
                    artifacts.clear_unblock_context(issue.id)
            if issue.phase == "ready_for_implementation":
                artifacts.clear_plan_rejection_context(issue.id)
        artifacts.write_issue_snapshot(issue)
        print(f"Issue {issue.id} advanced to {issue.phase}")
        return 0
    if args.issue_command == "approve-plan":
        issue = store.get_issue(args.id)
        if issue.phase != "awaiting_plan_approval":
            raise ValueError(f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_plan_approval'")
        issue = store.transition_issue(args.id, "ready_for_implementation", None, args.message)
        artifacts.clear_plan_rejection_context(issue.id)
        artifacts.write_issue_snapshot(issue)
        print(f"Issue {issue.id} plan approved; advanced to {issue.phase}")
        return 0
    if args.issue_command == "approve-merge":
        issue = store.get_issue(args.id)
        if issue.phase != "awaiting_merge_approval":
            raise ValueError(f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_merge_approval'")
        branch = args.branch.strip() if args.branch else None
        mode = _merge_mode_arg(args.mode) if args.mode else (config.merge_mode if config else "auto")
        remote = args.remote.strip() if args.remote else (config.pr_remote if config else None)
        message = args.message.strip() or "Human approved worktree merge and cleanup"
        artifacts.write_merge_request(issue.id, target_branch=branch, message=message, mode=mode, remote_name=remote)
        issue = store.transition_issue(args.id, "ready_for_merge", None, message)
        artifacts.write_issue_snapshot(issue)
        branch_message = f" targeting {branch}" if branch else ""
        remote_message = f" via remote {remote}" if remote else ""
        mode_message = "pull request" if mode == "pull_request" else mode
        print(f"Issue {issue.id} merge approved{branch_message} using {mode_message} mode{remote_message}; advanced to {issue.phase}")
        return 0
    if args.issue_command == "reset-to-draft":
        if config is None:
            raise ValueError("Reset requires AppConfig so workspace state can be cleaned safely")
        result = reset_issue_to_draft(config, store, artifacts, args.id, args.message)
        print_reset_result(result)
        return 0
    if args.issue_command == "stop":
        if config is None:
            raise ValueError("Stop requires AppConfig so interrupted run state can be recovered safely")
        result = stop_issue(config, store, artifacts, args.id, args.message, stopped_by="cli")
        print_stop_result(result)
        return 0
    if args.issue_command == "delete":
        expected = f"DELETE {args.id}"
        if args.confirm != expected:
            raise ValueError(f"Confirmation must be exactly {expected!r}")
        if config is None:
            raise ValueError("Delete requires AppConfig so workspace state can be cleaned safely")
        result = delete_issue(config, store, artifacts, args.id, args.message)
        print_delete_result(result)
        return 0
    if args.issue_command == "reject-plan":
        feedback = read_feedback(args.feedback, args.feedback_file)
        issue = store.get_issue(args.id)
        if issue.phase != "awaiting_plan_approval":
            raise ValueError(f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_plan_approval'")
        artifacts.save_prior_plan(issue.id)
        artifacts.write_plan_feedback(issue.id, feedback)
        issue = store.reject_plan(args.id, feedback)
        artifacts.write_issue_snapshot(issue)
        print(f"Issue {issue.id} plan rejected; returned to {issue.phase}")
        return 0
    if args.issue_command == "answer-human-input":
        answer = read_answer(args.answer, args.answer_file)
        issue, request = store.answer_human_input_request(args.id, answer, answered_by="cli")
        artifacts.append_human_input_answer(request)
        artifacts.write_human_input_summary(issue.id, store.list_human_input_requests(issue.id))
        artifacts.write_issue_snapshot(issue)
        print(f"Issue {issue.id} human input answered; resumed at {issue.phase}")
        return 0
    raise ValueError(f"Unknown issue command: {args.issue_command}")


def handle_worker(args: argparse.Namespace, store: IssueStore, artifacts: ArtifactStore, config: AppConfig) -> int:
    if args.worker_command == "once":
        concurrency = _positive_setting(args.concurrency, config.worker_concurrency)
        results = process_batch(store, artifacts, config, concurrency)
        if not results:
            print("No ready issues.")
            return 0
        for result in results:
            print_result(result)
        return 0
    if args.worker_command == "loop":
        stop_event = threading.Event()
        interval_seconds = _non_negative_setting(args.interval, config.worker_interval_seconds)
        concurrency = _positive_setting(args.concurrency, config.worker_concurrency)
        try:
            run_worker_loop(
                store,
                artifacts,
                config,
                interval_seconds=interval_seconds,
                concurrency=concurrency,
                stop_event=stop_event,
                on_result=print_result,
            )
        except KeyboardInterrupt:
            stop_event.set()
            print("\nStopping agent-team worker")
        return 0
    raise ValueError(f"Unknown worker command: {args.worker_command}")


def _positive_setting(value: int | None, default: int) -> int:
    return max(1, default if value is None else value)


def _non_negative_setting(value: int | None, default: int) -> int:
    return max(0, default if value is None else value)


def _string_setting(value: str | None, default: str) -> str:
    return default if value is None else value


def _int_setting(value: int | None, default: int) -> int:
    return default if value is None else value


def _bool_setting(value: bool | None, default: bool) -> bool:
    return default if value is None else value


def _add_unsafe_allow_remote_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--unsafe-allow-remote",
        dest="unsafe_allow_remote",
        action="store_true",
        default=None,
        help="Allow binding the unauthenticated web UI to a non-loopback address.",
    )
    parser.add_argument(
        "--no-unsafe-allow-remote",
        dest="unsafe_allow_remote",
        action="store_false",
        help=argparse.SUPPRESS,
    )


def read_description(description: str | None, description_file: str | None) -> str:
    return read_required_text(description, description_file, "description")


def read_feedback(feedback: str | None, feedback_file: str | None) -> str:
    return read_required_text(feedback, feedback_file, "feedback")


def read_answer(answer: str | None, answer_file: str | None) -> str:
    return read_required_text(answer, answer_file, "answer")


def read_optional_text(value: str | None, value_file: str | None) -> str | None:
    if value_file is not None:
        return Path(value_file).read_text(encoding="utf-8")
    if value is not None:
        return value
    return None


def read_required_text(value: str | None, value_file: str | None, name: str) -> str:
    if value_file:
        return Path(value_file).read_text(encoding="utf-8")
    if value:
        return value
    stdin = sys.stdin.read().strip()
    if stdin:
        return stdin
    raise ValueError(f"Provide --{name}, --{name}-file, or stdin")


def _has_issue_edit(args: argparse.Namespace) -> bool:
    return any(
        (
            args.title is not None,
            args.description is not None,
            args.description_file is not None,
            args.repo is not None,
            args.clear_repo,
            args.priority is not None,
            args.tags is not None,
            args.clear_tags,
        )
    )


def _edited_optional_text(
    value: str | None,
    clear: bool,
    current: str | None,
    field_name: str,
    clear_flag: str,
) -> str | None:
    if clear:
        return None
    if value is None:
        return current
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"--{field_name} cannot be empty; use --{clear_flag} to clear it")
    return cleaned


def _required_repo_path(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("target repo is required")
    return cleaned


def _merge_mode_arg(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def print_issue(issue: object, store: IssueStore, artifacts: ArtifactStore) -> None:
    issue_id = getattr(issue, "id")
    workspace = artifacts.read_workspace_metadata(issue_id)
    print(f"ID: {issue_id}")
    print(f"Title: {getattr(issue, 'title')}")
    print(f"Status: {getattr(issue, 'status')}")
    print(f"Phase: {getattr(issue, 'phase')}")
    blocked_summary = getattr(issue, "blocked_summary", None)
    if blocked_summary:
        print(f"Blocked summary: {blocked_summary}")
    print(f"Repo: {getattr(issue, 'repo_path') or ''}")
    if workspace:
        print(f"Workspace: {workspace.get('workspace_repo_path') or ''}")
        print(f"Worktree root: {workspace.get('worktree_root') or ''}")
    print()
    print(getattr(issue, "description"))
    print()
    human_input_requests = store.list_human_input_requests(issue_id)
    if human_input_requests:
        print("Human input:")
        for request in human_input_requests:
            status = request.status
            print(
                f"- {request.id} {status} requested by {request.requested_by_phase}; "
                f"resume: {request.resume_phase}"
            )
            print(f"  Question: {request.question}")
            if request.answer:
                print(f"  Answer: {request.answer}")
        print()
    issue_artifacts = artifacts.list_issue_artifacts(issue_id)
    if issue_artifacts:
        print("Artifacts:")
        for artifact in issue_artifacts:
            print(f"- {artifact.label}: {artifact.relative_path}")
        print()
    print("Runs:")
    for run in store.list_runs(issue_id):
        print(f"- {run['id']} {run['phase']} {run['status']} {run['summary'] or ''}")
    print()
    print("Events:")
    for event in store.list_events(issue_id):
        print(f"- {event['created_at']} {event['event_type']}: {event['message']}")


def print_result(result: object) -> None:
    print(f"Issue {getattr(result, 'issue_id')} {getattr(result, 'phase')} -> {getattr(result, 'next_phase')}")
    print(f"Run: {getattr(result, 'run_id')}")
    print(f"Status: {getattr(result, 'status')}")
    print(f"Summary: {getattr(result, 'summary')}")
    artifact_path = getattr(result, "artifact_path")
    if artifact_path:
        print(f"Artifact: {artifact_path}")


def print_reset_result(result: ResetIssueResult) -> None:
    print(f"Issue {result.issue_id} reset to draft (was {result.prior_phase})")
    print(f"Deleted runs: {result.deleted_runs}")
    print(f"Deleted events: {result.deleted_events}")
    print(f"Cleared artifact/log entries: {result.deleted_artifacts}")
    if result.removed_workspace_paths:
        print("Removed workspaces:")
        for path in result.removed_workspace_paths:
            print(f"- {path}")
    else:
        print("Removed workspaces: none")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"- {warning}")


def print_stop_result(result: StopIssueResult) -> None:
    print(f"Issue {result.issue_id} stopped at {result.issue.phase} (was {result.prior_phase})")
    if result.stopped_human_input_request is not None:
        print(f"Stopped human input request: {result.stopped_human_input_request.id}")


def print_delete_result(result: DeleteIssueResult) -> None:
    print(f"Issue {result.issue_id} deleted entirely (was {result.prior_phase})")
    print(f"Deleted runs: {result.deleted_runs}")
    print(f"Deleted events: {result.deleted_events}")
    print(f"Removed artifact/log entries: {result.deleted_artifacts}")
    print("Removed issue row and artifact directory")
    if result.removed_workspace_paths:
        print("Removed workspaces:")
        for path in result.removed_workspace_paths:
            print(f"- {path}")
    else:
        print("Removed workspaces: none")
    if result.warnings:
        print("Warnings:")
        for warning in result.warnings:
            print(f"- {warning}")


if __name__ == "__main__":
    raise SystemExit(main())
