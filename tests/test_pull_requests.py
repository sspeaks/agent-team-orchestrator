from __future__ import annotations

import json
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
    is_safe_pull_request_url,
    parse_azure_devops_remote,
    parse_github_remote,
    parse_pull_request_remote,
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
                    "number,url,title,headRefName,baseRefName,state",
                )
            ],
        )

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
                "number,url,title,headRefName,baseRefName,state",
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
                            "state": "OPEN",
                        }
                    ]
                )
            ]
        )

        with self.assertRaisesRegex(PullRequestError, "unsafe pull request URL scheme"):
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
                            "state": "OPEN",
                        }
                    ]
                )
            ]
        )

        with self.assertRaisesRegex(PullRequestError, "credential-bearing pull request URL"):
            create_or_get_pull_request(remote, request, runner)

        self.assertFalse(is_safe_pull_request_url("https://user:secret@github.com/owner/repo/pull/42"))

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
        self.assertNotIn("plain-secret", message)
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz123456", message)
        self.assertLess(len(message), 2300)

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
