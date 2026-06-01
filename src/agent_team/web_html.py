from __future__ import annotations

import html
import json
from typing import Any

from .pull_requests import is_safe_pull_request_url
from .web_jobs import WebJob
from .web_models import RepoContext
from .web_routing import issue_url as _issue_url


PHASE_LABELS = {
    "draft": "Draft",
    "needs_research": "Needs research",
    "researching": "Researching",
    "ready_for_plan": "Ready for planning",
    "planning": "Planning",
    "awaiting_plan_approval": "Plan approval needed",
    "ready_for_implementation": "Ready for implementation",
    "implementing": "Implementing",
    "ready_for_validation": "Ready for validation",
    "validating": "Validating",
    "ready_for_review": "Ready for review",
    "reviewing": "Reviewing",
    "awaiting_merge_approval": "Merge approval needed",
    "ready_for_merge": "Ready to merge",
    "merging": "Merging",
    "ready_for_merge_conflict_resolution": "Ready to resolve merge conflicts",
    "resolving_merge_conflicts": "Resolving merge conflicts",
    "awaiting_human_input": "Human input needed",
    "blocked": "Blocked",
    "done": "Done",
}


def phase_label(phase: object) -> str:
    text = "" if phase is None else str(phase)
    return PHASE_LABELS.get(text, text.replace("_", " ").strip().capitalize() or "Unknown")


def phase_option_label(phase: object) -> str:
    text = "" if phase is None else str(phase)
    label = phase_label(text)
    return f"{label} ({text})" if text and label != text else label


def _repo_hidden_input(repo_context: RepoContext) -> str:
    if repo_context.repo_path is None:
        return ""
    return f'<input type="hidden" name="repo" value="{_esc(repo_context.repo_path)}">'


def _repo_selector_html(repo_context: RepoContext) -> str:
    options = [
        f'<option value=""{" selected" if repo_context.repo_path is None else ""}>All repos</option>'
    ]
    options.extend(
        f'<option value="{_esc(repo)}"{" selected" if repo == repo_context.repo_path else ""}>{_esc(repo)}</option>'
        for repo in repo_context.known_repos
    )
    return (
        '<form method="get" action="/" class="repo-context">'
        '<label for="repo-context-select">Repo context</label>'
        f'<select id="repo-context-select" name="repo" onchange="this.form.submit()">{"".join(options)}</select>'
        '<button type="submit">Set</button>'
        "</form>"
    )


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _shorten(value: object, limit: int = 90) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _runtime_summary(runtime: dict[str, Any]) -> str:
    if runtime.get("mode") == "serve":
        return (
            f"Mode: serve - autonomous workers {runtime.get('worker_concurrency')} - "
            f"poll interval {runtime.get('worker_interval_seconds')}s - queued browser actions {runtime.get('web_workers')}"
        )
    return f"Mode: web-only - queued browser actions {runtime.get('web_workers')}"


def _phase_timeline_html(steps: list[dict[str, Any]]) -> str:
    rendered = []
    for step in steps:
        artifact = step.get("artifact")
        if isinstance(artifact, dict) and artifact.get("url"):
            artifact_label = artifact.get("label") or f"{str(step['label']).lower()} artifact"
            title = f"Open {artifact_label}"
            rendered.append(
                f'<a class="phase-step {_esc(step["status"])} has-artifact" '
                f'href="{_esc(artifact["url"])}" title="{_esc(title)}" aria-label="{_esc(title)}">'
                f'{_esc(step["label"])}</a>'
            )
        else:
            rendered.append(f'<span class="phase-step {_esc(step["status"])}">{_esc(step["label"])}</span>')
    return "".join(rendered)


def _summary_card(label: str, key: str, value: object, description: str) -> str:
    return (
        f'<article class="summary-card" data-summary-card="{_esc(key)}">'
        f'<strong data-summary-count>{_esc(value)}</strong>'
        f"<b>{_esc(label)}</b>"
        f"<span>{_esc(description)}</span>"
        "</article>"
    )


