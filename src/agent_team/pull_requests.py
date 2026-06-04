from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import quote, unquote, urlparse, urlunparse


class PullRequestError(RuntimeError):
    """Raised when pull request provider commands fail."""


@dataclass(frozen=True)
class PullRequestRemote:
    provider: str
    remote_name: str
    url: str
    repo: str
    owner: str | None = None
    org: str | None = None
    project: str | None = None


@dataclass(frozen=True)
class PullRequestRequest:
    source_branch: str
    target_branch: str
    title: str
    body_path: str | Path | None = None
    description: str = ""


@dataclass(frozen=True)
class PullRequestResult:
    provider: str
    remote_name: str
    source_branch: str
    target_branch: str
    title: str
    url: str
    id: str | None
    number: int | None
    status: str
    is_existing: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class PullRequestStatusSnapshot:
    provider: str
    checked_at: str
    status: str
    merge_state: str | None
    is_open: bool
    is_closed: bool
    is_merged: bool
    has_conflicts: bool
    head_sha: str | None
    url: str
    raw: dict[str, Any]
    id: str | None = None
    number: int | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    closed_at: str | None = None
    merged_at: str | None = None

    def metadata_updates(self) -> dict[str, Any]:
        final_status = None
        if self.is_merged:
            final_status = "merged"
        elif self.is_closed:
            final_status = "closed"
        updates: dict[str, Any] = {
            "last_status_check_at": self.checked_at,
            "last_status": self.status,
            "last_merge_state": self.merge_state,
            "last_head_commit": self.head_sha,
            "last_is_open": self.is_open,
            "last_is_closed": self.is_closed,
            "last_is_merged": self.is_merged,
            "last_has_conflicts": self.has_conflicts,
            "pr_status": self.status,
            "final_status": final_status,
            "raw_status": self.raw,
        }
        if self.closed_at is not None:
            updates["closed_at"] = self.closed_at
        if self.merged_at is not None:
            updates["merged_at"] = self.merged_at
        return updates


@dataclass(frozen=True)
class PullRequestCommentResult:
    provider: str
    id: str | None
    url: str | None
    marker: str
    body: str
    created: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, args: Sequence[str]) -> CommandResult:
        ...


_DEFAULT_PROVIDER_COMMAND_TIMEOUT_SECONDS = 120.0
_NONINTERACTIVE_ENV = {
    "GH_PROMPT_DISABLED": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no",
}
_ADO_REST_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


