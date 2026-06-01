from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import unquote, urlparse


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
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, args: Sequence[str]) -> CommandResult:
        ...


class SubprocessCommandRunner:
    def run(self, args: Sequence[str]) -> CommandResult:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            text=True,
        )
        return CommandResult(
            args=tuple(str(arg) for arg in args),
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


_GITHUB_SCP_RE = re.compile(r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")
_ADO_SCP_RE = re.compile(
    r"^(?:git@)?ssh\.dev\.azure\.com:v3/"
    r"(?P<org>[^/]+)/(?P<project>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def parse_pull_request_remote(remote_name: str, remote_url: str) -> PullRequestRemote | None:
    return parse_github_remote(remote_name, remote_url) or parse_azure_devops_remote(remote_name, remote_url)


def parse_github_remote(remote_name: str, remote_url: str) -> PullRequestRemote | None:
    url = remote_url.strip()
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
        "number,url,title,headRefName,baseRefName,state",
    ]
    listed = runner.run(list_args)
    _ensure_success("gh pr list", listed, "Run 'gh auth login' and verify the repository is accessible.")
    existing = _first_mapping_from_list(_load_json(listed.stdout, "gh pr list"), "gh pr list")
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

    viewed = runner.run(
        [
            "gh",
            "pr",
            "view",
            url,
            "--repo",
            repo,
            "--json",
            "number,url,title,headRefName,baseRefName,state",
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
            _description(request),
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


def _github_result(
    remote: PullRequestRemote,
    request: PullRequestRequest,
    raw: dict[str, Any],
    *,
    is_existing: bool,
) -> PullRequestResult:
    number = _as_int(raw.get("number"))
    return PullRequestResult(
        provider=remote.provider,
        remote_name=remote.remote_name,
        source_branch=str(raw.get("headRefName") or request.source_branch),
        target_branch=str(raw.get("baseRefName") or request.target_branch),
        title=str(raw.get("title") or request.title),
        url=str(raw.get("url") or ""),
        id=str(number) if number is not None else None,
        number=number,
        status=str(raw.get("state") or ""),
        is_existing=is_existing,
        raw=dict(raw),
    )


def _ado_result(
    remote: PullRequestRemote,
    request: PullRequestRequest,
    raw: dict[str, Any],
    *,
    is_existing: bool,
) -> PullRequestResult:
    number = _as_int(raw.get("pullRequestId"))
    return PullRequestResult(
        provider=remote.provider,
        remote_name=remote.remote_name,
        source_branch=_strip_heads(str(raw.get("sourceRefName") or request.source_branch)),
        target_branch=_strip_heads(str(raw.get("targetRefName") or request.target_branch)),
        title=str(raw.get("title") or request.title),
        url=str(raw.get("url") or ""),
        id=str(number) if number is not None else None,
        number=number,
        status=str(raw.get("status") or ""),
        is_existing=is_existing,
        raw=dict(raw),
    )


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


def _strip_heads(branch: str) -> str:
    prefix = "refs/heads/"
    return branch[len(prefix) :] if branch.startswith(prefix) else branch


def _description(request: PullRequestRequest) -> str:
    if request.description:
        return request.description
    if request.body_path is None:
        return ""
    return Path(request.body_path).read_text(encoding="utf-8")


def _ensure_success(command: str, result: CommandResult, hint: str) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout).strip() or "no output"
    raise PullRequestError(f"{command} failed with exit code {result.returncode}: {detail}. {hint}")


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
    if not isinstance(value, list):
        raise PullRequestError(f"{command} returned unexpected JSON; expected a list.")
    for item in value:
        if isinstance(item, dict):
            return item
    return None


def _first_non_empty_line(stdout: str) -> str:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


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