def _active_job_text(job: dict[str, Any] | None) -> str:
    if not job:
        return "none"
    return f"{job['status']} - {job['message']}"


def _render_blocked_reason(reason: dict[str, Any] | None) -> str:
    if reason is None:
        return '<section data-blocked-reason hidden></section>'

    details = _blocked_reason_details(reason)
    error = f'<pre class="blocked-reason-error">{_esc(reason["error"])}</pre>' if reason.get("error") else ""
    summary = reason.get("summary") or reason.get("headline")
    artifact_excerpt = ""
    if reason.get("artifact_excerpt") and reason["artifact_excerpt"] != summary:
        artifact_excerpt = f'<p class="blocked-reason-artifact-excerpt">{_esc(reason["artifact_excerpt"])}</p>'
    links = _blocked_reason_links(reason)
    return f"""
            <section class="panel attention blocked-reason-panel" data-blocked-reason>
              <h2>Blocked reason</h2>
              <p class="blocked-reason-summary">{_esc(summary)}</p>
              <p class="muted">{_esc(details)}</p>
              {error}
              {artifact_excerpt}
              {links}
            </section>
    """


def _render_closed_synopsis(synopsis: dict[str, Any] | None) -> str:
    if synopsis is None:
        return '<section data-closed-synopsis hidden></section>'

    change_excerpt = ""
    if synopsis.get("change_excerpt"):
        change_excerpt = f'<p class="closed-synopsis-change">{_esc(synopsis["change_excerpt"])}</p>'
    merge_summary = ""
    if synopsis.get("merge_summary"):
        merge_summary = f'<p class="closed-synopsis-merge">{_esc(synopsis["merge_summary"])}</p>'
    pull_request = _closed_synopsis_pull_request_html(synopsis)
    links = _closed_synopsis_links_html(synopsis)
    return f"""
            <section class="panel closed-synopsis-panel" data-closed-synopsis>
              <h2>Closed synopsis</h2>
              <p class="closed-synopsis-summary">{_esc(synopsis.get("summary") or synopsis.get("headline"))}</p>
              <p class="muted">{_esc(_closed_synopsis_details(synopsis))}</p>
              {change_excerpt}
              {merge_summary}
              {pull_request}
              {links}
            </section>
    """


def _render_human_input_panel(human_input: dict[str, Any]) -> str:
    pending = human_input.get("pending") if isinstance(human_input, dict) else None
    if not pending:
        return ""
    options = pending.get("options") or []
    options_html = ""
    if options:
        options_html = (
            "<h3>Options</h3><ul>"
            + "".join(f"<li>{_esc(option)}</li>" for option in options)
            + "</ul>"
        )
    context = pending.get("context")
    context_html = f"<h3>Context</h3><pre>{_esc(context)}</pre>" if context else ""
    return f"""
              <div class="panel priority human-input-panel" data-human-input-panel>
                <h2>Human input needed</h2>
                <dl class="metadata">
                  <dt>Requested by</dt><dd>{_esc(pending.get("requested_by_phase"))}</dd>
                  <dt>Resume phase</dt><dd>{_esc(pending.get("resume_phase"))}</dd>
                  <dt>Requested decision</dt><dd>{_esc(pending.get("requested_decision"))}</dd>
                </dl>
                <h3>Question</h3>
                <pre>{_esc(pending.get("question"))}</pre>
                <h3>Rationale</h3>
                <pre>{_esc(pending.get("rationale"))}</pre>
                {options_html}
                {context_html}
              </div>
    """


