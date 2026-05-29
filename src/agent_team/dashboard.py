from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .db import IssueStore


def render_dashboard(store: IssueStore) -> str:
    data = store.dashboard_summary()
    lines: list[str] = []
    lines.append("Agent Team Dashboard")
    lines.append(f"Generated: {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}")
    lines.append("")

    lines.append("Issue counts")
    phase_counts = data["phase_counts"]
    if phase_counts:
        lines.append("status | phase | count")
        lines.append("--- | --- | ---")
        for row in phase_counts:
            lines.append(f"{row['status']} | {row['phase']} | {row['count']}")
    else:
        lines.append("No issues.")
    lines.append("")

    lines.append("Active work")
    active_locks = data["active_locks"]
    if active_locks:
        lines.append("issue | phase | owner | lock expires | title")
        lines.append("--- | --- | --- | --- | ---")
        for row in active_locks:
            lines.append(
                f"{row['id']} | {row['phase']} | {row['lock_owner']} | "
                f"{row['lock_expires_at']} | {_shorten(row['title'], 60)}"
            )
    else:
        lines.append("No active locks/runs.")
    lines.append("")

    lines.append("Draft backlog")
    draft_issues = data["draft_issues"]
    if draft_issues:
        lines.append("issue | priority | updated | title")
        lines.append("--- | --- | --- | ---")
        for row in draft_issues:
            lines.append(
                f"{row['id']} | P{row['priority']} | {row['updated_at']} | "
                f"{_shorten(row['title'], 70)}"
            )
    else:
        lines.append("No draft issues.")
    lines.append("")

    lines.append("Human input needed")
    human_input_needed = data.get("human_input_needed", [])
    if human_input_needed:
        lines.append("issue | priority | resume | question | title")
        lines.append("--- | --- | --- | --- | ---")
        for row in human_input_needed:
            lines.append(
                f"{row['id']} | P{row['priority']} | {row['resume_phase'] or ''} | "
                f"{_shorten(row['question'] or '', 70)} | {_shorten(row['title'], 60)}"
            )
    else:
        lines.append("No issues awaiting human input.")
    lines.append("")

    lines.append("Open issues")
    open_issues = data["open_issues"]
    if open_issues:
        lines.append("issue | priority | phase | updated | title")
        lines.append("--- | --- | --- | --- | ---")
        for row in open_issues:
            lines.append(
                f"{row['id']} | P{row['priority']} | {row['phase']} | "
                f"{row['updated_at']} | {_shorten(row['title'], 70)}"
            )
    else:
        lines.append("No open issues.")
    lines.append("")

    lines.append("Recently merged")
    recently_merged = data["recently_merged"]
    if recently_merged:
        lines.append("issue | merged | run | summary | title")
        lines.append("--- | --- | --- | --- | ---")
        for row in recently_merged:
            lines.append(
                f"{row['issue_id']} | {row['completed_at'] or row['started_at']} | "
                f"{_shorten(row['run_id'], 8)} | {_shorten(row['summary'] or '', 80)} | "
                f"{_shorten(row['title'], 60)}"
            )
    else:
        lines.append("No merged issues yet.")
    lines.append("")

    lines.append("Recent runs")
    recent_runs = data["recent_runs"]
    if recent_runs:
        lines.append("run | issue | phase | runner | status | summary")
        lines.append("--- | --- | --- | --- | --- | ---")
        for row in recent_runs:
            lines.append(
                f"{_shorten(row['id'], 8)} | {row['issue_id']} | {row['phase']} | "
                f"{row['runner']} | {row['status']} | {_shorten(row['summary'] or '', 80)}"
            )
    else:
        lines.append("No runs yet.")
    lines.append("")

    lines.append("Recent events")
    recent_events = data["recent_events"]
    if recent_events:
        lines.append("time | issue | event | message")
        lines.append("--- | --- | --- | ---")
        for row in recent_events:
            lines.append(
                f"{row['created_at']} | {row['issue_id']} | {row['event_type']} | "
                f"{_shorten(row['message'], 90)}"
            )
    else:
        lines.append("No events yet.")
    return "\n".join(lines)


def _shorten(value: object, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"