class SubprocessCommandRunner:
    def __init__(self, timeout_seconds: float = _DEFAULT_PROVIDER_COMMAND_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")
        self.timeout_seconds = timeout_seconds

    def run(self, args: Sequence[str]) -> CommandResult:
        command = [str(arg) for arg in args]
        if not command:
            raise PullRequestError("Pull request provider command was empty.")
        env = os.environ.copy()
        env.update(_NONINTERACTIVE_ENV)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=env,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            executable = Path(command[0]).name or command[0]
            raise PullRequestError(
                f"Pull request command '{executable}' timed out after {self.timeout_seconds:g} seconds. "
                "Verify provider CLI authentication is configured non-interactively before retrying."
            ) from exc
        except OSError as exc:
            executable = Path(command[0]).name or command[0]
            raise PullRequestError(_command_launch_error(executable, exc)) from exc
        return CommandResult(
            args=tuple(command),
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


SAFE_PULL_REQUEST_DESCRIPTION = (
    "Created by agent-team orchestrator. Full implementation context remains in local agent-team artifacts."
)
_COMMAND_OUTPUT_MAX_CHARS = 2_000
_GITHUB_SCP_RE = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
_ADO_SCP_RE = re.compile(
    r"^(?:git@)?ssh\.dev\.azure\.com:v3/"
    r"(?P<org>[^/]+)/(?P<project>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
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


def parse_pull_request_remote(remote_name: str, remote_url: str) -> PullRequestRemote | None:
    if _url_embeds_disallowed_credentials(remote_url):
        return None
    return parse_github_remote(remote_name, remote_url) or parse_azure_devops_remote(remote_name, remote_url)


def parse_github_remote(remote_name: str, remote_url: str) -> PullRequestRemote | None:
    url = remote_url.strip()
    if _has_query_or_fragment(url):
        return None
    if _url_embeds_disallowed_credentials(url):
        return None
    match = _GITHUB_SCP_RE.match(url)
    if match:
        return PullRequestRemote(
            provider="github",
            remote_name=remote_name,
            url=remote_url,
            owner=match.group("owner"),
            repo=_strip_dot_git(match.group("repo")),
        )

    parsed = urlparse(url)
    if parsed.scheme not in {"https", "ssh"} or (parsed.hostname or "").lower() != "github.com":
        return None
    if parsed.scheme == "ssh" and parsed.username not in {None, "git"}:
        return None

    parts = _path_parts(parsed.path)
    if len(parts) != 2:
        return None
    owner, repo = parts
    repo = _strip_dot_git(repo)
    if not owner or not repo:
        return None
    return PullRequestRemote(
        provider="github",
        remote_name=remote_name,
        url=remote_url,
        owner=owner,
        repo=repo,
    )


def parse_azure_devops_remote(remote_name: str, remote_url: str) -> PullRequestRemote | None:
    url = remote_url.strip()
    if _has_query_or_fragment(url):
        return None
    if _url_embeds_disallowed_credentials(url):
        return None
    match = _ADO_SCP_RE.match(url)
    if match:
        return _ado_remote(
            remote_name,
            remote_url,
            org=match.group("org"),
            project=match.group("project"),
            repo=match.group("repo"),
        )

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = _path_parts(parsed.path)

    if parsed.scheme == "https" and host == "dev.azure.com":
        if len(parts) == 4 and parts[2] == "_git":
            return _ado_remote(remote_name, remote_url, org=parts[0], project=parts[1], repo=parts[3])
        return None

    if parsed.scheme == "https" and host.endswith(".visualstudio.com") and host != "visualstudio.com":
        if len(parts) == 3 and parts[1] == "_git":
            org = host[: -len(".visualstudio.com")]
            return _ado_remote(remote_name, remote_url, org=org, project=parts[0], repo=parts[2])
        return None

    if parsed.scheme == "ssh" and host == "ssh.dev.azure.com":
        if parsed.username not in {None, "git"}:
            return None
        if len(parts) == 4 and parts[0] == "v3":
            return _ado_remote(remote_name, remote_url, org=parts[1], project=parts[2], repo=parts[3])

    return None


def create_or_get_pull_request(
    remote: PullRequestRemote,
    request: PullRequestRequest,
    runner: CommandRunner | None = None,
) -> PullRequestResult:
    active_runner = runner or SubprocessCommandRunner()
    if remote.provider == "github":
        return _github_create_or_get(remote, request, active_runner)
    if remote.provider == "azure-devops":
        return _ado_create_or_get(remote, request, active_runner)
    raise PullRequestError(
        f"Unsupported pull request provider '{remote.provider}' for remote '{remote.remote_name}'. "
        "Supported providers are GitHub and Azure DevOps Services."
    )


def pull_request_remote_from_metadata(metadata: dict[str, Any]) -> PullRequestRemote:
    provider = _clean_json_string(metadata.get("provider"))
    remote_name = _clean_json_string(metadata.get("remote_name")) or "origin"
    remote_url = _clean_json_string(metadata.get("remote_url"))
    identity = _metadata_identity(metadata)
    if identity is not None:
        if identity[0] == "github" and len(identity) == 3:
            return PullRequestRemote(
                provider="github",
                remote_name=remote_name,
                url=remote_url or f"https://github.com/{identity[1]}/{identity[2]}.git",
                owner=identity[1],
                repo=identity[2],
            )
        if identity[0] == "azure-devops" and len(identity) == 4:
            return PullRequestRemote(
                provider="azure-devops",
                remote_name=remote_name,
                url=remote_url or f"https://dev.azure.com/{identity[1]}/{identity[2]}/_git/{identity[3]}",
                org=identity[1],
                project=identity[2],
                repo=identity[3],
            )
    if remote_url:
        remote = parse_pull_request_remote(remote_name, remote_url)
        if remote is not None and (provider is None or provider == remote.provider):
            return remote
    raise PullRequestError("Pull request metadata does not contain a supported provider identity.")


def get_pull_request_status(
    metadata: dict[str, Any],
    runner: CommandRunner | None = None,
) -> PullRequestStatusSnapshot:
    remote = pull_request_remote_from_metadata(metadata)
    active_runner = runner or SubprocessCommandRunner()
    if remote.provider == "github":
        return _github_status_snapshot(remote, metadata, active_runner)
    if remote.provider == "azure-devops":
        return _ado_status_snapshot(remote, metadata, active_runner)
    raise PullRequestError(
        f"Unsupported pull request provider '{remote.provider}' for remote '{remote.remote_name}'. "
        "Supported providers are GitHub and Azure DevOps Services."
    )


def pull_request_conflict_marker(metadata: dict[str, Any]) -> str:
    stored = _clean_json_string(metadata.get("conflict_comment_marker"))
    if stored:
        return stored
    key = _clean_json_string(metadata.get("conflict_comment_key"))
    if key is None:
        provider = _clean_json_string(metadata.get("provider")) or "provider"
        source_branch = _clean_json_string(metadata.get("source_branch")) or "source"
        pr_id = _clean_json_string(metadata.get("id")) or str(_as_int(metadata.get("number")) or "unknown")
        key = f"{provider}:{pr_id}:{source_branch}"
    return f"<!-- agent-team-orchestrator-conflict:{key} -->"


def ensure_pull_request_conflict_comment(
    metadata: dict[str, Any],
    body: str,
    runner: CommandRunner | None = None,
) -> PullRequestCommentResult:
    remote = pull_request_remote_from_metadata(metadata)
    marker = pull_request_conflict_marker(metadata)
    comment_body = body if marker in body else f"{marker}\n\n{body.strip()}"
    active_runner = runner or SubprocessCommandRunner()
    if remote.provider == "github":
        return _github_conflict_comment(remote, metadata, marker, comment_body, active_runner)
    if remote.provider == "azure-devops":
        return _ado_conflict_comment(remote, metadata, marker, comment_body, active_runner)
    raise PullRequestError(
        f"Unsupported pull request provider '{remote.provider}' for remote '{remote.remote_name}'. "
        "Supported providers are GitHub and Azure DevOps Services."
    )


def is_safe_pull_request_url(url: object) -> bool:
    text = "" if url is None else str(url).strip()
    if not text:
        return False
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _github_create_or_get(
    remote: PullRequestRemote,
    request: PullRequestRequest,
    runner: CommandRunner,
) -> PullRequestResult:
    repo = _github_repo(remote)
    list_args = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--head",
        request.source_branch,
        "--base",
        request.target_branch,
        "--state",
        "open",
        "--json",
        "number,url,title,headRefName,baseRefName,state,headRepository,headRepositoryOwner",
    ]
    listed = runner.run(list_args)
    _ensure_success("gh pr list", listed, "Run 'gh auth login' and verify the repository is accessible.")
    existing = _github_existing_pull_request(remote, request, _load_json(listed.stdout, "gh pr list"))
    if existing is not None:
        return _github_result(remote, request, existing, is_existing=True)

    if request.body_path is None:
        raise PullRequestError("GitHub pull request creation requires PullRequestRequest.body_path for --body-file.")

    created = runner.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            request.target_branch,
            "--head",
            request.source_branch,
            "--title",
            request.title,
            "--body-file",
            str(request.body_path),
        ]
    )
    _ensure_success("gh pr create", created, "Run 'gh auth login' and verify the branch was pushed.")
    url = _first_non_empty_line(created.stdout)
    if not url:
        raise PullRequestError("gh pr create succeeded but did not return a pull request URL.")
    _validate_github_pull_request_url(remote, url, "gh pr create")

    viewed = runner.run(
        [
            "gh",
            "pr",
            "view",
            url,
            "--repo",
            repo,
            "--json",
            "number,url,title,headRefName,baseRefName,state,headRepository,headRepositoryOwner",
        ]
    )
    _ensure_success("gh pr view", viewed, "Verify the created pull request URL is accessible.")
    raw = _expect_mapping(_load_json(viewed.stdout, "gh pr view"), "gh pr view")
    return _github_result(remote, request, raw, is_existing=False)