def _closed_synopsis_details(synopsis: dict[str, Any]) -> str:
    parts = [f"Source: {synopsis.get('source') or 'recorded state'}"]
    if synopsis.get("completed_at"):
        parts.append(f"Completed: {synopsis['completed_at']}")
    if synopsis.get("merged_at"):
        parts.append(f"Merged: {synopsis['merged_at']}")
    if synopsis.get("target_branch"):
        parts.append(f"Target branch: {synopsis['target_branch']}")
    if synopsis.get("merge_commit"):
        parts.append(f"Merge commit: {synopsis['merge_commit']}")
    if synopsis.get("worktree_commit"):
        parts.append(f"Worktree commit: {synopsis['worktree_commit']}")
    pull_request = synopsis.get("pull_request")
    if isinstance(pull_request, dict):
        if pull_request.get("provider"):
            parts.append(f"PR provider: {pull_request['provider']}")
        if pull_request.get("target_branch") and not synopsis.get("target_branch"):
            parts.append(f"Target branch: {pull_request['target_branch']}")
        if pull_request.get("head_commit"):
            parts.append(f"PR head: {pull_request['head_commit']}")
    return " - ".join(parts)


def _closed_synopsis_pull_request_html(synopsis: dict[str, Any]) -> str:
    pull_request = synopsis.get("pull_request")
    if not isinstance(pull_request, dict):
        return ""
    url = pull_request.get("url")
    number = pull_request.get("number") or pull_request.get("id") or ""
    label = f"Pull request #{number}" if number else "Pull request"
    link = (
        f'<a href="{_esc(url)}" rel="noreferrer">{_esc(label)}</a>'
        if is_safe_pull_request_url(url)
        else _esc(label)
    )
    details = " - ".join(
        part
        for part in (
            f"source {pull_request.get('source_branch')}" if pull_request.get("source_branch") else "",
            f"target {pull_request.get('target_branch')}" if pull_request.get("target_branch") else "",
            str(pull_request.get("status") or ""),
        )
        if part
    )
    details_html = f'<span class="muted"> {_esc(details)}</span>' if details else ""
    return f'<p class="closed-synopsis-pr">{link}{details_html}</p>'


def _closed_synopsis_links_html(synopsis: dict[str, Any]) -> str:
    links = [
        f'<li><a href="{_esc(link["url"])}">{_esc(link["label"])}</a></li>'
        for link in synopsis.get("links", [])
        if link and link.get("url")
    ]
    if not links:
        return ""
    return f'<ul class="item-list closed-synopsis-links">{"".join(links)}</ul>'


def _blocked_reason_details(reason: dict[str, Any]) -> str:
    labels = {
        "run": "agent run",
        "manual_transition": "manual transition",
        "transition": "transition",
        "fallback": "recorded state",
    }
    parts = [f"Source: {labels.get(str(reason.get('source')), 'recorded state')}"]
    if reason.get("phase"):
        parts.append(f"Phase: {reason['phase']}")
    if reason.get("status"):
        parts.append(f"Status: {reason['status']}")
    if reason.get("run_id"):
        parts.append(f"Run: {str(reason['run_id'])[:8]}")
    if reason.get("started_at"):
        parts.append(f"Started: {reason['started_at']}")
    if reason.get("completed_at"):
        parts.append(f"Completed: {reason['completed_at']}")
    suggested_transition = reason.get("suggested_transition")
    if isinstance(suggested_transition, dict) and suggested_transition.get("label"):
        parts.append(f"Suggested retry: {suggested_transition['label']}")
    return " - ".join(parts)


def _blocked_reason_links(reason: dict[str, Any]) -> str:
    links = [
        f'<a class="button" href="{_esc(link["url"])}">{_esc(link["label"])}</a>'
        for link in (reason.get("artifact"), reason.get("log"))
        if link and link.get("url")
    ]
    if not links:
        return ""
    return f'<p class="blocked-reason-links">{" ".join(links)}</p>'


def _log_meta_text(log: dict[str, Any]) -> str:
    if not log.get("exists"):
        label = log.get("label") or "No run log yet"
        return f"{label} (not created yet)"
    size = _format_bytes(int(log.get("size_bytes") or 0))
    truncated = "tail, " if log.get("truncated") else ""
    return f"{log.get('relative_path')} ({truncated}{size})"


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def _bootstrap_scripts(bootstrap: dict[str, Any] | None) -> str:
    if bootstrap is None:
        return ""
    payload = json.dumps(bootstrap, ensure_ascii=True, sort_keys=True).replace("</", "<\\/")
    return (
        '<script id="agent-team-bootstrap" type="application/json">'
        f"{payload}"
        "</script>\n"
        '<script src="/static/app.js"></script>'
    )


