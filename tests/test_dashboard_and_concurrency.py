import tempfile
import unittest
from pathlib import Path

from agent_team.artifacts import ArtifactStore
from agent_team.cli import process_batch
from agent_team.config import AppConfig
from agent_team.dashboard import render_dashboard
from agent_team.db import IssueStore
from agent_team.lifecycle import delete_issue, reset_issue_to_draft, stop_issue
from agent_team.models import HumanInputRequestDraft


class DashboardAndConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.config = AppConfig(
            home=self.home,
            db_path=self.home / "state.db",
            artifacts_dir=self.home / "issues",
            worktrees_dir=self.home / "worktrees",
            runner="dry-run",
            lock_ttl_seconds=60,
        )
        self.store = IssueStore(self.config.db_path)
        self.store.init_schema()
        self.artifacts = ArtifactStore(self.config.artifacts_dir)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_dashboard_contains_issue_progress(self) -> None:
        self.store.create_issue("dashboard issue", "desc")
        output = render_dashboard(self.store)
        self.assertIn("Agent Team Dashboard", output)
        self.assertIn("draft", output)
        self.assertIn("Draft backlog", output)
        self.assertIn("dashboard issue", output)

    def test_concurrent_batch_processes_multiple_issues(self) -> None:
        first = self.store.create_issue("first", "desc", ready=True)
        second = self.store.create_issue("second", "desc", ready=True)
        results = process_batch(self.store, self.artifacts, self.config, concurrency=2)
        self.assertEqual(len(results), 4)
        self.assertEqual(self.store.get_issue(first.id).phase, "awaiting_plan_approval")
        self.assertEqual(self.store.get_issue(second.id).phase, "awaiting_plan_approval")

    def test_concurrent_batch_skips_drafts(self) -> None:
        draft = self.store.create_issue("draft", "desc")
        ready = self.store.create_issue("ready", "desc", ready=True)

        results = process_batch(self.store, self.artifacts, self.config, concurrency=2)

        self.assertEqual(len(results), 2)
        self.assertEqual({result.issue_id for result in results}, {ready.id})
        self.assertEqual(self.store.get_issue(draft.id).phase, "draft")
        self.assertEqual(self.store.get_issue(ready.id).phase, "awaiting_plan_approval")

    def test_dashboard_summary_has_draft_bucket(self) -> None:
        draft = self.store.create_issue("draft", "desc")
        ready = self.store.create_issue("ready", "desc", ready=True)

        summary = self.store.dashboard_summary()

        self.assertEqual(summary["manager_bucket_counts"]["draft"], 1)
        self.assertEqual(summary["manager_bucket_counts"]["ready"], 1)
        self.assertIn(draft.id, {row["id"] for row in summary["draft_issues"]})
        self.assertNotIn(draft.id, {row["id"] for row in summary["ready_issues"]})
        self.assertIn(ready.id, {row["id"] for row in summary["ready_issues"]})

    def test_dashboard_summary_has_human_input_bucket(self) -> None:
        issue = self.store.create_issue("human input issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, "run-human"))
        self.store.transition_issue(issue.id, "researching", "run-human")
        self.store.create_run("run-human", issue.id, "research", "dry-run")
        self.store.complete_run_and_request_human_input(
            "run-human",
            issue.id,
            "needs human",
            None,
            HumanInputRequestDraft(
                requested_by_phase="research",
                resume_phase="needs_research",
                question="Which option should be used?",
                rationale="The decision affects correctness.",
                requested_decision="Choose an option.",
            ),
        )
        self.store.release_lock(issue.id, "worker", "run-human")

        summary = self.store.dashboard_summary()
        output = render_dashboard(self.store)

        self.assertEqual(summary["manager_bucket_counts"]["human_input_needed"], 1)
        self.assertEqual(summary["human_input_needed"][0]["id"], issue.id)
        self.assertIn("Human input needed", output)
        self.assertIn("Which option should be used?", output)

    def test_dashboard_active_work_order_is_stable_across_lock_refresh(self) -> None:
        lower_priority = self.store.create_issue("lower priority", "desc", priority=5, ready=True)
        higher_priority_older = self.store.create_issue("higher priority older", "desc", priority=1, ready=True)
        higher_priority_newer = self.store.create_issue("higher priority newer", "desc", priority=1, ready=True)
        self.assertTrue(self.store.acquire_lock(lower_priority.id, "worker-low", 3600, "run-low"))
        self.assertTrue(self.store.acquire_lock(higher_priority_older.id, "worker-high-old", 60, "run-high-old"))
        self.assertTrue(self.store.acquire_lock(higher_priority_newer.id, "worker-high-new", 120, "run-high-new"))
        expected_order = [higher_priority_older.id, higher_priority_newer.id, lower_priority.id]

        summary_before = self.store.dashboard_summary()
        self.assertEqual([row["id"] for row in summary_before["active_work"]], expected_order)

        self.assertTrue(self.store.refresh_run_lock(higher_priority_older.id, "worker-high-old", "run-high-old", 7200))
        summary_after = self.store.dashboard_summary()

        self.assertEqual([row["id"] for row in summary_after["active_work"]], expected_order)
        refreshed = next(row for row in summary_after["active_work"] if row["id"] == higher_priority_older.id)
        self.assertNotEqual(refreshed["lock_expires_at"], summary_before["active_work"][0]["lock_expires_at"])

    def test_store_queries_can_scope_to_repo_context(self) -> None:
        repo_a = "/tmp/repo-a"
        repo_b = "/tmp/repo-b"
        draft_a = self.store.create_issue("draft a", "desc", repo_path=repo_a)
        ready_a = self.store.create_issue("ready a", "desc", repo_path=repo_a, ready=True)
        locked_a = self.store.create_issue("locked a", "desc", repo_path=repo_a, ready=True)
        approval_a = self.store.create_issue("approval a", "desc", repo_path=repo_a, ready=True)
        self.store.transition_issue(approval_a.id, "researching")
        self.store.transition_issue(approval_a.id, "ready_for_plan")
        self.store.transition_issue(approval_a.id, "planning")
        self.store.transition_issue(approval_a.id, "awaiting_plan_approval")
        ready_b = self.store.create_issue("ready b", "desc", repo_path=repo_b, ready=True)
        locked_b = self.store.create_issue("locked b", "desc", repo_path=repo_b, ready=True)
        blocked_b = self.store.create_issue("blocked b", "desc", repo_path=repo_b, ready=True)
        self.store.transition_issue(blocked_b.id, "blocked")
        no_repo = self.store.create_issue("no repo", "desc", ready=True)

        self.assertTrue(self.store.acquire_lock(locked_a.id, "owner-a", 60, "run-a"))
        self.store.create_run("run-a", locked_a.id, "research", "dry-run")
        self.store.complete_run("run-a", locked_a.id, "success", "repo a completed", None)
        self.assertTrue(self.store.acquire_lock(locked_b.id, "owner-b", 60, "run-b"))
        self.store.create_run("run-b", locked_b.id, "research", "dry-run")
        self.store.complete_run("run-b", locked_b.id, "success", "repo b completed", None)

        self.assertEqual(self.store.list_known_repos(), [repo_a, repo_b])
        self.assertEqual(
            {issue.id for issue in self.store.list_issues("open", repo_path=repo_a)},
            {draft_a.id, ready_a.id, locked_a.id, approval_a.id},
        )

        summary_a = self.store.dashboard_summary(repo_path=repo_a)
        summary_b = self.store.dashboard_summary(repo_path=repo_b)
        summary_unknown = self.store.dashboard_summary(repo_path="/tmp/unknown")
        summary_all = self.store.dashboard_summary()

        self.assertEqual(summary_a["manager_bucket_counts"]["draft"], 1)
        self.assertEqual(summary_a["manager_bucket_counts"]["ready"], 1)
        self.assertEqual(summary_a["manager_bucket_counts"]["approval_needed"], 1)
        self.assertEqual(summary_a["manager_bucket_counts"]["blocked"], 0)
        self.assertEqual({row["id"] for row in summary_a["active_work"]}, {locked_a.id})
        self.assertEqual({row["issue_id"] for row in summary_a["recent_runs"]}, {locked_a.id})
        self.assertEqual({row["issue_id"] for row in summary_a["recent_completed_runs"]}, {locked_a.id})
        self.assertTrue({row["issue_id"] for row in summary_a["recent_events"]}.issubset({draft_a.id, ready_a.id, locked_a.id, approval_a.id}))

        self.assertEqual(summary_b["manager_bucket_counts"]["ready"], 1)
        self.assertEqual(summary_b["manager_bucket_counts"]["blocked"], 1)
        self.assertEqual({row["id"] for row in summary_b["active_work"]}, {locked_b.id})
        self.assertEqual(summary_unknown["manager_bucket_counts"]["ready"], 0)
        self.assertEqual(summary_unknown["phase_counts"], [])
        self.assertGreaterEqual(summary_all["manager_bucket_counts"]["ready"], 3)
        self.assertIn(no_repo.id, {row["id"] for row in summary_all["ready_issues"]})

    def test_dashboard_recently_merged_filters_scopes_and_deduplicates(self) -> None:
        repo_a = "/tmp/repo-a"
        repo_b = "/tmp/repo-b"
        merged_a = self.store.create_issue("merged a", "desc", repo_path=repo_a, ready=True)
        merged_b = self.store.create_issue("merged b", "desc", repo_path=repo_b, ready=True)
        pr_finalized = self.store.create_issue("pr finalized", "desc", repo_path=repo_a, ready=True)
        open_with_merge_run = self.store.create_issue("open stray merge", "desc", repo_path=repo_a, ready=True)
        self._close_with_merge(merged_a.id, "merge-a-old", "old merge summary")
        self._close_with_merge(merged_b.id, "merge-b", "repo b merge summary")
        self._close_with_pr(pr_finalized.id, "pr-monitor-a", "Hosted pull request for issue is merged; closing.")
        self._set_run_times("merge-a-old", "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00")
        self._set_run_times("merge-b", "2026-01-02T00:00:00+00:00", "2026-01-02T00:01:00+00:00")
        self._set_event_time(pr_finalized.id, "pull_request.closed", "2026-01-06T00:00:00+00:00")
        self._insert_run(
            "merge-a-failed-later",
            merged_a.id,
            "merge",
            "failed",
            "failed merge should not win",
            "2026-01-03T00:00:00+00:00",
            "2026-01-03T00:01:00+00:00",
        )
        self._insert_run(
            "merge-a-newer",
            merged_a.id,
            "merge",
            "success",
            "newest successful merge",
            "2026-01-04T00:00:00+00:00",
            "2026-01-04T00:01:00+00:00",
        )
        self._insert_run(
            "merge-open",
            open_with_merge_run.id,
            "merge",
            "success",
            "open issue merge run should not appear",
            "2026-01-05T00:00:00+00:00",
            "2026-01-05T00:01:00+00:00",
        )

        summary_a = self.store.dashboard_summary(repo_path=repo_a)
        summary_b = self.store.dashboard_summary(repo_path=repo_b)
        summary_all = self.store.dashboard_summary()
        output = render_dashboard(self.store)

        self.assertEqual([row["issue_id"] for row in summary_a["recently_merged"]], [pr_finalized.id, merged_a.id])
        self.assertEqual(summary_a["recently_merged"][0]["run_id"], "pr-monitor-a")
        self.assertEqual(summary_a["recently_merged"][0]["phase"], "pr_monitor")
        self.assertEqual(summary_a["recently_merged"][0]["completed_at"], "2026-01-06T00:00:00+00:00")
        self.assertEqual(summary_a["recently_merged"][1]["run_id"], "merge-a-newer")
        self.assertEqual(summary_a["recently_merged"][1]["summary"], "newest successful merge")
        self.assertEqual([row["issue_id"] for row in summary_b["recently_merged"]], [merged_b.id])
        self.assertNotIn(open_with_merge_run.id, {row["issue_id"] for row in summary_all["recently_merged"]})
        self.assertIn("Recently finalized", output)
        self.assertIn("newest successful merge", output)
        self.assertIn("Hosted pull request for issue is merged", output)
        self.assertIn("merged a", output)

    def test_reset_issue_lands_in_draft_bucket_and_is_skipped_by_batch(self) -> None:
        reset = self.store.create_issue("reset", "desc", ready=True)
        other = self.store.create_issue("ready", "desc", ready=True)
        reset_issue_to_draft(self.config, self.store, self.artifacts, reset.id)

        summary = self.store.dashboard_summary()
        results = process_batch(self.store, self.artifacts, self.config, concurrency=2)

        self.assertIn(reset.id, {row["id"] for row in summary["draft_issues"]})
        self.assertNotIn(reset.id, {row["id"] for row in summary["ready_issues"]})
        self.assertEqual({result.issue_id for result in results}, {other.id})
        self.assertEqual(self.store.get_issue(reset.id).phase, "draft")

    def test_stopped_issue_lands_in_blocked_bucket_and_is_skipped_by_batch(self) -> None:
        stopped = self.store.create_issue("stopped", "desc", ready=True)
        other = self.store.create_issue("ready", "desc", ready=True)
        stop_issue(self.config, self.store, self.artifacts, stopped.id, "Pause this work.")

        summary = self.store.dashboard_summary()
        results = process_batch(self.store, self.artifacts, self.config, concurrency=2)

        self.assertIn(stopped.id, {row["id"] for row in summary["blocked_issues"]})
        self.assertNotIn(stopped.id, {row["id"] for row in summary["ready_issues"]})
        self.assertNotIn(stopped.id, {row["id"] for row in summary["human_input_needed"]})
        self.assertEqual({result.issue_id for result in results}, {other.id})
        self.assertEqual(self.store.get_issue(stopped.id).phase, "blocked")

    def test_deleted_issue_is_absent_from_dashboard_and_batch(self) -> None:
        deleted = self.store.create_issue("deleted", "desc", ready=True)
        other = self.store.create_issue("ready", "desc", ready=True)
        delete_issue(self.config, self.store, self.artifacts, deleted.id)

        summary = self.store.dashboard_summary()
        results = process_batch(self.store, self.artifacts, self.config, concurrency=2)
        listed_ids = set()
        for key in ("draft_issues", "ready_issues", "approval_issues", "blocked_issues", "open_issues"):
            listed_ids.update(row["id"] for row in summary[key])

        self.assertNotIn(deleted.id, listed_ids)
        self.assertEqual({result.issue_id for result in results}, {other.id})
        with self.assertRaisesRegex(KeyError, "Issue not found"):
            self.store.get_issue(deleted.id)

    def _close_with_merge(self, issue_id: int, run_id: str, summary: str) -> None:
        self._move_to_merge_approval(issue_id)
        self.store.transition_issue(issue_id, "ready_for_merge")
        self.assertTrue(self.store.acquire_lock(issue_id, "worker", 60, run_id))
        self.store.transition_issue(issue_id, "merging", run_id)
        self.store.create_run(run_id, issue_id, "merge", "dry-run")
        self.store.complete_run(run_id, issue_id, "success", summary, None, next_phase="done")
        self.store.transition_issue(issue_id, "done", run_id)
        self.store.release_lock(issue_id, "worker", run_id)

    def _close_with_pr(self, issue_id: int, monitor_id: str, summary: str) -> None:
        self._move_to_merge_approval(issue_id)
        self.store.transition_issue(issue_id, "ready_for_merge")
        self.store.transition_issue(issue_id, "merging")
        self.store.transition_issue(issue_id, "awaiting_pr_closure")
        self.assertIsNotNone(self.store.claim_pr_monitor_issue(issue_id, "worker", 60, monitor_id))
        self.store.record_event(issue_id, "pull_request.closed", summary, monitor_id)
        self.store.transition_issue(issue_id, "done", monitor_id, summary)
        self.store.release_lock(issue_id, "worker", monitor_id)

    def _set_event_time(self, issue_id: int, event_type: str, created_at: str) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE events
                SET created_at = ?
                WHERE id = (
                    SELECT id
                    FROM events
                    WHERE issue_id = ? AND event_type = ?
                    ORDER BY id DESC
                    LIMIT 1
                )
                """,
                (created_at, issue_id, event_type),
            )

    def _move_to_merge_approval(self, issue_id: int) -> None:
        if self.store.get_issue(issue_id).phase == "draft":
            self.store.transition_issue(issue_id, "needs_research")
        for phase in (
            "researching",
            "ready_for_plan",
            "planning",
            "awaiting_plan_approval",
            "ready_for_implementation",
            "implementing",
            "ready_for_validation",
            "validating",
            "ready_for_review",
            "reviewing",
            "awaiting_merge_approval",
        ):
            self.store.transition_issue(issue_id, phase)

    def _set_run_times(self, run_id: str, started_at: str, completed_at: str) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE runs SET started_at = ?, completed_at = ? WHERE id = ?",
                (started_at, completed_at, run_id),
            )

    def _insert_run(
        self,
        run_id: str,
        issue_id: int,
        phase: str,
        status: str,
        summary: str,
        started_at: str,
        completed_at: str,
    ) -> None:
        with self.store.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, issue_id, phase, runner, status, started_at, completed_at, summary, next_phase)
                VALUES (?, ?, ?, 'dry-run', ?, ?, ?, ?, 'done')
                """,
                (run_id, issue_id, phase, status, started_at, completed_at, summary),
            )


if __name__ == "__main__":
    unittest.main()