def _ado_create_or_get(
    remote: PullRequestRemote,
    request: PullRequestRequest,
    runner: CommandRunner,
) -> PullRequestResult:
    org_url = _ado_org_url(remote)
    source_branch = _ado_branch(request.source_branch)
    target_branch = _ado_branch(request.target_branch)
    list_args = [
        "az",
        "repos",
        "pr",
        "list",
        "--org",
        org_url,
        "--project",
        _require(remote.project, "project", remote),
        "--repository",
        remote.repo,
        "--source-branch",
        source_branch,
        "--target-branch",
        target_branch,
        "--status",
        "active",
        "--output",
        "json",
    ]
    listed = runner.run(list_args)
    _ensure_success(
        "az repos pr list",
        listed,
        "Run 'az login' and install/configure the azure-devops CLI extension.",
    )
    existing = _first_mapping_from_list(_load_json(listed.stdout, "az repos pr list"), "az repos pr list")
    if existing is not None:
        return _ado_result(remote, request, existing, is_existing=True)

    created = runner.run(
        [
            "az",
            "repos",
            "pr",
            "create",
            "--org",
            org_url,
            "--project",
            _require(remote.project, "project", remote),
            "--repository",
            remote.repo,
            "--source-branch",
            source_branch,
            "--target-branch",
            target_branch,
            "--title",
            request.title,
            "--description",
            SAFE_PULL_REQUEST_DESCRIPTION,
            "--output",
            "json",
        ]
    )
    _ensure_success(
        "az repos pr create",
        created,
        "Run 'az login' and verify the branch was pushed to Azure DevOps Services.",
    )
    raw = _expect_mapping(_load_json(created.stdout, "az repos pr create"), "az repos pr create")
    return _ado_result(remote, request, raw, is_existing=False)