def _manager_controls_html(controls: list[dict[str, Any]], csrf_token: str) -> str:
    if not controls:
        return "<p>No actions are available for this phase.</p>"
    primary = [control for control in controls if _control_group(control) == "primary"]
    advanced = [control for control in controls if _control_group(control) == "advanced"]
    danger = [control for control in controls if _control_group(control) == "danger"]
    parts = []
    if primary:
        parts.append('<div class="control-group primary-action-group">')
        parts.append("".join(_manager_control_html(control, csrf_token) for control in primary))
        parts.append("</div>")
    else:
        parts.append('<p class="muted">No primary action is available for this phase.</p>')
    if advanced:
        parts.append(
            '<details class="advanced-actions"><summary>Advanced actions: override phase</summary>'
            '<p class="muted">Use only when you intentionally need to move this issue to another machine phase.</p>'
            + "".join(_manager_control_html(control, csrf_token) for control in advanced)
            + "</details>"
        )
    if danger:
        parts.append(
            '<details class="danger-zone"><summary>Danger zone: reset or delete this issue</summary>'
            '<p class="muted">These actions are destructive and require exact confirmation text.</p>'
            + "".join(_manager_control_html(control, csrf_token) for control in danger)
            + "</details>"
        )
    return "".join(parts)


def _control_group(control: dict[str, Any]) -> str:
    group = control.get("group")
    if group in {"primary", "advanced", "danger"}:
        return str(group)
    action = str(control.get("action") or control.get("href") or "")
    if action.endswith("/actions/transition"):
        return "advanced"
    if action.endswith("/actions/reset-to-draft") or action.endswith("/actions/delete"):
        return "danger"
    return "primary"


