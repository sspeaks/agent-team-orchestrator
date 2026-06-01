from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Issue:
    id: int
    title: str
    description: str
    source: str
    external_id: str | None
    repo_path: str | None
    phase: str
    status: str
    priority: int
    tags: str | None
    lock_owner: str | None
    lock_expires_at: str | None
    current_run_id: str | None
    created_at: str
    updated_at: str
    blocked_summary: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Issue":
        blocked_summary = row["blocked_summary"] if _has_row_key(row, "blocked_summary") else None
        return cls(
            id=int(row["id"]),
            title=str(row["title"]),
            description=str(row["description"]),
            source=str(row["source"]),
            external_id=row["external_id"],
            repo_path=row["repo_path"],
            phase=str(row["phase"]),
            status=str(row["status"]),
            priority=int(row["priority"]),
            tags=row["tags"],
            lock_owner=row["lock_owner"],
            lock_expires_at=row["lock_expires_at"],
            current_run_id=row["current_run_id"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            blocked_summary=blocked_summary,
        )


@dataclass(frozen=True)
class AgentResult:
    status: str
    summary: str
    artifact_markdown: str
    suggested_next_phase: str | None = None
    error: str | None = None
    raw_stdout: str | None = None
    raw_stderr: str | None = None
    blocked_summary: str | None = None


@dataclass(frozen=True)
class HumanInputRequestDraft:
    requested_by_phase: str
    resume_phase: str
    question: str
    rationale: str
    requested_decision: str
    options: tuple[str, ...] = ()
    context: str | None = None


@dataclass(frozen=True)
class HumanInputRequest:
    id: str
    issue_id: int
    run_id: str | None
    requested_by_phase: str
    resume_phase: str
    question: str
    rationale: str
    requested_decision: str
    options: tuple[str, ...]
    context: str | None
    status: str
    created_at: str
    answered_at: str | None
    answer: str | None
    answered_by: str | None

    @classmethod
    def from_row(cls, row: Any) -> "HumanInputRequest":
        raw_options = row["options_json"]
        parsed_options: tuple[str, ...] = ()
        if raw_options:
            loaded = json.loads(str(raw_options))
            if isinstance(loaded, list):
                parsed_options = tuple(str(item) for item in loaded)
        return cls(
            id=str(row["id"]),
            issue_id=int(row["issue_id"]),
            run_id=row["run_id"],
            requested_by_phase=str(row["requested_by_phase"]),
            resume_phase=str(row["resume_phase"]),
            question=str(row["question"]),
            rationale=str(row["rationale"]),
            requested_decision=str(row["requested_decision"]),
            options=parsed_options,
            context=row["context"],
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            answered_at=row["answered_at"],
            answer=row["answer"],
            answered_by=row["answered_by"],
        )


def _has_row_key(row: Any, key: str) -> bool:
    if not hasattr(row, "keys"):
        return False
    return key in row.keys()