def _github_status_snapshot(
    remote: PullRequestRemote,
    metadata: dict[str, Any],
    runner: CommandRunner,
) -> PullRequestStatusSnapshot:
    repo = _github_repo(remote)
    pr_ref = _github_pr_ref(metadata)
    fields = "number,url,state,mergedAt,closedAt,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,headRefOid"
    viewed = runner.run(["gh", "pr", "view", pr_ref, "--repo", repo, "--json", fields])
    _ensure_success("gh pr view", viewed, "Run 'gh auth login' and verify the pull request is accessible.")
    raw = _expect_mapping(_load_json(viewed.stdout, "gh pr view"), "gh pr view")
    state = str(raw.get("state") or "").upper()
    mergeable = str(raw.get("mergeable") or "").upper() or None
    merge_state_status = str(raw.get("mergeStateStatus") or "").upper() or None
    merged_at = _clean_json_string(raw.get("mergedAt"))
    closed_at = _clean_json_string(raw.get("closedAt"))
    is_merged = state == "MERGED" or merged_at is not None
    is_open = state == "OPEN"
    is_closed = is_merged or state == "CLOSED" or closed_at is not None
    has_conflicts = mergeable == "CONFLICTING" or merge_state_status in {"DIRTY", "CONFLICTING", "HAS_CONFLICTS"}
    url = _validated_github_result_url(remote, raw.get("url") or metadata.get("url"), "gh pr view")
    return PullRequestStatusSnapshot(
        provider=remote.provider,
        checked_at=_utc_now_iso(),
        status=state or str(raw.get("state") or ""),
        merge_state=merge_state_status or mergeable,
        is_open=is_open,
        is_closed=is_closed,
        is_merged=is_merged,
        has_conflicts=has_conflicts,
        head_sha=_clean_json_string(raw.get("headRefOid")),
        url=url,
        raw=dict(raw),
        id=str(_as_int(raw.get("number"))) if _as_int(raw.get("number")) is not None else None,
        number=_as_int(raw.get("number")),
        source_branch=_clean_json_string(raw.get("headRefName")),
        target_branch=_clean_json_string(raw.get("baseRefName")),
        closed_at=closed_at,
        merged_at=merged_at,
    )


def _ado_status_snapshot(
    remote: PullRequestRemote,
    metadata: dict[str, Any],
    runner: CommandRunner,
) -> PullRequestStatusSnapshot:
    pr_id = _ado_pr_id(metadata)
    shown = runner.run(_ado_rest_args("get", _ado_pull_request_url(remote, pr_id)))
    _ensure_success(
        "az rest pull request",
        shown,
        "Run 'az login' and verify the Azure DevOps pull request is accessible.",
    )
    raw = _expect_mapping(_load_json(shown.stdout, "az rest pull request"), "az rest pull request")
    status = str(raw.get("status") or "").lower()
    merge_status = str(raw.get("mergeStatus") or "").lower() or None
    is_open = status == "active"
    is_merged = status == "completed"
    is_closed = status in {"completed", "abandoned"}
    url = _validated_ado_result_url(remote, _ado_result_url(raw) or metadata.get("url"), "az rest pull request")
    return PullRequestStatusSnapshot(
        provider=remote.provider,
        checked_at=_utc_now_iso(),
        status=status,
        merge_state=merge_status,
        is_open=is_open,
        is_closed=is_closed,
        is_merged=is_merged,
        has_conflicts=merge_status == "conflicts",
        head_sha=_ado_commit_id(raw.get("lastMergeSourceCommit")) or _clean_json_string(raw.get("sourceCommitId")),
        url=url,
        raw=dict(raw),
        id=str(_as_int(raw.get("pullRequestId"))) if _as_int(raw.get("pullRequestId")) is not None else pr_id,
        number=_as_int(raw.get("pullRequestId")),
        source_branch=_strip_heads(str(raw.get("sourceRefName") or "")) or None,
        target_branch=_strip_heads(str(raw.get("targetRefName") or "")) or None,
        closed_at=_clean_json_string(raw.get("closedDate")),
        merged_at=_clean_json_string(raw.get("closedDate")) if is_merged else None,
    )


def _github_conflict_comment(
    remote: PullRequestRemote,
    metadata: dict[str, Any],
    marker: str,
    body: str,
    runner: CommandRunner,
) -> PullRequestCommentResult:
    number = _github_pr_number(metadata)
    repo = _github_repo(remote)
    comments = runner.run(["gh", "api", f"repos/{repo}/issues/{number}/comments", "--paginate", "--slurp"])
    _ensure_success("gh api issue comments", comments, "Verify GitHub CLI can read pull request comments.")
    existing = _find_marked_github_comment(_load_json(comments.stdout, "gh api issue comments"), marker)
    if existing is not None:
        comment_id = str(existing["id"])
        updated = runner.run(["gh", "api", f"repos/{repo}/issues/comments/{comment_id}", "-X", "PATCH", "-f", f"body={body}"])
        _ensure_success("gh api update issue comment", updated, "Verify GitHub CLI can update pull request comments.")
        raw = _expect_mapping(_load_json(updated.stdout, "gh api update issue comment"), "gh api update issue comment")
        return PullRequestCommentResult(
            provider=remote.provider,
            id=str(raw.get("id") or comment_id),
            url=_safe_url_or_none(raw.get("html_url")),
            marker=marker,
            body=body,
            created=False,
            raw=raw,
        )
    created = runner.run(["gh", "api", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])
    _ensure_success("gh api create issue comment", created, "Verify GitHub CLI can create pull request comments.")
    raw = _expect_mapping(_load_json(created.stdout, "gh api create issue comment"), "gh api create issue comment")
    return PullRequestCommentResult(
        provider=remote.provider,
        id=str(raw.get("id")) if raw.get("id") is not None else None,
        url=_safe_url_or_none(raw.get("html_url")),
        marker=marker,
        body=body,
        created=True,
        raw=raw,
    )


