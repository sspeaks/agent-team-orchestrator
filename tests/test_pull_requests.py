from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from agent_team.pull_requests import (
    CommandResult,
    PullRequestError,
    PullRequestRemote,
    PullRequestRequest,
    SubprocessCommandRunner,
    create_or_get_pull_request,
    ensure_pull_request_conflict_comment,
    get_pull_request_status,
    is_safe_pull_request_url,
    parse_azure_devops_remote,
    parse_github_remote,
    parse_pull_request_remote,
    pull_request_remote_from_metadata,
)


class FakeRunner:
    def __init__(self, results: Sequence[CommandResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []

    def run(self, args: Sequence[str]) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        if not self.results:
            raise AssertionError(f"unexpected command: {call}")
        result = self.results.pop(0)
        return CommandResult(args=call, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def command_result(stdout: object, returncode: int = 0, stderr: str = "") -> CommandResult:
    rendered = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return CommandResult(args=(), returncode=returncode, stdout=rendered, stderr=stderr)


class PullRequestRemoteParsingTests(unittest.TestCase):
    def test_parse_github_remote_forms(self) -> None:
        cases = (
            "git@github.com:owner/repo.git",
            "ssh://git@github.com/owner/repo.git",
            "https://github.com/owner/repo.git",
        )
        for url in cases:
            with self.subTest(url=url):
                remote = parse_github_remote("origin", url)
                self.assertIsNotNone(remote)
                assert remote is not None
                self.assertEqual(remote.provider, "github")
                self.assertEqual(remote.remote_name, "origin")
                self.assertEqual(remote.owner, "owner")
                self.assertEqual(remote.repo, "repo")

    def test_parse_azure_devops_services_remote_forms(self) -> None:
        cases = (
            "https://dev.azure.com/org/project/_git/repo",
            "https://org.visualstudio.com/project/_git/repo",
            "ssh.dev.azure.com:v3/org/project/repo",
            "ssh://git@ssh.dev.azure.com/v3/org/project/repo",
        )
        for url in cases:
            with self.subTest(url=url):
                remote = parse_azure_devops_remote("upstream", url)
                self.assertIsNotNone(remote)
                assert remote is not None
                self.assertEqual(remote.provider, "azure-devops")
                self.assertEqual(remote.remote_name, "upstream")
                self.assertEqual(remote.org, "org")
                self.assertEqual(remote.project, "project")
                self.assertEqual(remote.repo, "repo")

    def test_unsupported_remote_returns_none(self) -> None:
        unsupported = (
            "https://example.com/org/project/_git/repo",
            "https://dev.azure.com/org/project",
            "https://server.example/tfs/project/_git/repo",
            "git@gitlab.com:owner/repo.git",
        )
        for url in unsupported:
            with self.subTest(url=url):
                self.assertIsNone(parse_pull_request_remote("origin", url))

    def test_parse_remote_rejects_query_or_fragment_components(self) -> None:
        urls = (
            "https://github.com/owner/repo.git?access_token=secret",
            "https://github.com/owner/repo.git#token=secret",
            "git@github.com:owner/repo.git?access_token=secret",
            "https://dev.azure.com/org/project/_git/repo?access_token=secret",
            "ssh.dev.azure.com:v3/org/project/repo#password=secret",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(parse_pull_request_remote("origin", url))

    def test_parse_remote_rejects_credential_bearing_urls(self) -> None:
        urls = (
            "https://token:secret@github.com/owner/repo.git",
            "https://token@github.com/owner/repo.git",
            "ssh://git:secret@github.com/owner/repo.git",
            "ssh://git:secret@ssh.dev.azure.com/v3/org/project/repo",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(parse_pull_request_remote("origin", url))


class PullRequestProviderTests(unittest.TestCase):
    def test_github_reuses_existing_open_pull_request(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "number": 42,
                            "url": "https://github.com/owner/repo/pull/42",
                            "title": "Existing PR",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                            "headRepositoryOwner": {"login": "owner"},
                            "state": "OPEN",
                        }
                    ]
                )
            ]
        )

        result = create_or_get_pull_request(remote, request, runner)

        self.assertTrue(result.is_existing)
        self.assertEqual(result.provider, "github")
        self.assertEqual(result.remote_name, "origin")
        self.assertEqual(result.number, 42)
        self.assertEqual(result.id, "42")
        self.assertEqual(result.url, "https://github.com/owner/repo/pull/42")
        self.assertEqual(result.source_branch, "feature")
        self.assertEqual(result.target_branch, "main")
        self.assertEqual(
            runner.calls,
            [
                (
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    "owner/repo",
                    "--head",
                    "feature",
                    "--base",
                    "main",
                    "--state",
                    "open",
                    "--json",
                    "number,url,title,headRefName,baseRefName,state,headRepository,headRepositoryOwner",
                )
            ],
        )

    def test_github_reuses_matching_head_repository_when_fork_pr_is_listed_first(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "number": 41,
                            "url": "https://github.com/owner/repo/pull/41",
                            "title": "Fork PR",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRepository": {"name": "repo", "nameWithOwner": "fork/repo"},
                            "headRepositoryOwner": {"login": "fork"},
                            "state": "OPEN",
                        },
                        {
                            "number": 42,
                            "url": "https://github.com/owner/repo/pull/42",
                            "title": "Same repo PR",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                            "headRepositoryOwner": {"login": "owner"},
                            "state": "OPEN",
                        },
                    ]
                )
            ]
        )

        result = create_or_get_pull_request(remote, request, runner)

        self.assertTrue(result.is_existing)
        self.assertEqual(result.number, 42)
        self.assertEqual(result.title, "Same repo PR")

    def test_github_creates_when_only_fork_pr_uses_same_branch_name(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        url = "https://github.com/owner/repo/pull/43"
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "number": 41,
                            "url": "https://github.com/owner/repo/pull/41",
                            "title": "Fork PR",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRepository": {"name": "repo", "nameWithOwner": "fork/repo"},
                            "headRepositoryOwner": {"login": "fork"},
                            "state": "OPEN",
                        }
                    ]
                ),
                command_result(f"{url}\n"),
                command_result(
                    {
                        "number": 43,
                        "url": url,
                        "title": "Add feature",
                        "headRefName": "feature",
                        "baseRefName": "main",
                        "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                        "headRepositoryOwner": {"login": "owner"},
                        "state": "OPEN",
                    }
                ),
            ]
        )

        result = create_or_get_pull_request(remote, request, runner)

        self.assertFalse(result.is_existing)
        self.assertEqual(result.number, 43)
        self.assertEqual(runner.calls[1][:4], ("gh", "pr", "create", "--repo"))

    def test_github_blocks_existing_pr_without_head_repository_metadata(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "number": 42,
                            "url": "https://github.com/owner/repo/pull/42",
                            "title": "Existing PR",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "state": "OPEN",
                        }
                    ]
                )
            ]
        )

        with self.assertRaisesRegex(PullRequestError, "without head repository metadata"):
            create_or_get_pull_request(remote, request, runner)

    def test_github_blocks_existing_same_repo_pr_without_complete_branch_metadata(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        cases = (
            {
                "number": 42,
                "url": "https://github.com/owner/repo/pull/42",
                "title": "Missing head branch",
                "baseRefName": "main",
                "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                "headRepositoryOwner": {"login": "owner"},
                "state": "OPEN",
            },
            {
                "number": 43,
                "url": "https://github.com/owner/repo/pull/43",
                "title": "Null base branch",
                "headRefName": "feature",
                "baseRefName": None,
                "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                "headRepositoryOwner": {"login": "owner"},
                "state": "OPEN",
            },
        )
        for candidate in cases:
            with self.subTest(title=candidate["title"]):
                runner = FakeRunner([command_result([candidate])])

                with self.assertRaisesRegex(PullRequestError, "without complete head/base branch metadata"):
                    create_or_get_pull_request(remote, request, runner)

                self.assertEqual(len(runner.calls), 1)

    def test_github_create_then_views_pull_request(self) -> None:
        remote = parse_pull_request_remote("origin", "git@github.com:owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        url = "https://github.com/owner/repo/pull/43"
        runner = FakeRunner(
            [
                command_result([]),
                command_result(f"{url}\n"),
                command_result(
                    {
                        "number": 43,
                        "url": url,
                        "title": "Add feature",
                        "headRefName": "feature",
                        "baseRefName": "main",
                        "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                        "headRepositoryOwner": {"login": "owner"},
                        "state": "OPEN",
                    }
                ),
            ]
        )

        result = create_or_get_pull_request(remote, request, runner)

        self.assertFalse(result.is_existing)
        self.assertEqual(result.number, 43)
        self.assertEqual(result.title, "Add feature")
        self.assertEqual(result.raw["state"], "OPEN")
        self.assertEqual(
            runner.calls[1],
            (
                "gh",
                "pr",
                "create",
                "--repo",
                "owner/repo",
                "--base",
                "main",
                "--head",
                "feature",
                "--title",
                "Add feature",
                "--body-file",
                "body.md",
            ),
        )
        self.assertEqual(
            runner.calls[2],
            (
                "gh",
                "pr",
                "view",
                url,
                "--repo",
                "owner/repo",
                "--json",
                "number,url,title,headRefName,baseRefName,state,headRepository,headRepositoryOwner",
            ),
        )

    def test_github_rejects_unsafe_pull_request_url_from_provider(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "number": 42,
                            "url": "javascript:alert(1)",
                            "title": "Existing PR",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                            "headRepositoryOwner": {"login": "owner"},
                            "state": "OPEN",
                        }
                    ]
                )
            ]
        )

        with self.assertRaisesRegex(PullRequestError, "unsafe pull request URL scheme"):
            create_or_get_pull_request(remote, request, runner)

    def test_github_rejects_pull_request_url_outside_selected_repository(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "number": 42,
                            "url": "https://attacker.example/owner/repo/pull/42",
                            "title": "Existing PR",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                            "headRepositoryOwner": {"login": "owner"},
                            "state": "OPEN",
                        }
                    ]
                )
            ]
        )

        with self.assertRaisesRegex(PullRequestError, "outside the selected GitHub repository"):
            create_or_get_pull_request(remote, request, runner)

    def test_github_rejects_credential_bearing_pull_request_url_from_provider(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "number": 42,
                            "url": "https://user:secret@github.com/owner/repo/pull/42",
                            "title": "Existing PR",
                            "headRefName": "feature",
                            "baseRefName": "main",
                            "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                            "headRepositoryOwner": {"login": "owner"},
                            "state": "OPEN",
                        }
                    ]
                )
            ]
        )

        with self.assertRaisesRegex(PullRequestError, "credential-bearing pull request URL"):
            create_or_get_pull_request(remote, request, runner)

        self.assertFalse(is_safe_pull_request_url("https://user:secret@github.com/owner/repo/pull/42"))

    def test_github_rejects_query_or_fragment_pull_request_url_from_provider(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        urls = (
            "https://github.com/owner/repo/pull/42?access_token=secret",
            "https://github.com/owner/repo/pull/42#token=secret",
        )
        for url in urls:
            with self.subTest(url=url):
                runner = FakeRunner(
                    [
                        command_result(
                            [
                                {
                                    "number": 42,
                                    "url": url,
                                    "title": "Existing PR",
                                    "headRefName": "feature",
                                    "baseRefName": "main",
                                    "headRepository": {"name": "repo", "nameWithOwner": "owner/repo"},
                                    "headRepositoryOwner": {"login": "owner"},
                                    "state": "OPEN",
                                }
                            ]
                        )
                    ]
                )

                with self.assertRaisesRegex(PullRequestError, "query or fragment"):
                    create_or_get_pull_request(remote, request, runner)

                self.assertFalse(is_safe_pull_request_url(url))

    def test_github_rejects_unsafe_create_url_before_viewing(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner([command_result([]), command_result("javascript:alert(1)\n")])

        with self.assertRaisesRegex(PullRequestError, "unsafe pull request URL scheme"):
            create_or_get_pull_request(remote, request, runner)
        self.assertEqual(len(runner.calls), 2)

    def test_github_rejects_credential_bearing_create_url_before_viewing(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner([command_result([]), command_result("https://user:secret@github.com/owner/repo/pull/43\n")])

        with self.assertRaisesRegex(PullRequestError, "credential-bearing pull request URL"):
            create_or_get_pull_request(remote, request, runner)
        self.assertEqual(len(runner.calls), 2)

    def test_github_rejects_query_or_fragment_create_url_before_viewing(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner([command_result([]), command_result("https://github.com/owner/repo/pull/43?token=secret\n")])

        with self.assertRaisesRegex(PullRequestError, "query or fragment"):
            create_or_get_pull_request(remote, request, runner)
        self.assertEqual(len(runner.calls), 2)

    def test_github_rejects_create_url_outside_selected_repository_before_viewing(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        request = PullRequestRequest(
            source_branch="feature",
            target_branch="main",
            title="Add feature",
            body_path=Path("body.md"),
        )
        runner = FakeRunner([command_result([]), command_result("https://github.com/other/repo/pull/43\n")])

        with self.assertRaisesRegex(PullRequestError, "outside the selected GitHub repository"):
            create_or_get_pull_request(remote, request, runner)
        self.assertEqual(len(runner.calls), 2)

    def test_azure_devops_reuses_existing_active_pull_request(self) -> None:
        remote = parse_pull_request_remote("origin", "https://dev.azure.com/org/project/_git/repo")
        assert remote is not None
        request = PullRequestRequest(source_branch="feature", target_branch="main", title="Add feature")
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "pullRequestId": 17,
                            "url": "https://dev.azure.com/org/project/_apis/git/repositories/repo/pullRequests/17",
                            "webUrl": "https://dev.azure.com/org/project/_git/repo/pullrequest/17",
                            "title": "Existing PR",
                            "status": "active",
                            "sourceRefName": "refs/heads/feature",
                            "targetRefName": "refs/heads/main",
                        }
                    ]
                )
            ]
        )

        result = create_or_get_pull_request(remote, request, runner)

        self.assertTrue(result.is_existing)
        self.assertEqual(result.provider, "azure-devops")
        self.assertEqual(result.number, 17)
        self.assertEqual(result.id, "17")
        self.assertEqual(result.url, "https://dev.azure.com/org/project/_git/repo/pullrequest/17")
        self.assertEqual(result.status, "active")
        self.assertEqual(result.source_branch, "feature")
        self.assertEqual(result.target_branch, "main")
        self.assertEqual(
            runner.calls,
            [
                (
                    "az",
                    "repos",
                    "pr",
                    "list",
                    "--org",
                    "https://dev.azure.com/org",
                    "--project",
                    "project",
                    "--repository",
                    "repo",
                    "--source-branch",
                    "refs/heads/feature",
                    "--target-branch",
                    "refs/heads/main",
                    "--status",
                    "active",
                    "--output",
                    "json",
                )
            ],
        )

    def test_azure_devops_create_pull_request(self) -> None:
        remote = parse_pull_request_remote("origin", "ssh://git@ssh.dev.azure.com/v3/org/project/repo")
        assert remote is not None
        sensitive_body = "Body text with token=do-not-leak"
        runner = FakeRunner(
            [
                command_result([]),
                command_result(
                    {
                        "pullRequestId": 18,
                        "url": "https://dev.azure.com/org/project/_apis/git/repositories/repo/pullRequests/18",
                        "_links": {
                            "web": {
                                "href": "https://dev.azure.com/org/project/_git/repo/pullrequest/18",
                            },
                        },
                        "title": "Add feature",
                        "status": "active",
                        "sourceRefName": "refs/heads/feature",
                        "targetRefName": "refs/heads/main",
                    }
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            body_path = Path(temp_dir) / "body.md"
            body_path.write_text(sensitive_body, encoding="utf-8")
            request = PullRequestRequest(
                source_branch="feature",
                target_branch="main",
                title="Add feature",
                body_path=body_path,
                description="Merge approval secret=do-not-leak",
            )

            result = create_or_get_pull_request(remote, request, runner)

        self.assertFalse(result.is_existing)
        self.assertEqual(result.number, 18)
        self.assertEqual(result.url, "https://dev.azure.com/org/project/_git/repo/pullrequest/18")
        create_args = runner.calls[1]
        description = create_args[create_args.index("--description") + 1]
        self.assertIn("agent-team orchestrator", description)
        self.assertNotIn("Body text", description)
        self.assertNotIn("secret=do-not-leak", description)
        self.assertFalse(any(sensitive_body in arg for arg in create_args))
        self.assertFalse(any("secret=do-not-leak" in arg for arg in create_args))
        self.assertEqual(
            create_args,
            (
                "az",
                "repos",
                "pr",
                "create",
                "--org",
                "https://dev.azure.com/org",
                "--project",
                "project",
                "--repository",
                "repo",
                "--source-branch",
                "refs/heads/feature",
                "--target-branch",
                "refs/heads/main",
                "--title",
                "Add feature",
                "--description",
                "Created by agent-team orchestrator. Full implementation context remains in local agent-team artifacts.",
                "--output",
                "json",
            ),
        )

    def test_azure_devops_rejects_pull_request_url_outside_selected_repository(self) -> None:
        remote = parse_pull_request_remote("origin", "https://dev.azure.com/org/project/_git/repo")
        assert remote is not None
        request = PullRequestRequest(source_branch="feature", target_branch="main", title="Add feature")
        runner = FakeRunner(
            [
                command_result(
                    [
                        {
                            "pullRequestId": 17,
                            "webUrl": "https://dev.azure.com/org/project/_git/other/pullrequest/17",
                            "title": "Existing PR",
                            "status": "active",
                            "sourceRefName": "refs/heads/feature",
                            "targetRefName": "refs/heads/main",
                        }
                    ]
                )
            ]
        )

        with self.assertRaisesRegex(PullRequestError, "outside the selected Azure DevOps Services repository"):
            create_or_get_pull_request(remote, request, runner)

    def test_azure_devops_does_not_read_or_pass_oversized_body(self) -> None:
        remote = parse_pull_request_remote("origin", "https://dev.azure.com/org/project/_git/repo")
        assert remote is not None
        runner = FakeRunner(
            [
                command_result([]),
                command_result(
                    {
                        "pullRequestId": 18,
                        "_links": {
                            "web": {
                                "href": "https://dev.azure.com/org/project/_git/repo/pullrequest/18",
                            },
                        },
                        "title": "Add feature",
                        "status": "active",
                        "sourceRefName": "refs/heads/feature",
                        "targetRefName": "refs/heads/main",
                    }
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            body_path = Path(temp_dir) / "body.md"
            body_path.write_text("sensitive " + ("x" * 50_000), encoding="utf-8")
            result = create_or_get_pull_request(
                remote,
                PullRequestRequest(
                    source_branch="feature",
                    target_branch="main",
                    title="Add feature",
                    body_path=body_path,
                ),
                runner,
            )

        self.assertFalse(result.is_existing)
        self.assertEqual(len(runner.calls), 2)
        self.assertFalse(any("sensitive" in arg or "x" * 100 in arg for call in runner.calls for arg in call))

    def test_cli_failure_surfaces_actionable_error(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        runner = FakeRunner([command_result("", returncode=1, stderr="not authenticated")])

        with self.assertRaisesRegex(PullRequestError, "gh pr list failed.*not authenticated.*gh auth login"):
            create_or_get_pull_request(
                remote,
                PullRequestRequest(
                    source_branch="feature",
                    target_branch="main",
                    title="Add feature",
                    body_path=Path("body.md"),
                ),
                runner,
            )

    def test_missing_provider_cli_surfaces_actionable_error(self) -> None:
        cases = (
            ("gh", "Install GitHub CLI"),
            ("az", "Install Azure CLI"),
        )
        for executable, expected_hint in cases:
            with self.subTest(executable=executable):
                with patch(
                    "agent_team.pull_requests.subprocess.run",
                    side_effect=FileNotFoundError(f"No such file or directory: '{executable}'"),
                ):
                    with self.assertRaisesRegex(PullRequestError, expected_hint):
                        SubprocessCommandRunner().run([executable, "--version"])

    def test_cli_failure_redacts_and_truncates_sensitive_output(self) -> None:
        remote = parse_pull_request_remote("origin", "https://github.com/owner/repo.git")
        assert remote is not None
        sensitive = (
            "failed https://user:secret@example.com/repo.git "
            "https://github.com/owner/repo.git?access_token=query-secret#password=fragment-secret "
            "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456 "
            "token=plain-secret "
            + ("x" * 3000)
        )
        runner = FakeRunner([command_result("", returncode=1, stderr=sensitive)])

        with self.assertRaises(PullRequestError) as caught:
            create_or_get_pull_request(
                remote,
                PullRequestRequest(
                    source_branch="feature",
                    target_branch="main",
                    title="Add feature",
                    body_path=Path("body.md"),
                ),
                runner,
            )

        message = str(caught.exception)
        self.assertIn("[redacted]", message)
        self.assertIn("[truncated]", message)
        self.assertNotIn("secret@example.com", message)
        self.assertNotIn("query-secret", message)
        self.assertNotIn("fragment-secret", message)
        self.assertNotIn("plain-secret", message)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", message)
        self.assertLess(len(message), 2300)

    def test_subprocess_runner_runs_provider_commands_non_interactively(self) -> None:
        completed = subprocess.CompletedProcess(args=["gh", "--version"], returncode=0, stdout="gh version\n", stderr="")
        with patch("agent_team.pull_requests.subprocess.run", return_value=completed) as run:
            result = SubprocessCommandRunner(timeout_seconds=7).run(["gh", "--version"])

        self.assertEqual(result.stdout, "gh version\n")
        run.assert_called_once()
        _, kwargs = run.call_args
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 7)
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        env = kwargs["env"]
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"], "no")

    def test_subprocess_runner_timeout_surfaces_pull_request_error(self) -> None:
        with patch(
            "agent_team.pull_requests.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh", "pr", "list"], timeout=1),
        ):
            with self.assertRaisesRegex(PullRequestError, "timed out after 1 seconds.*non-interactively"):
                SubprocessCommandRunner(timeout_seconds=1).run(["gh", "pr", "list"])

    def test_github_status_snapshot_detects_conflicts(self) -> None:
        metadata = {
            "provider": "github",
            "remote_name": "origin",
            "remote_identity": ["github", "owner", "repo"],
            "number": 7,
        }
        runner = FakeRunner(
            [
                command_result(
                    {
                        "number": 7,
                        "url": "https://github.com/owner/repo/pull/7",
                        "state": "OPEN",
                        "mergeable": "CONFLICTING",
                        "mergeStateStatus": "DIRTY",
                        "headRefName": "feature",
                        "baseRefName": "main",
                        "headRefOid": "a" * 40,
                    }
                )
            ]
        )

        snapshot = get_pull_request_status(metadata, runner)

        self.assertTrue(snapshot.is_open)
        self.assertTrue(snapshot.has_conflicts)
        self.assertFalse(snapshot.is_closed)
        self.assertEqual(snapshot.merge_state, "DIRTY")
        self.assertEqual(snapshot.head_sha, "a" * 40)
        self.assertEqual(
            runner.calls[0],
            (
                "gh",
                "pr",
                "view",
                "7",
                "--repo",
                "owner/repo",
                "--json",
                "number,url,state,mergedAt,closedAt,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,headRefOid",
            ),
        )

    def test_github_status_snapshot_closes_merged_pr(self) -> None:
        metadata = {
            "provider": "github",
            "remote_name": "origin",
            "remote_identity": ["github", "owner", "repo"],
            "url": "https://github.com/owner/repo/pull/7",
        }
        runner = FakeRunner(
            [
                command_result(
                    {
                        "number": 7,
                        "url": "https://github.com/owner/repo/pull/7",
                        "state": "MERGED",
                        "mergedAt": "2026-01-01T00:00:00Z",
                        "closedAt": "2026-01-01T00:00:00Z",
                        "mergeable": "MERGEABLE",
                        "headRefName": "feature",
                        "baseRefName": "main",
                    }
                )
            ]
        )

        snapshot = get_pull_request_status(metadata, runner)

        self.assertTrue(snapshot.is_closed)
        self.assertTrue(snapshot.is_merged)
        self.assertFalse(snapshot.has_conflicts)
        self.assertEqual(snapshot.merged_at, "2026-01-01T00:00:00Z")

    def test_github_status_snapshot_closes_unmerged_pr(self) -> None:
        metadata = {
            "provider": "github",
            "remote_name": "origin",
            "remote_identity": ["github", "owner", "repo"],
            "number": 7,
        }
        runner = FakeRunner(
            [
                command_result(
                    {
                        "number": 7,
                        "url": "https://github.com/owner/repo/pull/7",
                        "state": "CLOSED",
                        "closedAt": "2026-01-01T00:00:00Z",
                        "mergeable": "UNKNOWN",
                        "mergeStateStatus": "UNKNOWN",
                        "headRefName": "feature",
                        "baseRefName": "main",
                        "headRefOid": "b" * 40,
                    }
                )
            ]
        )

        snapshot = get_pull_request_status(metadata, runner)
        updates = snapshot.metadata_updates()

        self.assertFalse(snapshot.is_open)
        self.assertTrue(snapshot.is_closed)
        self.assertFalse(snapshot.is_merged)
        self.assertFalse(snapshot.has_conflicts)
        self.assertEqual(snapshot.status, "CLOSED")
        self.assertEqual(snapshot.closed_at, "2026-01-01T00:00:00Z")
        self.assertEqual(snapshot.head_sha, "b" * 40)
        self.assertEqual(updates["final_status"], "closed")
        self.assertEqual(updates["last_head_commit"], "b" * 40)
        self.assertNotIn("head_commit", updates)

    def test_azure_devops_status_snapshot_distinguishes_policy_failure_from_conflict(self) -> None:
        metadata = {
            "provider": "azure-devops",
            "remote_name": "origin",
            "remote_identity": ["azure-devops", "org", "project", "repo"],
            "id": "17",
        }
        runner = FakeRunner(
            [
                command_result(
                    {
                        "pullRequestId": 17,
                        "status": "active",
                        "mergeStatus": "rejectedByPolicy",
                        "sourceRefName": "refs/heads/feature",
                        "targetRefName": "refs/heads/main",
                        "lastMergeSourceCommit": {"commitId": "b" * 40},
                        "webUrl": "https://dev.azure.com/org/project/_git/repo/pullrequest/17",
                    }
                )
            ]
        )

        snapshot = get_pull_request_status(metadata, runner)

        self.assertTrue(snapshot.is_open)
        self.assertFalse(snapshot.has_conflicts)
        self.assertEqual(snapshot.merge_state, "rejectedbypolicy")
        self.assertEqual(snapshot.head_sha, "b" * 40)
        self.assertEqual(
            runner.calls[0],
            (
                "az",
                "repos",
                "pr",
                "show",
                "--id",
                "17",
                "--org",
                "https://dev.azure.com/org",
                "--output",
                "json",
            ),
        )

    def test_azure_devops_status_snapshot_detects_conflict(self) -> None:
        metadata = {
            "provider": "azure-devops",
            "remote_name": "origin",
            "remote_identity": ["azure-devops", "org", "project", "repo"],
            "id": "17",
        }
        runner = FakeRunner(
            [
                command_result(
                    {
                        "pullRequestId": 17,
                        "status": "active",
                        "mergeStatus": "conflicts",
                        "sourceRefName": "refs/heads/feature",
                        "targetRefName": "refs/heads/main",
                        "webUrl": "https://dev.azure.com/org/project/_git/repo/pullrequest/17",
                    }
                )
            ]
        )

        snapshot = get_pull_request_status(metadata, runner)

        self.assertTrue(snapshot.has_conflicts)
        self.assertTrue(snapshot.is_open)

    def test_azure_devops_status_snapshot_closes_completed_pr_as_merged(self) -> None:
        metadata = {
            "provider": "azure-devops",
            "remote_name": "origin",
            "remote_identity": ["azure-devops", "org", "project", "repo"],
            "id": "17",
        }
        runner = FakeRunner(
            [
                command_result(
                    {
                        "pullRequestId": 17,
                        "status": "completed",
                        "mergeStatus": "succeeded",
                        "closedDate": "2026-01-01T00:00:00Z",
                        "sourceRefName": "refs/heads/feature",
                        "targetRefName": "refs/heads/main",
                        "lastMergeSourceCommit": {"commitId": "c" * 40},
                        "webUrl": "https://dev.azure.com/org/project/_git/repo/pullrequest/17",
                    }
                )
            ]
        )

        snapshot = get_pull_request_status(metadata, runner)
        updates = snapshot.metadata_updates()

        self.assertFalse(snapshot.is_open)
        self.assertTrue(snapshot.is_closed)
        self.assertTrue(snapshot.is_merged)
        self.assertFalse(snapshot.has_conflicts)
        self.assertEqual(snapshot.status, "completed")
        self.assertEqual(snapshot.merge_state, "succeeded")
        self.assertEqual(snapshot.closed_at, "2026-01-01T00:00:00Z")
        self.assertEqual(snapshot.head_sha, "c" * 40)
        self.assertEqual(snapshot.source_branch, "feature")
        self.assertEqual(snapshot.target_branch, "main")
        self.assertEqual(updates["final_status"], "merged")
        self.assertEqual(updates["last_is_closed"], True)
        self.assertEqual(updates["last_is_merged"], True)
        self.assertEqual(updates["closed_at"], "2026-01-01T00:00:00Z")

    def test_azure_devops_status_snapshot_closes_abandoned_pr_without_merge(self) -> None:
        metadata = {
            "provider": "azure-devops",
            "remote_name": "origin",
            "remote_identity": ["azure-devops", "org", "project", "repo"],
            "id": "17",
        }
        runner = FakeRunner(
            [
                command_result(
                    {
                        "pullRequestId": 17,
                        "status": "abandoned",
                        "mergeStatus": "notSet",
                        "closedDate": "2026-01-01T00:00:00Z",
                        "sourceRefName": "refs/heads/feature",
                        "targetRefName": "refs/heads/main",
                        "sourceCommitId": "d" * 40,
                        "webUrl": "https://dev.azure.com/org/project/_git/repo/pullrequest/17",
                    }
                )
            ]
        )

        snapshot = get_pull_request_status(metadata, runner)
        updates = snapshot.metadata_updates()

        self.assertFalse(snapshot.is_open)
        self.assertTrue(snapshot.is_closed)
        self.assertFalse(snapshot.is_merged)
        self.assertFalse(snapshot.has_conflicts)
        self.assertEqual(snapshot.status, "abandoned")
        self.assertEqual(snapshot.merge_state, "notset")
        self.assertEqual(snapshot.closed_at, "2026-01-01T00:00:00Z")
        self.assertEqual(snapshot.head_sha, "d" * 40)
        self.assertEqual(snapshot.source_branch, "feature")
        self.assertEqual(snapshot.target_branch, "main")
        self.assertEqual(updates["final_status"], "closed")
        self.assertEqual(updates["last_is_closed"], True)
        self.assertEqual(updates["last_is_merged"], False)
        self.assertEqual(updates["closed_at"], "2026-01-01T00:00:00Z")

    def test_github_conflict_comment_updates_existing_marker(self) -> None:
        metadata = {
            "provider": "github",
            "remote_name": "origin",
            "remote_identity": ["github", "owner", "repo"],
            "number": 7,
            "conflict_comment_marker": "<!-- marker -->",
        }
        runner = FakeRunner(
            [
                command_result([[{"id": 99, "body": "prior\n<!-- marker -->"}]]),
                command_result({"id": 99, "html_url": "https://github.com/owner/repo/issues/7#issuecomment-99"}),
            ]
        )

        result = ensure_pull_request_conflict_comment(metadata, "<!-- marker -->\nupdated", runner)

        self.assertFalse(result.created)
        self.assertEqual(result.id, "99")
        self.assertEqual(runner.calls[1][:5], ("gh", "api", "repos/owner/repo/issues/comments/99", "-X", "PATCH"))

    def test_azure_devops_conflict_comment_creates_thread_when_marker_missing(self) -> None:
        metadata = {
            "provider": "azure-devops",
            "remote_name": "origin",
            "remote_identity": ["azure-devops", "org", "project", "repo"],
            "id": "17",
            "conflict_comment_marker": "<!-- marker -->",
        }
        runner = FakeRunner(
            [
                command_result({"value": []}),
                command_result({"id": 123, "comments": [{"id": 1, "content": "<!-- marker -->\nbody"}]}),
            ]
        )

        result = ensure_pull_request_conflict_comment(metadata, "<!-- marker -->\nbody", runner)

        self.assertTrue(result.created)
        self.assertEqual(result.id, "1")
        threads_url = "https://dev.azure.com/org/project/_apis/git/repositories/repo/pullRequests/17/threads?api-version=7.1"
        resource = "499b84ac-1321-427f-aa17-267ca6975798"
        self.assertEqual(
            runner.calls[0],
            ("az", "rest", "--method", "get", "--url", threads_url, "--resource", resource),
        )
        self.assertEqual(
            runner.calls[1][:8],
            ("az", "rest", "--method", "post", "--url", threads_url, "--resource", resource),
        )
        created_body = json.loads(runner.calls[1][runner.calls[1].index("--body") + 1])
        self.assertEqual(created_body["status"], "active")
        self.assertEqual(created_body["comments"][0]["content"], "<!-- marker -->\nbody")

    def test_azure_devops_conflict_comment_updates_existing_marker_with_resource(self) -> None:
        metadata = {
            "provider": "azure-devops",
            "remote_name": "origin",
            "remote_identity": ["azure-devops", "org", "project", "repo"],
            "id": "17",
            "conflict_comment_marker": "<!-- marker -->",
        }
        runner = FakeRunner(
            [
                command_result({"value": [{"id": 123, "comments": [{"id": 4, "content": "prior\n<!-- marker -->"}]}]}),
                command_result({"id": 4, "content": "<!-- marker -->\nupdated"}),
            ]
        )

        result = ensure_pull_request_conflict_comment(metadata, "<!-- marker -->\nupdated", runner)

        self.assertFalse(result.created)
        self.assertEqual(result.id, "4")
        resource = "499b84ac-1321-427f-aa17-267ca6975798"
        update_url = (
            "https://dev.azure.com/org/project/_apis/git/repositories/repo/pullRequests/17/"
            "threads/123/comments/4?api-version=7.1"
        )
        self.assertEqual(
            runner.calls[1][:8],
            ("az", "rest", "--method", "patch", "--url", update_url, "--resource", resource),
        )
        updated_body = json.loads(runner.calls[1][runner.calls[1].index("--body") + 1])
        self.assertEqual(updated_body["content"], "<!-- marker -->\nupdated")

    def test_pull_request_remote_from_metadata_reconstructs_identity_without_remote_url(self) -> None:
        remote = pull_request_remote_from_metadata(
            {
                "provider": "github",
                "remote_name": "origin",
                "remote_identity": ["github", "Owner", "Repo"],
            }
        )

        self.assertEqual(remote.provider, "github")
        self.assertEqual(remote.owner, "owner")
        self.assertEqual(remote.repo, "repo")

    def test_unsupported_provider_is_explicit(self) -> None:
        remote = PullRequestRemote(provider="unsupported", remote_name="origin", url="local://repo", repo="repo")

        with self.assertRaisesRegex(PullRequestError, "Unsupported pull request provider"):
            create_or_get_pull_request(
                remote,
                PullRequestRequest(source_branch="feature", target_branch="main", title="Add feature"),
                FakeRunner([]),
            )


if __name__ == "__main__":
    unittest.main()
