from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .blocked_summary import extract_blocked_summary, summarize_blocked_reason
from .locks import is_live_same_host_owner
from .models import HumanInputRequest, HumanInputRequestDraft, Issue, utc_now_iso
from .state_machine import (
    RUNNING_PHASES,
    READY_PHASES,
    agent_phase_for_running_phase,
    default_next_phase,
    ready_phase_for_running_phase,
    validate_human_input_resume_phase,
    validate_transition,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
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
  blocked_summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
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
  next_phase TEXT,
  FOREIGN KEY(issue_id) REFERENCES issues(id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  issue_id INTEGER NOT NULL,
  run_id TEXT,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(issue_id) REFERENCES issues(id)
);

CREATE TABLE IF NOT EXISTS human_input_requests (
  id TEXT PRIMARY KEY,
  issue_id INTEGER NOT NULL,
  run_id TEXT,
  requested_by_phase TEXT NOT NULL,
  resume_phase TEXT NOT NULL,
  question TEXT NOT NULL,
  rationale TEXT NOT NULL,
  requested_decision TEXT NOT NULL,
  options_json TEXT,
  context TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  answered_at TEXT,
  answer TEXT,
  answered_by TEXT,
  FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE,
  FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE SET NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_issues_ready
ON issues(status, phase, priority, last_scheduled_at, created_at, id);

CREATE INDEX IF NOT EXISTS idx_issues_repo_path
ON issues(repo_path);

CREATE UNIQUE INDEX IF NOT EXISTS idx_human_input_one_pending
ON human_input_requests(issue_id)
WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_human_input_issue_created
ON human_input_requests(issue_id, created_at, id);
"""


LockReclaimPredicate = Callable[[Optional[str]], bool]
TerminalNextPhaseResolver = Callable[[sqlite3.Row], Optional[str]]

_MAX_GENERATED_TITLE_LENGTH = 80
_TITLE_FALLBACK = "Untitled issue"
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]+\)")
_SENTENCE_RE = re.compile(r"(.+?[.!?])(?:\s+|$)")
_PR_MONITOR_RUN_ID_PREFIX = "pr-monitor-"
_PR_MONITOR_POST_TRANSITION_PHASES = {"blocked", "done"}


def _clean_issue_description(description: str) -> str:
    cleaned = description.strip()
    if not cleaned:
        raise ValueError("description is required")
    return cleaned


def _normalize_issue_title(title: str | None, description: str) -> str:
    if title is not None:
        cleaned_title = title.strip()
        if cleaned_title:
            return cleaned_title
    return _generate_issue_title(description)


def _generate_issue_title(description: str) -> str:
    in_fenced_block = False
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```") or line.startswith("~~~"):
            in_fenced_block = not in_fenced_block
            continue
        if in_fenced_block or re.fullmatch(r"(?:=+|-{3,})", line):
            continue
        candidate = _clean_title_candidate(line)
        if candidate:
            return _truncate_generated_title(candidate)
    return _TITLE_FALLBACK


def _clean_title_candidate(line: str) -> str:
    previous = None
    candidate = line
    while candidate != previous:
        previous = candidate
        candidate = re.sub(r"^(?:>\s*)+", "", candidate).strip()
        candidate = re.sub(r"^#{1,6}\s*", "", candidate).strip()
        candidate = re.sub(r"^[-*+]\s+\[[ xX]\]\s+", "", candidate).strip()
        candidate = re.sub(r"^[-*+]\s+", "", candidate).strip()
        candidate = re.sub(r"^\d+[.)]\s+", "", candidate).strip()
    candidate = _LINK_RE.sub(r"\1", candidate)
    candidate = candidate.replace("`", "")
    candidate = " ".join(candidate.split())
    sentence = _SENTENCE_RE.match(candidate)
    if sentence:
        candidate = sentence.group(1)
    return candidate.strip()


def _truncate_generated_title(title: str) -> str:
    if len(title) <= _MAX_GENERATED_TITLE_LENGTH:
        return title
    limit = _MAX_GENERATED_TITLE_LENGTH - 3
    shortened = title[:limit].rstrip()
    boundary = shortened.rfind(" ")
    if boundary > 0:
        shortened = shortened[:boundary].rstrip()
    if not shortened:
        shortened = title[:limit].rstrip()
    return f"{shortened}..."


@dataclass(frozen=True)
class RecoveryResult:
    issue_id: int
    run_id: str | None
    previous_phase: str
    next_phase: str
    action: str
    summary: str
    agent_phase: str | None = None


@dataclass(frozen=True)
class StopIssueResult:
    issue_id: int
    prior_phase: str
    issue: Issue
    stopped_human_input_request: HumanInputRequest | None = None


class IssueStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_issues_schema(conn)
            self._migrate_runs_schema(conn)
            conn.executescript(INDEXES)

    def create_issue(
        self,
        title: str | None = None,
        description: str = "",
        repo_path: str | None = None,
        priority: int = 3,
        tags: str | None = None,
        source: str = "local",
        external_id: str | None = None,
        ready: bool = False,
    ) -> Issue:
        now = utc_now_iso()
        phase = "needs_research" if ready else "draft"
        cleaned_description = _clean_issue_description(description)
        cleaned_title = _normalize_issue_title(title, cleaned_description)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO issues (
                  title, description, source, external_id, repo_path, phase, status,
                  priority, tags, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (
                    cleaned_title,
                    cleaned_description,
                    source,
                    external_id,
                    repo_path,
                    phase,
                    priority,
                    tags,
                    now,
                    now,
                ),
            )
            issue_id = int(cur.lastrowid)
            creation_kind = "ready" if ready else "draft"
            self._add_event(conn, issue_id, None, "issue.created", f"Created {creation_kind} issue: {cleaned_title}")
        return self.get_issue(issue_id)

    def update_draft_issue(
        self,
        issue_id: int,
        *,
        title: str | None,
        description: str,
        repo_path: str | None,
        priority: int,
        tags: str | None,
    ) -> Issue:
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            prior = Issue.from_row(row)
            cleaned_description = _clean_issue_description(description)
            if title is None:
                cleaned_title = prior.title
            else:
                cleaned_title = _normalize_issue_title(title, cleaned_description)
            if type(priority) is not int:
                raise ValueError("priority must be an integer")
            self._validate_draft_editable(prior, now)

            cur = conn.execute(
                """
                UPDATE issues
                SET title = ?, description = ?, repo_path = ?, priority = ?, tags = ?, updated_at = ?
                WHERE id = ?
                  AND status = 'open'
                  AND phase = 'draft'
                  AND (lock_expires_at IS NULL OR lock_expires_at < ?)
                """,
                (cleaned_title, cleaned_description, repo_path, priority, tags, now, issue_id, now),
            )
            if cur.rowcount != 1:
                latest = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
                if latest is None:
                    raise KeyError(f"Issue not found: {issue_id}")
                self._validate_draft_editable(Issue.from_row(latest), now)
                raise RuntimeError(f"Draft edit for issue {issue_id} did not update exactly one row")

            changed_fields = []
            for field, prior_value, updated_value in (
                ("title", prior.title, cleaned_title),
                ("description", prior.description, cleaned_description),
                ("repo_path", prior.repo_path, repo_path),
                ("priority", prior.priority, priority),
                ("tags", prior.tags, tags),
            ):
                if prior_value != updated_value:
                    changed_fields.append(field)
            changed_text = ", ".join(changed_fields) if changed_fields else "none"
            self._add_event(conn, issue_id, None, "issue.edited", f"Edited draft issue fields: {changed_text}")
        return self.get_issue(issue_id)

    def get_issue(self, issue_id: int) -> Issue:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            raise KeyError(f"Issue not found: {issue_id}")
        return Issue.from_row(row)

    def list_known_repos(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT repo_path
                FROM issues
                WHERE repo_path IS NOT NULL AND TRIM(repo_path) != ''
                ORDER BY repo_path ASC
                """
            ).fetchall()
        return [str(row["repo_path"]) for row in rows]

    def list_issues(self, status: str | None = None, repo_path: str | None = None) -> list[Issue]:
        query = "SELECT * FROM issues"
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if repo_path is not None:
            clauses.append("repo_path = ?")
            params.append(repo_path)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY priority ASC, id ASC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [Issue.from_row(row) for row in rows]

    def find_next_ready_issue(self, repo_path: str | None = None) -> Issue | None:
        issues = self.list_next_ready_issues(1, repo_path=repo_path)
        return issues[0] if issues else None

    def list_next_ready_issues(
        self,
        limit: int,
        exclude_issue_ids: set[int] | None = None,
        repo_path: str | None = None,
    ) -> list[Issue]:
        if limit <= 0:
            return []
        phases = tuple(READY_PHASES.keys())
        placeholders = ",".join("?" for _ in phases)
        excluded = sorted(exclude_issue_ids or set())
        exclude_clause = ""
        repo_clause = ""
        params: list[object] = [*phases, utc_now_iso()]
        if repo_path is not None:
            repo_clause = "AND repo_path = ?"
            params.append(repo_path)
        if excluded:
            exclude_placeholders = ",".join("?" for _ in excluded)
            exclude_clause = f"AND id NOT IN ({exclude_placeholders})"
            params.extend(excluded)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM issues
                WHERE status = 'open'
                  AND phase IN ({placeholders})
                  AND (lock_expires_at IS NULL OR lock_expires_at < ?)
                  {repo_clause}
                  {exclude_clause}
                ORDER BY priority ASC, COALESCE(last_scheduled_at, created_at) ASC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [Issue.from_row(row) for row in rows]

    def list_issues_awaiting_pr_closure(
        self,
        limit: int = 100,
        *,
        repo_path: str | None = None,
        exclude_issue_ids: set[int] | None = None,
    ) -> list[Issue]:
        if limit <= 0:
            return []
        excluded = sorted(exclude_issue_ids or set())
        exclude_clause = ""
        repo_clause = ""
        params: list[object] = [utc_now_iso()]
        if repo_path is not None:
            repo_clause = "AND repo_path = ?"
            params.append(repo_path)
        if excluded:
            exclude_placeholders = ",".join("?" for _ in excluded)
            exclude_clause = f"AND id NOT IN ({exclude_placeholders})"
            params.extend(excluded)
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM issues
                WHERE status = 'open'
                  AND phase = 'awaiting_pr_closure'
                  AND (lock_expires_at IS NULL OR lock_expires_at < ?)
                  {repo_clause}
                  {exclude_clause}
                ORDER BY priority ASC, COALESCE(last_scheduled_at, created_at) ASC, id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [Issue.from_row(row) for row in rows]

    def dashboard_summary(self, repo_path: str | None = None) -> dict[str, object]:
        now = utc_now_iso()
        issue_where = "WHERE repo_path = ?" if repo_path is not None else ""
        issue_and = "AND repo_path = ?" if repo_path is not None else ""
        joined_where = "WHERE issues.repo_path = ?" if repo_path is not None else ""
        joined_and = "AND issues.repo_path = ?" if repo_path is not None else ""
        issue_scope = [repo_path] if repo_path is not None else []
        with self.connect() as conn:
            phase_counts = conn.execute(
                f"""
                SELECT phase, status, COUNT(*) AS count
                FROM issues
                {issue_where}
                GROUP BY phase, status
                ORDER BY status, phase
                """,
                issue_scope,
            ).fetchall()
            active_locks = conn.execute(
                f"""
                SELECT id, title, phase, priority, updated_at, lock_owner, lock_expires_at, current_run_id,
                       blocked_summary
                FROM issues
                WHERE lock_expires_at IS NOT NULL AND lock_expires_at >= ?
                {issue_and}
                ORDER BY priority ASC, id ASC
                """,
                [now, *issue_scope],
            ).fetchall()
            recent_runs = conn.execute(
                f"""
                SELECT runs.id, runs.issue_id, issues.title, runs.phase, runs.runner, runs.status,
                       runs.started_at, runs.completed_at, runs.summary
                FROM runs
                JOIN issues ON issues.id = runs.issue_id
                {joined_where}
                ORDER BY runs.started_at DESC
                LIMIT 10
                """,
                issue_scope,
            ).fetchall()
            recent_events = conn.execute(
                f"""
                SELECT events.issue_id, issues.title, events.event_type, events.message, events.created_at
                FROM events
                JOIN issues ON issues.id = events.issue_id
                {joined_where}
                ORDER BY events.id DESC
                LIMIT 10
                """,
                issue_scope,
            ).fetchall()
            open_issues = conn.execute(
                f"""
                SELECT id, title, phase, priority, updated_at, blocked_summary
                FROM issues
                WHERE status = 'open'
                {issue_and}
                ORDER BY priority ASC, updated_at DESC
                LIMIT 25
                """,
                issue_scope,
            ).fetchall()
            awaiting_plan_approval = conn.execute(
                f"""
                SELECT id, title, phase, status, priority, updated_at,
                       lock_owner, lock_expires_at, current_run_id, blocked_summary
                FROM issues
                WHERE status = 'open' AND phase = 'awaiting_plan_approval'
                {issue_and}
                ORDER BY priority ASC, updated_at DESC
                LIMIT 25
                """,
                issue_scope,
            ).fetchall()
            awaiting_plan_approval_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM issues
                WHERE status = 'open' AND phase = 'awaiting_plan_approval'
                {issue_and}
                """,
                issue_scope,
            ).fetchone()
            awaiting_merge_approval = conn.execute(
                f"""
                SELECT id, title, phase, status, priority, updated_at,
                       lock_owner, lock_expires_at, current_run_id, blocked_summary
                FROM issues
                WHERE status = 'open' AND phase = 'awaiting_merge_approval'
                {issue_and}
                ORDER BY priority ASC, updated_at DESC
                LIMIT 25
                """,
                issue_scope,
            ).fetchall()
            awaiting_merge_approval_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM issues
                WHERE status = 'open' AND phase = 'awaiting_merge_approval'
                {issue_and}
                """,
                issue_scope,
            ).fetchone()
            human_input_needed = conn.execute(
                f"""
                SELECT issues.id, issues.title, issues.phase, issues.status, issues.priority, issues.updated_at,
                       issues.lock_owner, issues.lock_expires_at, issues.current_run_id, issues.blocked_summary,
                       human_input_requests.id AS request_id,
                       human_input_requests.question,
                       human_input_requests.resume_phase
                FROM issues
                LEFT JOIN human_input_requests
                  ON human_input_requests.issue_id = issues.id
                 AND human_input_requests.status = 'pending'
                WHERE issues.status = 'open' AND issues.phase = 'awaiting_human_input'
                {joined_and}
                ORDER BY issues.priority ASC, issues.updated_at DESC
                LIMIT 25
                """,
                issue_scope,
            ).fetchall()
            human_input_needed_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM issues
                WHERE status = 'open' AND phase = 'awaiting_human_input'
                {issue_and}
                """,
                issue_scope,
            ).fetchone()
            blocked_issues = conn.execute(
                f"""
                SELECT id, title, phase, status, priority, updated_at,
                       lock_owner, lock_expires_at, current_run_id, blocked_summary
                FROM issues
                WHERE status = 'open' AND phase = 'blocked'
                {issue_and}
                ORDER BY priority ASC, updated_at DESC
                LIMIT 25
                """,
                issue_scope,
            ).fetchall()
            blocked_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM issues
                WHERE status = 'open' AND phase = 'blocked'
                {issue_and}
                """,
                issue_scope,
            ).fetchone()
            draft_issues = conn.execute(
                f"""
                SELECT id, title, phase, status, priority, updated_at,
                       lock_owner, lock_expires_at, current_run_id, blocked_summary
                FROM issues
                WHERE status = 'open' AND phase = 'draft'
                {issue_and}
                ORDER BY priority ASC, updated_at DESC
                LIMIT 25
                """,
                issue_scope,
            ).fetchall()
            draft_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM issues
                WHERE status = 'open' AND phase = 'draft'
                {issue_and}
                """,
                issue_scope,
            ).fetchone()
            ready_phases = tuple(READY_PHASES.keys())
            ready_placeholders = ",".join("?" for _ in ready_phases)
            ready_params = [*ready_phases, now, *issue_scope]
            ready_issues = conn.execute(
                f"""
                SELECT id, title, phase, status, priority, updated_at,
                       lock_owner, lock_expires_at, current_run_id, blocked_summary
                FROM issues
                WHERE status = 'open'
                  AND phase IN ({ready_placeholders})
                  AND (lock_expires_at IS NULL OR lock_expires_at < ?)
                  {issue_and}
                ORDER BY priority ASC, updated_at DESC
                LIMIT 25
                """,
                ready_params,
            ).fetchall()
            ready_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM issues
                WHERE status = 'open'
                  AND phase IN ({ready_placeholders})
                  AND (lock_expires_at IS NULL OR lock_expires_at < ?)
                  {issue_and}
                """,
                ready_params,
            ).fetchone()
            recent_completed_runs = conn.execute(
                f"""
                SELECT runs.id, runs.issue_id, issues.title, runs.phase, runs.runner, runs.status,
                       runs.started_at, runs.completed_at, runs.summary
                FROM runs
                JOIN issues ON issues.id = runs.issue_id
                WHERE runs.status != 'running'
                {joined_and}
                ORDER BY COALESCE(runs.completed_at, runs.started_at) DESC
                LIMIT 10
                """,
                issue_scope,
            ).fetchall()
            finalized_scope = [*issue_scope, *issue_scope]
            recently_merged = conn.execute(
                f"""
                WITH finalizations AS (
                    SELECT
                        issues.id AS issue_id,
                        issues.title,
                        issues.repo_path,
                        issues.updated_at,
                        runs.id AS run_id,
                        runs.phase,
                        runs.runner,
                        runs.status,
                        runs.started_at,
                        runs.completed_at,
                        runs.summary,
                        runs.next_phase
                    FROM runs
                    JOIN issues ON issues.id = runs.issue_id
                    WHERE issues.status = 'closed'
                      AND issues.phase = 'done'
                      AND runs.phase = 'merge'
                      AND runs.status = 'success'
                      {joined_and}
                    UNION ALL
                    SELECT
                        issues.id AS issue_id,
                        issues.title,
                        issues.repo_path,
                        issues.updated_at,
                        events.run_id AS run_id,
                        'pr_monitor' AS phase,
                        'pull-request-monitor' AS runner,
                        'success' AS status,
                        NULL AS started_at,
                        events.created_at AS completed_at,
                        events.message AS summary,
                        'done' AS next_phase
                    FROM events
                    JOIN issues ON issues.id = events.issue_id
                    WHERE issues.status = 'closed'
                      AND issues.phase = 'done'
                      AND events.event_type = 'pull_request.closed'
                      {joined_and}
                ),
                ranked_finalizations AS (
                    SELECT
                        issue_id,
                        title,
                        repo_path,
                        updated_at,
                        run_id,
                        phase,
                        runner,
                        status,
                        started_at,
                        completed_at,
                        summary,
                        next_phase,
                        ROW_NUMBER() OVER (
                            PARTITION BY issue_id
                            ORDER BY COALESCE(completed_at, started_at, updated_at) DESC,
                                     COALESCE(started_at, completed_at, updated_at) DESC,
                                     COALESCE(run_id, '') DESC
                        ) AS row_num
                    FROM finalizations
                )
                SELECT issue_id, title, repo_path, updated_at, run_id, phase, runner, status,
                       started_at, completed_at, summary, next_phase
                FROM ranked_finalizations
                WHERE row_num = 1
                ORDER BY COALESCE(completed_at, started_at, updated_at) DESC,
                         COALESCE(started_at, completed_at, updated_at) DESC,
                         COALESCE(run_id, '') DESC
                LIMIT 10
                """,
                finalized_scope,
            ).fetchall()
        approval_issues = awaiting_plan_approval + awaiting_merge_approval
        approval_count = int(awaiting_plan_approval_count["count"]) + int(awaiting_merge_approval_count["count"])
        return {
            "phase_counts": phase_counts,
            "active_locks": active_locks,
            "recent_runs": recent_runs,
            "recent_events": recent_events,
            "open_issues": open_issues,
            "active_work": active_locks,
            "approval_issues": approval_issues,
            "awaiting_plan_approval": awaiting_plan_approval,
            "awaiting_merge_approval": awaiting_merge_approval,
            "human_input_needed": human_input_needed,
            "draft_issues": draft_issues,
            "draft_count": int(draft_count["count"]),
            "blocked_issues": blocked_issues,
            "ready_issues": ready_issues,
            "manager_bucket_counts": {
                "approval_needed": approval_count,
                "awaiting_plan_approval": int(awaiting_plan_approval_count["count"]),
                "awaiting_merge_approval": int(awaiting_merge_approval_count["count"]),
                "human_input_needed": int(human_input_needed_count["count"]),
                "draft": int(draft_count["count"]),
                "blocked": int(blocked_count["count"]),
                "ready": int(ready_count["count"]),
            },
            "recent_completed_runs": recent_completed_runs,
            "recently_merged": recently_merged,
        }

    def acquire_lock(
        self,
        issue_id: int,
        owner: str,
        ttl_seconds: int,
        run_id: str,
        expected_phase: str | None = None,
        mark_scheduled: bool = False,
    ) -> bool:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
        now = utc_now_iso()
        scheduled_at = datetime.now(timezone.utc).isoformat() if mark_scheduled else None
        phase_clause = "AND phase = ?" if expected_phase is not None else ""
        scheduled_assignment = ", last_scheduled_at = ?" if mark_scheduled else ""
        params: list[object] = [owner, expires_at, run_id, now]
        if mark_scheduled:
            params.append(scheduled_at)
        params.extend([issue_id, now])
        if expected_phase is not None:
            params.append(expected_phase)
        with self.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE issues
                SET lock_owner = ?, lock_expires_at = ?, current_run_id = ?, updated_at = ?{scheduled_assignment}
                WHERE id = ?
                  AND (lock_expires_at IS NULL OR lock_expires_at < ?)
                  {phase_clause}
                """,
                params,
            )
            if cur.rowcount == 1:
                self._add_event(conn, issue_id, run_id, "lock.acquired", f"{owner} acquired lock until {expires_at}")
                return True
            return False

    def refresh_run_lock(self, issue_id: int, owner: str, run_id: str, ttl_seconds: int) -> bool:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
        now = utc_now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE issues
                SET lock_expires_at = ?, updated_at = ?
                WHERE id = ? AND lock_owner = ? AND current_run_id = ?
                """,
                (expires_at, now, issue_id, owner, run_id),
            )
            return cur.rowcount == 1

    def refresh_issue_lock(
        self,
        issue_id: int,
        owner: str,
        ttl_seconds: int,
        *,
        expected_phase: str | None,
        expected_run_id: str | None,
    ) -> Issue | None:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
        now = utc_now_iso()
        params: list[object] = [expires_at, now, issue_id, owner]
        phase_guard = "" if expected_phase is None else _expected_value_guard("phase", expected_phase, params)
        run_guard = _expected_value_guard("current_run_id", expected_run_id, params)
        with self.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE issues
                SET lock_expires_at = ?, updated_at = ?
                WHERE id = ? AND lock_owner = ?
                  {phase_guard}
                  {run_guard}
                """,
                params,
            )
            if cur.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            return Issue.from_row(row)

    def begin_reset_issue(self, issue_id: int, owner: str, ttl_seconds: int) -> Issue:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            prior_issue = Issue.from_row(row)
            cur = conn.execute(
                """
                UPDATE issues
                SET lock_owner = ?, lock_expires_at = ?, current_run_id = NULL, updated_at = ?
                WHERE id = ?
                  AND (lock_expires_at IS NULL OR lock_expires_at < ?)
                """,
                (owner, expires_at, now, issue_id, now),
            )
            if cur.rowcount != 1:
                locked = conn.execute(
                    "SELECT lock_owner, lock_expires_at FROM issues WHERE id = ?",
                    (issue_id,),
                ).fetchone()
                raise ValueError(
                    f"Cannot reset issue {issue_id} while it has an active lock "
                    f"held by {locked['lock_owner'] or 'unknown'} until {locked['lock_expires_at']}"
                )
        return prior_issue

    def begin_delete_issue(self, issue_id: int, owner: str, ttl_seconds: int) -> Issue:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            prior_issue = Issue.from_row(row)
            cur = conn.execute(
                """
                UPDATE issues
                SET lock_owner = ?, lock_expires_at = ?, current_run_id = NULL, updated_at = ?
                WHERE id = ?
                  AND (lock_expires_at IS NULL OR lock_expires_at < ?)
                """,
                (owner, expires_at, now, issue_id, now),
            )
            if cur.rowcount != 1:
                locked = conn.execute(
                    "SELECT lock_owner, lock_expires_at FROM issues WHERE id = ?",
                    (issue_id,),
                ).fetchone()
                raise ValueError(
                    f"Cannot delete issue {issue_id} while it has an active lock "
                    f"held by {locked['lock_owner'] or 'unknown'} until {locked['lock_expires_at']}"
                )
        return prior_issue

    def complete_reset_issue_to_draft(self, issue_id: int, owner: str, message: str) -> tuple[Issue, int, int]:
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            if row["lock_owner"] != owner:
                raise RuntimeError(f"Reset reservation for issue {issue_id} is no longer held by this reset operation")

            conn.execute("DELETE FROM human_input_requests WHERE issue_id = ?", (issue_id,))
            run_cur = conn.execute("DELETE FROM runs WHERE issue_id = ?", (issue_id,))
            deleted_runs = int(run_cur.rowcount if run_cur.rowcount is not None else 0)
            event_cur = conn.execute("DELETE FROM events WHERE issue_id = ?", (issue_id,))
            deleted_events = int(event_cur.rowcount if event_cur.rowcount is not None else 0)
            cur = conn.execute(
                """
                UPDATE issues
                SET phase = 'draft',
                    status = 'open',
                    lock_owner = NULL,
                    lock_expires_at = NULL,
                    current_run_id = NULL,
                    last_scheduled_at = NULL,
                    blocked_summary = NULL,
                    updated_at = ?
                WHERE id = ? AND lock_owner = ?
                """,
                (now, issue_id, owner),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Reset reservation for issue {issue_id} was lost before completion")
            self._add_event(conn, issue_id, None, "issue.reset_to_draft", message)
            updated = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return Issue.from_row(updated), deleted_runs, deleted_events

    def complete_delete_issue(self, issue_id: int, owner: str) -> tuple[int, int]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            if row["lock_owner"] != owner:
                raise RuntimeError(f"Delete reservation for issue {issue_id} is no longer held by this delete operation")

            conn.execute("DELETE FROM human_input_requests WHERE issue_id = ?", (issue_id,))
            run_cur = conn.execute("DELETE FROM runs WHERE issue_id = ?", (issue_id,))
            deleted_runs = int(run_cur.rowcount if run_cur.rowcount is not None else 0)
            event_cur = conn.execute("DELETE FROM events WHERE issue_id = ?", (issue_id,))
            deleted_events = int(event_cur.rowcount if event_cur.rowcount is not None else 0)
            cur = conn.execute("DELETE FROM issues WHERE id = ? AND lock_owner = ?", (issue_id, owner))
            if cur.rowcount != 1:
                raise RuntimeError(f"Delete reservation for issue {issue_id} was lost before completion")
        return deleted_runs, deleted_events

    def release_lock(self, issue_id: int, owner: str, run_id: str | None = None) -> None:
        now = utc_now_iso()
        run_guard = ""
        params: list[object] = [now, issue_id, owner]
        if run_id is not None:
            run_guard = "AND current_run_id = ?"
            params.append(run_id)
        with self.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE issues
                SET lock_owner = NULL, lock_expires_at = NULL, current_run_id = NULL, updated_at = ?
                WHERE id = ? AND lock_owner = ?
                  {run_guard}
                """,
                params,
            )
            if cur.rowcount == 1:
                self._add_event(conn, issue_id, run_id, "lock.released", f"{owner} released lock")

    def claim_pr_monitor_issue(self, issue_id: int, owner: str, ttl_seconds: int, monitor_id: str) -> Issue | None:
        if not self.acquire_lock(
            issue_id,
            owner,
            ttl_seconds,
            monitor_id,
            expected_phase="awaiting_pr_closure",
            mark_scheduled=True,
        ):
            return None
        return self.get_issue(issue_id)

    def refresh_pr_monitor_issue(self, issue_id: int, owner: str, ttl_seconds: int, monitor_id: str) -> Issue | None:
        return self.refresh_issue_lock(
            issue_id,
            owner,
            ttl_seconds,
            expected_phase="awaiting_pr_closure",
            expected_run_id=monitor_id,
        )

    def record_event(self, issue_id: int, event_type: str, message: str, run_id: str | None = None) -> None:
        with self.connect() as conn:
            self._add_event(conn, issue_id, run_id, event_type, message)

    def transition_issue(
        self,
        issue_id: int,
        next_phase: str,
        run_id: str | None = None,
        message: str | None = None,
        *,
        blocked_summary: str | None = None,
    ) -> Issue:
        issue = self.get_issue(issue_id)
        if issue.phase == "awaiting_human_input":
            raise ValueError(
                f"Issue {issue.id} is awaiting human input; use answer-human-input to resume from the pending request"
            )
        if next_phase == "awaiting_human_input":
            raise ValueError(
                "Cannot transition to awaiting_human_input directly; "
                "human input requests must be created atomically by a structured agent request"
            )
        validate_transition(issue.phase, next_phase)
        status = "closed" if next_phase == "done" else "open"
        stored_blocked_summary = (
            summarize_blocked_reason(blocked_summary or message)
            if next_phase == "blocked"
            else None
        )
        now = utc_now_iso()
        with self.connect() as conn:
            if run_id is None:
                cur = conn.execute(
                    "UPDATE issues SET phase = ?, status = ?, blocked_summary = ?, updated_at = ? WHERE id = ?",
                    (next_phase, status, stored_blocked_summary, now, issue_id),
                )
            else:
                cur = conn.execute(
                    """
                    UPDATE issues
                    SET phase = ?, status = ?, blocked_summary = ?, updated_at = ?
                    WHERE id = ? AND current_run_id = ?
                    """,
                    (next_phase, status, stored_blocked_summary, now, issue_id, run_id),
                )
            if cur.rowcount != 1:
                raise RuntimeError(f"Run {run_id} is no longer current for issue {issue_id}")
            self._add_event(
                conn,
                issue_id,
                run_id,
                "issue.transitioned",
                message or f"Transitioned {issue.phase} -> {next_phase}",
            )
        return self.get_issue(issue_id)

    def stop_issue(
        self,
        issue_id: int,
        message: str,
        *,
        stopped_by: str = "cli",
    ) -> StopIssueResult:
        cleaned_message = message.strip()
        if not cleaned_message:
            raise ValueError("Stop message is required")
        cleaned_stopped_by = stopped_by.strip() or "unknown"
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            issue = Issue.from_row(row)
            if issue.status == "closed" or issue.phase == "done":
                raise ValueError(f"Issue {issue.id} is already closed and cannot be stopped")
            if issue.phase == "draft":
                raise ValueError(f"Issue {issue.id} is a draft and is already inactive")
            if issue.phase == "blocked":
                return StopIssueResult(issue.id, issue.phase, issue, None)
            if issue.current_run_id is not None:
                raise ValueError(f"Cannot stop issue {issue.id} while it has an active run {issue.current_run_id}")
            if issue.lock_expires_at is not None:
                raise ValueError(
                    f"Cannot stop issue {issue.id} while it has an active lock "
                    f"held by {issue.lock_owner or 'unknown'} until {issue.lock_expires_at}"
                )
            validate_transition(issue.phase, "blocked")

            stopped_request: HumanInputRequest | None = None
            if issue.phase == "awaiting_human_input":
                request_row = conn.execute(
                    """
                    SELECT * FROM human_input_requests
                    WHERE issue_id = ? AND status = 'pending'
                    ORDER BY created_at ASC, id ASC
                    LIMIT 1
                    """,
                    (issue_id,),
                ).fetchone()
                if request_row is not None:
                    request = HumanInputRequest.from_row(request_row)
                    cur = conn.execute(
                        """
                        UPDATE human_input_requests
                        SET status = 'stopped', answered_at = ?, answer = ?, answered_by = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (now, cleaned_message, cleaned_stopped_by, request.id),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"Human input request {request.id} was already resolved")
                    stopped_row = conn.execute("SELECT * FROM human_input_requests WHERE id = ?", (request.id,)).fetchone()
                    stopped_request = HumanInputRequest.from_row(stopped_row)
                    self._add_event(
                        conn,
                        issue_id,
                        stopped_request.run_id,
                        "human_input.stopped",
                        f"Stopped human input request {stopped_request.id}",
                    )

            cur = conn.execute(
                """
                UPDATE issues
                SET phase = 'blocked',
                    status = 'open',
                    lock_owner = NULL,
                    lock_expires_at = NULL,
                    current_run_id = NULL,
                    blocked_summary = ?,
                    updated_at = ?
                WHERE id = ? AND phase = ?
                  AND current_run_id IS NULL
                  AND lock_expires_at IS NULL
                """,
                (summarize_blocked_reason(cleaned_message), now, issue_id, issue.phase),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Issue {issue.id} could not be stopped because its state changed")
            self._add_event(conn, issue_id, None, "issue.transitioned", f"Stopped issue; transitioned {issue.phase} -> blocked")
            self._add_event(conn, issue_id, None, "issue.stopped", cleaned_message)
            updated_row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return StopIssueResult(
            issue_id=issue_id,
            prior_phase=issue.phase,
            issue=Issue.from_row(updated_row),
            stopped_human_input_request=stopped_request,
        )

    def reject_plan(self, issue_id: int, feedback: str, run_id: str | None = None) -> Issue:
        cleaned = feedback.strip()
        if not cleaned:
            raise ValueError("Plan rejection feedback is required")
        issue = self.get_issue(issue_id)
        if issue.phase != "awaiting_plan_approval":
            raise ValueError(f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_plan_approval'")
        validate_transition(issue.phase, "ready_for_plan")
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE issues SET phase = ?, status = ?, updated_at = ? WHERE id = ?",
                ("ready_for_plan", "open", now, issue_id),
            )
            self._add_event(conn, issue_id, run_id, "plan.rejected", cleaned)
        return self.get_issue(issue_id)

    def get_pending_human_input_request(self, issue_id: int) -> HumanInputRequest | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM human_input_requests
                WHERE issue_id = ? AND status = 'pending'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (issue_id,),
            ).fetchone()
        return HumanInputRequest.from_row(row) if row is not None else None

    def list_human_input_requests(self, issue_id: int) -> list[HumanInputRequest]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM human_input_requests
                WHERE issue_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (issue_id,),
            ).fetchall()
        return [HumanInputRequest.from_row(row) for row in rows]

    def answer_human_input_request(
        self,
        issue_id: int,
        answer: str,
        *,
        answered_by: str = "cli",
    ) -> tuple[Issue, HumanInputRequest]:
        cleaned_answer = answer.strip()
        if not cleaned_answer:
            raise ValueError("Human input answer is required")
        cleaned_answered_by = answered_by.strip() or "unknown"
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            issue_row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if issue_row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            issue = Issue.from_row(issue_row)
            if issue.phase != "awaiting_human_input":
                raise ValueError(f"Issue {issue.id} is in phase {issue.phase!r}, not 'awaiting_human_input'")
            if issue.lock_expires_at is not None and issue.lock_expires_at >= now:
                raise ValueError(
                    f"Cannot answer human input for issue {issue.id} while it has an active lock "
                    f"held by {issue.lock_owner or 'unknown'} until {issue.lock_expires_at}"
                )
            request_row = conn.execute(
                """
                SELECT * FROM human_input_requests
                WHERE issue_id = ? AND status = 'pending'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (issue_id,),
            ).fetchone()
            if request_row is None:
                raise ValueError(f"Issue {issue.id} has no pending human input request")
            request = HumanInputRequest.from_row(request_row)
            validate_human_input_resume_phase(request.requested_by_phase, request.resume_phase)
            validate_transition(issue.phase, request.resume_phase)
            cur = conn.execute(
                """
                UPDATE human_input_requests
                SET status = 'answered', answered_at = ?, answer = ?, answered_by = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, cleaned_answer, cleaned_answered_by, request.id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Human input request {request.id} was already answered")
            issue_cur = conn.execute(
                """
                UPDATE issues
                SET phase = ?, status = 'open', lock_owner = NULL, lock_expires_at = NULL,
                    current_run_id = NULL, updated_at = ?
                WHERE id = ? AND phase = 'awaiting_human_input'
                """,
                (request.resume_phase, now, issue_id),
            )
            if issue_cur.rowcount != 1:
                raise RuntimeError(f"Issue {issue.id} is no longer awaiting human input")
            self._add_event(
                conn,
                issue_id,
                request.run_id,
                "human_input.answered",
                f"Answered human input request {request.id}; resuming at {request.resume_phase}",
            )
            self._add_event(
                conn,
                issue_id,
                request.run_id,
                "issue.transitioned",
                f"Answered human input; transitioned awaiting_human_input -> {request.resume_phase}",
            )
            updated_row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            answered_row = conn.execute("SELECT * FROM human_input_requests WHERE id = ?", (request.id,)).fetchone()
        return Issue.from_row(updated_row), HumanInputRequest.from_row(answered_row)

    def create_run(self, run_id: str, issue_id: int, phase: str, runner: str) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, issue_id, phase, runner, status, started_at)
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (run_id, issue_id, phase, runner, now),
            )
            self._add_event(conn, issue_id, run_id, "run.started", f"Started {phase} with {runner}")

    def complete_run(
        self,
        run_id: str,
        issue_id: int,
        status: str,
        summary: str,
        artifact_path: str | None,
        error: str | None = None,
        next_phase: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            current = conn.execute(
                "SELECT current_run_id FROM issues WHERE id = ?",
                (issue_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"Issue not found: {issue_id}")
            if current["current_run_id"] != run_id:
                raise RuntimeError(f"Run {run_id} is no longer current for issue {issue_id}")
            cur = conn.execute(
                """
                UPDATE runs
                SET status = ?, completed_at = ?, summary = ?, artifact_path = ?, error = ?, next_phase = ?
                WHERE id = ?
                """,
                (status, now, summary, artifact_path, error, next_phase, run_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Run not found or already reset: {run_id}")
            self._add_event(conn, issue_id, run_id, f"run.{status}", summary)

    def complete_run_and_request_human_input(
        self,
        run_id: str,
        issue_id: int,
        summary: str,
        artifact_path: str | None,
        request: HumanInputRequestDraft,
    ) -> HumanInputRequest:
        self._validate_human_input_draft(request)
        now = utc_now_iso()
        request_id = str(uuid.uuid4())
        options_json = json.dumps(list(request.options), sort_keys=True) if request.options else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            issue_row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if issue_row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            issue = Issue.from_row(issue_row)
            run_row = conn.execute("SELECT * FROM runs WHERE id = ? AND issue_id = ?", (run_id, issue_id)).fetchone()
            if run_row is None:
                raise RuntimeError(f"Run not found or already reset: {run_id}")
            if issue.current_run_id != run_id:
                raise RuntimeError(f"Run {run_id} is no longer current for issue {issue_id}")
            if str(run_row["phase"]) != request.requested_by_phase:
                raise ValueError(
                    f"Human input requested by phase {request.requested_by_phase!r} does not match run phase "
                    f"{str(run_row['phase'])!r}"
                )
            validate_transition(issue.phase, "awaiting_human_input")
            validate_human_input_resume_phase(request.requested_by_phase, request.resume_phase)
            run_cur = conn.execute(
                """
                UPDATE runs
                SET status = 'success', completed_at = ?, summary = ?, artifact_path = ?, error = NULL,
                    next_phase = 'awaiting_human_input'
                WHERE id = ? AND status = 'running'
                """,
                (now, summary, artifact_path, run_id),
            )
            if run_cur.rowcount != 1:
                raise RuntimeError(f"Run {run_id} was not running")
            conn.execute(
                """
                INSERT INTO human_input_requests (
                  id, issue_id, run_id, requested_by_phase, resume_phase, question, rationale,
                  requested_decision, options_json, context, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    request_id,
                    issue_id,
                    run_id,
                    request.requested_by_phase,
                    request.resume_phase,
                    request.question,
                    request.rationale,
                    request.requested_decision,
                    options_json,
                    request.context,
                    now,
                ),
            )
            issue_cur = conn.execute(
                """
                UPDATE issues
                SET phase = 'awaiting_human_input',
                    status = 'open',
                    lock_owner = NULL,
                    lock_expires_at = NULL,
                    current_run_id = NULL,
                    updated_at = ?
                WHERE id = ? AND current_run_id = ?
                """,
                (now, issue_id, run_id),
            )
            if issue_cur.rowcount != 1:
                raise RuntimeError(f"Run {run_id} is no longer current for issue {issue_id}")
            self._add_event(conn, issue_id, run_id, "run.success", summary)
            self._add_event(
                conn,
                issue_id,
                run_id,
                "human_input.requested",
                f"Requested human input {request_id}; resume phase {request.resume_phase}",
            )
            self._add_event(
                conn,
                issue_id,
                run_id,
                "issue.transitioned",
                f"Transitioned {issue.phase} -> awaiting_human_input",
            )
            request_row = conn.execute("SELECT * FROM human_input_requests WHERE id = ?", (request_id,)).fetchone()
        return HumanInputRequest.from_row(request_row)

    def recover_interrupted_issue(
        self,
        issue_id: int,
        *,
        now: str | None = None,
        is_lock_reclaimable: LockReclaimPredicate | None = None,
        terminal_next_phase_resolver: TerminalNextPhaseResolver | None = None,
    ) -> RecoveryResult | None:
        now_iso = now or utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            return self._recover_issue_row(
                conn,
                row,
                now_iso,
                is_lock_reclaimable,
                terminal_next_phase_resolver,
            )

    def recover_interrupted_runs(
        self,
        *,
        now: str | None = None,
        is_lock_reclaimable: LockReclaimPredicate | None = None,
        terminal_next_phase_resolver: TerminalNextPhaseResolver | None = None,
        limit: int | None = None,
    ) -> list[RecoveryResult]:
        now_iso = now or utc_now_iso()
        running_placeholders = ",".join("?" for _ in RUNNING_PHASES.values())
        limit_clause = "" if limit is None else "LIMIT ?"
        params: list[object] = [*RUNNING_PHASES.values()]
        if limit is not None:
            params.append(max(0, limit))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT * FROM issues
                WHERE current_run_id IS NOT NULL
                   OR lock_expires_at IS NOT NULL
                   OR (status = 'open' AND phase IN ({running_placeholders}))
                ORDER BY updated_at ASC, id ASC
                {limit_clause}
                """,
                params,
            ).fetchall()
            results: list[RecoveryResult] = []
            for row in rows:
                result = self._recover_issue_row(
                    conn,
                    row,
                    now_iso,
                    is_lock_reclaimable,
                    terminal_next_phase_resolver,
                )
                if result is not None:
                    results.append(result)
            return results

    def _claim_interrupted_phase_recovery(
        self,
        issue_id: int,
        owner: str,
        ttl_seconds: int,
        *,
        phase: str,
        event_message: str,
        now: str | None = None,
        is_lock_reclaimable: LockReclaimPredicate | None = None,
    ) -> Issue | None:
        now_iso = now or utc_now_iso()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).replace(microsecond=0).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            if str(row["phase"]) != phase or not self._lock_is_recoverable(row, now_iso, is_lock_reclaimable):
                return None
            params: list[object] = [owner, expires_at, now_iso, issue_id, phase]
            guards = [
                _expected_value_guard("current_run_id", row["current_run_id"], params),
                _expected_value_guard("lock_owner", row["lock_owner"], params),
                _expected_value_guard("lock_expires_at", row["lock_expires_at"], params),
            ]
            cur = conn.execute(
                f"""
                UPDATE issues
                SET lock_owner = ?, lock_expires_at = ?, updated_at = ?
                WHERE id = ? AND phase = ?
                  {''.join(guards)}
                """,
                params,
            )
            if cur.rowcount != 1:
                return None
            self._add_event(
                conn,
                issue_id,
                row["current_run_id"],
                "lock.acquired",
                f"{owner} {event_message} until {expires_at}",
            )
            claimed = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            return Issue.from_row(claimed)

    def claim_interrupted_merge_recovery(
        self,
        issue_id: int,
        owner: str,
        ttl_seconds: int,
        *,
        now: str | None = None,
        is_lock_reclaimable: LockReclaimPredicate | None = None,
    ) -> Issue | None:
        now_iso = now or utc_now_iso()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).replace(microsecond=0).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            if str(row["phase"]) != "merging" or not self._lock_is_recoverable(row, now_iso, is_lock_reclaimable):
                return None
            params: list[object] = [owner, expires_at, now_iso, issue_id, "merging"]
            guards = [
                _expected_value_guard("current_run_id", row["current_run_id"], params),
                _expected_value_guard("lock_owner", row["lock_owner"], params),
                _expected_value_guard("lock_expires_at", row["lock_expires_at"], params),
            ]
            cur = conn.execute(
                f"""
                UPDATE issues
                SET lock_owner = ?, lock_expires_at = ?, updated_at = ?
                WHERE id = ? AND phase = ?
                  {''.join(guards)}
                """,
                params,
            )
            if cur.rowcount != 1:
                return None
            self._add_event(
                conn,
                issue_id,
                row["current_run_id"],
                "lock.acquired",
                f"{owner} claimed merge recovery until {expires_at}",
            )
            claimed = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            return Issue.from_row(claimed)

    def claim_interrupted_review_source_sync_recovery(
        self,
        issue_id: int,
        owner: str,
        ttl_seconds: int,
        *,
        now: str | None = None,
        is_lock_reclaimable: LockReclaimPredicate | None = None,
    ) -> Issue | None:
        return self._claim_interrupted_phase_recovery(
            issue_id,
            owner,
            ttl_seconds,
            phase="reviewing",
            event_message="claimed review source-sync recovery",
            now=now,
            is_lock_reclaimable=is_lock_reclaimable,
        )

    def recover_interrupted_merge(
        self,
        issue_id: int,
        *,
        next_phase: str,
        run_status: str,
        summary: str,
        artifact_path: str | None = None,
        now: str | None = None,
        is_lock_reclaimable: LockReclaimPredicate | None = None,
        claimed_owner: str | None = None,
        claimed_run_id: str | None = None,
    ) -> RecoveryResult | None:
        now_iso = now or utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            claimed_by_owner = (
                claimed_owner is not None
                and row["lock_owner"] == claimed_owner
                and row["current_run_id"] == claimed_run_id
            )
            if str(row["phase"]) != "merging" or (
                not claimed_by_owner and not self._lock_is_recoverable(row, now_iso, is_lock_reclaimable)
            ):
                return None
            run_id = row["current_run_id"]
            if not self._apply_recovery_issue_update(conn, row, next_phase, now_iso, summary):
                return None
            run = self._run_for_recovery(conn, issue_id, run_id, "merge")
            if run is not None:
                if str(run["status"]) == "running":
                    self._complete_recovered_run(
                        conn,
                        run,
                        run_status,
                        summary,
                        next_phase,
                        now_iso,
                        artifact_path,
                        None if run_status == "success" else summary,
                    )
                elif run["next_phase"] is None:
                    conn.execute("UPDATE runs SET next_phase = ? WHERE id = ? AND next_phase IS NULL", (next_phase, run["id"]))
            self._add_event(conn, issue_id, run_id, "issue.recovered", summary)
            return RecoveryResult(
                issue_id=issue_id,
                run_id=run_id,
                previous_phase="merging",
                next_phase=next_phase,
                action="merge_recovered",
                summary=summary,
                agent_phase="merge",
            )

    def recover_interrupted_review_source_sync(
        self,
        issue_id: int,
        *,
        next_phase: str,
        run_status: str,
        summary: str,
        artifact_path: str | None = None,
        now: str | None = None,
        is_lock_reclaimable: LockReclaimPredicate | None = None,
        claimed_owner: str | None = None,
        claimed_run_id: str | None = None,
    ) -> RecoveryResult | None:
        now_iso = now or utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
            if row is None:
                raise KeyError(f"Issue not found: {issue_id}")
            claimed_by_owner = (
                claimed_owner is not None
                and row["lock_owner"] == claimed_owner
                and row["current_run_id"] == claimed_run_id
            )
            if str(row["phase"]) != "reviewing" or (
                not claimed_by_owner and not self._lock_is_recoverable(row, now_iso, is_lock_reclaimable)
            ):
                return None
            run_id = row["current_run_id"]
            if not self._apply_recovery_issue_update(conn, row, next_phase, now_iso, summary):
                return None
            run = self._run_for_recovery(conn, issue_id, run_id, "review")
            if run is not None:
                if str(run["status"]) == "running":
                    self._complete_recovered_run(
                        conn,
                        run,
                        run_status,
                        summary,
                        next_phase,
                        now_iso,
                        artifact_path,
                        None if run_status == "success" else summary,
                    )
                elif run["next_phase"] is None:
                    conn.execute("UPDATE runs SET next_phase = ? WHERE id = ? AND next_phase IS NULL", (next_phase, run["id"]))
            self._add_event(conn, issue_id, run_id, "issue.recovered", summary)
            return RecoveryResult(
                issue_id=issue_id,
                run_id=run_id,
                previous_phase="reviewing",
                next_phase=next_phase,
                action="review_source_sync_recovered",
                summary=summary,
                agent_phase="review",
            )

    def add_event(self, issue_id: int, event_type: str, message: str, run_id: str | None = None) -> None:
        with self.connect() as conn:
            self._add_event(conn, issue_id, run_id, event_type, message)

    def list_events(self, issue_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM events WHERE issue_id = ? ORDER BY id ASC",
                (issue_id,),
            ).fetchall()

    def list_runs(self, issue_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM runs WHERE issue_id = ? ORDER BY started_at ASC",
                (issue_id,),
            ).fetchall()

    def _recover_issue_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        now_iso: str,
        is_lock_reclaimable: LockReclaimPredicate | None,
        terminal_next_phase_resolver: TerminalNextPhaseResolver | None,
    ) -> RecoveryResult | None:
        phase = str(row["phase"])
        issue_id = int(row["id"])
        if phase == "merging":
            return None
        if phase in READY_PHASES:
            return self._recover_ready_issue(conn, row, now_iso, is_lock_reclaimable)

        ready_phase = ready_phase_for_running_phase(phase)
        if ready_phase is None:
            return self._recover_post_transition_issue(conn, row, now_iso, is_lock_reclaimable)
        if not self._lock_is_recoverable(row, now_iso, is_lock_reclaimable):
            return None

        agent_phase = agent_phase_for_running_phase(phase)
        run = self._run_for_recovery(conn, issue_id, row["current_run_id"], agent_phase)
        run_id = str(run["id"]) if run is not None else row["current_run_id"]
        blocked_summary = None
        if run is None:
            next_phase = ready_phase
            action = "running_phase_reset"
            summary = f"Recovered interrupted {phase} state without a run row; returned to {ready_phase}."
        elif str(run["status"]) == "running":
            next_phase = ready_phase
            action = "run_interrupted"
            summary = f"Recovered interrupted {agent_phase} run {run['id']}; returned to {ready_phase}."
        else:
            next_phase, action, summary, blocked_summary = self._terminal_recovery_target(
                run, phase, terminal_next_phase_resolver
            )

        if not self._apply_recovery_issue_update(conn, row, next_phase, now_iso, summary, blocked_summary):
            return None
        if run is not None and str(run["status"]) == "running":
            self._complete_recovered_run(conn, run, "interrupted", summary, next_phase, now_iso, None, summary)
        elif run is not None and run["next_phase"] is None:
            conn.execute("UPDATE runs SET next_phase = ? WHERE id = ? AND next_phase IS NULL", (next_phase, run["id"]))
        self._add_event(conn, issue_id, run_id, "issue.recovered", summary)
        return RecoveryResult(issue_id, run_id, phase, next_phase, action, summary, agent_phase)

    def _recover_ready_issue(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        now_iso: str,
        is_lock_reclaimable: LockReclaimPredicate | None,
    ) -> RecoveryResult | None:
        if row["current_run_id"] is None and row["lock_expires_at"] is None:
            return None
        if not self._lock_is_recoverable(row, now_iso, is_lock_reclaimable):
            return None
        phase = str(row["phase"])
        issue_id = int(row["id"])
        agent_phase = READY_PHASES.get(phase)
        run = self._run_for_recovery(conn, issue_id, row["current_run_id"], agent_phase)
        run_id = str(run["id"]) if run is not None else row["current_run_id"]
        summary = f"Cleared stale scheduling lock for issue {issue_id} in {phase}."
        action = "ready_lock_cleared"
        if run is not None and str(run["status"]) == "running":
            summary = f"Recovered interrupted {agent_phase} run {run['id']} before phase transition; kept {phase} ready."
            action = "ready_run_interrupted"
        if not self._apply_recovery_issue_update(conn, row, phase, now_iso, summary):
            return None
        if run is not None and str(run["status"]) == "running":
            self._complete_recovered_run(conn, run, "interrupted", summary, phase, now_iso, None, summary)
        self._add_event(conn, issue_id, run_id, "issue.recovered", summary)
        return RecoveryResult(issue_id, run_id, phase, phase, action, summary, agent_phase)

    def _recover_post_transition_issue(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        now_iso: str,
        is_lock_reclaimable: LockReclaimPredicate | None,
    ) -> RecoveryResult | None:
        if row["current_run_id"] is None and row["lock_expires_at"] is None:
            return None
        if not self._lock_is_recoverable(row, now_iso, is_lock_reclaimable):
            return None
        phase = str(row["phase"])
        issue_id = int(row["id"])
        run = self._run_for_recovery(conn, issue_id, row["current_run_id"], None)
        if run is None:
            return self._recover_pr_monitor_post_transition_issue(conn, row, now_iso)
        if str(run["status"]) == "running":
            return None
        if not self._run_reached_issue_phase(run, phase):
            return None
        run_id = str(run["id"])
        summary = f"Cleared stale lock for completed {run['phase']} run {run_id} after issue reached {phase}."
        if not self._apply_recovery_issue_update(conn, row, phase, now_iso, summary):
            return None
        if run["next_phase"] is None:
            conn.execute("UPDATE runs SET next_phase = ? WHERE id = ? AND next_phase IS NULL", (phase, run_id))
        self._add_event(conn, issue_id, run_id, "issue.recovered", summary)
        return RecoveryResult(issue_id, run_id, phase, phase, "post_transition_lock_cleared", summary, str(run["phase"]))

    def _recover_pr_monitor_post_transition_issue(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        now_iso: str,
    ) -> RecoveryResult | None:
        phase = str(row["phase"])
        run_id = row["current_run_id"]
        if (
            phase not in _PR_MONITOR_POST_TRANSITION_PHASES
            or not isinstance(run_id, str)
            or not run_id.startswith(_PR_MONITOR_RUN_ID_PREFIX)
        ):
            return None
        issue_id = int(row["id"])
        summary = f"Cleared stale pull request monitor lock {run_id} after issue reached {phase}."
        blocked_summary = row["blocked_summary"] if phase == "blocked" else None
        if not self._apply_recovery_issue_update(conn, row, phase, now_iso, summary, blocked_summary):
            return None
        self._add_event(conn, issue_id, run_id, "issue.recovered", summary)
        return RecoveryResult(
            issue_id,
            run_id,
            phase,
            phase,
            "pr_monitor_post_transition_lock_cleared",
            summary,
            "pr_monitor",
        )

    def _terminal_recovery_target(
        self,
        run: sqlite3.Row,
        running_phase: str,
        terminal_next_phase_resolver: TerminalNextPhaseResolver | None,
    ) -> tuple[str, str, str, str | None]:
        stored_next_phase = run["next_phase"]
        if stored_next_phase:
            next_phase = str(stored_next_phase)
            return self._validated_terminal_recovery_target(run, running_phase, next_phase)
        if str(run["status"]) != "success":
            summary = (
                f"Recovered terminal {run['phase']} run {run['id']} with status {run['status']}; "
                "blocked for inspection."
            )
            return (
                "blocked",
                "terminal_run_blocked",
                summary,
                self._terminal_run_blocked_summary(run, summary, include_artifact_text=True),
            )
        resolved = terminal_next_phase_resolver(run) if terminal_next_phase_resolver is not None else None
        if resolved is None and str(run["runner"]) == "dry-run":
            resolved = default_next_phase(str(run["phase"]))
        if resolved is not None:
            return self._validated_terminal_recovery_target(run, running_phase, resolved)
        summary = (
            f"Recovered completed {run['phase']} run {run['id']} but could not determine its next phase; "
            "blocked for inspection."
        )
        return (
            "blocked",
            "terminal_run_blocked",
            summary,
            self._terminal_run_blocked_summary(run, summary),
        )

    @staticmethod
    def _validated_terminal_recovery_target(
        run: sqlite3.Row,
        running_phase: str,
        next_phase: str,
    ) -> tuple[str, str, str, str | None]:
        if next_phase == "awaiting_human_input":
            summary = (
                f"Recovered completed {run['phase']} run {run['id']} that requested human input, "
                "but no pending request was committed atomically; blocked for rerun or inspection."
            )
            return (
                "blocked",
                "terminal_run_blocked",
                summary,
                IssueStore._terminal_run_blocked_summary(run, summary),
            )
        try:
            validate_transition(running_phase, next_phase)
        except ValueError as exc:
            summary = (
                f"Recovered terminal {run['phase']} run {run['id']} with invalid next phase "
                f"{next_phase!r} from {running_phase}; blocked for inspection. {exc}"
            )
            return (
                "blocked",
                "terminal_run_blocked",
                summary,
                IssueStore._terminal_run_blocked_summary(run, summary),
            )
        blocked_summary = None
        if next_phase == "blocked":
            blocked_summary = IssueStore._terminal_run_blocked_summary(
                run,
                f"Recovered completed {run['phase']} run {run['id']}; advanced to {next_phase}.",
                include_artifact_text=True,
            )
        return (
            next_phase,
            "run_forward_completed",
            f"Recovered completed {run['phase']} run {run['id']}; advanced to {next_phase}.",
            blocked_summary,
        )

    @staticmethod
    def _terminal_run_blocked_summary(
        run: sqlite3.Row,
        fallback_summary: str,
        *,
        include_artifact_text: bool = False,
    ) -> str:
        artifact_text = None
        artifact_path = run["artifact_path"]
        if artifact_path:
            path = Path(str(artifact_path))
            if path.is_file():
                try:
                    artifact_text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    artifact_text = None
                artifact_summary = extract_blocked_summary(artifact_text)
                if artifact_summary:
                    return artifact_summary
        error = run["error"]
        if error:
            return summarize_blocked_reason(str(error))
        if include_artifact_text and artifact_text:
            return summarize_blocked_reason(artifact_text)
        if str(run["status"]) != "success" and run["summary"]:
            return summarize_blocked_reason(str(run["summary"]))
        return summarize_blocked_reason(fallback_summary)

    @staticmethod
    def _lock_is_recoverable(
        row: sqlite3.Row,
        now_iso: str,
        is_lock_reclaimable: LockReclaimPredicate | None,
    ) -> bool:
        owner = row["lock_owner"]
        expires_at = row["lock_expires_at"]
        if expires_at is None:
            return True
        if is_lock_reclaimable is not None and is_lock_reclaimable(owner):
            return True
        if str(expires_at) < now_iso:
            return not is_live_same_host_owner(owner)
        return False

    @staticmethod
    def _run_for_recovery(
        conn: sqlite3.Connection,
        issue_id: int,
        current_run_id: str | None,
        agent_phase: str | None,
    ) -> sqlite3.Row | None:
        if current_run_id:
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ? AND issue_id = ?",
                (current_run_id, issue_id),
            ).fetchone()
            return row
        if agent_phase is None:
            return None

        transition = conn.execute(
            """
            SELECT run_id FROM events
            WHERE issue_id = ? AND event_type = 'issue.transitioned' AND run_id IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (issue_id,),
        ).fetchone()
        if transition is not None:
            return conn.execute(
                "SELECT * FROM runs WHERE id = ? AND issue_id = ? AND phase = ?",
                (transition["run_id"], issue_id, agent_phase),
            ).fetchone()

        return conn.execute(
            """
            SELECT * FROM runs
            WHERE issue_id = ? AND phase = ? AND status = 'running'
            ORDER BY started_at DESC, id DESC
            LIMIT 1
            """,
            (issue_id, agent_phase),
        ).fetchone()

    @staticmethod
    def _apply_recovery_issue_update(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        next_phase: str,
        now_iso: str,
        summary: str | None,
        blocked_summary: str | None = None,
    ) -> bool:
        stored_blocked_summary = (
            summarize_blocked_reason(blocked_summary or summary) if next_phase == "blocked" else None
        )
        params: list[object] = [
            next_phase,
            "closed" if next_phase == "done" else "open",
            stored_blocked_summary,
            now_iso,
            int(row["id"]),
            row["phase"],
        ]
        guards = [
            _expected_value_guard("current_run_id", row["current_run_id"], params),
            _expected_value_guard("lock_owner", row["lock_owner"], params),
            _expected_value_guard("lock_expires_at", row["lock_expires_at"], params),
        ]
        cur = conn.execute(
            f"""
            UPDATE issues
            SET phase = ?, status = ?, lock_owner = NULL, lock_expires_at = NULL,
                current_run_id = NULL, blocked_summary = ?, updated_at = ?
            WHERE id = ? AND phase = ?
              {''.join(guards)}
            """,
            params,
        )
        return cur.rowcount == 1

    def _complete_recovered_run(
        self,
        conn: sqlite3.Connection,
        run: sqlite3.Row,
        status: str,
        summary: str,
        next_phase: str,
        now_iso: str,
        artifact_path: str | None,
        error: str | None,
    ) -> None:
        cur = conn.execute(
            """
            UPDATE runs
            SET status = ?, completed_at = ?, summary = ?, artifact_path = COALESCE(?, artifact_path),
                error = ?, next_phase = ?
            WHERE id = ? AND status = 'running'
            """,
            (status, now_iso, summary, artifact_path, error, next_phase, run["id"]),
        )
        if cur.rowcount == 1:
            self._add_event(conn, int(run["issue_id"]), str(run["id"]), f"run.{status}", summary)

    @staticmethod
    def _run_reached_issue_phase(run: sqlite3.Row, issue_phase: str) -> bool:
        stored_next_phase = run["next_phase"]
        if stored_next_phase:
            return str(stored_next_phase) == issue_phase
        running_phase = RUNNING_PHASES.get(str(run["phase"]))
        if running_phase is None:
            return False
        try:
            validate_transition(running_phase, issue_phase)
        except ValueError:
            return False
        return True

    @staticmethod
    def _validate_draft_editable(issue: Issue, now: str) -> None:
        if issue.status != "open" or issue.phase != "draft":
            raise ValueError(
                f"Issue {issue.id} is in phase {issue.phase!r} with status {issue.status!r}, not an open draft"
            )
        if issue.lock_expires_at is not None and issue.lock_expires_at >= now:
            raise ValueError(
                f"Cannot edit issue {issue.id} while it has an active lock "
                f"held by {issue.lock_owner or 'unknown'} until {issue.lock_expires_at}"
            )

    @staticmethod
    def _validate_human_input_draft(request: HumanInputRequestDraft) -> None:
        required_fields = {
            "requested_by_phase": request.requested_by_phase,
            "resume_phase": request.resume_phase,
            "question": request.question,
            "rationale": request.rationale,
            "requested_decision": request.requested_decision,
        }
        for field_name, value in required_fields.items():
            if not value.strip():
                raise ValueError(f"Human input request {field_name} is required")
        validate_human_input_resume_phase(request.requested_by_phase, request.resume_phase)

    @staticmethod
    def _migrate_issues_schema(conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(issues)").fetchall()}
        if "last_scheduled_at" not in columns:
            conn.execute("ALTER TABLE issues ADD COLUMN last_scheduled_at TEXT")
        if "blocked_summary" not in columns:
            conn.execute("ALTER TABLE issues ADD COLUMN blocked_summary TEXT")

    @staticmethod
    def _migrate_runs_schema(conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "next_phase" not in columns:
            conn.execute("ALTER TABLE runs ADD COLUMN next_phase TEXT")

    @staticmethod
    def _add_event(
        conn: sqlite3.Connection,
        issue_id: int,
        run_id: str | None,
        event_type: str,
        message: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO events (issue_id, run_id, event_type, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (issue_id, run_id, event_type, message, utc_now_iso()),
        )

    def clear(self) -> None:
        with self.connect() as conn:
            for table in ("human_input_requests", "events", "runs", "issues"):
                conn.execute(f"DELETE FROM {table}")


def _expected_value_guard(field: str, expected: object, params: list[object]) -> str:
    if expected is None:
        return f" AND {field} IS NULL"
    params.append(expected)
    return f" AND {field} = ?"