def _ado_conflict_comment(
    remote: PullRequestRemote,
    metadata: dict[str, Any],
    marker: str,
    body: str,
    runner: CommandRunner,
) -> PullRequestCommentResult:
    pr_id = _ado_pr_id(metadata)
    threads_url = _ado_threads_url(remote, pr_id)
    listed = runner.run(_ado_rest_args("get", threads_url))
    _ensure_success("az rest pull request threads", listed, "Verify Azure CLI can read pull request threads.")
    existing = _find_marked_ado_comment(_load_json(listed.stdout, "az rest pull request threads"), marker)
    if existing is not None:
        thread_id, comment_id = existing
        update_url = _ado_thread_comment_url(remote, pr_id, thread_id, comment_id)
        updated = runner.run(
            [
                *_ado_rest_args("patch", update_url),
                "--body",
                json.dumps({"content": body}),
            ]
        )
        _ensure_success("az rest update pull request comment", updated, "Verify Azure CLI can update pull request threads.")
        raw = _expect_mapping(
            _load_json(updated.stdout, "az rest update pull request comment"),
            "az rest update pull request comment",
        )
        return PullRequestCommentResult(
            provider=remote.provider,
            id=str(raw.get("id") or comment_id),
            url=None,
            marker=marker,
            body=body,
            created=False,
            raw=raw,
        )
    created = runner.run(
        [
            *_ado_rest_args("post", threads_url),
            "--body",
            json.dumps({"comments": [{"parentCommentId": 0, "content": body}], "status": "active"}),
        ]
    )
    _ensure_success("az rest create pull request thread", created, "Verify Azure CLI can create pull request threads.")
    raw = _expect_mapping(
        _load_json(created.stdout, "az rest create pull request thread"),
        "az rest create pull request thread",
    )
    comment_id = _first_ado_comment_id(raw)
    return PullRequestCommentResult(
        provider=remote.provider,
        id=str(comment_id) if comment_id is not None else (str(raw.get("id")) if raw.get("id") is not None else None),
        url=None,
        marker=marker,
        body=body,
        created=True,
        raw=raw,
    )


def _github_result(
    remote: PullRequestRemote,
    request: PullRequestRequest,
    raw: dict[str, Any],
    *,
    is_existing: bool,
) -> PullRequestResult:
    number = _as_int(raw.get("number"))
    url = _validated_github_result_url(remote, raw.get("url"), "gh pr result")
    return PullRequestResult(
        provider=remote.provider,
        remote_name=remote.remote_name,
        source_branch=str(raw.get("headRefName") or request.source_branch),
        target_branch=str(raw.get("baseRefName") or request.target_branch),
        title=str(raw.get("title") or request.title),
        url=url,
        id=str(number) if number is not None else None,
        number=number,
        status=str(raw.get("state") or ""),
        is_existing=is_existing,
        raw=dict(raw),
    )


def _github_existing_pull_request(
    remote: PullRequestRemote,
    request: PullRequestRequest,
    value: Any,
) -> dict[str, Any] | None:
    candidates = _mappings_from_list(value, "gh pr list")
    matching = [
        item
        for item in candidates
        if _github_branch_matches(request, item) and _github_head_repository_matches(remote, item)
    ]
    if len(matching) > 1:
        raise PullRequestError(
            "gh pr list returned multiple open pull requests from the selected head repository; "
            "unable to safely reuse an existing pull request."
        )
    if matching:
        return matching[0]
    incomplete_same_repo = [
        item
        for item in candidates
        if _github_branch_metadata_incomplete(item) and _github_head_repository_matches(remote, item)
    ]
    if incomplete_same_repo:
        raise PullRequestError(
            "gh pr list returned an open pull request from the selected head repository without complete "
            "head/base branch metadata; unable to safely reuse it or create another pull request."
        )
    ambiguous = []
    for item in candidates:
        if not _github_branch_matches(request, item):
            continue
        owner, repo = _github_head_repository(item)
        if owner is None or repo is None:
            ambiguous.append(item)
    if ambiguous:
        raise PullRequestError(
            "gh pr list returned an open pull request without head repository metadata; "
            "unable to safely reuse it or create another pull request."
        )
    return None