def _controls_signature(controls: list[dict[str, Any]]) -> str:
    return json.dumps(controls, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _manager_control_html(control: dict[str, Any], csrf_token: str) -> str:
    if control.get("kind") == "link":
        class_name = "button"
        if control.get("class_name"):
            class_name += f" {_esc(control.get('class_name'))}"
        href = control.get("href") or control.get("action") or ""
        return f'<a class="{class_name}" href="{_esc(href)}">{_esc(control.get("button", "Open"))}</a>'
    class_attr = f' class="{_esc(control.get("class_name"))}"' if control.get("class_name") else ""
    fields = "".join(_manager_control_field_html(field) for field in control.get("fields", []))
    legend = _esc(control.get("button", "Submit"))
    return (
        f'<form method="{_esc(control.get("method", "post"))}" action="{_esc(control.get("action", ""))}"{class_attr}>'
        f"<fieldset><legend>{legend}</legend>"
        f"{_csrf_field_html(csrf_token)}"
        f"{fields}"
        f'<button type="submit">{legend}</button>'
        "</fieldset>"
        "</form>"
    )


def _manager_control_field_html(field: dict[str, Any]) -> str:
    field_type = str(field.get("type") or "input")
    name = field.get("name") or ""
    value = field.get("value")
    placeholder = f' placeholder="{_esc(field.get("placeholder"))}"' if field.get("placeholder") else ""
    required = " required" if field.get("required") else ""
    if field_type == "textarea":
        rows = int(field.get("rows") or 3)
        control = f'<textarea name="{_esc(name)}" rows="{rows}"{required}{placeholder}>{_esc(value or "")}</textarea>'
    elif field_type == "select":
        options = "".join(
            f'<option value="{_esc(option.get("value"))}"'
            f'{" selected" if value is not None and str(value) == str(option.get("value")) else ""}>'
            f'{_esc(option.get("label") or option.get("value"))}</option>'
            for option in field.get("options", [])
        )
        control = f'<select name="{_esc(name)}"{required}>{options}</select>'
    else:
        input_type = field_type if field_type != "input" else "text"
        value_attr = f' value="{_esc(value)}"' if value is not None else ""
        control = f'<input type="{_esc(input_type)}" name="{_esc(name)}"{value_attr}{required}{placeholder}>'
    label = field.get("label")
    if not label:
        return control
    return f"<label>{_esc(label)} {control}</label>"


def _csrf_field_html(csrf_token: str) -> str:
    return f'<input type="hidden" name="_csrf_token" value="{_esc(csrf_token)}">'


def _workspace_metadata_rows(metadata: dict[str, Any] | None) -> str:
    if not metadata:
        return ""
    return (
        f"<dt>Workspace</dt><dd>{_esc(metadata.get('workspace_repo_path') or '')}</dd>"
        f"<dt>Worktree root</dt><dd>{_esc(metadata.get('worktree_root') or '')}</dd>"
    )


def _merge_branch_hint(
    merge_request: dict[str, Any] | None,
    workspace_metadata: dict[str, Any] | None,
) -> str:
    if merge_request and merge_request.get("target_branch"):
        return str(merge_request["target_branch"])
    if workspace_metadata and workspace_metadata.get("source_branch"):
        return str(workspace_metadata["source_branch"])
    return ""


def _phase_counts_table(rows: list[Any]) -> str:
    if not rows:
        return "<p>No issues.</p>"
    body = "".join(
        f"<tr><td>{_esc(row['status'])}</td><td>{_esc(phase_label(row['phase']))}<br><code>{_esc(row['phase'])}</code></td><td>{row['count']}</td></tr>" for row in rows
    )
    return f"<table><thead><tr><th>Status</th><th>Phase</th><th>Count</th></tr></thead><tbody>{body}</tbody></table>"


def _active_locks_table(rows: list[Any], repo_context: RepoContext | None = None) -> str:
    if not rows:
        return "<p>No active locks/runs.</p>"
    body = "".join(
        "<tr>"
        f'<td><a href="{_esc(_issue_url(row["id"], repo_context))}">#{row["id"]}</a></td>'
        f"<td>{_esc(phase_label(row['phase']))}<br><code>{_esc(row['phase'])}</code></td>"
        f"<td>{_esc(row['lock_owner'])}</td>"
        f"<td>{_esc(row['lock_expires_at'])}</td>"
        f"<td>{_esc(_shorten(row['title']))}</td>"
        "</tr>"
        for row in rows
    )
    return f"<table><thead><tr><th>Issue</th><th>Phase</th><th>Owner</th><th>Lock expires</th><th>Title</th></tr></thead><tbody>{body}</tbody></table>"


def _open_issues_table(rows: list[Any], repo_context: RepoContext | None = None) -> str:
    if not rows:
        return "<p>No open issues.</p>"
    body = "".join(
        "<tr>"
        f'<td><a href="{_esc(_issue_url(row["id"], repo_context))}">#{row["id"]}</a></td>'
        f"<td>P{row['priority']}</td>"
        f"<td>{_esc(phase_label(row['phase']))}<br><code>{_esc(row['phase'])}</code></td>"
        f"<td>{_esc(row['updated_at'])}</td>"
        f"<td>{_esc(_shorten(row['title']))}</td>"
        "</tr>"
        for row in rows
    )
    return f"<table><thead><tr><th>Issue</th><th>Priority</th><th>Phase</th><th>Updated</th><th>Title</th></tr></thead><tbody>{body}</tbody></table>"


def _recent_runs_table(rows: list[Any], repo_context: RepoContext | None = None) -> str:
    if not rows:
        return "<p>No runs yet.</p>"
    items = "".join(
        "<li>"
        f'<strong><a href="{_esc(_issue_url(row["issue_id"], repo_context))}">Issue #{row["issue_id"]}</a> - {_esc(phase_label(row["phase"]))} - {_esc(row["status"])}</strong>'
        f'<span class="muted">{_esc(_shorten(row["summary"] or "No summary recorded.", 140))}</span>'
        "</li>"
        for row in rows
    )
    return f'<ul class="item-list compact-run-list">{items}</ul>'


def _recently_merged_table(rows: list[Any], repo_context: RepoContext | None = None) -> str:
    if not rows:
        return "<p>No finalized issues yet.</p>"
    body = "".join(
        "<tr>"
        f'<td><a href="{_esc(_issue_url(row["issue_id"], repo_context))}">#{row["issue_id"]}</a></td>'
        f"<td>{_esc(row['completed_at'] or row['started_at'] or row['updated_at'])}</td>"
        f"<td>{_esc(_shorten(row['run_id'], 8))}</td>"
        f"<td>{_esc(_shorten(row['summary'] or ''))}</td>"
        f"<td>{_esc(_shorten(row['title']))}</td>"
        "</tr>"
        for row in rows
    )
    return f"<table><thead><tr><th>Issue</th><th>Finalized</th><th>Run</th><th>Summary</th><th>Title</th></tr></thead><tbody>{body}</tbody></table>"


def _recent_events_table(rows: list[Any], repo_context: RepoContext | None = None) -> str:
    if not rows:
        return "<p>No events yet.</p>"
    body = "".join(
        "<tr>"
        f"<td>{_esc(row['created_at'])}</td>"
        f'<td><a href="{_esc(_issue_url(row["issue_id"], repo_context))}">#{row["issue_id"]}</a></td>'
        f"<td>{_esc(row['event_type'])}</td>"
        f"<td>{_esc(_shorten(row['message']))}</td>"
        "</tr>"
        for row in rows
    )
    return f"<table><thead><tr><th>Time</th><th>Issue</th><th>Event</th><th>Message</th></tr></thead><tbody>{body}</tbody></table>"


def _jobs_table(jobs: list[WebJob]) -> str:
    if not jobs:
        return "<p>No queued browser actions in this server session.</p>"
    body = "".join(
        "<tr>"
        f"<td><code>{_esc(job.id[:8])}</code></td>"
        f"<td>{_esc(job.status)}</td>"
        f"<td>{_esc(job.action)}</td>"
        f"<td>{_esc(job.issue_id or '')}</td>"
        f"<td>{_esc(job.message)}</td>"
        f"<td>{_esc(job.updated_at)}</td>"
        "</tr>"
        for job in jobs
    )
    return f"<table><thead><tr><th>Action</th><th>Status</th><th>Request</th><th>Issue</th><th>Message</th><th>Updated</th></tr></thead><tbody>{body}</tbody></table>"


def _issue_runs_table(rows: list[Any]) -> str:
    if not rows:
        return "<p>No runs yet.</p>"
    body = "".join(
        "<tr>"
        f"<td><code>{_esc(row['id'])}</code></td>"
        f"<td>{_esc(row['phase'])}</td>"
        f"<td>{_esc(row['runner'])}</td>"
        f"<td>{_esc(row['status'])}</td>"
        f"<td>{_esc(row['started_at'])}</td>"
        f"<td>{_esc(row['completed_at'] or '')}</td>"
        f"<td>{_esc(row['summary'] or '')}</td>"
        "</tr>"
        for row in rows
    )
    return f"<table><thead><tr><th>Run</th><th>Phase</th><th>Runner</th><th>Status</th><th>Started</th><th>Completed</th><th>Summary</th></tr></thead><tbody>{body}</tbody></table>"


def _issue_events_table(rows: list[Any]) -> str:
    if not rows:
        return "<p>No events yet.</p>"
    body = "".join(
        "<tr>"
        f"<td>{_esc(row['created_at'])}</td>"
        f"<td>{_esc(row['event_type'])}</td>"
        f"<td>{_esc(row['run_id'] or '')}</td>"
        f"<td>{_esc(row['message'])}</td>"
        "</tr>"
        for row in rows
    )
    return f"<table><thead><tr><th>Time</th><th>Event</th><th>Run</th><th>Message</th></tr></thead><tbody>{body}</tbody></table>"
