from __future__ import annotations

import urllib.parse
from http import HTTPStatus

from .web_errors import WebError
from .web_models import RepoContext


def split_path(raw_path: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urllib.parse.urlsplit(raw_path)
    return parsed.path or "/", urllib.parse.parse_qs(parsed.query, keep_blank_values=True)


def path_parts(path: str) -> list[str]:
    return [urllib.parse.unquote(part) for part in path.strip("/").split("/") if part]


def artifact_route(path: str) -> tuple[int, str]:
    parts = path.strip("/").split("/", 2)
    if len(parts) != 3 or parts[0] != "artifacts":
        raise WebError(HTTPStatus.NOT_FOUND, f"Unknown path: {path}")
    return parse_issue_id(urllib.parse.unquote(parts[1])), urllib.parse.unquote(parts[2])


def parse_issue_id(value: str) -> int:
    try:
        issue_id = int(value)
    except ValueError as exc:
        raise WebError(HTTPStatus.BAD_REQUEST, f"Invalid issue id: {value}") from exc
    if issue_id <= 0:
        raise WebError(HTTPStatus.BAD_REQUEST, f"Invalid issue id: {value}")
    return issue_id


def single(values: list[str] | None) -> str:
    return values[-1] if values else ""


def with_query(path: str, **params: object) -> str:
    filtered = {key: value for key, value in params.items() if value is not None and value != ""}
    if not filtered:
        return path
    return path + "?" + urllib.parse.urlencode(filtered)


def context_url(path: str, repo_context: RepoContext | None = None, **params: object) -> str:
    context_params: dict[str, object] = {}
    if repo_context is not None and repo_context.repo_path is not None:
        context_params["repo"] = repo_context.repo_path
    context_params.update(params)
    return with_query(path, **context_params)


def issue_url(issue_id: object, repo_context: RepoContext | None = None) -> str:
    return context_url(f"/issues/{issue_id}", repo_context)
