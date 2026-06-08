from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Dict
from unittest.mock import patch

import agent_team.cli as cli_module
from agent_team.artifacts import ArtifactStore
import agent_team.worker as worker_module
from agent_team.cli import build_parser, handle_issue, handle_worker, print_issue
from agent_team.config import AppConfig, load_config
from agent_team.db import IssueStore
from agent_team.lifecycle import delete_issue, reset_issue_to_draft, stop_issue
from agent_team.locks import make_lock_owner
from agent_team.models import AgentResult, HumanInputRequestDraft, Issue
from agent_team.orchestrator import Orchestrator, ProcessResult
from agent_team.pull_requests import PullRequestResult
from agent_team.runners.base import AgentRunner
from agent_team.worker import process_batch, run_worker_loop
from agent_team.workspaces import WorkspaceManager, WorkspaceMergeRecovery


class RawOutputRunner(AgentRunner):
    name = "raw-output"

    def run(self, phase: str, issue: Issue, context: Dict[str, str]) -> AgentResult:
        return AgentResult(
            status="success",
            summary="raw output runner completed",
            artifact_markdown="# Clean deliverable\n\nRecommendation: `ready_for_plan`",
            suggested_next_phase="ready_for_plan",
            raw_stdout="tool transcript",
            raw_stderr="debug output",
        )


class RequeueOncePlanRunner(AgentRunner):
    name = "requeue-once"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, phase: str, issue: Issue, context: Dict[str, str]) -> AgentResult:
        self.calls += 1
        if phase == "plan" and self.calls == 1:
            return AgentResult(
                status="requeued",
                summary="Source repo changed during read-only plan phase",
                artifact_markdown="Plan discarded and requeued\n\nRecommendation: `ready_for_plan`",
                suggested_next_phase="ready_for_plan",
                error="Source repo changed during read-only plan phase",
            )
        return AgentResult(
            status="success",
            summary="Plan completed after requeue",
            artifact_markdown="Plan complete\n\nRecommendation: `ready_for_implementation`",
            suggested_next_phase="awaiting_plan_approval",
        )


class HumanInputRunner(AgentRunner):
    name = "human-input"

    def __init__(self, artifact_markdown: str | None = None) -> None:
        self.artifact_markdown = artifact_markdown

    def run(self, phase: str, issue: Issue, context: Dict[str, str]) -> AgentResult:
        artifact = self.artifact_markdown or _human_input_artifact(phase, _resume_phase_for_phase(phase))
        return AgentResult(
            status="success",
            summary=f"{phase} needs human input",
            artifact_markdown=artifact,
            suggested_next_phase="awaiting_human_input",
        )


def _resume_phase_for_phase(phase: str) -> str:
    return {
        "research": "needs_research",
        "plan": "ready_for_plan",
        "implementation": "ready_for_implementation",
        "validation": "ready_for_validation",
        "review": "ready_for_review",
        "merge_conflict_resolution": "ready_for_merge_conflict_resolution",
    }[phase]


def _human_input_artifact(phase: str, resume_phase: str) -> str:
    return f"""
## Human input request

- Requested by phase: `{phase}`
- Resume phase: `{resume_phase}`
- Question: Should the agent proceed with the risky decision?
- Rationale: The choice materially affects correctness.
- Requested decision: Approve a safe direction.
- Options:
  - Proceed
  - Stop
- Context: This is a test request.

Recommendation: `awaiting_human_input`
"""


class StoreAndWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.config = AppConfig(
            home=self.home,
            db_path=self.home / "state.db",
            artifacts_dir=self.home / "issues",
            worktrees_dir=self.home / "worktrees",
            locks_dir=self.home / "locks",
            runner="dry-run",
            lock_ttl_seconds=60,
        )
        self.store = IssueStore(self.config.db_path)
        self.store.init_schema()
        self.artifacts = ArtifactStore(self.config.artifacts_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_lock_acquire_and_release(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self.assertTrue(self.store.acquire_lock(issue.id, "worker-a", 60, "run-1"))
        self.assertFalse(self.store.acquire_lock(issue.id, "worker-b", 60, "run-2"))
        self.store.release_lock(issue.id, "worker-a", "run-1")
        self.assertTrue(self.store.acquire_lock(issue.id, "worker-b", 60, "run-2"))
        with self.store.connect() as conn:
            row = conn.execute("SELECT last_scheduled_at FROM issues WHERE id = ?", (issue.id,)).fetchone()
        self.assertIsNone(row["last_scheduled_at"])

    def test_lock_acquire_can_mark_issue_scheduled(self) -> None:
        issue = self.store.create_issue("title", "desc")

        self.assertTrue(self.store.acquire_lock(issue.id, "worker-a", 60, "run-1", mark_scheduled=True))

        with self.store.connect() as conn:
            row = conn.execute("SELECT last_scheduled_at FROM issues WHERE id = ?", (issue.id,)).fetchone()
        self.assertIsNotNone(row["last_scheduled_at"])

    def test_transition_to_blocked_stores_and_clears_summary(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        verbose_reason = """
        # Blocked reason

        Database credentials are missing. Grant the agent access to the test database.

        Traceback (most recent call last):
          File "runner.py", line 7, in <module>

        Recommendation: `blocked`
        """

        blocked = self.store.transition_issue(issue.id, "blocked", message=verbose_reason)

        self.assertEqual(
            blocked.blocked_summary,
            "Database credentials are missing. Grant the agent access to the test database.",
        )
        summary = self.store.dashboard_summary()
        self.assertEqual(summary["blocked_issues"][0]["blocked_summary"], blocked.blocked_summary)

        resumed = self.store.transition_issue(issue.id, "needs_research", message="Retry with credentials.")

        self.assertEqual(resumed.phase, "needs_research")
        self.assertIsNone(resumed.blocked_summary)

    def test_transition_to_blocked_prefers_explicit_summary(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)

        blocked = self.store.transition_issue(
            issue.id,
            "blocked",
            message="A long technical error with stack traces and tool output.",
            blocked_summary="The test database credentials are missing. Add them to the workspace and rerun research.",
        )

        self.assertEqual(
            blocked.blocked_summary,
            "The test database credentials are missing. Add them to the workspace and rerun research.",
        )

    def test_stop_issue_blocks_ready_and_approval_phases_without_deleting_history(self) -> None:
        ready = self.store.create_issue("ready stop", "desc", ready=True)
        approval = self.store.create_issue("approval stop", "desc", ready=True)
        self._move_to_plan_approval(approval.id)
        self.store.create_run("run-old", approval.id, "research", "dry-run")
        self.artifacts.write_phase_artifact(approval.id, "research", "run-old", "old research")

        ready_result = stop_issue(self.config, self.store, self.artifacts, ready.id, "Pause ready work.")
        approval_result = stop_issue(self.config, self.store, self.artifacts, approval.id, "Pause before implementation.")

        self.assertEqual(ready_result.prior_phase, "needs_research")
        self.assertEqual(ready_result.issue.phase, "blocked")
        self.assertEqual(ready_result.issue.blocked_summary, "Pause ready work.")
        self.assertEqual(approval_result.prior_phase, "awaiting_plan_approval")
        self.assertEqual(approval_result.issue.phase, "blocked")
        self.assertEqual(self.store.list_runs(approval.id)[0]["id"], "run-old")
        self.assertTrue(self.artifacts.phase_artifact_path(approval.id, "research").exists())
        snapshot = json.loads((self.config.artifacts_dir / str(approval.id) / "issue.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["phase"], "blocked")
        self.assertIn(ready.id, {row["id"] for row in self.store.dashboard_summary()["blocked_issues"]})
        self.assertNotIn(ready.id, {issue.id for issue in self.store.list_next_ready_issues(10)})
        event_types = [event["event_type"] for event in self.store.list_events(approval.id)]
        self.assertEqual(event_types[-2:], ["issue.transitioned", "issue.stopped"])

    def test_stop_issue_from_human_input_closes_pending_request_and_updates_artifacts(self) -> None:
        issue = self.store.create_issue("human stop", "desc", ready=True)
        Orchestrator(self.store, self.artifacts, self.config, runner=HumanInputRunner()).process_issue(issue.id)

        result = stop_issue(self.config, self.store, self.artifacts, issue.id, "Pause until product decides.", stopped_by="test")

        self.assertEqual(result.prior_phase, "awaiting_human_input")
        self.assertEqual(result.issue.phase, "blocked")
        self.assertEqual(result.issue.blocked_summary, "Pause until product decides.")
        self.assertIsNotNone(result.stopped_human_input_request)
        self.assertEqual(result.stopped_human_input_request.status, "stopped")
        self.assertEqual(result.stopped_human_input_request.answer, "Pause until product decides.")
        self.assertEqual(result.stopped_human_input_request.answered_by, "test")
        self.assertIsNone(self.store.get_pending_human_input_request(issue.id))
        requests = self.store.list_human_input_requests(issue.id)
        self.assertEqual(requests[0].status, "stopped")
        self.assertIn("Stop reason", self.artifacts.human_input_markdown_path(issue.id).read_text(encoding="utf-8"))
        self.assertIn("Pause until product decides.", self.artifacts.human_input_markdown_path(issue.id).read_text(encoding="utf-8"))
        jsonl = self.artifacts.human_input_jsonl_path(issue.id).read_text(encoding="utf-8")
        self.assertIn('"type": "requested"', jsonl)
        self.assertIn('"type": "stopped"', jsonl)
        event_types = [event["event_type"] for event in self.store.list_events(issue.id)]
        self.assertIn("human_input.stopped", event_types)
        self.assertIn("issue.stopped", event_types)

    def test_stop_issue_recovers_stale_run_before_blocking(self) -> None:
        issue = self.store.create_issue("stale stop", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "dead-run"))
        self.store.transition_issue(issue.id, "researching", "dead-run")
        self.store.create_run("dead-run", issue.id, "research", "dry-run")
        self._expire_lock(issue.id)

        result = stop_issue(self.config, self.store, self.artifacts, issue.id, "Stop after stale runner.")

        self.assertEqual(result.prior_phase, "needs_research")
        self.assertEqual(result.issue.phase, "blocked")
        self.assertIsNone(result.issue.current_run_id)
        self.assertIsNone(result.issue.lock_expires_at)
        self.assertEqual(self.store.list_runs(issue.id)[0]["status"], "interrupted")
        event_types = [event["event_type"] for event in self.store.list_events(issue.id)]
        self.assertIn("issue.recovered", event_types)
        self.assertIn("issue.stopped", event_types)

    def test_stop_force_blocks_active_lock_and_rejects_draft_and_closed_issue(self) -> None:
        locked = self.store.create_issue("locked stop", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(locked.id, "worker", 60, "run-locked"))
        stop_result = stop_issue(self.config, self.store, self.artifacts, locked.id)

        stopped_lock = self.store.get_issue(locked.id)
        self.assertEqual(stop_result.prior_phase, "needs_research")
        self.assertEqual(stopped_lock.phase, "blocked")
        self.assertIsNone(stopped_lock.current_run_id)
        self.assertIsNone(stopped_lock.lock_expires_at)
        self.assertEqual(stopped_lock.blocked_summary, "Issue stopped by manager")
        event_types = [event["event_type"] for event in self.store.list_events(locked.id)]
        self.assertIn("run.stopped", event_types)
        self.assertIn("issue.stopped", event_types)

        draft = self.store.create_issue("draft stop", "desc")
        with self.assertRaisesRegex(ValueError, "draft"):
            stop_issue(self.config, self.store, self.artifacts, draft.id)

        done = self.store.create_issue("done stop", "desc", ready=True)
        self.store.transition_issue(done.id, "researching")
        self.store.transition_issue(done.id, "ready_for_plan")
        self.store.transition_issue(done.id, "planning")
        self.store.transition_issue(done.id, "awaiting_plan_approval")
        self.store.transition_issue(done.id, "ready_for_implementation")
        self.store.transition_issue(done.id, "implementing")
        self.store.transition_issue(done.id, "ready_for_validation")
        self.store.transition_issue(done.id, "validating")
        self.store.transition_issue(done.id, "ready_for_review")
        self.store.transition_issue(done.id, "reviewing")
        self.store.transition_issue(done.id, "awaiting_merge_approval")
        self.store.transition_issue(done.id, "ready_for_merge")
        self.store.transition_issue(done.id, "merging")
        self.store.transition_issue(done.id, "done")
        with self.assertRaisesRegex(ValueError, "closed"):
            stop_issue(self.config, self.store, self.artifacts, done.id)

    def test_stop_force_marks_current_running_run_stopped(self) -> None:
        issue = self.store.create_issue("active run stop", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, "run-active"))
        self.store.transition_issue(issue.id, "researching", "run-active")
        self.store.create_run("run-active", issue.id, "research", "dry-run")

        result = stop_issue(self.config, self.store, self.artifacts, issue.id, "Manager cancelled active work.")

        stopped = self.store.get_issue(issue.id)
        run = self.store.list_runs(issue.id)[0]
        self.assertEqual(result.prior_phase, "researching")
        self.assertEqual(stopped.phase, "blocked")
        self.assertIsNone(stopped.current_run_id)
        self.assertIsNone(stopped.lock_owner)
        self.assertEqual(stopped.blocked_summary, "Manager cancelled active work.")
        self.assertEqual(run["status"], "stopped")
        self.assertEqual(run["summary"], "Manager cancelled active work.")
        self.assertEqual(run["error"], "Manager cancelled active work.")
        self.assertEqual(run["next_phase"], "blocked")
        self.assertEqual(self.store.get_forced_stop_state(issue.id, "run-active"), "Manager cancelled active work.")

    def test_create_run_guard_rejects_force_stop_race_before_run_row(self) -> None:
        issue = self.store.create_issue("race stop", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, "run-race"))

        stop_issue(self.config, self.store, self.artifacts, issue.id, "Stop before run row.")

        with self.assertRaisesRegex(RuntimeError, "no longer current"):
            self.store.create_run(
                "run-race",
                issue.id,
                "research",
                "dry-run",
                expected_current_run_id="run-race",
            )
        self.assertEqual(self.store.list_runs(issue.id), [])

    def test_orchestrator_force_stop_cancels_runner_and_preserves_blocked_state(self) -> None:
        issue = self.store.create_issue("cancel active run", "desc", ready=True)

        class CancellableBlockingRunner(AgentRunner):
            name = "blocking"

            def __init__(self) -> None:
                self.started = threading.Event()
                self.cancelled = threading.Event()
                self.cancel_reason: str | None = None

            def run(self, phase: str, issue: Issue, context: Dict[str, str]) -> AgentResult:
                self.started.set()
                self.cancelled.wait(timeout=5)
                return AgentResult(
                    status="success",
                    summary="runner completed after cancellation",
                    artifact_markdown="Recommendation: `ready_for_plan`",
                    suggested_next_phase="ready_for_plan",
                )

            def cancel_run(self, run_id: str, reason: str) -> bool:
                self.cancel_reason = reason
                self.cancelled.set()
                return True

        runner = CancellableBlockingRunner()
        orchestrator = Orchestrator(self.store, self.artifacts, self.config, runner=runner)
        result_holder: list[ProcessResult] = []
        errors: list[BaseException] = []

        def process() -> None:
            try:
                result_holder.append(orchestrator.process_issue(issue.id))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=process)
        thread.start()
        try:
            self.assertTrue(runner.started.wait(timeout=5))
            stop_issue(self.config, self.store, self.artifacts, issue.id, "Stop active orchestrator run.")
            thread.join(timeout=5)
        finally:
            runner.cancelled.set()
            thread.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(result_holder), 1)
        result = result_holder[0]
        self.assertEqual(result.status, "stopped")
        self.assertEqual(result.next_phase, "blocked")
        self.assertEqual(self.store.get_issue(issue.id).phase, "blocked")
        self.assertEqual(self.store.list_runs(issue.id)[0]["status"], "stopped")
        self.assertEqual(runner.cancel_reason, "Stop active orchestrator run.")
        self.assertFalse(self.artifacts.phase_artifact_path(issue.id, "research").exists())

    def test_cli_stop_prints_summary_and_uses_default_message(self) -> None:
        issue = self.store.create_issue("cli stop", "desc", ready=True)
        args = build_parser().parse_args(["issue", "stop", str(issue.id)])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts, self.config)

        stopped = self.store.get_issue(issue.id)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stopped.phase, "blocked")
        self.assertEqual(stopped.blocked_summary, "Issue stopped by manager")
        self.assertIn(f"Issue {issue.id} stopped at blocked", output.getvalue())
        show_output = io.StringIO()
        with contextlib.redirect_stdout(show_output):
            print_issue(stopped, self.store, self.artifacts)
        self.assertIn("Blocked summary: Issue stopped by manager", show_output.getvalue())

    def test_schema_migration_adds_issue_columns(self) -> None:
        old_db = self.home / "old-state.db"
        created_at = "2026-01-01T00:00:00+00:00"
        with sqlite3.connect(old_db) as conn:
            conn.execute(
                """
                CREATE TABLE issues (
                  id INTEGER PRIMARY KEY,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT 'local',
                  external_id TEXT,
                  repo_path TEXT,
                  phase TEXT NOT NULL,
                  status TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 3,
                  tags TEXT,
                  lock_owner TEXT,
                  lock_expires_at TEXT,
                  current_run_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO issues (
                  id, title, description, source, phase, status, priority, created_at, updated_at
                )
                VALUES (1, 'old issue', 'desc', 'local', 'needs_research', 'open', 3, ?, ?)
                """,
                (created_at, created_at),
            )

        store = IssueStore(old_db)
        store.init_schema()
        store.init_schema()

        with store.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(issues)").fetchall()}
            row = conn.execute("SELECT last_scheduled_at, blocked_summary FROM issues WHERE id = 1").fetchone()
        self.assertIn("last_scheduled_at", columns)
        self.assertIn("blocked_summary", columns)
        self.assertIsNone(row["last_scheduled_at"])
        self.assertIsNone(row["blocked_summary"])

    def test_schema_migration_adds_run_next_phase(self) -> None:
        old_db = self.home / "old-runs-state.db"
        with sqlite3.connect(old_db) as conn:
            conn.executescript(
                """
                CREATE TABLE issues (
                  id INTEGER PRIMARY KEY,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL,
                  source TEXT NOT NULL DEFAULT 'local',
                  external_id TEXT,
                  repo_path TEXT,
                  phase TEXT NOT NULL,
                  status TEXT NOT NULL,
                  priority INTEGER NOT NULL DEFAULT 3,
                  tags TEXT,
                  lock_owner TEXT,
                  lock_expires_at TEXT,
                  current_run_id TEXT,
                  last_scheduled_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE runs (
                  id TEXT PRIMARY KEY,
                  issue_id INTEGER NOT NULL,
                  phase TEXT NOT NULL,
                  runner TEXT NOT NULL,
                  status TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  summary TEXT,
                  artifact_path TEXT,
                  error TEXT,
                  FOREIGN KEY(issue_id) REFERENCES issues(id)
                );
                CREATE TABLE events (
                  id INTEGER PRIMARY KEY,
                  issue_id INTEGER NOT NULL,
                  run_id TEXT,
                  event_type TEXT NOT NULL,
                  message TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(issue_id) REFERENCES issues(id)
                );
                """
            )

        store = IssueStore(old_db)
        store.init_schema()
        store.init_schema()

        with store.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        self.assertIn("next_phase", columns)

    def test_ready_issue_order_uses_priority_then_round_robin_timestamp(self) -> None:
        scheduled = self.store.create_issue("scheduled", "desc", priority=3, ready=True)
        unscheduled = self.store.create_issue("unscheduled", "desc", priority=3, ready=True)
        urgent = self.store.create_issue("urgent", "desc", priority=1, ready=True)
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE issues SET created_at = ?, last_scheduled_at = ? WHERE id = ?",
                ("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:10+00:00", scheduled.id),
            )
            conn.execute(
                "UPDATE issues SET created_at = ?, last_scheduled_at = NULL WHERE id = ?",
                ("2026-01-01T00:00:02+00:00", unscheduled.id),
            )
            conn.execute(
                "UPDATE issues SET created_at = ?, last_scheduled_at = ? WHERE id = ?",
                ("2026-01-01T00:00:03+00:00", "2026-01-01T00:00:20+00:00", urgent.id),
            )

        ordered = self.store.list_next_ready_issues(3)

        self.assertEqual([issue.id for issue in ordered], [urgent.id, unscheduled.id, scheduled.id])

    def test_ready_issue_selection_and_process_next_can_scope_by_repo(self) -> None:
        repo_a = "/tmp/repo-a"
        repo_b = "/tmp/repo-b"
        repo_b_first = self.store.create_issue("repo b", "desc", repo_path=repo_b, priority=1, ready=True)
        repo_a_second = self.store.create_issue("repo a", "desc", repo_path=repo_a, priority=5, ready=True)

        self.assertEqual(self.store.find_next_ready_issue(repo_path=repo_a).id, repo_a_second.id)
        self.assertEqual(
            [issue.id for issue in self.store.list_next_ready_issues(5, repo_path=repo_b)],
            [repo_b_first.id],
        )

        orchestrator = Orchestrator(self.store, self.artifacts, self.config)
        scoped_result = orchestrator.process_next(repo_path=repo_a)

        self.assertIsNotNone(scoped_result)
        self.assertEqual(scoped_result.issue_id, repo_a_second.id)
        self.assertEqual(self.store.get_issue(repo_a_second.id).phase, "ready_for_plan")
        self.assertEqual(self.store.get_issue(repo_b_first.id).phase, "needs_research")

        global_result = orchestrator.process_next()

        self.assertIsNotNone(global_result)
        self.assertEqual(global_result.issue_id, repo_b_first.id)
        self.assertEqual(self.store.get_issue(repo_b_first.id).phase, "ready_for_plan")

    def test_new_issue_defaults_to_draft(self) -> None:
        issue = self.store.create_issue("title", "desc")

        self.assertEqual(issue.phase, "draft")
        self.assertEqual(issue.status, "open")
        self.assertIsNone(self.store.find_next_ready_issue())
        self.assertIsNone(Orchestrator(self.store, self.artifacts, self.config).process_next())
        with self.assertRaisesRegex(ValueError, "not in a runnable phase"):
            Orchestrator(self.store, self.artifacts, self.config).process_issue(issue.id)

    def test_create_issue_can_opt_into_ready_phase(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)

        self.assertEqual(issue.phase, "needs_research")
        self.assertEqual(self.store.find_next_ready_issue().id, issue.id)

    def test_published_draft_becomes_worker_runnable(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self.store.transition_issue(issue.id, "needs_research", message="publish draft")

        result = Orchestrator(self.store, self.artifacts, self.config).process_next()

        self.assertIsNotNone(result)
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")

    def test_create_issue_generates_title_from_description_when_title_omitted_or_blank(self) -> None:
        issue = self.store.create_issue(description="  # Investigate [migration](https://example.test) failure. More details.  ")

        self.assertEqual(issue.title, "Investigate migration failure.")
        self.assertEqual(issue.description, "# Investigate [migration](https://example.test) failure. More details.")
        events = self.store.list_events(issue.id)
        self.assertIn("Created draft issue: Investigate migration failure.", events[0]["message"])

        cases = [
            (" ", "# Heading title\n\nDetails", "Heading title"),
            (None, "> - [x] Fix [portal](https://example.test)", "Fix portal"),
            (None, "```\ncode title\n```\nReal title here.", "Real title here."),
            (None, "1. Ordered list title with   extra spaces", "Ordered list title with extra spaces"),
            (None, "```\n```", "Untitled issue"),
        ]
        for title, description, expected_title in cases:
            with self.subTest(description=description):
                generated = self.store.create_issue(title, description)
                self.assertEqual(generated.title, expected_title)

    def test_create_issue_preserves_explicit_title_and_rejects_blank_description(self) -> None:
        long_title = "Custom " + " ".join(["title"] * 20)

        issue = self.store.create_issue(f"  {long_title}  ", "  Description text  ")

        self.assertEqual(issue.title, long_title)
        self.assertEqual(issue.description, "Description text")
        with self.assertRaisesRegex(ValueError, "description is required"):
            self.store.create_issue(description=" ")

    def test_generated_titles_are_truncated_on_word_boundary(self) -> None:
        description = (
            "Investigate a very long migration validation failure that appears after "
            "regional failover and needs careful diagnosis before implementation."
        )

        issue = self.store.create_issue(description=description)

        self.assertLessEqual(len(issue.title), 80)
        self.assertEqual(
            issue.title,
            "Investigate a very long migration validation failure that appears after...",
        )

    def test_cli_create_defaults_to_draft_and_ready_flag_opts_in(self) -> None:
        draft_args = build_parser().parse_args(
            ["issue", "create", "--description", "draft description", "--repo", "/tmp/repo"]
        )
        ready_args = build_parser().parse_args(
            ["issue", "create", "--description", "ready description", "--repo", "/tmp/repo", "--ready"]
        )

        self.assertEqual(handle_issue(draft_args, self.store, self.artifacts), 0)
        self.assertEqual(handle_issue(ready_args, self.store, self.artifacts), 0)

        issues = self.store.list_issues()
        self.assertEqual(issues[0].phase, "draft")
        self.assertEqual(issues[0].title, "draft description")
        self.assertEqual(issues[1].phase, "needs_research")
        self.assertEqual(issues[1].title, "ready description")

    def test_cli_create_preserves_explicit_title_override(self) -> None:
        args = build_parser().parse_args(
            [
                "issue",
                "create",
                "--title",
                "  custom title  ",
                "--description",
                "generated title",
                "--repo",
                "/tmp/repo",
            ]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(handle_issue(args, self.store, self.artifacts), 0)

        issue = self.store.list_issues()[0]
        self.assertEqual(issue.title, "custom title")
        self.assertIn(f"Created issue {issue.id}: custom title", output.getvalue())

    def test_update_draft_issue_updates_metadata_preserves_state_and_records_event(self) -> None:
        issue = self.store.create_issue(
            "old title",
            "old desc",
            repo_path="/old/repo",
            priority=4,
            tags="old,tags",
            source="external",
            external_id="EXT-1",
        )
        original_created = "2026-01-01T00:00:00+00:00"
        original_updated = "2026-01-01T00:00:01+00:00"
        expired_lock = "2026-01-01T00:00:02+00:00"
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE issues
                SET created_at = ?,
                    updated_at = ?,
                    lock_owner = ?,
                    lock_expires_at = ?,
                    current_run_id = ?,
                    last_scheduled_at = ?
                WHERE id = ?
                """,
                (original_created, original_updated, "expired-owner", expired_lock, "run-expired", expired_lock, issue.id),
            )

        updated = self.store.update_draft_issue(
            issue.id,
            title="  new title  ",
            description="  new desc  ",
            repo_path="/new/repo",
            priority=1,
            tags="new,tags",
        )

        self.assertEqual(updated.title, "new title")
        self.assertEqual(updated.description, "new desc")
        self.assertEqual(updated.repo_path, "/new/repo")
        self.assertEqual(updated.priority, 1)
        self.assertEqual(updated.tags, "new,tags")
        self.assertEqual(updated.source, "external")
        self.assertEqual(updated.external_id, "EXT-1")
        self.assertEqual(updated.phase, "draft")
        self.assertEqual(updated.status, "open")
        self.assertEqual(updated.lock_owner, "expired-owner")
        self.assertEqual(updated.lock_expires_at, expired_lock)
        self.assertEqual(updated.current_run_id, "run-expired")
        self.assertEqual(updated.created_at, original_created)
        self.assertNotEqual(updated.updated_at, original_updated)
        self.assertIsNone(self.store.find_next_ready_issue())
        with self.store.connect() as conn:
            row = conn.execute("SELECT last_scheduled_at FROM issues WHERE id = ?", (issue.id,)).fetchone()
        self.assertEqual(row["last_scheduled_at"], expired_lock)
        events = self.store.list_events(issue.id)
        self.assertEqual([event["event_type"] for event in events], ["issue.created", "issue.edited"])
        self.assertIn("title, description, repo_path, priority, tags", events[-1]["message"])
        self.assertNotIn("new desc", events[-1]["message"])

    def test_update_draft_issue_preserves_overrides_and_regenerates_titles(self) -> None:
        issue = self.store.create_issue("title", "desc")

        preserved = self.store.update_draft_issue(
            issue.id,
            title=None,
            description="new description should not rename",
            repo_path=None,
            priority=3,
            tags=None,
        )
        self.assertEqual(preserved.title, "title")
        self.assertEqual(preserved.description, "new description should not rename")
        self.assertIn("description", self.store.list_events(issue.id)[-1]["message"])
        self.assertNotIn("title", self.store.list_events(issue.id)[-1]["message"])

        regenerated = self.store.update_draft_issue(
            issue.id,
            title=" ",
            description="Regenerated title. More details.",
            repo_path=None,
            priority=3,
            tags=None,
        )
        self.assertEqual(regenerated.title, "Regenerated title.")
        self.assertIn("title, description", self.store.list_events(issue.id)[-1]["message"])

    def test_update_draft_issue_rejects_invalid_state_values_and_active_lock(self) -> None:
        issue = self.store.create_issue("title", "desc")
        with self.assertRaisesRegex(ValueError, "description is required"):
            self.store.update_draft_issue(
                issue.id,
                title="title",
                description=" ",
                repo_path=None,
                priority=3,
                tags=None,
            )
        with self.assertRaisesRegex(ValueError, "priority must be an integer"):
            self.store.update_draft_issue(
                issue.id,
                title="title",
                description="desc",
                repo_path=None,
                priority="3",  # type: ignore[arg-type]
                tags=None,
            )

        ready = self.store.create_issue("ready", "desc", ready=True)
        with self.assertRaisesRegex(ValueError, "not an open draft"):
            self.store.update_draft_issue(
                ready.id,
                title="ready edit",
                description="desc",
                repo_path=None,
                priority=3,
                tags=None,
            )

        closed = self.store.create_issue("closed", "desc")
        with self.store.connect() as conn:
            conn.execute("UPDATE issues SET status = 'closed' WHERE id = ?", (closed.id,))
        with self.assertRaisesRegex(ValueError, "not an open draft"):
            self.store.update_draft_issue(
                closed.id,
                title="closed edit",
                description="desc",
                repo_path=None,
                priority=3,
                tags=None,
            )

        locked = self.store.create_issue("locked", "desc")
        self.assertTrue(self.store.acquire_lock(locked.id, "test-owner", 60, "run-1"))
        with self.assertRaisesRegex(ValueError, "active lock"):
            self.store.update_draft_issue(
                locked.id,
                title="locked edit",
                description="desc",
                repo_path=None,
                priority=3,
                tags=None,
            )

    def test_cli_edit_draft_updates_snapshot_and_supports_partial_clear(self) -> None:
        issue = self.store.create_issue("old title", "old desc", repo_path="/old/repo", priority=4, tags="old,tags")
        description_path = self.home / "description.txt"
        description_path.write_text("file desc", encoding="utf-8")
        args = build_parser().parse_args(
            [
                "issue",
                "edit",
                str(issue.id),
                "--title",
                "new title",
                "--description-file",
                str(description_path),
                "--repo",
                "/new/repo",
                "--priority",
                "1",
                "--tags",
                "new,tags",
            ]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts)

        self.assertEqual(exit_code, 0)
        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.title, "new title")
        self.assertEqual(updated.description, "file desc")
        self.assertEqual(updated.repo_path, "/new/repo")
        self.assertEqual(updated.priority, 1)
        self.assertEqual(updated.tags, "new,tags")
        snapshot = json.loads((self.config.artifacts_dir / str(issue.id) / "issue.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["title"], "new title")
        self.assertEqual(snapshot["repo_path"], "/new/repo")
        self.assertIn(f"Issue {issue.id} edited", output.getvalue())

        clear_args = build_parser().parse_args(
            ["issue", "edit", str(issue.id), "--title", "partial title", "--clear-repo", "--clear-tags"]
        )
        with self.assertRaisesRegex(ValueError, "target repo is required"):
            handle_issue(clear_args, self.store, self.artifacts)

    def test_cli_edit_without_title_preserves_title_and_blank_title_regenerates(self) -> None:
        issue = self.store.create_issue("custom title", "old desc")

        description_args = build_parser().parse_args(
            ["issue", "edit", str(issue.id), "--description", "New description title. Details."]
        )
        self.assertEqual(handle_issue(description_args, self.store, self.artifacts), 0)
        preserved = self.store.get_issue(issue.id)
        self.assertEqual(preserved.title, "custom title")
        self.assertEqual(preserved.description, "New description title. Details.")

        regenerate_args = build_parser().parse_args(["issue", "edit", str(issue.id), "--title", ""])
        self.assertEqual(handle_issue(regenerate_args, self.store, self.artifacts), 0)
        regenerated = self.store.get_issue(issue.id)
        self.assertEqual(regenerated.title, "New description title.")

    def test_cli_edit_rejects_noop_invalid_clear_values_and_non_draft(self) -> None:
        draft = self.store.create_issue("draft", "desc", repo_path="/repo", tags="tag")
        no_fields = build_parser().parse_args(["issue", "edit", str(draft.id)])
        with self.assertRaisesRegex(ValueError, "at least one draft field"):
            handle_issue(no_fields, self.store, self.artifacts)

        empty_repo = build_parser().parse_args(["issue", "edit", str(draft.id), "--repo", " "])
        with self.assertRaisesRegex(ValueError, "--repo cannot be empty"):
            handle_issue(empty_repo, self.store, self.artifacts)
        empty_tags = build_parser().parse_args(["issue", "edit", str(draft.id), "--tags", " "])
        with self.assertRaisesRegex(ValueError, "--tags cannot be empty"):
            handle_issue(empty_tags, self.store, self.artifacts)

        ready = self.store.create_issue("ready", "desc", ready=True)
        non_draft = build_parser().parse_args(["issue", "edit", str(ready.id), "--title", "new"])
        with self.assertRaisesRegex(ValueError, "not an open draft"):
            handle_issue(non_draft, self.store, self.artifacts)

    def test_worker_once_advances_issue_and_writes_artifact(self) -> None:
        issue = self.store.create_issue("title", "desc", repo_path="/tmp/repo", ready=True)
        result = Orchestrator(self.store, self.artifacts, self.config).process_next()
        self.assertIsNotNone(result)
        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "ready_for_plan")
        artifact = self.config.artifacts_dir / str(issue.id) / "research.md"
        self.assertTrue(artifact.exists())
        self.assertIn("Dry-run runner completed", artifact.read_text(encoding="utf-8"))
        logs = list((self.config.artifacts_dir / str(issue.id) / "logs").glob("research-*.md"))
        self.assertEqual(len(logs), 1)
        self.assertIn("Raw research run log", logs[0].read_text(encoding="utf-8"))
        issue_snapshot = json.loads((self.config.artifacts_dir / str(issue.id) / "issue.json").read_text(encoding="utf-8"))
        self.assertEqual(issue_snapshot["phase"], "ready_for_plan")

    def test_completed_run_records_next_phase(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)

        result = Orchestrator(self.store, self.artifacts, self.config).process_next()

        self.assertIsNotNone(result)
        runs = self.store.list_runs(issue.id)
        self.assertEqual(runs[-1]["next_phase"], "ready_for_plan")

    def test_process_issue_refreshes_lock_while_runner_is_active(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        started = threading.Event()
        release = threading.Event()

        class BlockingRunner(AgentRunner):
            name = "blocking-heartbeat"

            def run(self, phase: str, issue: Issue, context: Dict[str, str]) -> AgentResult:
                started.set()
                if not release.wait(5):
                    raise AssertionError("Timed out waiting to finish heartbeat runner")
                return AgentResult(
                    status="success",
                    summary="heartbeat runner completed",
                    artifact_markdown="heartbeat artifact\n\nRecommendation: `ready_for_plan`",
                    suggested_next_phase="ready_for_plan",
                )

        errors: list[BaseException] = []
        config = replace(self.config, lock_ttl_seconds=1)

        def process_issue() -> None:
            try:
                Orchestrator(self.store, self.artifacts, config, runner=BlockingRunner()).process_issue(issue.id)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=process_issue)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            initial_expires_at = self.store.get_issue(issue.id).lock_expires_at
            self.assertIsNotNone(initial_expires_at)
            refreshed_expires_at = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current_expires_at = self.store.get_issue(issue.id).lock_expires_at
                if current_expires_at is not None and current_expires_at > initial_expires_at:
                    refreshed_expires_at = current_expires_at
                    break
                time.sleep(0.05)
            self.assertIsNotNone(refreshed_expires_at)
        finally:
            release.set()
            thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")

    def test_recovery_marks_running_run_interrupted_and_is_idempotent(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "dead-run"))
        self.store.transition_issue(issue.id, "researching", "dead-run")
        self.store.create_run("dead-run", issue.id, "research", "dry-run")
        self._expire_lock(issue.id)
        orchestrator = Orchestrator(self.store, self.artifacts, self.config)

        result = orchestrator.recover_interrupted_issue(issue.id)
        second = orchestrator.recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertIsNone(second)
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "needs_research")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "interrupted")
        events = [event["event_type"] for event in self.store.list_events(issue.id)]
        self.assertEqual(events.count("issue.recovered"), 1)
        history = (self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8")
        self.assertIn("run_interrupted", history)

    def test_process_next_recovers_stuck_running_issue_then_runs_it(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "dead-run"))
        self.store.transition_issue(issue.id, "researching", "dead-run")
        self.store.create_run("dead-run", issue.id, "research", "dry-run")
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).process_next()

        self.assertIsNotNone(result)
        self.assertEqual(result.phase, "research")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")
        self.assertEqual([run["status"] for run in self.store.list_runs(issue.id)], ["interrupted", "success"])

    def test_recovery_forward_completes_terminal_run_with_next_phase(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "plan-run"))
        self.store.transition_issue(issue.id, "planning", "plan-run")
        self.store.create_run("plan-run", issue.id, "plan", "dry-run")
        self.store.complete_run(
            "plan-run",
            issue.id,
            "success",
            "plan completed",
            None,
            next_phase="awaiting_plan_approval",
        )
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "success")

    def test_recovery_blocks_terminal_run_with_invalid_stored_next_phase(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "plan-run"))
        self.store.transition_issue(issue.id, "planning", "plan-run")
        self.store.create_run("plan-run", issue.id, "plan", "dry-run")
        self.store.complete_run(
            "plan-run",
            issue.id,
            "success",
            "plan completed",
            None,
            next_phase="done",
        )
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertEqual(result.next_phase, "blocked")
        self.assertEqual(result.action, "terminal_run_blocked")
        self.assertIn("invalid next phase", result.summary)
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "blocked")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        run = self.store.list_runs(issue.id)[-1]
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["next_phase"], "done")

    def test_recovery_forward_completes_terminal_run_after_current_run_id_cleared(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "plan-run"))
        self.store.transition_issue(issue.id, "planning", "plan-run")
        self.store.create_run("plan-run", issue.id, "plan", "dry-run")
        self.store.complete_run(
            "plan-run",
            issue.id,
            "success",
            "plan completed",
            None,
            next_phase="awaiting_plan_approval",
        )
        self.store.release_lock(issue.id, "legacy-worker", "plan-run")
        self.assertIsNone(self.store.get_issue(issue.id).current_run_id)

        orchestrator = Orchestrator(self.store, self.artifacts, self.config)
        result = orchestrator.recover_interrupted_issue(issue.id)
        second = orchestrator.recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertIsNone(second)
        self.assertEqual(result.next_phase, "awaiting_plan_approval")
        self.assertEqual(result.action, "run_forward_completed")
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "awaiting_plan_approval")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "success")

    def test_recovery_blocks_terminal_run_after_current_run_id_cleared_with_invalid_next_phase(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "plan-run"))
        self.store.transition_issue(issue.id, "planning", "plan-run")
        self.store.create_run("plan-run", issue.id, "plan", "dry-run")
        self.store.complete_run(
            "plan-run",
            issue.id,
            "success",
            "plan completed",
            None,
            next_phase="done",
        )
        self.store.release_lock(issue.id, "legacy-worker", "plan-run")

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertEqual(result.next_phase, "blocked")
        self.assertEqual(result.action, "terminal_run_blocked")
        self.assertIn("invalid next phase", result.summary)
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "blocked")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "success")

    def test_recovery_blocks_terminal_run_with_invalid_utf8_artifact(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "plan-run"))
        self.store.transition_issue(issue.id, "planning", "plan-run")
        self.store.create_run("plan-run", issue.id, "plan", "copilot")
        artifact_path = self.artifacts.phase_artifact_path(issue.id, "plan")
        artifact_path.write_bytes(b"\xff\xfe\xff")
        self.store.complete_run(
            "plan-run",
            issue.id,
            "success",
            "plan completed",
            str(artifact_path),
            next_phase=None,
        )
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertEqual(result.next_phase, "blocked")
        self.assertEqual(result.action, "terminal_run_blocked")
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "blocked")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        run = self.store.list_runs(issue.id)[-1]
        self.assertEqual(run["status"], "success")
        self.assertEqual(run["next_phase"], "blocked")

    def test_recovery_prefers_terminal_artifact_blocked_summary(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        blocked_summary = "The source checkout credentials are missing. Add them and rerun research."
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "failed-run"))
        self.store.transition_issue(issue.id, "researching", "failed-run")
        self.store.create_run("failed-run", issue.id, "research", "copilot-cli")
        artifact_path = self.artifacts.write_phase_artifact(
            issue.id,
            "research",
            "failed-run",
            (
                "# Research blocked\n\n"
                "Verbose diagnostics and retry metadata should not be the primary blocked reason.\n\n"
                f"Blocked summary: {blocked_summary}\n"
                "Recommendation: `blocked`\n"
            ),
        )
        self.store.complete_run(
            "failed-run",
            issue.id,
            "failed",
            "Copilot CLI research failed with verbose internal details.",
            str(artifact_path),
            "Traceback and subprocess output should stay technical.",
        )
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertEqual(result.next_phase, "blocked")
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "blocked")
        self.assertEqual(recovered.blocked_summary, blocked_summary)
        self.assertNotIn("Recovered terminal", recovered.blocked_summary)
        issue_payload_path = self.config.artifacts_dir / str(issue.id) / "issue.json"
        issue_payload = json.loads(issue_payload_path.read_text(encoding="utf-8"))
        self.assertEqual(issue_payload["blocked_summary"], blocked_summary)

    def test_recovery_clears_stale_lock_after_plan_reaches_approval(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "plan-run"))
        self.store.transition_issue(issue.id, "planning", "plan-run")
        self.store.create_run("plan-run", issue.id, "plan", "dry-run")
        self.store.complete_run(
            "plan-run",
            issue.id,
            "success",
            "plan completed",
            None,
            next_phase="awaiting_plan_approval",
        )
        self.store.transition_issue(issue.id, "awaiting_plan_approval", "plan-run")
        self._expire_lock(issue.id)
        orchestrator = Orchestrator(self.store, self.artifacts, self.config)

        result = orchestrator.recover_interrupted_issue(issue.id)
        second = orchestrator.recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertIsNone(second)
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "awaiting_plan_approval")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "success")
        events = [event["event_type"] for event in self.store.list_events(issue.id)]
        self.assertEqual(events.count("issue.recovered"), 1)

    def test_global_recovery_clears_stale_lock_after_merge_reaches_done(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self._move_to_merge_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_merge")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "merge-run"))
        self.store.transition_issue(issue.id, "merging", "merge-run")
        self.store.create_run("merge-run", issue.id, "merge", "workspace-merge")
        self.store.complete_run(
            "merge-run",
            issue.id,
            "success",
            "merge completed",
            None,
            next_phase="done",
        )
        self.store.transition_issue(issue.id, "done", "merge-run")
        self._expire_lock(issue.id)

        results = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_runs()

        self.assertEqual(len(results), 1)
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "done")
        self.assertEqual(recovered.status, "closed")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        self.assertEqual(results[0].action, "post_transition_lock_cleared")

    def test_global_merge_recovery_blocks_unreadable_workspace_metadata(self) -> None:
        issue = self._start_stale_merge_run()
        self.artifacts.workspace_metadata_path(issue.id).write_text("{", encoding="utf-8")

        results = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_runs()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].next_phase, "blocked")
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "blocked")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        run = self.store.list_runs(issue.id)[-1]
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["next_phase"], "blocked")
        artifact = self.artifacts.phase_artifact_path(issue.id, "merge").read_text(encoding="utf-8")
        self.assertIn("Workspace metadata is unreadable", artifact)
        self.assertIn("workspace.json", artifact)
        self.assertIn("Recommendation: `blocked`", artifact)

    def test_global_merge_recovery_blocks_unreadable_merged_workspace_metadata(self) -> None:
        issue = self._start_stale_merge_run()
        self.artifacts.merged_workspace_metadata_path(issue.id).write_text("{", encoding="utf-8")

        results = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_runs()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].next_phase, "blocked")
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "blocked")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)
        run = self.store.list_runs(issue.id)[-1]
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["next_phase"], "blocked")
        artifact = self.artifacts.phase_artifact_path(issue.id, "merge").read_text(encoding="utf-8")
        self.assertIn("Merged workspace metadata is unreadable", artifact)
        self.assertIn("workspace.merged.json", artifact)
        self.assertIn("Recommendation: `blocked`", artifact)

    def test_merge_recovery_claims_issue_before_workspace_recovery(self) -> None:
        issue = self._start_stale_merge_run()
        observed: dict[str, str | None] = {}
        store = self.store

        class FakeWorkspaceManager:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def recover_interrupted_merge(self, merge_issue: Issue) -> WorkspaceMergeRecovery:
                claimed = store.get_issue(merge_issue.id)
                observed["phase"] = claimed.phase
                observed["lock_owner"] = claimed.lock_owner
                observed["lock_expires_at"] = claimed.lock_expires_at
                return WorkspaceMergeRecovery(
                    next_phase="ready_for_merge",
                    run_status="interrupted",
                    summary="Recovered interrupted merge before source merge completed; retrying merge is safe.",
                    artifact_markdown="# Merge Result\n\nRecommendation: `ready_for_merge`",
                )

        with patch("agent_team.orchestrator.WorkspaceManager", FakeWorkspaceManager):
            result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertEqual(observed["phase"], "merging")
        self.assertIsNotNone(observed["lock_owner"])
        self.assertNotEqual(observed["lock_owner"], "legacy-worker")
        self.assertIsNotNone(observed["lock_expires_at"])
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "ready_for_merge")
        self.assertIsNone(recovered.lock_owner)
        self.assertIsNone(recovered.lock_expires_at)

    def test_merge_recovery_lost_lease_does_not_finalize_stale_workspace_result(self) -> None:
        issue = self._start_stale_merge_run()
        artifacts = self.artifacts
        store = self.store
        metadata = {
            "issue_id": issue.id,
            "original_repo_path": str(self.home / "source"),
            "source_root": str(self.home / "source"),
            "source_git_common_dir": str(self.home / "source" / ".git"),
            "relative_subpath": "",
            "worktree_root": str(self.home / "worktree"),
            "workspace_repo_path": str(self.home / "worktree"),
            "source_branch": "master",
            "source_head": "source-head",
            "created_at": "2026-01-01T00:00:00+00:00",
            "cleanup_removed": True,
            "merge_commit": "merge-commit",
            "merge_target_branch": "master",
            "worktree_head": "worktree-head",
            "worktree_commit": None,
        }

        class FakeWorkspaceManager:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def recover_interrupted_merge(self, merge_issue: Issue) -> WorkspaceMergeRecovery:
                artifacts.write_merged_workspace_metadata(merge_issue.id, metadata)
                artifacts.delete_workspace_metadata(merge_issue.id)
                with store.connect() as conn:
                    conn.execute(
                        """
                        UPDATE issues
                        SET lock_owner = ?, lock_expires_at = ?
                        WHERE id = ?
                        """,
                        ("other-recovery", "2999-01-01T00:00:00+00:00", merge_issue.id),
                    )
                return WorkspaceMergeRecovery(
                    next_phase="done",
                    run_status="success",
                    summary="Recovered completed merge after cleanup.",
                    artifact_markdown="# Merge Result\n\nRecommendation: `done`",
                )

        with patch("agent_team.orchestrator.WorkspaceManager", FakeWorkspaceManager):
            result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNone(result)
        still_merging = self.store.get_issue(issue.id)
        self.assertEqual(still_merging.phase, "merging")
        self.assertEqual(still_merging.lock_owner, "other-recovery")
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "running")
        self.assertFalse(self.artifacts.phase_artifact_path(issue.id, "merge").exists())

        self._expire_lock(issue.id)
        recovered = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.next_phase, "done")
        self.assertEqual(self.store.get_issue(issue.id).phase, "done")
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "success")

    def test_recovery_does_not_reclaim_unexpired_legacy_lock(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "dead-run"))
        self.store.transition_issue(issue.id, "researching", "dead-run")
        self.store.create_run("dead-run", issue.id, "research", "dry-run")

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNone(result)
        self.assertEqual(self.store.get_issue(issue.id).phase, "researching")
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "running")

    def test_recovery_does_not_reclaim_expired_live_structured_lock(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        owner = make_lock_owner("dry-run")
        self.assertTrue(self.store.acquire_lock(issue.id, owner, 60, "live-run"))
        self.store.transition_issue(issue.id, "researching", "live-run")
        self.store.create_run("live-run", issue.id, "research", "dry-run")
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNone(result)
        locked = self.store.get_issue(issue.id)
        self.assertEqual(locked.phase, "researching")
        self.assertEqual(locked.lock_owner, owner)
        self.assertEqual(locked.current_run_id, "live-run")
        self.assertIsNotNone(locked.lock_expires_at)
        self.assertEqual(self.store.list_runs(issue.id)[-1]["status"], "running")

    def test_ready_phase_recovery_clears_stale_scheduling_lock(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "dead-run"))
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "needs_research")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)

    def test_artifact_is_archived_before_phase_rerun(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.artifacts.write_phase_artifact(issue.id, "research", "old-run", "old research")

        result = Orchestrator(self.store, self.artifacts, self.config).process_next()

        self.assertIsNotNone(result)
        archive = self.config.artifacts_dir / str(issue.id) / "archive" / f"research-before-{result.run_id}.md"
        self.assertTrue(archive.is_file())
        self.assertIn("old research", archive.read_text(encoding="utf-8"))
        self.assertIn("Dry-run runner completed", (self.config.artifacts_dir / str(issue.id) / "research.md").read_text(encoding="utf-8"))
        relative_paths = {artifact.relative_path for artifact in self.artifacts.list_issue_artifacts(issue.id)}
        self.assertIn(f"archive/research-before-{result.run_id}.md", relative_paths)

    def test_raw_runner_output_is_written_to_logs(self) -> None:
        issue = self.store.create_issue("title", "desc", repo_path="/tmp/repo", ready=True)
        result = Orchestrator(self.store, self.artifacts, self.config, runner=RawOutputRunner()).process_next()
        self.assertIsNotNone(result)
        artifact = self.config.artifacts_dir / str(issue.id) / "research.md"
        self.assertIn("# Clean deliverable", artifact.read_text(encoding="utf-8"))
        logs = list((self.config.artifacts_dir / str(issue.id) / "logs").glob("research-*.md"))
        self.assertEqual(len(logs), 1)
        log_text = logs[0].read_text(encoding="utf-8")
        self.assertIn("tool transcript", log_text)
        self.assertIn("debug output", log_text)
        history = json.loads((self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(history["log_path"], str(logs[0]))

    def test_orchestrator_persists_agent_blocked_summary(self) -> None:
        issue = self.store.create_issue("title", "desc", repo_path="/tmp/repo", ready=True)

        class BlockingRunner(AgentRunner):
            name = "blocking"

            def run(self, phase: str, issue: Issue, context: Dict[str, str]) -> AgentResult:
                return AgentResult(
                    status="blocked",
                    summary="Verbose runner failed with many implementation details.",
                    artifact_markdown=(
                        "The runner cannot continue.\n\n"
                        "Blocked summary: Missing repository credentials. Add the credentials and rerun research.\n"
                        "Recommendation: `blocked`"
                    ),
                    suggested_next_phase="blocked",
                    error="Verbose runner failed with many implementation details.",
                    blocked_summary="Missing repository credentials. Add the credentials and rerun research.",
                )

        result = Orchestrator(self.store, self.artifacts, self.config, runner=BlockingRunner()).process_next()

        self.assertIsNotNone(result)
        self.assertEqual(result.next_phase, "blocked")
        blocked = self.store.get_issue(issue.id)
        self.assertEqual(
            blocked.blocked_summary,
            "Missing repository credentials. Add the credentials and rerun research.",
        )
        history = json.loads((self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8").strip())
        self.assertEqual(history["blocked_summary"], blocked.blocked_summary)

    def test_dry_run_flow_blocks_merge_without_workspace(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        orchestrator = Orchestrator(self.store, self.artifacts, self.config)
        self.assertIsNotNone(orchestrator.process_next())
        self.assertIsNotNone(orchestrator.process_next())
        planned = self.store.get_issue(issue.id)
        self.assertEqual(planned.phase, "awaiting_plan_approval")
        self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")
        for _ in range(3):
            self.assertIsNotNone(orchestrator.process_next())
        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "awaiting_merge_approval")
        self.store.transition_issue(issue.id, "ready_for_merge", message="test approved merge")
        self.assertIsNotNone(orchestrator.process_next())
        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "blocked")
        self.assertEqual(updated.status, "open")
        issue_snapshot = json.loads((self.config.artifacts_dir / str(issue.id) / "issue.json").read_text(encoding="utf-8"))
        self.assertEqual(issue_snapshot["phase"], "blocked")
        self.assertEqual(issue_snapshot["status"], "open")
        self.assertIn("no target repo", self.store.list_runs(issue.id)[-1]["summary"])

    def test_worker_stops_for_plan_approval(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        orchestrator = Orchestrator(self.store, self.artifacts, self.config)
        self.assertIsNotNone(orchestrator.process_next())
        self.assertIsNotNone(orchestrator.process_next())
        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "awaiting_plan_approval")
        self.assertIsNone(orchestrator.process_next())

    def test_worker_stops_for_merge_approval(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        orchestrator = Orchestrator(self.store, self.artifacts, self.config)
        self.assertIsNotNone(orchestrator.process_next())
        self.assertIsNotNone(orchestrator.process_next())
        self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")
        for _ in range(3):
            self.assertIsNotNone(orchestrator.process_next())
        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "awaiting_merge_approval")
        self.assertIsNone(orchestrator.process_next())

    def test_process_batch_refills_slot_when_run_finishes(self) -> None:
        first = self.store.create_issue("first", "desc", ready=True)
        second = self.store.create_issue("second", "desc", ready=True)
        third = self.store.create_issue("third", "desc", ready=True)
        case = self
        starts: list[int] = []
        start_events = {issue.id: threading.Event() for issue in (first, second, third)}
        release_slow = threading.Event()
        slow_finished = threading.Event()
        lock = threading.Lock()
        active = 0
        max_active = 0

        class FakeOrchestrator:
            def __init__(self, store: IssueStore, artifacts: ArtifactStore, config: AppConfig) -> None:
                self.store = store

            def process_issue(self, issue_id: int, phase: str | None = None) -> ProcessResult:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    starts.append(issue_id)
                start_events[issue_id].set()
                try:
                    case.store.transition_issue(issue_id, "researching")
                    if issue_id == first.id:
                        release_slow.wait(5)
                        slow_finished.set()
                    return ProcessResult(
                        issue_id=issue_id,
                        run_id=f"run-{issue_id}",
                        phase="research",
                        status="success",
                        next_phase="researching",
                        summary="done",
                        artifact_path=None,
                    )
                finally:
                    with lock:
                        active -= 1

        results: list[ProcessResult] = []
        errors: list[BaseException] = []

        def run_batch() -> None:
            try:
                results.extend(process_batch(case.store, case.artifacts, case.config, concurrency=2))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(worker_module, "Orchestrator", FakeOrchestrator):
            thread = threading.Thread(target=run_batch)
            thread.start()
            try:
                self.assertTrue(start_events[third.id].wait(2))
                self.assertFalse(slow_finished.is_set())
            finally:
                release_slow.set()
                thread.join(5)

        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(set(starts), {first.id, second.id, third.id})
        self.assertEqual({result.issue_id for result in results}, {first.id, second.id, third.id})
        self.assertLessEqual(max_active, 2)

    def test_process_batch_refills_slot_when_new_issue_becomes_ready(self) -> None:
        first = self.store.create_issue("first", "desc", ready=True)
        case = self
        starts: list[int] = []
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        first_finished = threading.Event()
        lock = threading.Lock()

        class FakeOrchestrator:
            def __init__(self, store: IssueStore, artifacts: ArtifactStore, config: AppConfig) -> None:
                self.store = store

            def process_issue(self, issue_id: int, phase: str | None = None) -> ProcessResult:
                with lock:
                    starts.append(issue_id)
                case.store.transition_issue(issue_id, "researching")
                if issue_id == first.id:
                    first_started.set()
                    release_first.wait(5)
                    first_finished.set()
                else:
                    second_started.set()
                return ProcessResult(
                    issue_id=issue_id,
                    run_id=f"run-{issue_id}",
                    phase="research",
                    status="success",
                    next_phase="researching",
                    summary="done",
                    artifact_path=None,
                )

        results: list[ProcessResult] = []
        errors: list[BaseException] = []

        def run_batch() -> None:
            try:
                results.extend(process_batch(case.store, case.artifacts, case.config, concurrency=2))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(worker_module, "Orchestrator", FakeOrchestrator):
            with patch.object(worker_module, "SLOT_REFILL_POLL_SECONDS", 0.01):
                thread = threading.Thread(target=run_batch)
                thread.start()
                try:
                    self.assertTrue(first_started.wait(2))
                    second = self.store.create_issue("second", "desc", ready=True)
                    self.assertTrue(second_started.wait(2))
                    self.assertFalse(first_finished.is_set())
                finally:
                    release_first.set()
                    thread.join(5)

        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(starts, [first.id, second.id])
        self.assertEqual({result.issue_id for result in results}, {first.id, second.id})

    def test_process_batch_refills_after_lock_race(self) -> None:
        first = self.store.create_issue("first", "desc", ready=True)
        second = self.store.create_issue("second", "desc", ready=True)
        case = self
        starts: list[int] = []

        class FakeOrchestrator:
            def __init__(self, store: IssueStore, artifacts: ArtifactStore, config: AppConfig) -> None:
                self.store = store

            def process_issue(self, issue_id: int, phase: str | None = None) -> ProcessResult:
                starts.append(issue_id)
                if issue_id == first.id:
                    case.store.acquire_lock(first.id, "other-worker", 60, "other-run")
                    raise RuntimeError("Issue is locked by another worker")
                case.store.transition_issue(issue_id, "researching")
                return ProcessResult(
                    issue_id=issue_id,
                    run_id=f"run-{issue_id}",
                    phase="research",
                    status="success",
                    next_phase="researching",
                    summary="done",
                    artifact_path=None,
                )

        with patch.object(worker_module, "Orchestrator", FakeOrchestrator):
            results = process_batch(self.store, self.artifacts, self.config, concurrency=1)

        self.assertEqual(starts, [first.id, second.id])
        self.assertEqual([result.issue_id for result in results], [second.id])

    def test_process_batch_drains_until_idle(self) -> None:
        issues = [self.store.create_issue(f"issue {index}", "desc", ready=True) for index in range(3)]

        results = process_batch(self.store, self.artifacts, self.config, concurrency=2)

        self.assertEqual(len(results), 6)
        self.assertEqual({result.issue_id for result in results}, {issue.id for issue in issues})
        for issue in issues:
            self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")

    def test_process_batch_requeues_changed_plan_to_ready_for_plan(self) -> None:
        issue = self.store.create_issue("plan source changed", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        runner = RequeueOncePlanRunner()

        def build_orchestrator(store: IssueStore, artifacts: ArtifactStore, config: AppConfig) -> Orchestrator:
            return Orchestrator(store, artifacts, config, runner=runner)

        with patch.object(worker_module, "Orchestrator", build_orchestrator):
            results = process_batch(self.store, self.artifacts, self.config, concurrency=1)

        self.assertEqual([result.status for result in results], ["requeued", "success"])
        self.assertEqual([result.next_phase for result in results], ["ready_for_plan", "awaiting_plan_approval"])
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")
        self.assertEqual(runner.calls, 2)

    def test_process_batch_recovers_interrupted_run_before_scheduling(self) -> None:
        issue = self.store.create_issue("stuck", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "dead-run"))
        self.store.transition_issue(issue.id, "researching", "dead-run")
        self.store.create_run("dead-run", issue.id, "research", "dry-run")
        self._expire_lock(issue.id)

        results = process_batch(self.store, self.artifacts, self.config, concurrency=1)

        self.assertEqual([result.phase for result in results], ["research", "plan"])
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")
        self.assertEqual([run["status"] for run in self.store.list_runs(issue.id)], ["interrupted", "success", "success"])

    def test_process_batch_does_not_schedule_when_stop_is_already_set(self) -> None:
        issue = self.store.create_issue("ready", "desc", ready=True)
        stop_event = threading.Event()
        stop_event.set()

        results = process_batch(self.store, self.artifacts, self.config, concurrency=2, stop_event=stop_event)

        self.assertEqual(results, [])
        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")

    def test_orchestrator_persists_human_input_request_and_worker_skips_it(self) -> None:
        issue = self.store.create_issue("human needed", "desc", ready=True)

        result = Orchestrator(self.store, self.artifacts, self.config, runner=HumanInputRunner()).process_issue(issue.id)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.next_phase, "awaiting_human_input")
        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "awaiting_human_input")
        pending = self.store.get_pending_human_input_request(issue.id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.resume_phase, "needs_research")
        self.assertEqual(self.store.find_next_ready_issue(), None)
        self.assertIsNone(Orchestrator(self.store, self.artifacts, self.config).process_next())
        self.assertIn(
            "Should the agent proceed",
            (self.config.artifacts_dir / str(issue.id) / "human_input.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            '"type": "requested"',
            (self.config.artifacts_dir / str(issue.id) / "human_input.jsonl").read_text(encoding="utf-8"),
        )
        self.assertIn("human_input_request", (self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8"))

    def test_orchestrator_blocks_malformed_human_input_request(self) -> None:
        issue = self.store.create_issue("bad human request", "desc", ready=True)
        runner = HumanInputRunner("Recommendation: `awaiting_human_input`")

        result = Orchestrator(self.store, self.artifacts, self.config, runner=runner).process_issue(issue.id)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.next_phase, "blocked")
        self.assertIn("Invalid human input request", result.summary)
        self.assertEqual(self.store.get_issue(issue.id).phase, "blocked")
        self.assertIsNone(self.store.get_pending_human_input_request(issue.id))

    def test_answer_human_input_request_persists_decision_and_resumes(self) -> None:
        issue = self.store.create_issue("answer me", "desc", ready=True)
        Orchestrator(self.store, self.artifacts, self.config, runner=HumanInputRunner()).process_issue(issue.id)

        updated, request = self.store.answer_human_input_request(issue.id, "Use the safe option.", answered_by="test")
        self.artifacts.append_human_input_answer(request)
        self.artifacts.write_human_input_summary(issue.id, self.store.list_human_input_requests(issue.id))

        self.assertEqual(updated.phase, "needs_research")
        self.assertEqual(request.status, "answered")
        self.assertEqual(request.answer, "Use the safe option.")
        self.assertIsNone(self.store.get_pending_human_input_request(issue.id))
        event_types = [event["event_type"] for event in self.store.list_events(issue.id)]
        self.assertIn("human_input.answered", event_types)
        summary = (self.config.artifacts_dir / str(issue.id) / "human_input.md").read_text(encoding="utf-8")
        self.assertIn("Use the safe option.", summary)
        self.assertIn('"type": "answered"', (self.config.artifacts_dir / str(issue.id) / "human_input.jsonl").read_text(encoding="utf-8"))

    def test_human_input_answer_rejects_empty_wrong_phase_and_double_answer(self) -> None:
        issue = self.store.create_issue("answer validation", "desc", ready=True)
        with self.assertRaisesRegex(ValueError, "not 'awaiting_human_input'"):
            self.store.answer_human_input_request(issue.id, "answer")
        Orchestrator(self.store, self.artifacts, self.config, runner=HumanInputRunner()).process_issue(issue.id)
        with self.assertRaisesRegex(ValueError, "answer is required"):
            self.store.answer_human_input_request(issue.id, " ")
        self.store.answer_human_input_request(issue.id, "answer")
        with self.assertRaisesRegex(ValueError, "not 'awaiting_human_input'"):
            self.store.answer_human_input_request(issue.id, "second")

    def test_human_input_generic_transition_and_duplicate_pending_are_rejected(self) -> None:
        issue = self.store.create_issue("guarded", "desc", ready=True)
        Orchestrator(self.store, self.artifacts, self.config, runner=HumanInputRunner()).process_issue(issue.id)

        with self.assertRaisesRegex(ValueError, "answer-human-input"):
            self.store.transition_issue(issue.id, "needs_research")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_pending_human_input(issue.id, "duplicate")

    def test_generic_transition_to_human_input_without_request_is_rejected(self) -> None:
        issue = self.store.create_issue("manual human input", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")

        with self.assertRaisesRegex(ValueError, "Cannot transition to awaiting_human_input directly"):
            self.store.transition_issue(issue.id, "awaiting_human_input")

        self.assertEqual(self.store.get_issue(issue.id).phase, "researching")
        self.assertIsNone(self.store.get_pending_human_input_request(issue.id))

    def test_cli_advance_to_human_input_without_request_is_rejected(self) -> None:
        issue = self.store.create_issue("cli manual human input", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        args = build_parser().parse_args(["issue", "advance", str(issue.id), "--to", "awaiting_human_input"])

        with self.assertRaisesRegex(ValueError, "Cannot manually transition to awaiting_human_input"):
            handle_issue(args, self.store, self.artifacts)

        self.assertEqual(self.store.get_issue(issue.id).phase, "researching")
        self.assertIsNone(self.store.get_pending_human_input_request(issue.id))

    def test_cli_answer_human_input_updates_artifacts_and_show_output(self) -> None:
        issue = self.store.create_issue("cli answer", "desc", ready=True)
        Orchestrator(self.store, self.artifacts, self.config, runner=HumanInputRunner()).process_issue(issue.id)
        args = build_parser().parse_args(["issue", "answer-human-input", str(issue.id), "--answer", "CLI decision"])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")
        self.assertIn("resumed at needs_research", output.getvalue())
        self.assertIn("CLI decision", (self.config.artifacts_dir / str(issue.id) / "human_input.md").read_text(encoding="utf-8"))
        show_output = io.StringIO()
        with contextlib.redirect_stdout(show_output):
            print_issue(self.store.get_issue(issue.id), self.store, self.artifacts)
        self.assertIn("Human input:", show_output.getvalue())
        self.assertIn("CLI decision", show_output.getvalue())

    def test_recovery_blocks_terminal_run_that_requested_uncommitted_human_input(self) -> None:
        issue = self.store.create_issue("stale human", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "human-run"))
        self.store.transition_issue(issue.id, "researching", "human-run")
        self.store.create_run("human-run", issue.id, "research", "copilot-cli")
        artifact_path = self.artifacts.write_phase_artifact(
            issue.id,
            "research",
            "human-run",
            _human_input_artifact("research", "needs_research"),
        )
        self.store.complete_run(
            "human-run",
            issue.id,
            "success",
            "needs human",
            str(artifact_path),
            next_phase="awaiting_human_input",
        )
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)

        self.assertIsNotNone(result)
        self.assertEqual(result.next_phase, "blocked")
        self.assertIn("requested human input", result.summary)
        self.assertEqual(self.store.get_issue(issue.id).phase, "blocked")
        self.assertIsNone(self.store.get_pending_human_input_request(issue.id))

    def test_process_batch_does_not_refill_after_stop_is_requested(self) -> None:
        first = self.store.create_issue("first", "desc", ready=True)
        second = self.store.create_issue("second", "desc", ready=True)
        stop_event = threading.Event()
        starts: list[int] = []
        case = self

        class FakeOrchestrator:
            def __init__(self, store: IssueStore, artifacts: ArtifactStore, config: AppConfig) -> None:
                self.store = store

            def process_issue(self, issue_id: int, phase: str | None = None) -> ProcessResult:
                starts.append(issue_id)
                case.store.transition_issue(issue_id, "researching")
                stop_event.set()
                return ProcessResult(
                    issue_id=issue_id,
                    run_id=f"run-{issue_id}",
                    phase="research",
                    status="success",
                    next_phase="researching",
                    summary="done",
                    artifact_path=None,
                )

        with patch.object(worker_module, "Orchestrator", FakeOrchestrator):
            results = process_batch(self.store, self.artifacts, self.config, concurrency=1, stop_event=stop_event)

        self.assertEqual(starts, [first.id])
        self.assertEqual([result.issue_id for result in results], [first.id])
        self.assertEqual(self.store.get_issue(second.id).phase, "needs_research")

    def test_run_worker_loop_uses_stop_event_wait_and_reports_results(self) -> None:
        result = ProcessResult(
            issue_id=1,
            run_id="run-1",
            phase="research",
            status="success",
            next_phase="ready_for_plan",
            summary="done",
            artifact_path=None,
        )
        seen: list[ProcessResult] = []

        class StopAfterWait:
            def __init__(self) -> None:
                self.intervals: list[int] = []
                self.stopped = False

            def is_set(self) -> bool:
                return self.stopped

            def wait(self, interval: int) -> bool:
                self.intervals.append(interval)
                self.stopped = True
                return True

        stop_event = StopAfterWait()
        with patch.object(worker_module, "process_batch", return_value=[result]) as batch:
            run_worker_loop(
                self.store,
                self.artifacts,
                self.config,
                interval_seconds=42,
                concurrency=3,
                stop_event=stop_event,  # type: ignore[arg-type]
                on_result=seen.append,
            )

        self.assertEqual(seen, [result])
        self.assertEqual(stop_event.intervals, [42])
        batch.assert_called_once_with(self.store, self.artifacts, self.config, 3, stop_event=stop_event)

    def test_worker_once_uses_config_concurrency_default(self) -> None:
        args = build_parser().parse_args(["worker", "once"])
        config = replace(self.config, worker_concurrency=4)

        output = io.StringIO()
        with patch.object(cli_module, "process_batch", return_value=[]) as batch:
            with contextlib.redirect_stdout(output):
                exit_code = handle_worker(args, self.store, self.artifacts, config)

        self.assertEqual(exit_code, 0)
        self.assertIn("No ready issues.", output.getvalue())
        batch.assert_called_once_with(self.store, self.artifacts, config, 4)

    def test_worker_loop_uses_config_defaults(self) -> None:
        args = build_parser().parse_args(["worker", "loop"])
        config = replace(self.config, worker_concurrency=3, worker_interval_seconds=17)

        with patch.object(cli_module, "run_worker_loop") as loop:
            exit_code = handle_worker(args, self.store, self.artifacts, config)

        self.assertEqual(exit_code, 0)
        self.assertEqual(loop.call_args.kwargs["interval_seconds"], 17)
        self.assertEqual(loop.call_args.kwargs["concurrency"], 3)
        self.assertIs(loop.call_args.kwargs["on_result"], cli_module.print_result)

    def test_worker_once_uses_config_file_default(self) -> None:
        config_path = self.home / "worker-config.jsonc"
        config_path.write_text(
            json.dumps(
                {
                    "home": str(self.home / "config-file-state"),
                    "runner": "dry-run",
                    "worker": {"worker_concurrency": 5},
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(config_path)
        args = build_parser().parse_args(["worker", "once"])

        with patch.object(cli_module, "process_batch", return_value=[]) as batch:
            exit_code = handle_worker(args, self.store, self.artifacts, config)

        self.assertEqual(exit_code, 0)
        batch.assert_called_once_with(self.store, self.artifacts, config, 5)

    def test_reject_plan_records_feedback_and_returns_to_ready_for_plan(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self._move_to_plan_approval(issue.id)

        updated = self.store.reject_plan(issue.id, "Use a smaller scope.")

        self.assertEqual(updated.phase, "ready_for_plan")
        events = self.store.list_events(issue.id)
        self.assertEqual(events[-1]["event_type"], "plan.rejected")
        self.assertEqual(events[-1]["message"], "Use a smaller scope.")

    def test_plan_rejection_artifacts_are_listed(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self.artifacts.write_phase_artifact(issue.id, "plan", "run-1", "Draft plan content")

        self.artifacts.save_prior_plan(issue.id)
        self.artifacts.write_plan_feedback(issue.id, "Use a smaller scope.")

        prior_path = self.config.artifacts_dir / str(issue.id) / "plan_prior.md"
        feedback_path = self.config.artifacts_dir / str(issue.id) / "plan_feedback.md"
        self.assertIn("Draft plan content", prior_path.read_text(encoding="utf-8"))
        self.assertIn("Use a smaller scope.", feedback_path.read_text(encoding="utf-8"))
        relative_paths = {artifact.relative_path for artifact in self.artifacts.list_issue_artifacts(issue.id)}
        self.assertIn("plan_prior.md", relative_paths)
        self.assertIn("plan_feedback.md", relative_paths)

    def test_merge_conflict_resolution_artifact_is_listed_and_readable(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self.artifacts.write_phase_artifact(
            issue.id,
            "merge_conflict_resolution",
            "run-1",
            "Resolved conflicts in README.md",
        )

        artifacts = {artifact.relative_path: artifact for artifact in self.artifacts.list_issue_artifacts(issue.id)}
        conflict_artifact = artifacts["merge_conflict_resolution.md"]
        self.assertEqual(conflict_artifact.label, "merge conflict resolution artifact")
        self.assertEqual(conflict_artifact.kind, "phase")

        metadata = {
            artifact.relative_path: artifact for artifact in self.artifacts.list_issue_artifact_metadata(issue.id)
        }
        self.assertEqual(metadata["merge_conflict_resolution.md"].label, "merge conflict resolution artifact")
        self.assertIn(
            "Resolved conflicts in README.md",
            self.artifacts.read_issue_artifact(issue.id, "merge_conflict_resolution.md"),
        )

    def test_unblock_context_artifact_write_list_read_clear_lifecycle(self) -> None:
        issue = self.store.create_issue("title", "desc")

        path = self.artifacts.write_unblock_context(
            issue.id,
            "needs_research",
            "Resume research with the cached path.",
        )

        self.assertEqual(path, self.artifacts.unblock_context_path(issue.id))
        content = path.read_text(encoding="utf-8")
        self.assertIn("Resume phase: `needs_research`", content)
        self.assertIn("Resume research with the cached path.", content)
        artifacts = {artifact.relative_path: artifact for artifact in self.artifacts.list_issue_artifacts(issue.id)}
        unblock_artifact = artifacts["unblock_context.md"]
        self.assertEqual(unblock_artifact.label, "unblock guidance")
        self.assertEqual(unblock_artifact.kind, "unblock_context")
        self.assertIn(
            "Resume research with the cached path.",
            self.artifacts.read_issue_artifact(issue.id, "unblock_context.md"),
        )

        self.artifacts.clear_unblock_context(issue.id)

        self.assertFalse(path.exists())

    def test_rejected_plan_reruns_and_clears_rejection_context(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        orchestrator = Orchestrator(self.store, self.artifacts, self.config)
        self.assertIsNotNone(orchestrator.process_next())
        self.assertIsNotNone(orchestrator.process_next())
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")

        self.artifacts.save_prior_plan(issue.id)
        self.artifacts.write_plan_feedback(issue.id, "Use a smaller scope.")
        self.store.reject_plan(issue.id, "Use a smaller scope.")

        self.assertIsNotNone(orchestrator.process_next())
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")
        self.assertFalse((self.config.artifacts_dir / str(issue.id) / "plan_feedback.md").exists())
        self.assertFalse((self.config.artifacts_dir / str(issue.id) / "plan_prior.md").exists())

    def test_cli_reject_plan_writes_feedback_artifacts(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self._move_to_plan_approval(issue.id)
        self.artifacts.write_phase_artifact(issue.id, "plan", "run-1", "Draft plan content")
        args = build_parser().parse_args(
            ["issue", "reject-plan", str(issue.id), "--feedback", "Use a smaller scope."]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")
        self.assertIn("plan rejected", output.getvalue())
        self.assertIn(
            "Use a smaller scope.",
            (self.config.artifacts_dir / str(issue.id) / "plan_feedback.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Draft plan content",
            (self.config.artifacts_dir / str(issue.id) / "plan_prior.md").read_text(encoding="utf-8"),
        )

    def test_cli_issue_advance_out_of_blocked_with_message_creates_unblock_context(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.transition_issue(issue.id, "blocked", message="needs manager guidance")
        args = build_parser().parse_args(
            ["issue", "advance", str(issue.id), "--to", "needs_research", "--message", "Try the cached path first"]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")
        self.assertIn("advanced to needs_research", output.getvalue())
        content = self.artifacts.unblock_context_path(issue.id).read_text(encoding="utf-8")
        self.assertIn("Resume phase: `needs_research`", content)
        self.assertIn("Try the cached path first", content)

    def test_cli_issue_advance_out_of_blocked_without_message_clears_unblock_context(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.transition_issue(issue.id, "blocked", message="needs manager guidance")
        self.artifacts.write_unblock_context(issue.id, "needs_research", "stale guidance")
        args = build_parser().parse_args(["issue", "advance", str(issue.id), "--to", "needs_research"])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")
        self.assertFalse(self.artifacts.unblock_context_path(issue.id).exists())

    def test_cli_non_blocked_transition_message_does_not_create_unblock_context(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        args = build_parser().parse_args(
            ["issue", "advance", str(issue.id), "--to", "researching", "--message", "Audit-only note"]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "researching")
        self.assertFalse(self.artifacts.unblock_context_path(issue.id).exists())
        self.assertEqual(self.store.list_events(issue.id)[-1]["message"], "Audit-only note")

    def test_cli_approve_merge_records_request_and_advances(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self._move_to_merge_approval(issue.id)
        args = build_parser().parse_args(
            ["issue", "approve-merge", str(issue.id), "--branch", "main", "--message", "merge approved"]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts)

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_merge")
        request = self.artifacts.read_merge_request(issue.id)
        self.assertIsNotNone(request)
        self.assertEqual(request["target_branch"], "main")
        self.assertEqual(request["message"], "merge approved")
        self.assertEqual(request["mode"], "auto")
        self.assertIsNone(request["remote_name"])
        self.assertIn("merge approved targeting main", output.getvalue())

    def test_cli_approve_merge_defaults_to_configured_merge_mode(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self._move_to_merge_approval(issue.id)
        config = replace(self.config, merge_mode="local")
        args = build_parser().parse_args(
            ["issue", "approve-merge", str(issue.id), "--branch", "main", "--message", "merge approved"]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts, config)

        self.assertEqual(exit_code, 0)
        request = self.artifacts.read_merge_request(issue.id)
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["mode"], "local")
        self.assertIn("using local mode", output.getvalue())

    def test_cli_approve_merge_defaults_to_configured_pr_remote(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self._move_to_merge_approval(issue.id)
        config = replace(self.config, pr_remote="upstream")
        args = build_parser().parse_args(
            ["issue", "approve-merge", str(issue.id), "--branch", "main", "--message", "merge approved"]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts, config)

        self.assertEqual(exit_code, 0)
        request = self.artifacts.read_merge_request(issue.id)
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["mode"], "auto")
        self.assertEqual(request["remote_name"], "upstream")
        self.assertIn("using auto mode via remote upstream", output.getvalue())

    def test_cli_approve_merge_records_pull_request_mode_and_remote(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self._move_to_merge_approval(issue.id)
        args = build_parser().parse_args(
            [
                "issue",
                "approve-merge",
                str(issue.id),
                "--mode",
                "pull-request",
                "--remote",
                "upstream",
                "--message",
                "open a PR",
            ]
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts)

        self.assertEqual(exit_code, 0)
        request = self.artifacts.read_merge_request(issue.id)
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["mode"], "pull_request")
        self.assertEqual(request["remote_name"], "upstream")
        self.assertIn("using pull request mode via remote upstream", output.getvalue())

    def test_cli_approve_merge_rejects_wrong_phase(self) -> None:
        issue = self.store.create_issue("title", "desc")
        args = build_parser().parse_args(["issue", "approve-merge", str(issue.id)])

        with self.assertRaisesRegex(ValueError, "not 'awaiting_merge_approval'"):
            handle_issue(args, self.store, self.artifacts)

    def test_reset_to_draft_clears_state_and_preserves_core_metadata(self) -> None:
        issue = self.store.create_issue(
            "title",
            "desc",
            repo_path="/tmp/repo",
            priority=1,
            tags="a,b",
            source="obsidian",
            external_id="Inbox.md:8",
            ready=True,
        )
        self._move_to_plan_approval(issue.id)
        self.store.create_run("run-old", issue.id, "research", "dry-run")
        self.store.add_event(issue.id, "custom.event", "old event")
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE issues
                SET lock_owner = 'expired-worker',
                    lock_expires_at = '2000-01-01T00:00:00+00:00',
                    current_run_id = 'run-old',
                    last_scheduled_at = '2026-01-01T00:00:00+00:00'
                WHERE id = ?
                """,
                (issue.id,),
            )
        self.artifacts.write_issue_snapshot(self.store.get_issue(issue.id))
        self.artifacts.write_phase_artifact(issue.id, "research", "run-old", "old research")
        self.artifacts.run_log_path(issue.id, "research", "run-old").write_text("old log", encoding="utf-8")
        self.artifacts.append_history(issue.id, {"old": True})
        workspace = self.config.worktrees_dir / f"issue-{issue.id}-stale"
        workspace.mkdir(parents=True)
        self.artifacts.write_workspace_metadata(
            issue.id,
            {"workspace_repo_path": str(workspace), "worktree_root": str(workspace)},
        )
        self.artifacts.write_merge_request(issue.id, target_branch="main", message="old merge")
        self.artifacts.write_plan_feedback(issue.id, "old feedback")
        self._insert_pending_human_input(issue.id, "reset-human")
        self.artifacts.write_human_input_summary(issue.id, self.store.list_human_input_requests(issue.id))

        result = reset_issue_to_draft(self.config, self.store, self.artifacts, issue.id, "restart workflow")

        updated = self.store.get_issue(issue.id)
        self.assertEqual(result.prior_phase, "awaiting_plan_approval")
        self.assertEqual(updated.phase, "draft")
        self.assertEqual(updated.status, "open")
        self.assertEqual(updated.title, "title")
        self.assertEqual(updated.description, "desc")
        self.assertEqual(updated.source, "obsidian")
        self.assertEqual(updated.external_id, "Inbox.md:8")
        self.assertEqual(updated.repo_path, "/tmp/repo")
        self.assertEqual(updated.priority, 1)
        self.assertEqual(updated.tags, "a,b")
        self.assertEqual(updated.created_at, issue.created_at)
        self.assertIsNone(updated.lock_owner)
        self.assertIsNone(updated.lock_expires_at)
        self.assertIsNone(updated.current_run_id)
        with self.store.connect() as conn:
            row = conn.execute("SELECT last_scheduled_at FROM issues WHERE id = ?", (issue.id,)).fetchone()
        self.assertIsNone(row["last_scheduled_at"])
        self.assertEqual(self.store.list_runs(issue.id), [])
        self.assertEqual(self.store.list_human_input_requests(issue.id), [])
        events = self.store.list_events(issue.id)
        self.assertEqual([event["event_type"] for event in events], ["issue.reset_to_draft"])
        self.assertIn("restart workflow", events[0]["message"])
        self.assertIn("awaiting_plan_approval", events[0]["message"])
        issue_dir = self.config.artifacts_dir / str(issue.id)
        self.assertEqual({path.name for path in issue_dir.iterdir()}, {"issue.json", "logs"})
        self.assertEqual(list((issue_dir / "logs").iterdir()), [])
        snapshot = json.loads((issue_dir / "issue.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["phase"], "draft")
        self.assertGreaterEqual(result.deleted_runs, 1)
        self.assertGreaterEqual(result.deleted_events, 1)
        self.assertGreaterEqual(result.deleted_artifacts, 1)

    def test_reset_rejects_active_lock(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, "run-locked"))

        with self.assertRaisesRegex(ValueError, "active lock"):
            reset_issue_to_draft(self.config, self.store, self.artifacts, issue.id)

    def test_delete_issue_removes_state_artifacts_and_workspace(self) -> None:
        issue = self.store.create_issue(
            "delete me",
            "desc",
            repo_path="/tmp/repo",
            priority=1,
            tags="delete",
            source="obsidian",
            external_id="Inbox.md:9",
            ready=True,
        )
        self._move_to_plan_approval(issue.id)
        self.store.create_run("run-old", issue.id, "research", "dry-run")
        self.store.add_event(issue.id, "custom.event", "old event")
        self.artifacts.write_issue_snapshot(self.store.get_issue(issue.id))
        self.artifacts.write_phase_artifact(issue.id, "research", "run-old", "old research")
        self.artifacts.run_log_path(issue.id, "research", "run-old").write_text("old log", encoding="utf-8")
        self.artifacts.append_history(issue.id, {"old": True})
        workspace = self.config.worktrees_dir / f"issue-{issue.id}-stale"
        workspace.mkdir(parents=True)
        self.artifacts.write_workspace_metadata(
            issue.id,
            {"workspace_repo_path": str(workspace), "worktree_root": str(workspace)},
        )
        self.artifacts.write_merge_request(issue.id, target_branch="main", message="old merge")
        self.artifacts.write_plan_feedback(issue.id, "old feedback")
        self._insert_pending_human_input(issue.id, "delete-human")
        self.artifacts.write_human_input_summary(issue.id, self.store.list_human_input_requests(issue.id))
        issue_dir = self.config.artifacts_dir / str(issue.id)

        result = delete_issue(self.config, self.store, self.artifacts, issue.id, "remove permanently")

        self.assertEqual(result.issue_id, issue.id)
        self.assertEqual(result.prior_phase, "awaiting_plan_approval")
        self.assertGreaterEqual(result.deleted_runs, 1)
        self.assertGreaterEqual(result.deleted_events, 1)
        self.assertGreaterEqual(result.deleted_artifacts, 1)
        self.assertIn(str(workspace), result.removed_workspace_paths)
        with self.assertRaisesRegex(KeyError, "Issue not found"):
            self.store.get_issue(issue.id)
        self.assertEqual(self.store.list_runs(issue.id), [])
        self.assertEqual(self.store.list_events(issue.id), [])
        self.assertFalse(issue_dir.exists())
        self.assertFalse(workspace.exists())

        replacement = self.store.create_issue("replacement", "desc")
        self.artifacts.write_issue_snapshot(replacement)
        replacement_dir = self.config.artifacts_dir / str(replacement.id)
        self.assertEqual({path.name for path in replacement_dir.iterdir()}, {"issue.json", "logs"})

    def test_delete_rejects_active_lock(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, "run-locked"))

        with self.assertRaisesRegex(ValueError, "active lock"):
            delete_issue(self.config, self.store, self.artifacts, issue.id)

    def test_delete_issue_without_artifacts_reports_zero_artifacts(self) -> None:
        issue = self.store.create_issue("title", "desc")

        result = delete_issue(self.config, self.store, self.artifacts, issue.id)

        self.assertEqual(result.deleted_artifacts, 0)
        self.assertFalse((self.config.artifacts_dir / str(issue.id)).exists())

    def test_delete_releases_reservation_after_cleanup_failure(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.artifacts.write_issue_snapshot(issue)

        with patch.object(self.artifacts, "delete_issue_artifacts", side_effect=RuntimeError("cleanup failed")):
            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                delete_issue(self.config, self.store, self.artifacts, issue.id)

        unlocked = self.store.get_issue(issue.id)
        self.assertIsNone(unlocked.lock_owner)
        self.assertIsNone(unlocked.lock_expires_at)
        self.assertIsNone(unlocked.current_run_id)

        delete_issue(self.config, self.store, self.artifacts, issue.id)
        with self.assertRaisesRegex(KeyError, "Issue not found"):
            self.store.get_issue(issue.id)

    def test_delete_releases_reservation_after_db_finalization_failure(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        issue_dir = self.config.artifacts_dir / str(issue.id)
        self.artifacts.write_issue_snapshot(issue)

        with patch.object(self.store, "complete_delete_issue", side_effect=RuntimeError("db failed")):
            with self.assertRaisesRegex(RuntimeError, "db failed"):
                delete_issue(self.config, self.store, self.artifacts, issue.id)

        self.assertFalse(issue_dir.exists())
        unlocked = self.store.get_issue(issue.id)
        self.assertIsNone(unlocked.lock_owner)
        self.assertIsNone(unlocked.lock_expires_at)
        self.assertIsNone(unlocked.current_run_id)

        delete_issue(self.config, self.store, self.artifacts, issue.id)
        with self.assertRaisesRegex(KeyError, "Issue not found"):
            self.store.get_issue(issue.id)

    def test_reset_fences_stale_worker_updates_after_expired_lock(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        run_id = "run-stale"
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, run_id))
        self.store.transition_issue(issue.id, "researching", run_id)
        self.store.create_run(run_id, issue.id, "research", "dry-run")
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE issues SET lock_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (issue.id,),
            )

        reset_issue_to_draft(self.config, self.store, self.artifacts, issue.id)

        with self.assertRaisesRegex(RuntimeError, "no longer current"):
            self.store.complete_run(run_id, issue.id, "success", "stale success", None)
        with self.assertRaisesRegex(RuntimeError, "no longer current"):
            self.store.transition_issue(issue.id, "needs_research", run_id)
        self.store.release_lock(issue.id, "worker", run_id)
        events = self.store.list_events(issue.id)
        self.assertEqual([event["event_type"] for event in events], ["issue.reset_to_draft"])

    def test_orchestrator_does_not_recreate_artifacts_when_stale_worker_finishes_after_reset(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        started = threading.Event()
        release = threading.Event()

        class BlockingRunner(AgentRunner):
            name = "blocking"

            def run(self, phase: str, issue: Issue, context: Dict[str, str]) -> AgentResult:
                started.set()
                if not release.wait(5):
                    raise AssertionError("Timed out waiting to finish stale worker run")
                return AgentResult(
                    status="success",
                    summary="stale runner completed",
                    artifact_markdown="stale artifact\n\nRecommendation: `ready_for_plan`",
                    suggested_next_phase="ready_for_plan",
                    raw_stdout="stale stdout",
                )

        errors: list[BaseException] = []

        def process_issue() -> None:
            try:
                Orchestrator(self.store, self.artifacts, self.config, runner=BlockingRunner()).process_issue(issue.id)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=process_issue)
        thread.start()
        self.assertTrue(started.wait(5))
        running = self.store.get_issue(issue.id)
        self.assertEqual(running.phase, "researching")
        self.assertIsNotNone(running.current_run_id)
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE issues SET lock_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (issue.id,),
            )

        reset_issue_to_draft(self.config, self.store, self.artifacts, issue.id)
        release.set()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("no longer current", str(errors[0]))
        issue_dir = self.config.artifacts_dir / str(issue.id)
        self.assertEqual({path.name for path in issue_dir.iterdir()}, {"issue.json", "logs"})
        self.assertEqual(list((issue_dir / "logs").iterdir()), [])
        self.assertEqual([event["event_type"] for event in self.store.list_events(issue.id)], ["issue.reset_to_draft"])

    def test_cli_reset_to_draft_prints_summary(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.create_run("run-old", issue.id, "research", "dry-run")
        self.artifacts.write_phase_artifact(issue.id, "research", "run-old", "old research")
        args = build_parser().parse_args(["issue", "reset-to-draft", str(issue.id), "--message", "restart"])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts, self.config)

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "draft")
        text = output.getvalue()
        self.assertIn("reset to draft", text)
        self.assertIn("Deleted runs: 1", text)
        self.assertIn("Cleared artifact/log entries", text)

    def test_cli_delete_requires_confirmation_and_prints_summary(self) -> None:
        issue = self.store.create_issue("title", "desc", ready=True)
        self.store.create_run("run-old", issue.id, "research", "dry-run")
        self.artifacts.write_phase_artifact(issue.id, "research", "run-old", "old research")

        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            build_parser().parse_args(["issue", "delete", str(issue.id)])
        bad_args = build_parser().parse_args(
            ["issue", "delete", str(issue.id), "--confirm", f"DELETE {issue.id + 1}"]
        )
        with self.assertRaisesRegex(ValueError, "Confirmation must be exactly"):
            handle_issue(bad_args, self.store, self.artifacts, self.config)
        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")
        self.assertTrue((self.config.artifacts_dir / str(issue.id)).is_dir())

        args = build_parser().parse_args(
            ["issue", "delete", str(issue.id), "--confirm", f"DELETE {issue.id}", "--message", "cleanup"]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = handle_issue(args, self.store, self.artifacts, self.config)

        self.assertEqual(exit_code, 0)
        with self.assertRaisesRegex(KeyError, "Issue not found"):
            self.store.get_issue(issue.id)
        self.assertFalse((self.config.artifacts_dir / str(issue.id)).exists())
        text = output.getvalue()
        self.assertIn("deleted entirely", text)
        self.assertIn("Deleted runs: 1", text)
        self.assertIn("Removed issue row and artifact directory", text)

    def test_artifact_reset_unlinks_symlinked_directories_without_following(self) -> None:
        issue = self.store.create_issue("title", "desc")
        issue_dir = self.artifacts.issue_dir(issue.id)
        external = self.home / "external"
        external.mkdir()
        (external / "kept.txt").write_text("kept", encoding="utf-8")
        symlink = issue_dir / "linked-dir"
        symlink.symlink_to(external, target_is_directory=True)

        self.artifacts.reset_issue_artifacts(issue.id)

        self.assertFalse(symlink.exists())
        self.assertTrue((external / "kept.txt").is_file())

    def test_artifact_delete_handles_missing_and_rejects_symlinked_issue_dir(self) -> None:
        self.assertEqual(self.artifacts.delete_issue_artifacts(999), 0)
        external = self.home / "external-artifacts"
        external.mkdir()
        (external / "kept.txt").write_text("kept", encoding="utf-8")
        symlink = self.config.artifacts_dir / "123"
        symlink.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            self.artifacts.delete_issue_artifacts(123)

        self.assertTrue(symlink.is_symlink())
        self.assertTrue((external / "kept.txt").is_file())

    def test_cli_run_accepts_merge_phases(self) -> None:
        args = build_parser().parse_args(["run", "--issue", "1", "--phase", "merge"])
        self.assertEqual(args.phase, "merge")
        args = build_parser().parse_args(["run", "--issue", "1", "--phase", "merge_conflict_resolution"])
        self.assertEqual(args.phase, "merge_conflict_resolution")

    def test_issue_show_prints_workspace_metadata(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self.artifacts.write_workspace_metadata(
            issue.id,
            {
                "workspace_repo_path": "/tmp/worktrees/issue-1",
                "worktree_root": "/tmp/worktrees/issue-1",
            },
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            print_issue(issue, self.store, self.artifacts)

        text = output.getvalue()
        self.assertIn("Workspace: /tmp/worktrees/issue-1", text)
        self.assertIn("Worktree root: /tmp/worktrees/issue-1", text)

    def test_issue_show_prints_available_artifacts(self) -> None:
        issue = self.store.create_issue("title", "desc")
        self.artifacts.write_phase_artifact(
            issue.id,
            "merge_conflict_resolution",
            "run-1",
            "Resolved conflicts in README.md",
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            print_issue(issue, self.store, self.artifacts)

        text = output.getvalue()
        self.assertIn("Artifacts:", text)
        self.assertIn("- merge conflict resolution artifact: merge_conflict_resolution.md", text)

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_copilot_research_and_plan_use_source_without_workspace(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot()
        config = self._copilot_config(script)
        issue = self.store.create_issue("title", "desc", repo_path=str(repo), ready=True)
        orchestrator = Orchestrator(self.store, self.artifacts, config)

        research_result = orchestrator.process_next()
        plan_result = orchestrator.process_next()

        self.assertIsNotNone(research_result)
        self.assertIsNotNone(plan_result)
        records = self._records(record_dir)
        self.assertEqual(len(records), 2)
        self.assertEqual({Path(record["cwd"]) for record in records}, {repo.resolve()})
        for record in records:
            add_dirs = self._add_dirs(record["argv"])
            self.assertIn(str(repo.resolve()), add_dirs)
            self.assertIn(str(self.config.artifacts_dir / str(issue.id)), add_dirs)
            self.assertNotIn("--yolo", record["argv"])
            self.assertNotIn("--allow-all-tools", record["argv"])
        self.assertIsNone(self.artifacts.read_workspace_metadata(issue.id))
        history_lines = (self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertTrue(history_lines)
        self.assertNotIn("workspace_repo_path", json.loads(history_lines[0]))

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_plan_source_mutation_requeues_and_preserves_rejection_context(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot_mutating_first_plan()
        config = self._copilot_config(script)
        issue = self.store.create_issue("title", "desc", repo_path=str(repo), ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.artifacts.write_phase_artifact(issue.id, "plan", "old-run", "Old rejected plan")
        self.artifacts.save_prior_plan(issue.id)
        self.artifacts.write_plan_feedback(issue.id, "Use a smaller scope.")
        issue_dir = self.config.artifacts_dir / str(issue.id)
        orchestrator = Orchestrator(self.store, self.artifacts, config)

        first_result = orchestrator.process_next()

        self.assertIsNotNone(first_result)
        self.assertEqual(first_result.status, "requeued")
        self.assertEqual(first_result.next_phase, "ready_for_plan")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")
        discarded_plan = (issue_dir / "plan.md").read_text(encoding="utf-8")
        self.assertIn("Plan discarded and requeued", discarded_plan)
        self.assertIn("Recommendation: `ready_for_plan`", discarded_plan)
        self.assertTrue((issue_dir / "plan_prior.md").is_file())
        self.assertTrue((issue_dir / "plan_feedback.md").is_file())

        second_result = orchestrator.process_next()

        self.assertIsNotNone(second_result)
        self.assertEqual(second_result.status, "success")
        self.assertEqual(second_result.next_phase, "awaiting_plan_approval")
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")
        fresh_plan = (issue_dir / "plan.md").read_text(encoding="utf-8")
        self.assertIn("Recommendation: `ready_for_implementation`", fresh_plan)
        self.assertNotIn("Plan discarded and requeued", fresh_plan)
        self.assertFalse((issue_dir / "plan_prior.md").exists())
        self.assertFalse((issue_dir / "plan_feedback.md").exists())
        self.assertEqual(len(self._records(record_dir)), 2)

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_copilot_implementation_creates_issue_workspace(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot()
        config = self._copilot_config(script)
        issue = self.store.create_issue("title", "desc", repo_path=str(repo), ready=True)
        orchestrator = Orchestrator(self.store, self.artifacts, config)
        self.assertIsNotNone(orchestrator.process_next())
        self.assertIsNotNone(orchestrator.process_next())
        self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")

        implementation_result = orchestrator.process_next()

        self.assertIsNotNone(implementation_result)
        records = self._records(record_dir)
        workspace_records = [record for record in records if Path(record["cwd"]) != repo.resolve()]
        self.assertEqual(len(workspace_records), 1)
        workspace_record = workspace_records[0]
        cwd = Path(workspace_record["cwd"])
        self.assertNotEqual(cwd, repo.resolve())
        self.assertTrue(str(cwd).startswith(str(config.worktrees_dir)))
        add_dirs = self._add_dirs(workspace_record["argv"])
        self.assertIn(str(cwd), add_dirs)
        self.assertIn(str(self.config.artifacts_dir / str(issue.id)), add_dirs)
        self.assertNotIn("--yolo", workspace_record["argv"])
        self.assertNotIn("--allow-all-tools", workspace_record["argv"])
        metadata = self.artifacts.read_workspace_metadata(issue.id)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["workspace_repo_path"], str(cwd))
        history_lines = (self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertIn("workspace_repo_path", json.loads(history_lines[-1]))
        self.assertIn("workspace_commit", json.loads(history_lines[-1]))

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_copilot_review_rework_loop_preserves_implementation_commits(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot_review_loop()
        config = self._copilot_config(script)
        issue = self.store.create_issue("loop title", "desc", repo_path=str(repo), ready=True)
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")
        orchestrator = Orchestrator(self.store, self.artifacts, config)

        phase_results = [orchestrator.process_next() for _ in range(6)]
        self.artifacts.write_merge_request(issue.id, target_branch=None, message="approved loop")
        self.store.transition_issue(issue.id, "ready_for_merge", message="approved loop")
        merge_result = orchestrator.process_next()

        self.assertEqual(
            [result.phase for result in phase_results if result is not None],
            ["implementation", "validation", "review", "implementation", "validation", "review"],
        )
        self.assertIsNotNone(merge_result)
        self.assertEqual(merge_result.next_phase, "done")
        self.assertEqual(self.store.get_issue(issue.id).phase, "done")
        self.assertEqual((repo / "implementation-1.txt").read_text(encoding="utf-8"), "implementation 1\n")
        self.assertEqual((repo / "implementation-2.txt").read_text(encoding="utf-8"), "implementation 2\n")
        subjects = self._git(repo, "log", "--format=%s", "--max-count=10").splitlines()
        implementation_subjects = [
            f"Issue {issue.id}: Created workspace marker file 1.",
            f"Issue {issue.id}: Created workspace marker file 2.",
        ]
        for subject in implementation_subjects:
            self.assertIn(subject, subjects)
        self.assertIn(f"Merge issue {issue.id}: loop title", subjects[0])
        self.assertFalse(
            any(subject.startswith(f"Issue {issue.id} implementation ") for subject in subjects)
        )
        self.assertFalse(any(subject.startswith("Implement issue") for subject in subjects))
        merge_message = self._git(repo, "log", "-1", "--format=%B")
        self.assertIn("Merge approval: approved loop", merge_message)
        for subject in implementation_subjects:
            self.assertIn(subject, merge_message)
        history_events = [
            json.loads(line)
            for line in (self.config.artifacts_dir / str(issue.id) / "history.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        implementation_events = [event for event in history_events if event["phase"] == "implementation"]
        self.assertEqual(len(implementation_events), 2)
        self.assertTrue(all(event.get("workspace_commit") for event in implementation_events))
        self.assertEqual(len(self._records(record_dir)), 6)

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_review_rejection_syncs_source_before_reimplementation(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot_review_loop()
        config = self._copilot_config(script)
        issue = self.store.create_issue("sync title", "desc", repo_path=str(repo), ready=True)
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")
        orchestrator = Orchestrator(self.store, self.artifacts, config)

        implementation_result = orchestrator.process_next()
        validation_result = orchestrator.process_next()
        self._commit_file(repo, "source-advance.txt", "source\n")
        source_head = self._git(repo, "rev-parse", "HEAD")
        review_result = orchestrator.process_next()

        self.assertIsNotNone(implementation_result)
        self.assertIsNotNone(validation_result)
        self.assertIsNotNone(review_result)
        self.assertEqual(review_result.phase, "review")
        self.assertEqual(review_result.next_phase, "ready_for_implementation")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_implementation")
        metadata = self.artifacts.read_workspace_metadata(issue.id)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["last_source_sync_status"], "synced")
        self.assertEqual(metadata["last_source_sync_head"], source_head)
        workspace_root = Path(str(metadata["worktree_root"]))
        self.assertTrue(self._git_check(workspace_root, "merge-base", "--is-ancestor", source_head, "HEAD"))
        self.assertFalse((self.config.artifacts_dir / str(issue.id) / "merge.md").exists())
        history_events = [
            json.loads(line)
            for line in (self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        review_events = [event for event in history_events if event["phase"] == "review"]
        self.assertEqual(review_events[-1]["workspace_source_sync"]["status"], "synced")

        second_implementation = orchestrator.process_next()

        self.assertIsNotNone(second_implementation)
        self.assertEqual(second_implementation.phase, "implementation")
        self.assertEqual(len(self._records(record_dir)), 4)

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_review_rejection_source_sync_conflict_routes_to_resolution(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot_review_loop()
        config = self._copilot_config(script)
        issue = self.store.create_issue("sync conflict", "desc", repo_path=str(repo))
        info = WorkspaceManager(config.worktrees_dir, self.artifacts, config.locks_dir).prepare(issue)
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "README.md")
        self._git(info.worktree_root, "commit", "-m", "workspace change")
        (repo / "README.md").write_text("source change\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "source change")
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation")
        self.store.transition_issue(issue.id, "implementing")
        self.store.transition_issue(issue.id, "ready_for_validation")
        self.store.transition_issue(issue.id, "validating")
        self.store.transition_issue(issue.id, "ready_for_review")
        orchestrator = Orchestrator(self.store, self.artifacts, config)

        review_result = orchestrator.process_next()

        self.assertIsNotNone(review_result)
        self.assertEqual(review_result.phase, "review")
        self.assertEqual(review_result.next_phase, "ready_for_merge_conflict_resolution")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_merge_conflict_resolution")
        merge_artifact = (self.config.artifacts_dir / str(issue.id) / "merge.md").read_text(encoding="utf-8")
        self.assertIn("Workspace Source Sync", merge_artifact)
        self.assertIn("README.md", merge_artifact)
        self.assertIn("Recommendation: `ready_for_merge_conflict_resolution`", merge_artifact)
        review_artifact = (self.config.artifacts_dir / str(issue.id) / "review.md").read_text(encoding="utf-8")
        self.assertIn("Recommendation: `ready_for_implementation`", review_artifact)
        self.assertIn("<<<<<<<", (info.worktree_root / "README.md").read_text(encoding="utf-8"))
        self.assertNotIn("<<<<<<<", (repo / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(self._git(repo, "status", "--porcelain"), "")
        history_events = [
            json.loads(line)
            for line in (self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        review_events = [event for event in history_events if event["phase"] == "review"]
        self.assertEqual(review_events[-1]["workspace_source_sync"]["status"], "conflicts")
        self.assertEqual(self._records(record_dir)[0]["agent"], "agent-team-review")

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_source_sync_conflict_resolution_to_implementation_creates_snapshot_commit(self) -> None:
        repo = self._create_repo("source")
        review_record_dir, review_script = self._fake_copilot_review_loop()
        review_config = self._copilot_config(review_script)
        resolution_record_dir, resolution_script = self._fake_copilot_conflict_resolution(
            recommendation="ready_for_implementation"
        )
        resolution_config = self._copilot_config(resolution_script)
        issue = self.store.create_issue("sync conflict", "desc", repo_path=str(repo))
        info = WorkspaceManager(review_config.worktrees_dir, self.artifacts, review_config.locks_dir).prepare(issue)
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "README.md")
        self._git(info.worktree_root, "commit", "-m", "workspace change")
        (repo / "README.md").write_text("source change\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "source change")
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation")
        self.store.transition_issue(issue.id, "implementing")
        self.store.transition_issue(issue.id, "ready_for_validation")
        self.store.transition_issue(issue.id, "validating")
        self.store.transition_issue(issue.id, "ready_for_review")
        review_orchestrator = Orchestrator(self.store, self.artifacts, review_config)
        resolution_orchestrator = Orchestrator(self.store, self.artifacts, resolution_config)

        review_result = review_orchestrator.process_next()
        resolution_result = resolution_orchestrator.process_next()

        self.assertIsNotNone(review_result)
        self.assertEqual(review_result.next_phase, "ready_for_merge_conflict_resolution")
        self.assertIsNotNone(resolution_result)
        self.assertEqual(resolution_result.phase, "merge_conflict_resolution")
        self.assertEqual(resolution_result.next_phase, "ready_for_implementation")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_implementation")
        self.assertEqual((info.worktree_root / "README.md").read_text(encoding="utf-8"), "resolved by copilot\n")
        self.assertEqual(self._git(info.worktree_root, "status", "--porcelain"), "")
        self.assertFalse(self._git_check(info.worktree_root, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
        parents = self._git(info.worktree_root, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 3)
        history_events = [
            json.loads(line)
            for line in (self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        resolution_events = [event for event in history_events if event["phase"] == "merge_conflict_resolution"]
        self.assertEqual(len(resolution_events), 1)
        self.assertTrue(resolution_events[0].get("workspace_commit"))
        self.assertEqual(self._records(review_record_dir)[0]["agent"], "agent-team-review")
        self.assertEqual(self._records(resolution_record_dir)[0]["agent"], "agent-team-merge-conflict-resolution")

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_copilot_end_to_end_validation_runs_real_check(self) -> None:
        repo = self._create_repo("source")
        initial_hello = 'print("Hello, world!")\n'
        self._commit_file(repo, "hello.py", initial_hello)
        record_dir, script = self._fake_copilot_e2e_validation()
        config = self._copilot_config(script)
        issue = self.store.create_issue(
            "add named greeting",
            "Update hello.py so it can greet a provided name and prove it with a real validation check.",
            repo_path=str(repo),
            ready=True,
        )
        orchestrator = Orchestrator(self.store, self.artifacts, config)

        research_result = orchestrator.process_next()
        plan_result = orchestrator.process_next()

        self.assertIsNotNone(research_result)
        self.assertIsNotNone(plan_result)
        self.assertEqual([research_result.phase, plan_result.phase], ["research", "plan"])
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")
        approve_plan_args = build_parser().parse_args(
            ["issue", "approve-plan", str(issue.id), "--message", "approved e2e plan"]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(handle_issue(approve_plan_args, self.store, self.artifacts), 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_implementation")

        phase_results = [orchestrator.process_next() for _ in range(3)]

        self.assertEqual(
            [result.phase for result in phase_results if result is not None],
            ["implementation", "validation", "review"],
        )
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_merge_approval")
        self.assertEqual((repo / "hello.py").read_text(encoding="utf-8"), initial_hello)
        self.assertFalse((repo / "test_hello.py").exists())
        before_merge = subprocess.run(
            [sys.executable, "hello.py"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(before_merge.stdout, "Hello, world!\n")
        self.assertEqual(self._git(repo, "status", "--porcelain"), "")

        records = self._records(record_dir)
        records_by_agent = {record["agent"]: record for record in records}
        self.assertEqual(
            set(records_by_agent),
            {
                "agent-team-research",
                "agent-team-plan",
                "agent-team-implementation",
                "agent-team-validation",
                "agent-team-review",
            },
        )
        implementation_cwd = Path(str(records_by_agent["agent-team-implementation"]["cwd"])).resolve()
        validation_cwd = Path(str(records_by_agent["agent-team-validation"]["cwd"])).resolve()
        self.assertTrue(str(implementation_cwd).startswith(str(config.worktrees_dir.resolve())))
        self.assertTrue(str(validation_cwd).startswith(str(config.worktrees_dir.resolve())))
        self.assertNotEqual(implementation_cwd, repo.resolve())
        self.assertEqual(implementation_cwd, validation_cwd)
        validation_record = records_by_agent["agent-team-validation"]
        validation_command = validation_record["validation_command"]
        self.assertEqual(validation_command["returncode"], 0)
        self.assertIn("Hello, Azure!", validation_command["stdout"] + validation_command["stderr"])
        self.assertIn("-m unittest discover", " ".join(validation_command["command"]))

        validation_artifact = (self.config.artifacts_dir / str(issue.id) / "validation.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Command:", validation_artifact)
        self.assertIn("-m unittest discover", validation_artifact)
        self.assertIn("Return code: 0", validation_artifact)
        self.assertIn("Hello, Azure!", validation_artifact)
        self.assertIn("Recommendation: `ready_for_review`", validation_artifact)
        workspace_metadata = self.artifacts.read_workspace_metadata(issue.id)
        self.assertIsNotNone(workspace_metadata)
        workspace_root = Path(workspace_metadata["worktree_root"])

        approve_merge_args = build_parser().parse_args(
            ["issue", "approve-merge", str(issue.id), "--message", "approved e2e merge"]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(handle_issue(approve_merge_args, self.store, self.artifacts), 0)
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_merge")
        merge_result = orchestrator.process_next()

        self.assertIsNotNone(merge_result)
        self.assertEqual(merge_result.phase, "merge")
        self.assertEqual(merge_result.next_phase, "done")
        self.assertEqual(self.store.get_issue(issue.id).phase, "done")
        after_merge = subprocess.run(
            [sys.executable, "hello.py", "Azure"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(after_merge.stdout, "Hello, Azure!\n")
        final_check = subprocess.run(
            [sys.executable, "-m", "unittest", "discover"],
            cwd=repo,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(final_check.returncode, 0, final_check.stdout + final_check.stderr)
        self.assertEqual(self._git(repo, "status", "--porcelain"), "")
        self.assertIsNone(self.artifacts.read_workspace_metadata(issue.id))
        self.assertTrue(self.artifacts.merged_workspace_metadata_path(issue.id).is_file())
        self.assertFalse(workspace_root.exists())

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_copilot_validation_blocks_without_existing_workspace(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot()
        config = self._copilot_config(script)
        issue = self.store.create_issue("title", "desc", repo_path=str(repo), ready=True)
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")
        self.store.transition_issue(issue.id, "implementing")
        self.store.transition_issue(issue.id, "ready_for_validation")

        result = Orchestrator(self.store, self.artifacts, config).process_next()

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.store.get_issue(issue.id).phase, "blocked")
        self.assertIn("Workspace metadata is missing", result.summary)
        self.assertEqual(self._records(record_dir), [])

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_concurrent_copilot_runs_get_distinct_workspaces_for_same_repo(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot()
        config = self._copilot_config(script)
        first = self.store.create_issue("first", "desc", repo_path=str(repo), ready=True)
        second = self.store.create_issue("second", "desc", repo_path=str(repo), ready=True)
        for issue in (first, second):
            self._move_to_plan_approval(issue.id)
            self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")

        results = process_batch(self.store, self.artifacts, config, concurrency=2)

        self.assertEqual(len(results), 6)
        records = self._records(record_dir)
        self.assertEqual(len(records), 6)
        cwds = {record["cwd"] for record in records}
        self.assertEqual(len(cwds), 2)
        self.assertNotIn(str(repo.resolve()), cwds)
        self.assertFalse((repo / "copilot-marker.txt").exists())

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_copilot_subdirectory_repo_runs_in_worktree_subdirectory(self) -> None:
        repo = self._create_repo("source")
        subdir = repo / "pkg"
        subdir.mkdir()
        (subdir / "module.txt").write_text("pkg", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-m", "add pkg")
        record_dir, script = self._fake_copilot()
        config = self._copilot_config(script)
        issue = self.store.create_issue("title", "desc", repo_path=str(subdir), ready=True)
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")

        Orchestrator(self.store, self.artifacts, config).process_next()

        record = self._records(record_dir)[0]
        metadata = self.artifacts.read_workspace_metadata(issue.id)
        self.assertIsNotNone(metadata)
        self.assertEqual(record["cwd"], metadata["workspace_repo_path"])
        self.assertTrue(record["cwd"].endswith("/pkg"))
        add_dirs = self._add_dirs(record["argv"])
        self.assertIn(record["cwd"], add_dirs)
        self.assertIn(str(self.config.artifacts_dir / str(issue.id)), add_dirs)

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_copilot_workspace_failure_blocks_implementation_without_invoking_runner(self) -> None:
        non_git = self.home / "non-git"
        non_git.mkdir()
        record_dir, script = self._fake_copilot()
        config = self._copilot_config(script)
        issue = self.store.create_issue("title", "desc", repo_path=str(non_git), ready=True)
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation", message="test approved plan")

        result = Orchestrator(self.store, self.artifacts, config).process_next()

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.store.get_issue(issue.id).phase, "blocked")
        self.assertEqual(self._records(record_dir), [])
        artifact = self.config.artifacts_dir / str(issue.id) / "implementation.md"
        self.assertIn("not inside a Git worktree", artifact.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_merge_phase_merges_workspace_and_closes_issue(self) -> None:
        repo = self._create_repo("source")
        issue = self.store.create_issue("merge title", "desc", repo_path=str(repo))
        info = WorkspaceManager(self.config.worktrees_dir, self.artifacts, self.config.locks_dir).prepare(issue)
        (info.worktree_root / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "feature.txt")
        self._git(info.worktree_root, "commit", "-m", "feature")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_merge_request(issue.id, target_branch=None, message="approved")
        self.store.transition_issue(issue.id, "ready_for_merge", message="approved")

        result = Orchestrator(self.store, self.artifacts, self.config).process_next()

        self.assertIsNotNone(result)
        self.assertEqual(result.phase, "merge")
        self.assertEqual(result.next_phase, "done")
        self.assertEqual(self.store.get_issue(issue.id).phase, "done")
        self.assertEqual((repo / "feature.txt").read_text(encoding="utf-8"), "feature\n")
        self.assertFalse(info.worktree_root.exists())
        self.assertTrue((self.config.artifacts_dir / str(issue.id) / "merge.md").is_file())
        self.assertIsNone(self.artifacts.read_workspace_metadata(issue.id))
        self.assertTrue(self.artifacts.merged_workspace_metadata_path(issue.id).is_file())
        runs = self.store.list_runs(issue.id)
        self.assertEqual(runs[-1]["runner"], "workspace-merge")

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_merge_phase_pull_request_mode_opens_pr_and_closes_issue(self) -> None:
        repo = self._create_repo("source")
        self._git(repo, "remote", "add", "origin", "https://github.com/owner/repo.git")
        target_branch = self._git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        source_head = self._git(repo, "rev-parse", "HEAD")
        issue = self.store.create_issue("merge title", "sensitive token=do-not-leak", repo_path=str(repo))
        info = WorkspaceManager(self.config.worktrees_dir, self.artifacts, self.config.locks_dir).prepare(issue)
        self._commit_file(info.worktree_root, "feature.txt", "feature\n")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_merge_request(
            issue.id,
            target_branch=None,
            message="approved with secret=do-not-leak",
            mode="pull_request",
            remote_name="origin",
        )
        self.store.transition_issue(issue.id, "ready_for_merge", message="approved")
        pr_result = PullRequestResult(
            provider="github",
            remote_name="origin",
            source_branch=f"agent-team/issue-{issue.id}",
            target_branch=target_branch,
            title=f"Issue {issue.id}: merge title",
            url="https://github.com/owner/repo/pull/7",
            id="7",
            number=7,
            status="OPEN",
            is_existing=False,
            raw={"number": 7},
        )

        with patch.object(WorkspaceManager, "_push_pull_request_branch") as push_branch:
            with patch("agent_team.workspaces.create_or_get_pull_request", return_value=pr_result) as create_pr:
                result = Orchestrator(self.store, self.artifacts, self.config).process_next()

        self.assertIsNotNone(result)
        self.assertEqual(result.phase, "merge")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.next_phase, "done")
        self.assertEqual(
            result.summary,
            f"Opened pull request for issue {issue.id} into {target_branch}: https://github.com/owner/repo/pull/7",
        )
        self.assertEqual(self.store.get_issue(issue.id).phase, "done")
        self.assertEqual(self._git(repo, "rev-parse", "HEAD"), source_head)
        self.assertFalse((repo / "feature.txt").exists())
        self.assertFalse(info.worktree_root.exists())
        push_branch.assert_called_once()
        self.assertEqual(push_branch.call_args.args[3], f"agent-team/issue-{issue.id}")
        request = create_pr.call_args.args[1]
        self.assertEqual(request.source_branch, f"agent-team/issue-{issue.id}")
        self.assertEqual(request.target_branch, target_branch)
        self.assertIsNotNone(request.body_path)
        pr_metadata = self.artifacts.read_pull_request_metadata(issue.id)
        self.assertIsNotNone(pr_metadata)
        assert pr_metadata is not None
        self.assertTrue(pr_metadata["cleanup_removed"])
        self.assertEqual(pr_metadata["url"], "https://github.com/owner/repo/pull/7")
        self.assertEqual(pr_metadata["provider"], "github")
        self.assertEqual(pr_metadata["remote_name"], "origin")
        self.assertEqual(pr_metadata["source_branch"], f"agent-team/issue-{issue.id}")
        self.assertIsNone(self.artifacts.read_workspace_metadata(issue.id))
        self.assertFalse(self.artifacts.merged_workspace_metadata_path(issue.id).exists())
        merge_artifact = (self.config.artifacts_dir / str(issue.id) / "merge.md").read_text(encoding="utf-8")
        self.assertIn("- Status: `pull_request`", merge_artifact)
        self.assertIn("Pull request URL: https://github.com/owner/repo/pull/7", merge_artifact)
        runs = self.store.list_runs(issue.id)
        self.assertEqual(runs[-1]["runner"], "workspace-merge")
        self.assertEqual(runs[-1]["status"], "success")

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_merge_phase_commits_uncommitted_workspace_changes(self) -> None:
        repo = self._create_repo("source")
        issue = self.store.create_issue("merge title", "desc", repo_path=str(repo))
        info = WorkspaceManager(self.config.worktrees_dir, self.artifacts, self.config.locks_dir).prepare(issue)
        (info.worktree_root / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_merge_request(issue.id, target_branch=None, message="approved")
        self.store.transition_issue(issue.id, "ready_for_merge", message="approved")

        result = Orchestrator(self.store, self.artifacts, self.config).process_next()

        self.assertIsNotNone(result)
        self.assertEqual(result.next_phase, "done")
        self.assertEqual(self.store.get_issue(issue.id).phase, "done")
        self.assertEqual((repo / "feature.txt").read_text(encoding="utf-8"), "feature\n")
        merged = json.loads(self.artifacts.merged_workspace_metadata_path(issue.id).read_text(encoding="utf-8"))
        self.assertIsNotNone(merged["worktree_commit"])
        self.assertIn("Worktree commit created", (self.config.artifacts_dir / str(issue.id) / "merge.md").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_concurrent_merge_batch_serializes_same_repo(self) -> None:
        repo = self._create_repo("source")
        first = self.store.create_issue("merge first", "desc", repo_path=str(repo))
        second = self.store.create_issue("merge second", "desc", repo_path=str(repo))
        manager = WorkspaceManager(self.config.worktrees_dir, self.artifacts, self.config.locks_dir)
        first_info = manager.prepare(first)
        second_info = manager.prepare(second)
        self._commit_file(first_info.worktree_root, "feature-one.txt", "one\n")
        self._commit_file(second_info.worktree_root, "feature-two.txt", "two\n")
        for issue in (first, second):
            self._move_to_merge_approval(issue.id)
            self.artifacts.write_merge_request(issue.id, target_branch=None, message="approved")
            self.store.transition_issue(issue.id, "ready_for_merge", message="approved")

        results = process_batch(self.store, self.artifacts, self.config, concurrency=2)

        self.assertEqual(len(results), 2)
        self.assertEqual({result.next_phase for result in results}, {"done"})
        self.assertEqual(self.store.get_issue(first.id).phase, "done")
        self.assertEqual(self.store.get_issue(second.id).phase, "done")
        self.assertEqual((repo / "feature-one.txt").read_text(encoding="utf-8"), "one\n")
        self.assertEqual((repo / "feature-two.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(self._git(repo, "status", "--porcelain"), "")
        self.assertFalse(first_info.worktree_root.exists())
        self.assertFalse(second_info.worktree_root.exists())
        self.assertIsNone(self.artifacts.read_workspace_metadata(first.id))
        self.assertIsNone(self.artifacts.read_workspace_metadata(second.id))

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_merge_conflict_routes_to_resolution_then_validation(self) -> None:
        repo = self._create_repo("source")
        issue = self.store.create_issue("conflict title", "desc", repo_path=str(repo))
        info = WorkspaceManager(self.config.worktrees_dir, self.artifacts, self.config.locks_dir).prepare(issue)
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "README.md")
        self._git(info.worktree_root, "commit", "-m", "workspace change")
        (repo / "README.md").write_text("source change\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "source change")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_merge_request(issue.id, target_branch=None, message="approved")
        self.store.transition_issue(issue.id, "ready_for_merge", message="approved")
        orchestrator = Orchestrator(self.store, self.artifacts, self.config)

        merge_result = orchestrator.process_next()

        self.assertIsNotNone(merge_result)
        self.assertEqual(merge_result.next_phase, "ready_for_merge_conflict_resolution")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_merge_conflict_resolution")
        self.assertIn("README.md", (self.config.artifacts_dir / str(issue.id) / "merge.md").read_text(encoding="utf-8"))
        self.assertIn("<<<<<<<", (info.worktree_root / "README.md").read_text(encoding="utf-8"))

        resolution_result = orchestrator.process_next()

        self.assertIsNotNone(resolution_result)
        self.assertEqual(resolution_result.phase, "merge_conflict_resolution")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_validation")

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_recovery_routes_interrupted_review_source_sync_conflicts_to_resolution(self) -> None:
        repo = self._create_repo("source")
        issue = self.store.create_issue("source sync conflict", "desc", repo_path=str(repo))
        manager = WorkspaceManager(self.config.worktrees_dir, self.artifacts, self.config.locks_dir)
        info = manager.prepare(issue)
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "README.md")
        self._git(info.worktree_root, "commit", "-m", "workspace change")
        (repo / "README.md").write_text("source change\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "source change")
        self.assertEqual(manager.sync_source_into_workspace(issue, info).status, "conflicts")
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation")
        self.store.transition_issue(issue.id, "implementing")
        self.store.transition_issue(issue.id, "ready_for_validation")
        self.store.transition_issue(issue.id, "validating")
        self.store.transition_issue(issue.id, "ready_for_review")
        run_id = "review-source-sync-run"
        self.assertTrue(
            self.store.acquire_lock(
                issue.id,
                "stale-worker",
                -60,
                run_id,
                expected_phase="ready_for_review",
            )
        )
        self.store.transition_issue(issue.id, "reviewing", run_id, "Starting review")
        self.store.create_run(run_id, issue.id, "review", "copilot-cli")
        orchestrator = Orchestrator(self.store, self.artifacts, self.config)

        results = orchestrator.recover_interrupted_runs()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "review_source_sync_recovered")
        self.assertEqual(results[0].next_phase, "ready_for_merge_conflict_resolution")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_merge_conflict_resolution")
        run = self.store.list_runs(issue.id)[-1]
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["next_phase"], "ready_for_merge_conflict_resolution")
        merge_artifact = (self.config.artifacts_dir / str(issue.id) / "merge.md").read_text(encoding="utf-8")
        self.assertIn("Workspace Source Sync", merge_artifact)
        self.assertIn("README.md", merge_artifact)
        self.assertIn("<<<<<<<", (info.worktree_root / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(self._git(repo, "status", "--porcelain"), "")

    @unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
    def test_copilot_merge_conflict_resolution_creates_snapshot_commit(self) -> None:
        repo = self._create_repo("source")
        record_dir, script = self._fake_copilot_conflict_resolution()
        config = self._copilot_config(script)
        issue = self.store.create_issue("conflict title", "desc", repo_path=str(repo))
        info = WorkspaceManager(config.worktrees_dir, self.artifacts, config.locks_dir).prepare(issue)
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "README.md")
        self._git(info.worktree_root, "commit", "-m", "workspace change")
        (repo / "README.md").write_text("source change\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "source change")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_merge_request(issue.id, target_branch=None, message="approved")
        self.store.transition_issue(issue.id, "ready_for_merge", message="approved")
        orchestrator = Orchestrator(self.store, self.artifacts, config)

        merge_result = orchestrator.process_next()
        resolution_result = orchestrator.process_next()

        self.assertIsNotNone(merge_result)
        self.assertEqual(merge_result.next_phase, "ready_for_merge_conflict_resolution")
        self.assertIsNotNone(resolution_result)
        self.assertEqual(resolution_result.phase, "merge_conflict_resolution")
        self.assertEqual(resolution_result.next_phase, "ready_for_validation")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_validation")
        self.assertEqual((info.worktree_root / "README.md").read_text(encoding="utf-8"), "resolved by copilot\n")
        self.assertEqual(self._git(info.worktree_root, "status", "--porcelain"), "")
        message = self._git(info.worktree_root, "log", "-1", "--format=%B")
        self.assertEqual(
            message.splitlines()[0],
            f"Issue {issue.id}: Kept Copilot's resolved README content.",
        )
        self.assertIn("Phase: merge_conflict_resolution", message)
        parents = self._git(info.worktree_root, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 3)
        history_events = [
            json.loads(line)
            for line in (self.config.artifacts_dir / str(issue.id) / "history.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        resolution_events = [event for event in history_events if event["phase"] == "merge_conflict_resolution"]
        self.assertEqual(len(resolution_events), 1)
        self.assertTrue(resolution_events[0].get("workspace_commit"))
        self.assertEqual(len(self._records(record_dir)), 1)

    def _copilot_config(self, script: Path) -> AppConfig:
        return AppConfig(
            home=self.home,
            db_path=self.home / "state.db",
            artifacts_dir=self.home / "issues",
            worktrees_dir=self.home / "worktrees",
            locks_dir=self.home / "locks",
            runner="copilot-cli",
            lock_ttl_seconds=60,
            copilot_command=str(script),
            runner_timeout_seconds=30,
        )

    def _create_repo(self, name: str) -> Path:
        repo = self.home / name
        repo.mkdir()
        self._git(repo, "init")
        self._git(repo, "config", "user.email", "test@example.com")
        self._git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("# test\n", encoding="utf-8")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-m", "initial")
        return repo

    def _commit_file(self, repo: Path, filename: str, content: str) -> None:
        (repo / filename).write_text(content, encoding="utf-8")
        self._git(repo, "add", filename)
        self._git(repo, "commit", "-m", f"add {filename}")

    def _fake_copilot(self) -> tuple[Path, Path]:
        record_dir = self.home / "copilot-records"
        record_dir.mkdir()
        script = self.home / "fake-copilot"
        script.write_text(
            f"""#!/usr/bin/env python3
import json
import pathlib
import sys
import uuid

records = pathlib.Path({str(record_dir)!r})
cwd = pathlib.Path.cwd()
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
agent = agent.rsplit(":", 1)[-1]
if agent not in ("agent-team-research", "agent-team-plan"):
    (cwd / "copilot-marker.txt").write_text("marker", encoding="utf-8")
if agent == "agent-team-plan":
    recommendation = "ready_for_implementation"
elif agent in ("agent-team-implementation", "agent-team-merge-conflict-resolution"):
    recommendation = "ready_for_validation"
elif agent == "agent-team-review":
    recommendation = "awaiting_merge_approval"
elif agent == "agent-team-validation":
    recommendation = "ready_for_review"
else:
    recommendation = "ready_for_plan"
if agent == "agent-team-implementation":
    artifact = (
        "1. Summary of changes\\n\\n"
        "Updated the workspace copilot marker.\\n\\n"
        "6. Recommendation: `" + recommendation + "`"
    )
elif agent == "agent-team-merge-conflict-resolution":
    artifact = (
        "1. Resolution strategy\\n\\n"
        "Updated the workspace copilot marker during conflict resolution.\\n\\n"
        "5. Recommendation: `" + recommendation + "`"
    )
else:
    artifact = "1. Result\\n\\nRecommendation: `" + recommendation + "`"
payload = {{"cwd": str(cwd), "argv": argv}}
(records / f"{{uuid.uuid4()}}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
print(artifact)
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return record_dir, script

    def _fake_copilot_mutating_first_plan(self) -> tuple[Path, Path]:
        record_dir = self.home / "copilot-requeue-records"
        record_dir.mkdir()
        state_file = self.home / "plan-mutated"
        script = self.home / "fake-copilot-mutating-plan"
        script.write_text(
            f"""#!/usr/bin/env python3
import json
import pathlib
import sys
import uuid

records = pathlib.Path({str(record_dir)!r})
state_file = pathlib.Path({str(state_file)!r})
cwd = pathlib.Path.cwd()
argv = sys.argv[1:]
agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
agent = agent.rsplit(":", 1)[-1]
if agent == "agent-team-plan" and not state_file.exists():
    state_file.write_text("done", encoding="utf-8")
    (cwd / "README.md").write_text("# changed during plan\\n", encoding="utf-8")
payload = {{"cwd": str(cwd), "argv": argv, "agent": agent}}
(records / f"{{uuid.uuid4()}}.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
print("1. Result\\n\\nRecommendation: `ready_for_implementation`")
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return record_dir, script

    def _fake_copilot_review_loop(self) -> tuple[Path, Path]:
        record_dir = self.home / "copilot-review-loop-records"
        record_dir.mkdir()
        state_file = self.home / "review-loop-state.json"
        script = self.home / "fake-copilot-review-loop"
        script.write_text(
            f"""#!/usr/bin/env python3
import json
import pathlib
import sys
import uuid

records = pathlib.Path({str(record_dir)!r})
state_file = pathlib.Path({str(state_file)!r})
cwd = pathlib.Path.cwd()
argv = sys.argv[1:]
agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
agent = agent.rsplit(":", 1)[-1]
state = {{"implementation": 0, "review": 0}}
if state_file.exists():
    state = json.loads(state_file.read_text(encoding="utf-8"))
if agent == "agent-team-implementation":
    state["implementation"] += 1
    path = cwd / ("implementation-" + str(state["implementation"]) + ".txt")
    path.write_text("implementation " + str(state["implementation"]) + "\\n", encoding="utf-8")
    recommendation = "ready_for_validation"
    artifact = (
        "1. Summary of changes\\n\\n"
        "Created workspace marker file " + str(state["implementation"]) + ".\\n\\n"
        "2. Files changed\\n\\n"
        "- implementation-" + str(state["implementation"]) + ".txt\\n\\n"
        "6. Recommendation: `" + recommendation + "`"
    )
elif agent == "agent-team-validation":
    recommendation = "ready_for_review"
    artifact = "1. Result\\n\\n" + agent + " completed\\n\\nRecommendation: `" + recommendation + "`"
elif agent == "agent-team-review":
    state["review"] += 1
    recommendation = "ready_for_implementation" if state["review"] == 1 else "awaiting_merge_approval"
    artifact = "1. Result\\n\\n" + agent + " completed\\n\\nRecommendation: `" + recommendation + "`"
else:
    recommendation = "ready_for_plan"
    artifact = "1. Result\\n\\n" + agent + " completed\\n\\nRecommendation: `" + recommendation + "`"
state_file.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
payload = {{"cwd": str(cwd), "argv": argv, "agent": agent, "state": state}}
(records / (str(uuid.uuid4()) + ".json")).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
print(artifact)
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return record_dir, script

    def _fake_copilot_e2e_validation(self) -> tuple[Path, Path]:
        record_dir = self.home / "copilot-e2e-records"
        record_dir.mkdir()
        script = self.home / "fake-copilot-e2e-validation"
        script.write_text(
            f"""#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import uuid

records = pathlib.Path({str(record_dir)!r})
cwd = pathlib.Path.cwd()
argv = sys.argv[1:]
agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
agent = agent.rsplit(":", 1)[-1]
validation_command = None
if agent == "agent-team-plan":
    recommendation = "ready_for_implementation"
    artifact = "1. Plan\\n\\nAdd named greeting support and validate it end-to-end.\\n\\nRecommendation: `ready_for_implementation`"
elif agent == "agent-team-implementation":
    (cwd / "hello.py").write_text(
        "import sys\\n\\n"
        "def greeting(name: str = \\"world\\") -> str:\\n"
        "    return \\"Hello, \\" + name + \\"!\\"\\n\\n"
        "if __name__ == \\"__main__\\":\\n"
        "    name = sys.argv[1] if len(sys.argv) > 1 else \\"world\\"\\n"
        "    print(greeting(name))\\n",
        encoding="utf-8",
    )
    (cwd / "test_hello.py").write_text(
        "import subprocess\\n"
        "import sys\\n"
        "import unittest\\n"
        "from pathlib import Path\\n\\n"
        "class HelloScriptTests(unittest.TestCase):\\n"
        "    def test_named_greeting(self):\\n"
        "        completed = subprocess.run(\\n"
        "            [sys.executable, str(Path(__file__).with_name(\\"hello.py\\")), \\"Azure\\"],\\n"
        "            text=True,\\n"
        "            capture_output=True,\\n"
        "            check=True,\\n"
        "        )\\n"
        "        self.assertEqual(completed.stdout, \\"Hello, Azure!\\\\n\\")\\n"
        "        print(completed.stdout.strip())\\n\\n"
        "if __name__ == \\"__main__\\":\\n"
        "    unittest.main()\\n",
        encoding="utf-8",
    )
    recommendation = "ready_for_validation"
    artifact = "1. Implementation\\n\\nUpdated hello.py and added a unittest-backed real behavior check.\\n\\nRecommendation: `ready_for_validation`"
elif agent == "agent-team-validation":
    command = [sys.executable, "-m", "unittest", "discover"]
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env={{**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}},
        text=True,
        capture_output=True,
        check=False,
    )
    validation_command = {{
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }}
    observed_output = completed.stdout + completed.stderr
    observed_greeting = "Hello, Azure!" in observed_output
    recommendation = "ready_for_review" if completed.returncode == 0 and observed_greeting else "ready_for_implementation"
    artifact = (
        "1. Validation result\\n\\n"
        "Command: `" + " ".join(command) + "`\\n\\n"
        "Return code: " + str(completed.returncode) + "\\n\\n"
        "Expected greeting: Hello, Azure!\\n\\n"
        "Greeting observed in command output: " + str(observed_greeting) + "\\n\\n"
        "Stdout:\\n```text\\n" + completed.stdout + "\\n```\\n\\n"
        "Stderr:\\n```text\\n" + completed.stderr + "\\n```\\n\\n"
        "Recommendation: `" + recommendation + "`"
    )
elif agent == "agent-team-review":
    recommendation = "awaiting_merge_approval"
    artifact = "1. Review\\n\\nImplementation and validation evidence are ready to merge.\\n\\nRecommendation: `awaiting_merge_approval`"
else:
    recommendation = "ready_for_plan"
    artifact = "1. Research\\n\\nThe requested hello-world feature is scoped for planning.\\n\\nRecommendation: `ready_for_plan`"
payload = {{"cwd": str(cwd), "argv": argv, "agent": agent}}
if validation_command is not None:
    payload["validation_command"] = validation_command
(records / (str(uuid.uuid4()) + ".json")).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
print(artifact)
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return record_dir, script

    def _fake_copilot_conflict_resolution(self, recommendation: str = "ready_for_validation") -> tuple[Path, Path]:
        record_dir = self.home / "copilot-conflict-records"
        record_dir.mkdir()
        script = self.home / "fake-copilot-conflict-resolution"
        script.write_text(
            f"""#!/usr/bin/env python3
import json
import pathlib
import sys
import uuid

records = pathlib.Path({str(record_dir)!r})
cwd = pathlib.Path.cwd()
argv = sys.argv[1:]
agent = argv[argv.index("--agent") + 1] if "--agent" in argv else ""
agent = agent.rsplit(":", 1)[-1]
if agent == "agent-team-merge-conflict-resolution":
    (cwd / "README.md").write_text("resolved by copilot\\n", encoding="utf-8")
    recommendation = {recommendation!r}
    artifact = (
        "1. Conflicted files resolved\\n\\n"
        "- README.md\\n\\n"
        "2. Resolution strategy\\n\\n"
        "Kept Copilot's resolved README content.\\n\\n"
        "5. Recommendation: `" + recommendation + "`"
    )
else:
    recommendation = "ready_for_plan"
    artifact = "1. Result\\n\\n" + agent + " completed\\n\\nRecommendation: `" + recommendation + "`"
payload = {{"cwd": str(cwd), "argv": argv, "agent": agent}}
(records / (str(uuid.uuid4()) + ".json")).write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
print(artifact)
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return record_dir, script

    @staticmethod
    def _records(record_dir: Path) -> list[dict[str, object]]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(record_dir.glob("*.json"))]

    @staticmethod
    def _add_dirs(argv: list[str]) -> list[str]:
        return [argv[index + 1] for index, arg in enumerate(argv) if arg == "--add-dir"]

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        completed = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
        return completed.stdout.strip()

    @staticmethod
    def _git_check(repo: Path, *args: str) -> bool:
        completed = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
        return completed.returncode == 0

    def _move_to_plan_approval(self, issue_id: int) -> None:
        if self.store.get_issue(issue_id).phase == "draft":
            self.store.transition_issue(issue_id, "needs_research")
        self.store.transition_issue(issue_id, "researching")
        self.store.transition_issue(issue_id, "ready_for_plan")
        self.store.transition_issue(issue_id, "planning")
        self.store.transition_issue(issue_id, "awaiting_plan_approval")

    def _move_to_merge_approval(self, issue_id: int) -> None:
        self._move_to_plan_approval(issue_id)
        self.store.transition_issue(issue_id, "ready_for_implementation")
        self.store.transition_issue(issue_id, "implementing")
        self.store.transition_issue(issue_id, "ready_for_validation")
        self.store.transition_issue(issue_id, "validating")
        self.store.transition_issue(issue_id, "ready_for_review")
        self.store.transition_issue(issue_id, "reviewing")
        self.store.transition_issue(issue_id, "awaiting_merge_approval")

    def _insert_pending_human_input(self, issue_id: int, request_id: str) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO human_input_requests (
                  id, issue_id, requested_by_phase, resume_phase, question, rationale,
                  requested_decision, status, created_at
                )
                VALUES (?, ?, 'research', 'needs_research', 'Q', 'R', 'D', 'pending', ?)
                """,
                (request_id, issue_id, "2026-01-01T00:00:00+00:00"),
            )

    def _expire_lock(self, issue_id: int) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE issues SET lock_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", issue_id),
            )

    def _start_stale_merge_run(self) -> Issue:
        issue = self.store.create_issue("title", "desc", repo_path=str(self.home / "source"), ready=True)
        self._move_to_merge_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_merge")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "merge-run"))
        self.store.transition_issue(issue.id, "merging", "merge-run")
        self.store.create_run("merge-run", issue.id, "merge", "workspace-merge")
        self._expire_lock(issue.id)
        return issue


if __name__ == "__main__":
    unittest.main()