def _github_branch_matches(request: PullRequestRequest, raw: dict[str, Any]) -> bool:
    head, base = _github_branch_names(raw)
    return head == request.source_branch and base == request.target_branch


def _github_branch_metadata_incomplete(raw: dict[str, Any]) -> bool:
    head, base = _github_branch_names(raw)
    return head is None or base is None


def _github_branch_names(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    return _clean_json_string(raw.get("headRefName")), _clean_json_string(raw.get("baseRefName"))


def _github_head_repository_matches(remote: PullRequestRemote, raw: dict[str, Any]) -> bool:
    owner, repo = _github_head_repository(raw)
    return (
        owner is not None
        and repo is not None
        and remote.owner is not None
        and owner.casefold() == remote.owner.casefold()
        and repo.casefold() == remote.repo.casefold()
    )


def _github_head_repository(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    owner: str | None = None
    repo: str | None = None
    head_repository = raw.get("headRepository")
    if isinstance(head_repository, dict):
        name_with_owner = _clean_json_string(head_repository.get("nameWithOwner"))
        if name_with_owner and "/" in name_with_owner:
            owner, repo = name_with_owner.split("/", 1)
        repo = _clean_json_string(head_repository.get("name")) or repo
        owner = _github_owner_login(head_repository.get("owner")) or owner
    elif isinstance(head_repository, str):
        value = head_repository.strip()
        if "/" in value:
            owner, repo = value.split("/", 1)
        elif value:
            repo = value
    owner = _github_owner_login(raw.get("headRepositoryOwner")) or owner
    return owner, repo


def _github_owner_login(value: Any) -> str | None:
    if isinstance(value, dict):
        return _clean_json_string(value.get("login") or value.get("name"))
    if isinstance(value, str):
        return value.strip() or None
    return None


def _ado_result(
    remote: PullRequestRemote,
    request: PullRequestRequest,
    raw: dict[str, Any],
    *,
    is_existing: bool,
) -> PullRequestResult:
    number = _as_int(raw.get("pullRequestId"))
    url = _validated_ado_result_url(remote, _ado_result_url(raw), "az repos pr result")
    return PullRequestResult(
        provider=remote.provider,
        remote_name=remote.remote_name,
        source_branch=_strip_heads(str(raw.get("sourceRefName") or request.source_branch)),
        target_branch=_strip_heads(str(raw.get("targetRefName") or request.target_branch)),
        title=str(raw.get("title") or request.title),
        url=url,
        id=str(number) if number is not None else None,
        number=number,
        status=str(raw.get("status") or ""),
        is_existing=is_existing,
        raw=dict(raw),
    )


def _ado_result_url(raw: dict[str, Any]) -> Any:
    if raw.get("webUrl"):
        return raw.get("webUrl")
    links = raw.get("_links")
    if isinstance(links, dict):
        web = links.get("web")
        if isinstance(web, dict) and web.get("href"):
            return web.get("href")
    return raw.get("url")


def _ado_remote(remote_name: str, remote_url: str, *, org: str, project: str, repo: str) -> PullRequestRemote | None:
    org = unquote(org)
    project = unquote(project)
    repo = _strip_dot_git(unquote(repo))
    if not org or not project or not repo:
        return None
    return PullRequestRemote(
        provider="azure-devops",
        remote_name=remote_name,
        url=remote_url,
        org=org,
        project=project,
        repo=repo,
    )


def _path_parts(path: str) -> list[str]:
    return [unquote(part) for part in path.strip("/").split("/") if part]


def _strip_dot_git(value: str) -> str:
    return value[:-4] if value.endswith(".git") else value


def _github_repo(remote: PullRequestRemote) -> str:
    owner = _require(remote.owner, "owner", remote)
    return f"{owner}/{remote.repo}"


def _ado_org_url(remote: PullRequestRemote) -> str:
    return f"https://dev.azure.com/{_require(remote.org, 'org', remote)}"


def _require(value: str | None, name: str, remote: PullRequestRemote) -> str:
    if value:
        return value
    raise PullRequestError(f"Remote '{remote.remote_name}' is missing required {name} metadata.")


def _ado_branch(branch: str) -> str:
    return branch if branch.startswith("refs/") else f"refs/heads/{branch}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _metadata_identity(metadata: dict[str, Any]) -> tuple[str, ...] | None:
    identity = metadata.get("remote_identity")
    if not isinstance(identity, (list, tuple)):
        return None
    parts: list[str] = []
    for part in identity:
        cleaned = _clean_json_string(part)
        if cleaned is None:
            return None
        parts.append(cleaned.casefold())
    return tuple(parts) if parts else None


def _github_pr_ref(metadata: dict[str, Any]) -> str:
    number = _as_int(metadata.get("number"))
    if number is not None:
        return str(number)
    url = _clean_json_string(metadata.get("url"))
    if url:
        return url
    raise PullRequestError("GitHub pull request metadata is missing number and URL.")


def _github_pr_number(metadata: dict[str, Any]) -> int:
    number = _as_int(metadata.get("number"))
    if number is None:
        raise PullRequestError("GitHub pull request metadata is missing number.")
    return number


def _ado_pr_id(metadata: dict[str, Any]) -> str:
    pr_id = _clean_json_string(metadata.get("id"))
    if pr_id:
        return pr_id
    number = _as_int(metadata.get("number"))
    if number is not None:
        return str(number)
    raise PullRequestError("Azure DevOps pull request metadata is missing id.")


def _ado_commit_id(value: Any) -> str | None:
    if isinstance(value, dict):
        return _clean_json_string(value.get("commitId"))
    return _clean_json_string(value)


def _safe_url_or_none(value: Any) -> str | None:
    text = _clean_json_string(value)
    return text if is_safe_pull_request_url(text) else None


def _find_marked_github_comment(value: Any, marker: str) -> dict[str, Any] | None:
    for item in _flatten_github_comment_pages(value):
        if marker in str(item.get("body") or "") and item.get("id") is not None:
            return item
    return None


def _flatten_github_comment_pages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return [dict(item) for item in value]
    flattened: list[dict[str, Any]] = []
    if isinstance(value, list):
        for page in value:
            if isinstance(page, list):
                flattened.extend(dict(item) for item in page if isinstance(item, dict))
    return flattened


def _ado_threads_url(remote: PullRequestRemote, pr_id: str) -> str:
    return (
        f"{_ado_project_url(remote)}/_apis/git/repositories/{_ado_url_part(remote.repo)}"
        f"/pullRequests/{_ado_url_part(pr_id)}/threads?api-version=7.1"
    )


def _ado_pull_request_url(remote: PullRequestRemote, pr_id: str) -> str:
    return (
        f"{_ado_project_url(remote)}/_apis/git/repositories/{_ado_url_part(remote.repo)}"
        f"/pullRequests/{_ado_url_part(pr_id)}?api-version=7.1"
    )


def _ado_thread_comment_url(remote: PullRequestRemote, pr_id: str, thread_id: object, comment_id: object) -> str:
    return (
        f"{_ado_project_url(remote)}/_apis/git/repositories/{_ado_url_part(remote.repo)}"
        f"/pullRequests/{_ado_url_part(pr_id)}/threads/{_ado_url_part(thread_id)}"
        f"/comments/{_ado_url_part(comment_id)}?api-version=7.1"
    )


def _ado_rest_args(method: str, url: str) -> list[str]:
    return [
        "az",
        "rest",
        "--method",
        method,
        "--url",
        url,
        "--resource",
        _ADO_REST_RESOURCE,
    ]


def _ado_project_url(remote: PullRequestRemote) -> str:
    return f"{_ado_org_url(remote)}/{_ado_url_part(_require(remote.project, 'project', remote))}"


def _ado_url_part(value: object) -> str:
    return quote(str(value), safe="")


def _find_marked_ado_comment(value: Any, marker: str) -> tuple[object, object] | None:
    threads = value.get("value") if isinstance(value, dict) else value
    if not isinstance(threads, list):
        return None
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        thread_id = thread.get("id")
        comments = thread.get("comments")
        if thread_id is None or not isinstance(comments, list):
            continue
        for comment in comments:
            if isinstance(comment, dict) and marker in str(comment.get("content") or "") and comment.get("id") is not None:
                return thread_id, comment["id"]
    return None


def _first_ado_comment_id(raw: dict[str, Any]) -> object | None:
    comments = raw.get("comments")
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict) and comment.get("id") is not None:
                return comment["id"]
    return None


def _strip_heads(branch: str) -> str:
    prefix = "refs/heads/"
    return branch[len(prefix) :] if branch.startswith(prefix) else branch


def _ensure_success(command: str, result: CommandResult, hint: str) -> None:
    if result.returncode == 0:
        return
    detail = _safe_command_output(result.stderr or result.stdout)
    raise PullRequestError(f"{command} failed with exit code {result.returncode}: {detail}. {hint}")


def _command_launch_error(executable: str, exc: OSError) -> str:
    detail = _safe_command_output(str(exc))
    return f"Failed to start pull request command '{executable}': {detail}. {_command_install_hint(executable)}"


def _command_install_hint(executable: str) -> str:
    if executable == "gh":
        return "Install GitHub CLI and run 'gh auth login' before retrying."
    if executable == "az":
        return (
            "Install Azure CLI, install/configure the azure-devops extension, and run 'az login' "
            "before retrying."
        )
    return "Install the required pull request provider CLI and verify it is on PATH before retrying."


def _safe_command_output(output: str) -> str:
    redacted = _redact_command_output(output).strip() or "no output"
    if len(redacted) <= _COMMAND_OUTPUT_MAX_CHARS:
        return redacted
    return f"{redacted[:_COMMAND_OUTPUT_MAX_CHARS]}... [truncated]"


def _redact_command_output(output: str) -> str:
    redacted = _CREDENTIAL_URL_RE.sub(r"\1[redacted]@", output)
    redacted = _AUTH_HEADER_RE.sub(r"\1[redacted]", redacted)
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[redacted]", redacted)
    return _GH_TOKEN_RE.sub("[redacted]", redacted)


def _load_json(stdout: str, command: str) -> Any:
    try:
        return json.loads(stdout or "null")
    except json.JSONDecodeError as exc:
        raise PullRequestError(f"{command} did not return valid JSON: {exc}") from exc


def _expect_mapping(value: Any, command: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise PullRequestError(f"{command} returned unexpected JSON; expected an object.")


def _first_mapping_from_list(value: Any, command: str) -> dict[str, Any] | None:
    for item in _mappings_from_list(value, command):
        return item
    return None


def _mappings_from_list(value: Any, command: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PullRequestError(f"{command} returned unexpected JSON; expected a list.")
    mappings: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            mappings.append(item)
    return mappings


def _first_non_empty_line(stdout: str) -> str:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _validated_github_result_url(remote: PullRequestRemote, value: Any, command: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    return _validate_github_pull_request_url(remote, url, command)


def _validated_ado_result_url(remote: PullRequestRemote, value: Any, command: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    return _validate_ado_pull_request_url(remote, url, command)


def _validate_github_pull_request_url(remote: PullRequestRemote, url: str, command: str) -> str:
    normalized = _validate_pull_request_url(url, command)
    parsed = urlparse(normalized)
    parts = _path_parts(parsed.path)
    if (
        (parsed.hostname or "").casefold() != "github.com"
        or len(parts) < 4
        or parts[0].casefold() != _casefold_required(remote.owner, "owner", remote)
        or parts[1].casefold() != remote.repo.casefold()
        or parts[2] != "pull"
    ):
        raise PullRequestError(
            f"{command} returned a pull request URL outside the selected GitHub repository "
            f"{_github_repo(remote)}."
        )
    return normalized


def _validate_ado_pull_request_url(remote: PullRequestRemote, url: str, command: str) -> str:
    normalized = _validate_pull_request_url(url, command)
    parsed = urlparse(normalized)
    if not _ado_pull_request_url_matches_remote(parsed, remote):
        raise PullRequestError(
            f"{command} returned a pull request URL outside the selected Azure DevOps Services repository "
            f"{_require(remote.org, 'org', remote)}/{_require(remote.project, 'project', remote)}/{remote.repo}."
        )
    return normalized


def _validate_pull_request_url(url: str, command: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except ValueError as exc:
        raise PullRequestError(f"{command} returned an invalid pull request URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise PullRequestError(f"{command} returned an unsafe pull request URL scheme; expected http or https.")
    if parsed.username is not None or parsed.password is not None:
        raise PullRequestError(f"{command} returned a credential-bearing pull request URL.")
    if parsed.query or parsed.fragment:
        raise PullRequestError(f"{command} returned a pull request URL with query or fragment components.")
    return urlunparse(parsed._replace(scheme=parsed.scheme.lower()))


def _ado_pull_request_url_matches_remote(parsed: Any, remote: PullRequestRemote) -> bool:
    host = (parsed.hostname or "").casefold()
    org = _casefold_required(remote.org, "org", remote)
    project = _casefold_required(remote.project, "project", remote)
    repo = remote.repo.casefold()
    parts = [part.casefold() for part in _path_parts(parsed.path)]
    if host == "dev.azure.com":
        if len(parts) >= 6 and parts[:4] == [org, project, "_git", repo] and parts[4] == "pullrequest":
            return True
        if (
            len(parts) >= 8
            and parts[:5] == [org, project, "_apis", "git", "repositories"]
            and parts[5] == repo
            and parts[6] == "pullrequests"
        ):
            return True
        return False
    visualstudio_suffix = ".visualstudio.com"
    if host.endswith(visualstudio_suffix) and host != "visualstudio.com":
        host_org = host[: -len(visualstudio_suffix)]
        if host_org != org:
            return False
        if len(parts) >= 5 and parts[:3] == [project, "_git", repo] and parts[3] == "pullrequest":
            return True
        if (
            len(parts) >= 7
            and parts[:4] == [project, "_apis", "git", "repositories"]
            and parts[4] == repo
            and parts[5] == "pullrequests"
        ):
            return True
    return False


def _has_query_or_fragment(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return any(marker in url for marker in ("?", "#"))
    return bool(parsed.query or parsed.fragment)


def _url_embeds_disallowed_credentials(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    if parsed.password is not None:
        return True
    return parsed.scheme.lower() in {"http", "https"} and parsed.username is not None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _clean_json_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _casefold_required(value: str | None, name: str, remote: PullRequestRemote) -> str:
    return _require(value, name, remote).casefold()
