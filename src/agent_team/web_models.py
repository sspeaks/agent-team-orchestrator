from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeInfo:
    mode: str = "web-only"
    web_workers: int = 1
    worker_concurrency: int | None = None
    worker_interval_seconds: int | None = None


@dataclass(frozen=True)
class RepoContext:
    repo_path: str | None
    known_repos: list[str]


@dataclass(frozen=True)
class IssueMetadataForm:
    description: str
    repo_path: str | None
    priority: int
    tags: str | None
