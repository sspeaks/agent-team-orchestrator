from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

from agent_team.artifacts import ArtifactStore
from agent_team.models import Issue
from agent_team.pull_requests import PullRequestRemote, PullRequestResult
from agent_team.workspaces import WorkspaceError, WorkspaceManager, _SelectedRemote


@unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")
class WorkspaceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.artifacts = ArtifactStore(self.home / "issues")
        self.manager = WorkspaceManager(self.home / "worktrees", self.artifacts, self.home / "locks")
        self.repo = self._create_repo()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_creates_detached_worktree_and_metadata(self) -> None:
        info = self.manager.prepare(self._issue(1, self.repo))

        self.assertTrue(info.worktree_root.is_dir())
        self.assertEqual(info.workspace_repo_path, info.worktree_root)
        self.assertEqual(self._git(info.worktree_root, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        metadata = self.artifacts.read_workspace_metadata(1)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["workspace_repo_path"], str(info.workspace_repo_path))

    def test_reuses_existing_worktree_without_resetting(self) -> None:
        info = self.manager.prepare(self._issue(1, self.repo))
        marker = info.workspace_repo_path / "marker.txt"
        marker.write_text("kept", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("source dirty after creation", encoding="utf-8")

        reused = self.manager.prepare(self._issue(1, self.repo))

        self.assertEqual(reused.workspace_repo_path, info.workspace_repo_path)
        self.assertEqual(marker.read_text(encoding="utf-8"), "kept")

    def test_existing_returns_valid_workspace_without_creating(self) -> None:
        info = self.manager.prepare(self._issue(1, self.repo))
        (self.repo / "untracked.txt").write_text("source dirty after creation", encoding="utf-8")

        existing = self.manager.existing(self._issue(1, self.repo))

        self.assertEqual(existing.workspace_repo_path, info.workspace_repo_path)

    def test_existing_blocks_missing_workspace_without_creating(self) -> None:
        issue = self._issue(1, self.repo)
        expected_root = self.manager.worktrees_dir / self.manager._workspace_name(
            issue.id,
            self.repo.resolve(),
            (self.repo / ".git").resolve(),
        )

        with self.assertRaisesRegex(WorkspaceError, "Workspace metadata is missing"):
            self.manager.existing(issue)

        self.assertFalse(expected_root.exists())

    def test_different_issues_get_distinct_worktrees_for_same_repo(self) -> None:
        first = self.manager.prepare(self._issue(1, self.repo))
        second = self.manager.prepare(self._issue(2, self.repo))

        self.assertNotEqual(first.worktree_root, second.worktree_root)

    def test_subdirectory_repo_path_maps_to_worktree_subdirectory(self) -> None:
        subdir = self.repo / "pkg"
        subdir.mkdir()
        (subdir / "module.txt").write_text("pkg", encoding="utf-8")
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-m", "add pkg")

        info = self.manager.prepare(self._issue(1, subdir))

        self.assertEqual(info.relative_subpath, "pkg")
        self.assertEqual(info.workspace_repo_path, info.worktree_root / "pkg")
        self.assertTrue((info.workspace_repo_path / "module.txt").is_file())

    def test_missing_target_path_blocks(self) -> None:
        with self.assertRaisesRegex(WorkspaceError, "does not exist"):
            self.manager.prepare(self._issue(1, self.home / "missing"))

    def test_non_git_target_path_blocks(self) -> None:
        non_git = self.home / "non-git"
        non_git.mkdir()

        with self.assertRaisesRegex(WorkspaceError, "not inside a Git worktree"):
            self.manager.prepare(self._issue(1, non_git))

    def test_dirty_source_blocks_first_creation(self) -> None:
        (self.repo / "dirty.txt").write_text("dirty", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceError, "uncommitted or untracked"):
            self.manager.prepare(self._issue(1, self.repo))

    def test_commit_phase_snapshot_commits_dirty_worktree(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        (info.worktree_root / "feature.txt").write_text("feature\n", encoding="utf-8")

        commit = self.manager.commit_phase_snapshot(
            issue,
            info,
            phase="implementation",
            run_id="12345678-90ab-cdef-1234-567890abcdef",
            summary="Copilot CLI implementation recommended ready_for_validation for issue 1",
            artifact_markdown=(
                "<!-- run_id: 12345678-90ab-cdef-1234-567890abcdef -->\n\n"
                "1. Summary of changes\n\n"
                "Added artifact-derived workspace snapshot subjects.\n\n"
                "2. Files changed\n\n"
                "- `feature.txt`\n\n"
                "6. Recommendation: `ready_for_validation`"
            ),
            next_phase="ready_for_validation",
        )

        self.assertIsNotNone(commit)
        self.assertEqual(self._git(info.worktree_root, "status", "--porcelain"), "")
        self.assertEqual(self._git(info.worktree_root, "rev-parse", "HEAD"), commit)
        message = self._git(info.worktree_root, "log", "-1", "--format=%B")
        subject = message.splitlines()[0]
        self.assertEqual(subject, "Issue 1: Added artifact-derived workspace snapshot subjects.")
        self.assertNotIn("12345678", subject)
        self.assertFalse(subject.startswith("Issue 1 implementation "))
        self.assertIn("Issue: 1", message)
        self.assertIn("Phase: implementation", message)
        self.assertIn("Run ID: 12345678-90ab-cdef-1234-567890abcdef", message)
        self.assertIn("Summary: Added artifact-derived workspace snapshot subjects.", message)
        self.assertIn(
            "Runner Summary: Copilot CLI implementation recommended ready_for_validation for issue 1",
            message,
        )
        self.assertIn("Next Phase: ready_for_validation", message)

    def test_commit_phase_snapshot_clean_worktree_returns_none(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        head = self._git(info.worktree_root, "rev-parse", "HEAD")

        commit = self.manager.commit_phase_snapshot(
            issue,
            info,
            phase="implementation",
            run_id="run-clean",
            summary="No changes",
            artifact_markdown="1. Summary\n\nNo changes\n\nRecommendation: `ready_for_validation`",
            next_phase="ready_for_validation",
        )

        self.assertIsNone(commit)
        self.assertEqual(self._git(info.worktree_root, "rev-parse", "HEAD"), head)

    def test_commit_phase_snapshot_completes_merge_state_without_tree_diff(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "README.md")
        self._git(info.worktree_root, "commit", "-m", "workspace change")
        (self.repo / "README.md").write_text("source change\n", encoding="utf-8")
        self._git(self.repo, "add", "README.md")
        self._git(self.repo, "commit", "-m", "source change")
        self.assertEqual(self.manager.merge_and_cleanup(issue).status, "conflicts")
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")

        commit = self.manager.commit_phase_snapshot(
            issue,
            info,
            phase="merge_conflict_resolution",
            run_id="abcdef12-3456-7890-abcd-ef1234567890",
            summary="Resolved by keeping workspace content",
            artifact_markdown=(
                "1. Conflicted files resolved\n\n"
                "- `README.md`\n\n"
                "2. Resolution strategy\n\n"
                "Kept reviewed README content while completing the source merge.\n\n"
                "5. Recommendation: `ready_for_validation`"
            ),
            next_phase="ready_for_validation",
        )

        self.assertIsNotNone(commit)
        self.assertEqual(self._git(info.worktree_root, "status", "--porcelain"), "")
        parents = self._git(info.worktree_root, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 3)
        message = self._git(info.worktree_root, "log", "-1", "--format=%B")
        self.assertEqual(
            message.splitlines()[0],
            "Issue 1: Kept reviewed README content while completing the source merge.",
        )
        self.assertNotIn("abcdef12", message.splitlines()[0])
        self.assertIn("Phase: merge_conflict_resolution", message)
        self.assertIn(
            "Summary: Kept reviewed README content while completing the source merge.",
            message,
        )

    def test_phase_snapshot_commit_message_extracts_artifact_sections_and_fallbacks(self) -> None:
        issue = self._issue(7, self.repo)

        subject, body = WorkspaceManager._phase_snapshot_commit_message(
            issue,
            "implementation",
            "run-1",
            "runner fallback",
            (
                "<!-- run_id: run-1 -->\n\n"
                "## Summary of changes\n\n"
                "Added markdown section parsing\n"
                "and multiline summary cleanup.\n\n"
                "## Tests/checks run\n\n"
                "- not run\n\n"
                "Recommendation: `ready_for_validation`"
            ),
            "ready_for_validation",
        )
        self.assertEqual(
            subject,
            "Issue 7: Added markdown section parsing and multiline summary cleanup.",
        )
        self.assertIn(
            "Summary: Added markdown section parsing and multiline summary cleanup.",
            body,
        )

        subject, body = WorkspaceManager._phase_snapshot_commit_message(
            issue,
            "implementation",
            "run-2",
            "runner fallback",
            "<!-- run_id: run-2 -->\n\nRecommendation: `ready_for_validation`",
            "ready_for_validation",
        )
        self.assertEqual(subject, "Issue 7: runner fallback")
        self.assertIn("Summary: runner fallback", body)
        self.assertNotIn("Runner Summary:", body)

        empty_issue = Issue(
            id=8,
            title="",
            description="desc",
            source="local",
            external_id=None,
            repo_path=None,
            phase="needs_research",
            status="open",
            priority=3,
            tags=None,
            lock_owner=None,
            lock_expires_at=None,
            current_run_id=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        subject, body = WorkspaceManager._phase_snapshot_commit_message(
            empty_issue,
            "implementation",
            "run-3",
            "",
            "",
            "ready_for_validation",
        )
        self.assertEqual(subject, "Issue 8: Workspace snapshot")
        self.assertIn("Summary: Workspace snapshot", body)

    def test_phase_snapshot_commit_message_truncates_long_subject_detail(self) -> None:
        issue = self._issue(9, self.repo)
        long_summary = (
            "Updated commit message extraction to produce concise readable subjects while preserving "
            "audit metadata for every workspace snapshot commit"
        )

        subject, body = WorkspaceManager._phase_snapshot_commit_message(
            issue,
            "implementation",
            "run-long",
            "runner fallback",
            f"1. Summary of changes\n\n{long_summary}\n\nRecommendation: `ready_for_validation`",
            "ready_for_validation",
        )

        self.assertTrue(subject.startswith("Issue 9: Updated commit message extraction"))
        self.assertTrue(subject.endswith("..."))
        self.assertLessEqual(len(subject.split(": ", 1)[1]), 72)
        self.assertIn(f"Summary: {long_summary}", body)

    def test_prepare_recovers_clean_orphaned_worktree_metadata(self) -> None:
        issue = self._issue(1, self.repo)
        expected_root = self.manager.worktrees_dir / self.manager._workspace_name(
            issue.id,
            self.repo.resolve(),
            (self.repo / ".git").resolve(),
        )
        self.manager.worktrees_dir.mkdir(parents=True)
        self._git(self.repo, "worktree", "add", "--detach", str(expected_root), "HEAD")

        info = self.manager.prepare(issue)

        self.assertEqual(info.worktree_root, expected_root.resolve())
        metadata = self.artifacts.read_workspace_metadata(issue.id)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["worktree_root"], str(expected_root.resolve()))

    def test_prepare_blocks_dirty_orphaned_worktree_without_deleting_it(self) -> None:
        issue = self._issue(1, self.repo)
        expected_root = self.manager.worktrees_dir / self.manager._workspace_name(
            issue.id,
            self.repo.resolve(),
            (self.repo / ".git").resolve(),
        )
        self.manager.worktrees_dir.mkdir(parents=True)
        self._git(self.repo, "worktree", "add", "--detach", str(expected_root), "HEAD")
        (expected_root / "dirty.txt").write_text("dirty", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceError, "has local changes"):
            self.manager.prepare(issue)

        self.assertTrue((expected_root / "dirty.txt").is_file())

    def test_metadata_mismatch_blocks_reuse(self) -> None:
        self.manager.prepare(self._issue(1, self.repo))
        metadata_path = self.artifacts.workspace_metadata_path(1)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["source_root"] = str(self.home / "other")
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceError, "does not match"):
            self.manager.prepare(self._issue(1, self.repo))

    def test_merge_and_cleanup_merges_commits_and_removes_worktree(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        (info.worktree_root / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "feature.txt")
        self._git(info.worktree_root, "commit", "-m", "feature")
        self.artifacts.write_merge_request(issue.id, target_branch=None, message="looks good")

        result = self.manager.merge_and_cleanup(issue)

        self.assertEqual(result.status, "merged")
        self.assertTrue(result.cleanup_removed)
        self.assertFalse(info.worktree_root.exists())
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "feature\n")
        self.assertIsNone(self.artifacts.read_workspace_metadata(issue.id))
        merged = json.loads(self.artifacts.merged_workspace_metadata_path(issue.id).read_text(encoding="utf-8"))
        self.assertEqual(merged["merge_commit"], result.merge_commit)
        self.assertTrue(merged["cleanup_removed"])
        self.assertEqual(self._git(self.repo, "branch", "--list", "agent-team/issue-1-merge"), "")
        merge_message = self._git(self.repo, "log", "-1", "--format=%B")
        self.assertIn("Merge approval: looks good", merge_message)
        self.assertIn("feature", merge_message)

    def test_concurrent_same_repo_merges_are_serialized(self) -> None:
        first_issue = self._issue(1, self.repo)
        second_issue = self._issue(2, self.repo)
        first = self.manager.prepare(first_issue)
        second = self.manager.prepare(second_issue)
        self._commit_file(first.worktree_root, "feature-one.txt", "one\n")
        self._commit_file(second.worktree_root, "feature-two.txt", "two\n")

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.manager.merge_and_cleanup, first_issue),
                executor.submit(self.manager.merge_and_cleanup, second_issue),
            ]
            results = [future.result() for future in as_completed(futures)]

        self.assertEqual([result.status for result in results], ["merged", "merged"])
        self.assertEqual((self.repo / "feature-one.txt").read_text(encoding="utf-8"), "one\n")
        self.assertEqual((self.repo / "feature-two.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(self._git(self.repo, "status", "--porcelain"), "")
        self.assertFalse(first.worktree_root.exists())
        self.assertFalse(second.worktree_root.exists())
        self.assertIsNone(self.artifacts.read_workspace_metadata(first_issue.id))
        self.assertIsNone(self.artifacts.read_workspace_metadata(second_issue.id))
        self.assertEqual(self._git(self.repo, "branch", "--list", "agent-team/issue-*"), "")

    def test_merge_commits_dirty_worktree_before_merging(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        (info.worktree_root / "feature.txt").write_text("feature\n", encoding="utf-8")

        result = self.manager.merge_and_cleanup(issue)

        self.assertEqual(result.status, "merged")
        self.assertIsNotNone(result.worktree_commit)
        self.assertFalse(info.worktree_root.exists())
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "feature\n")
        self.assertIn(
            "Issue 1 final workspace snapshot: issue 1",
            self._git(self.repo, "log", "--format=%s", "--max-count=3"),
        )
        self.assertNotIn("Implement issue 1: issue 1", self._git(self.repo, "log", "--format=%s", "--max-count=3"))
        merged = json.loads(self.artifacts.merged_workspace_metadata_path(issue.id).read_text(encoding="utf-8"))
        self.assertEqual(merged["worktree_commit"], result.worktree_commit)

    def test_auto_remote_finalizes_by_pull_request_without_merging_source(self) -> None:
        self._git(self.repo, "remote", "add", "origin", "https://token:secret@github.com/owner/repo.git")
        self._git(self.repo, "remote", "set-url", "--push", "origin", "https://github.com/owner/repo.git")
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._commit_file(info.worktree_root, "feature.txt", "feature\n")
        source_head = self._git(self.repo, "rev-parse", "HEAD")
        pr_result = PullRequestResult(
            provider="github",
            remote_name="origin",
            source_branch="agent-team/issue-1",
            target_branch="master",
            title="Issue 1: issue 1",
            url="https://github.com/owner/repo/pull/7",
            id="7",
            number=7,
            status="OPEN",
            is_existing=False,
            raw={"number": 7},
        )

        with patch.object(self.manager, "_push_pull_request_branch") as push_branch:
            with patch("agent_team.workspaces.create_or_get_pull_request", return_value=pr_result) as create_pr:
                result = self.manager.merge_and_cleanup(issue)

        self.assertEqual(result.status, "pull_request")
        self.assertEqual(result.pr_url, "https://github.com/owner/repo/pull/7")
        self.assertFalse(info.worktree_root.exists())
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), source_head)
        self.assertFalse((self.repo / "feature.txt").exists())
        self.assertEqual(push_branch.call_args.args[3], "agent-team/issue-1")
        self.assertEqual(push_branch.call_args.args[4], result.worktree_head)
        request = create_pr.call_args.args[1]
        self.assertEqual(request.source_branch, "agent-team/issue-1")
        self.assertEqual(request.target_branch, "master")
        metadata = self.artifacts.read_pull_request_metadata(issue.id)
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertTrue(metadata["cleanup_removed"])
        self.assertEqual(metadata["url"], "https://github.com/owner/repo/pull/7")
        self.assertEqual(metadata["head_commit"], result.worktree_head)
        self.assertEqual(metadata["remote_url"], "https://github.com/owner/repo.git")
        self.assertNotIn("remote_push_url", metadata)
        self.assertNotIn("secret", json.dumps(metadata))
        self.assertIsNone(self.artifacts.read_workspace_metadata(issue.id))
        self.assertFalse(self.artifacts.merged_workspace_metadata_path(issue.id).exists())

    def test_pull_request_remote_allows_same_repo_https_and_ssh_push_urls(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        cases = (
            (
                "https://github.com/owner/repo.git",
                "git@github.com:owner/repo.git",
                "github",
            ),
            (
                "https://dev.azure.com/org/project/_git/repo",
                "ssh.dev.azure.com:v3/org/project/repo",
                "azure-devops",
            ),
        )
        self._git(self.repo, "remote", "add", "origin", cases[0][0])
        for fetch_url, push_url, provider in cases:
            with self.subTest(provider=provider):
                self._git(self.repo, "remote", "set-url", "origin", fetch_url)
                self._git(self.repo, "remote", "set-url", "--push", "origin", push_url)

                mode, selected_remote = self.manager._select_finalization_mode(info, "pull_request", "origin")

                self.assertEqual(mode, "pull_request")
                self.assertIsNotNone(selected_remote)
                assert selected_remote is not None
                self.assertEqual(selected_remote.remote.provider, provider)

    def test_pull_request_remote_blocks_mismatched_push_url(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._git(self.repo, "remote", "add", "origin", "https://github.com/owner/repo.git")
        self._git(self.repo, "remote", "set-url", "--push", "origin", "https://github.com/other/repo.git")

        with self.assertRaisesRegex(WorkspaceError, "push URL resolves to GitHub other/repo"):
            self.manager._select_finalization_mode(info, "pull_request", "origin")

    def test_pull_request_remote_blocks_unsupported_push_url(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._git(self.repo, "remote", "add", "origin", "https://github.com/owner/repo.git")
        self._git(self.repo, "remote", "set-url", "--push", "origin", str(self.home / "remote.git"))

        with self.assertRaisesRegex(WorkspaceError, "push URL does not resolve to a supported"):
            self.manager._select_finalization_mode(info, "pull_request", "origin")

    def test_push_pull_request_branch_uses_validated_push_url_directly(self) -> None:
        bare_remote = self.home / "validated-push.git"
        self._git(bare_remote.parent, "init", "--bare", str(bare_remote))
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        selected_remote = _SelectedRemote(
            remote=PullRequestRemote(
                provider="github",
                remote_name="origin",
                url="https://github.com/owner/repo.git",
                repo="repo",
                owner="owner",
            ),
            push_url=str(bare_remote),
        )
        head = self._git(info.worktree_root, "rev-parse", "HEAD")

        self.manager._push_pull_request_branch(issue, info, selected_remote, "agent-team/issue-1", head)

        self.assertEqual(
            self._git(self.repo, "ls-remote", str(bare_remote), "refs/heads/agent-team/issue-1").split()[0],
            head,
        )

    def test_remote_branch_probe_redacts_credentials_in_git_errors(self) -> None:
        with self.assertRaises(WorkspaceError) as caught:
            self.manager._remote_branch_head(
                self.repo,
                "https://token:secret@127.0.0.1:1/owner/repo.git",
                "agent-team/issue-1",
            )

        message = str(caught.exception)
        self.assertIn("https://[redacted]@127.0.0.1", message)
        self.assertNotIn("token:secret", message)
        self.assertNotIn("secret@127.0.0.1", message)

    def test_auto_unsupported_remote_blocks_instead_of_local_merge(self) -> None:
        self._git(self.repo, "remote", "add", "origin", "https://example.com/owner/repo.git")
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._commit_file(info.worktree_root, "feature.txt", "feature\n")

        with self.assertRaisesRegex(WorkspaceError, "no.*supported pull request provider"):
            self.manager.merge_and_cleanup(issue)

        self.assertTrue(info.worktree_root.exists())
        self.assertFalse((self.repo / "feature.txt").exists())

    def test_explicit_local_mode_uses_local_merge_even_with_remote(self) -> None:
        self._git(self.repo, "remote", "add", "origin", "https://example.com/owner/repo.git")
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._commit_file(info.worktree_root, "feature.txt", "feature\n")
        self.artifacts.write_merge_request(issue.id, target_branch=None, message="approved", mode="local")

        result = self.manager.merge_and_cleanup(issue)

        self.assertEqual(result.status, "merged")
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "feature\n")

    def test_pull_request_recovery_finishes_partial_cleanup(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._commit_file(info.worktree_root, "feature.txt", "feature\n")
        head = self._git(info.worktree_root, "rev-parse", "HEAD")
        metadata = {
            **info.to_metadata(),
            "cleanup_removed": False,
            "finalized_at": "2026-01-01T00:00:00+00:00",
            "mode": "pull_request",
            "provider": "github",
            "remote_name": "origin",
            "source_branch": "agent-team/issue-1",
            "target_branch": "master",
            "head_commit": head,
            "worktree_head": head,
            "worktree_commit": None,
            "merge_branch": "agent-team/issue-1-merge",
            "title": "Issue 1: issue 1",
            "url": "https://github.com/owner/repo/pull/7",
            "id": "7",
            "number": 7,
            "pr_status": "OPEN",
            "is_existing": False,
            "raw": {"number": 7},
        }
        self.artifacts.write_pull_request_metadata(issue.id, metadata)

        recovery = self.manager.recover_interrupted_merge(issue)

        self.assertEqual(recovery.next_phase, "done")
        self.assertEqual(recovery.run_status, "success")
        self.assertFalse(info.worktree_root.exists())
        self.assertIsNone(self.artifacts.read_workspace_metadata(issue.id))
        recovered = self.artifacts.read_pull_request_metadata(issue.id)
        self.assertIsNotNone(recovered)
        assert recovered is not None
        self.assertTrue(recovered["cleanup_removed"])
        self.assertIn("Pull request URL", recovery.artifact_markdown)

    def test_merge_blocks_when_source_branch_is_unknown(self) -> None:
        self._git(self.repo, "checkout", "--detach")
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        (info.worktree_root / "feature.txt").write_text("feature\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "feature.txt")
        self._git(info.worktree_root, "commit", "-m", "feature")

        with self.assertRaisesRegex(WorkspaceError, "explicit target branch"):
            self.manager.merge_and_cleanup(issue)

    def test_merge_conflict_is_prepared_in_issue_worktree(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "README.md")
        self._git(info.worktree_root, "commit", "-m", "workspace change")
        (self.repo / "README.md").write_text("source change\n", encoding="utf-8")
        self._git(self.repo, "add", "README.md")
        self._git(self.repo, "commit", "-m", "source change")

        result = self.manager.merge_and_cleanup(issue)

        self.assertEqual(result.status, "conflicts")
        self.assertEqual(result.conflict_files, ("README.md",))
        self.assertTrue(info.worktree_root.is_dir())
        self.assertIsNotNone(self.artifacts.read_workspace_metadata(issue.id))
        self.assertIn("<<<<<<<", (info.worktree_root / "README.md").read_text(encoding="utf-8"))
        self.assertNotIn("<<<<<<<", (self.repo / "README.md").read_text(encoding="utf-8"))

    def test_merge_recovery_routes_existing_conflicts_to_resolution(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        (info.worktree_root / "README.md").write_text("workspace change\n", encoding="utf-8")
        self._git(info.worktree_root, "add", "README.md")
        self._git(info.worktree_root, "commit", "-m", "workspace change")
        (self.repo / "README.md").write_text("source change\n", encoding="utf-8")
        self._git(self.repo, "add", "README.md")
        self._git(self.repo, "commit", "-m", "source change")
        self.assertEqual(self.manager.merge_and_cleanup(issue).status, "conflicts")

        recovery = self.manager.recover_interrupted_merge(issue)

        self.assertEqual(recovery.next_phase, "ready_for_merge_conflict_resolution")
        self.assertEqual(recovery.run_status, "interrupted")
        self.assertIn("README.md", recovery.artifact_markdown)
        self.assertTrue(info.worktree_root.is_dir())

    def test_merge_recovery_finishes_partial_cleanup_from_merged_metadata(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._commit_file(info.worktree_root, "feature.txt", "feature\n")
        worktree_head = self._git(info.worktree_root, "rev-parse", "HEAD")
        branch = self._git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        self._git(self.repo, "merge", "--no-ff", worktree_head, "-m", "merge feature")
        merge_commit = self._git(self.repo, "rev-parse", "HEAD")
        merged_metadata = {
            **info.to_metadata(),
            "cleanup_removed": False,
            "merge_commit": merge_commit,
            "merge_target_branch": branch,
            "merged_at": "2026-01-01T00:00:00+00:00",
            "worktree_head": worktree_head,
            "worktree_commit": None,
        }
        self.artifacts.write_merged_workspace_metadata(issue.id, merged_metadata)

        recovery = self.manager.recover_interrupted_merge(issue)

        self.assertEqual(recovery.next_phase, "done")
        self.assertEqual(recovery.run_status, "success")
        self.assertFalse(info.worktree_root.exists())
        self.assertIsNone(self.artifacts.read_workspace_metadata(issue.id))
        recovered = self.artifacts.read_merged_workspace_metadata(issue.id)
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered["cleanup_removed"])

    def test_merge_recovery_blocks_unreadable_workspace_metadata(self) -> None:
        issue = self._issue(1, self.repo)
        self.artifacts.workspace_metadata_path(issue.id).write_text("{", encoding="utf-8")

        recovery = self.manager.recover_interrupted_merge(issue)

        self.assertEqual(recovery.next_phase, "blocked")
        self.assertEqual(recovery.run_status, "blocked")
        self.assertIn("Workspace metadata is unreadable", recovery.summary)
        self.assertIn("workspace.json", recovery.summary)
        self.assertIn("Recommendation: `blocked`", recovery.artifact_markdown)

    def test_merge_recovery_blocks_unreadable_merged_workspace_metadata(self) -> None:
        issue = self._issue(1, self.repo)
        self.artifacts.merged_workspace_metadata_path(issue.id).write_text("{", encoding="utf-8")

        recovery = self.manager.recover_interrupted_merge(issue)

        self.assertEqual(recovery.next_phase, "blocked")
        self.assertEqual(recovery.run_status, "blocked")
        self.assertIn("Merged workspace metadata is unreadable", recovery.summary)
        self.assertIn("workspace.merged.json", recovery.summary)
        self.assertIn("Recommendation: `blocked`", recovery.artifact_markdown)

    def test_merge_recovery_does_not_mark_noop_worktree_done(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)

        recovery = self.manager.recover_interrupted_merge(issue)

        self.assertEqual(recovery.next_phase, "ready_for_merge")
        self.assertEqual(recovery.run_status, "interrupted")
        self.assertTrue(info.worktree_root.is_dir())

    def test_merge_blocks_for_issue_without_workspace_or_repo(self) -> None:
        with self.assertRaisesRegex(WorkspaceError, "no target repo"):
            self.manager.merge_and_cleanup(self._issue(1, None))

    def test_reset_removes_recorded_worktree_and_merge_branch(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._git(self.repo, "branch", "agent-team/issue-1-merge")

        result = self.manager.reset_issue_workspace(issue)

        self.assertIn(str(info.worktree_root), result.removed_paths)
        self.assertFalse(info.worktree_root.exists())
        self.assertEqual(self._git(self.repo, "branch", "--list", "agent-team/issue-1-merge"), "")

    def test_reset_removes_deterministic_orphan_without_metadata(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self.artifacts.delete_workspace_metadata(issue.id)

        result = self.manager.reset_issue_workspace(issue)

        self.assertIn(str(info.worktree_root), result.removed_paths)
        self.assertFalse(info.worktree_root.exists())

    def test_reset_removes_git_locked_registered_worktree(self) -> None:
        issue = self._issue(1, self.repo)
        info = self.manager.prepare(issue)
        self._git(self.repo, "worktree", "lock", str(info.worktree_root))

        result = self.manager.reset_issue_workspace(issue)

        self.assertIn(str(info.worktree_root), result.removed_paths)
        self.assertFalse(info.worktree_root.exists())
        self.assertNotIn(str(info.worktree_root), self._git(self.repo, "worktree", "list", "--porcelain"))
        self.artifacts.delete_workspace_metadata(issue.id)
        recreated = self.manager.prepare(issue)
        self.assertTrue(recreated.worktree_root.is_dir())

    def test_reset_scans_stale_issue_worktrees_and_unlinks_symlinks(self) -> None:
        stale = self.manager.worktrees_dir / "issue-1-stale"
        stale.mkdir(parents=True)
        (stale / "old.txt").write_text("old", encoding="utf-8")
        outside = self.home / "outside"
        outside.mkdir()
        (outside / "kept.txt").write_text("kept", encoding="utf-8")
        linked = self.manager.worktrees_dir / "issue-1-linked"
        linked.symlink_to(outside, target_is_directory=True)

        result = self.manager.reset_issue_workspace(self._issue(1, None))

        self.assertIn(str(stale), result.removed_paths)
        self.assertIn(str(linked), result.removed_paths)
        self.assertFalse(stale.exists())
        self.assertFalse(linked.is_symlink())
        self.assertTrue((outside / "kept.txt").is_file())

    def test_reset_removes_stale_worktree_when_repo_path_is_missing(self) -> None:
        stale = self.manager.worktrees_dir / "issue-1-stale"
        stale.mkdir(parents=True)

        result = self.manager.reset_issue_workspace(self._issue(1, self.home / "missing"))

        self.assertIn(str(stale), result.removed_paths)
        self.assertFalse(stale.exists())
        self.assertTrue(result.warnings)

    def test_reset_rejects_workspace_metadata_outside_worktrees_dir(self) -> None:
        outside = self.home / "outside-worktree"
        outside.mkdir()
        self.artifacts.write_workspace_metadata(1, {"worktree_root": str(outside)})

        with self.assertRaisesRegex(WorkspaceError, "outside worktrees"):
            self.manager.reset_issue_workspace(self._issue(1, None))

    def test_reset_rejects_workspace_metadata_pointing_at_worktrees_root(self) -> None:
        self.manager.worktrees_dir.mkdir()
        unrelated = self.manager.worktrees_dir / "issue-2-unrelated"
        unrelated.mkdir()
        (unrelated / "kept.txt").write_text("kept", encoding="utf-8")
        self.artifacts.write_workspace_metadata(1, {"worktree_root": str(self.manager.worktrees_dir)})

        with self.assertRaisesRegex(WorkspaceError, "worktrees root"):
            self.manager.reset_issue_workspace(self._issue(1, None))

        self.assertTrue(self.manager.worktrees_dir.is_dir())
        self.assertTrue((unrelated / "kept.txt").is_file())

    def test_reset_rejects_workspace_metadata_pointing_at_sibling_issue_worktree(self) -> None:
        self.manager.worktrees_dir.mkdir()
        sibling = self.manager.worktrees_dir / "issue-2-unrelated"
        sibling.mkdir()
        (sibling / "kept.txt").write_text("kept", encoding="utf-8")
        owned_stale = self.manager.worktrees_dir / "issue-1-stale"
        owned_stale.mkdir()
        self.artifacts.write_workspace_metadata(1, {"worktree_root": str(sibling)})

        with self.assertRaisesRegex(WorkspaceError, "not owned by issue 1"):
            self.manager.reset_issue_workspace(self._issue(1, None))

        self.assertTrue((sibling / "kept.txt").is_file())
        self.assertTrue(owned_stale.is_dir())

    def _create_repo(self) -> Path:
        repo = self.home / "repo"
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

    @staticmethod
    def _issue(issue_id: int, repo_path: Path | None) -> Issue:
        return Issue(
            id=issue_id,
            title=f"issue {issue_id}",
            description="desc",
            source="local",
            external_id=None,
            repo_path=str(repo_path) if repo_path is not None else None,
            phase="needs_research",
            status="open",
            priority=3,
            tags=None,
            lock_owner=None,
            lock_expires_at=None,
            current_run_id=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        completed = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
