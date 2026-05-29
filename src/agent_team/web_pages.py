from __future__ import annotations

from typing import Any

from .models import Issue
from .web_html import (
    _active_job_text,
    _active_locks_table,
    _esc,
    _issue_events_table,
    _issue_runs_table,
    _jobs_table,
    _log_meta_text,
    _manager_controls_html,
    _open_issues_table,
    _phase_counts_table,
    _phase_timeline_html,
    _recent_events_table,
    _recent_runs_table,
    _recently_merged_table,
    _render_blocked_reason,
    _render_closed_synopsis,
    _render_human_input_panel,
    _runtime_summary,
    _summary_card,
    phase_label,
)
from .web_jobs import WebJob
from .web_models import RepoContext


def render_dashboard_body(
    payload: dict[str, Any],
    context: RepoContext,
    csrf_field: str,
    run_next_url: str,
    jobs: list[WebJob],
) -> str:
    return f"""
            <section class="hero">
              <div>
                 <p class="eyebrow">Manager cockpit</p>
                 <h2>What needs attention now</h2>
                 <p class="muted">Prioritized work buckets first; diagnostics stay available when you need them.</p>
                 <p class="muted" data-runtime-status>{_esc(_runtime_summary(payload["runtime"]))}</p>
                 <p class="live-status" data-live-status aria-live="polite">Live updates enabled</p>
               </div>
              <form method="post" action="{_esc(run_next_url)}" class="hero-action">
                {csrf_field}
                <button type="submit">Run next ready issue</button>
              </form>
            </section>
            <section class="summary-grid" aria-label="Dashboard summary">
              {_summary_card("Active work", "active_work", payload["summary"]["active_work"], "Agents currently holding locks or running.")}
              {_summary_card("Approval needed", "approval_needed", payload["summary"]["approval_needed"], "Plans and merges waiting on a manager.")}
              {_summary_card("Human input", "human_input_needed", payload["summary"]["human_input_needed"], "Questions or approvals waiting for an answer.")}
              {_summary_card("Blocked", "blocked", payload["summary"]["blocked"], "Issues that need intervention before agents can continue.")}
              {_summary_card("Draft backlog", "draft", payload["summary"]["draft"], "Issues waiting to be published before agents can run.")}
              {_summary_card("Ready to run", "ready", payload["summary"]["ready"], "Issues ready for an agent run.")}
              {_summary_card("Recently merged", "recently_merged", payload["summary"]["recently_merged"], "Latest issues merged and closed.")}
            </section>
            <section class="panel-grid manager-buckets" aria-label="Priority work buckets">
              <div class="panel priority">
                <h2>Active work</h2>
                <div data-dashboard-list="active_work">{_active_locks_table(payload["active_work"], context)}</div>
              </div>
              <div class="panel priority">
                <h2>Approval needed</h2>
                <div data-dashboard-list="approval_issues">{_open_issues_table(payload["approval_issues"], context)}</div>
              </div>
              <div class="panel priority">
                <h2>Human input needed</h2>
                <div data-dashboard-list="human_input_needed">{_open_issues_table(payload["human_input_needed"], context)}</div>
              </div>
              <div class="panel attention">
                <h2>Blocked</h2>
                <div data-dashboard-list="blocked_issues">{_open_issues_table(payload["blocked_issues"], context)}</div>
              </div>
              <div class="panel">
                <h2>Draft backlog</h2>
                <div data-dashboard-list="draft_issues">{_open_issues_table(payload["draft_issues"], context)}</div>
              </div>
              <div class="panel">
                <h2>Ready to run</h2>
                <div data-dashboard-list="ready_issues">{_open_issues_table(payload["ready_issues"], context)}</div>
              </div>
             </section>
             <section class="panel-grid activity-grid" aria-label="Recent activity">
               <div class="panel">
                 <h2>Recently merged</h2>
                 <div data-dashboard-list="recently_merged">{_recently_merged_table(payload["recently_merged"], context)}</div>
               </div>
               <div class="panel compact-activity">
                 <h2>Run activity</h2>
                 <p class="muted">Compact view of the latest runs. Full run identifiers and runner details are in issue diagnostics.</p>
                 <div data-dashboard-list="recent_runs">{_recent_runs_table(payload["recent_runs"], context)}</div>
              </div>
            </section>
            <details class="panel diagnostics" data-dashboard-diagnostics>
              <summary>Diagnostics: recent events, issue counts, open issues, and queued browser actions</summary>
              <div class="panel-grid diagnostics-grid">
                <div>
                  <h2>Recent events</h2>
                  <div data-dashboard-list="recent_events">{_recent_events_table(payload["recent_events"], context)}</div>
                </div>
                <div>
                  <h2>Open issues</h2>
                  <div data-dashboard-list="open_issues">{_open_issues_table(payload["open_issues"], context)}</div>
                </div>
                <div>
                  <h2>Issue counts</h2>
                  <div data-dashboard-list="phase_counts">{_phase_counts_table(payload["phase_counts"])}</div>
                </div>
                <div>
                  <h2>Queued browser actions</h2>
                  <div data-dashboard-list="jobs">{_jobs_table(jobs)}</div>
                </div>
              </div>
            </details>
            """


def render_issue_detail_body(
    issue: Issue,
    payload: dict[str, Any],
    workspace_rows: str,
    log_payload: dict[str, Any],
    plan_review: str,
    artifacts: str,
    csrf_token: str,
) -> str:
    human_input_panel = _render_human_input_panel(payload["human_input"])
    controls = _manager_controls_html(payload["manager_controls"], csrf_token)
    return f"""
            <section class="hero issue-hero">
              <div>
                <p class="eyebrow">Issue #{issue.id}</p>
                <h2 data-issue-title>{_esc(issue.title)}</h2>
                <p class="phase-line">
                  <strong data-issue-phase-label>{_esc(phase_label(issue.phase))}</strong>
                  <span class="muted">Raw phase: <code data-issue-phase>{_esc(issue.phase)}</code> &middot; Status: <strong data-issue-status>{_esc(issue.status)}</strong></span>
                </p>
                <p class="live-status" data-live-status aria-live="polite">Live updates enabled</p>
              </div>
              <dl class="hero-facts">
                <dt>Current run</dt><dd data-current-run>{_esc(issue.current_run_id or "none")}</dd>
                <dt>Lock owner</dt><dd data-lock-owner>{_esc(issue.lock_owner or "none")}</dd>
                <dt>Lock expires</dt><dd data-lock-expiry>{_esc(issue.lock_expires_at or "none")}</dd>
                <dt>Queued browser action</dt><dd data-active-job>{_esc(_active_job_text(payload["active_job"]))}</dd>
              </dl>
            </section>
            {_render_blocked_reason(payload["blocked_reason"])}
            {_render_closed_synopsis(payload["closed_synopsis"])}
            <section class="panel priority next-action-panel">
              <h2>Next action</h2>
              <p class="next-action" data-next-action>{_esc(payload["next_action"])}</p>
            </section>
            <section>
              <h2>Workflow progress <span class="muted">(Phase timeline)</span></h2>
              <nav class="timeline" aria-label="Workflow progress" data-phase-timeline>{_phase_timeline_html(payload["phase_timeline"])}</nav>
            </section>
            <section class="panel-grid issue-grid issue-action-grid">
              {plan_review}
              {human_input_panel}
              <div class="panel primary-controls-panel">
                <h2>Primary controls</h2>
                <div class="action-stack" data-action-stack data-controls-signature="{_esc(payload["manager_controls_signature"])}">{controls}</div>
              </div>
              <div class="panel">
                <h2>Issue context</h2>
                <dl class="metadata">
                  <dt>Priority</dt><dd data-issue-priority>P{issue.priority}</dd>
                  <dt>Repo</dt><dd data-issue-repo>{_esc(issue.repo_path or "")}</dd>
                  <dt>Tags</dt><dd data-issue-tags>{_esc(issue.tags or "")}</dd>
                  <dt>Created</dt><dd>{_esc(issue.created_at)}</dd>
                  <dt>Updated</dt><dd>{_esc(issue.updated_at)}</dd>
                </dl>
              </div>
            </section>
            <section class="panel-grid issue-grid evidence-grid">
              <div class="panel">
                <h2>Description</h2>
                <pre data-issue-description>{_esc(issue.description)}</pre>
              </div>
              <div class="panel">
                <h2>Artifacts and logs</h2>
                <div data-issue-artifacts>{artifacts}</div>
              </div>
              <div class="panel current-log-panel">
                <div class="section-header">
                  <h2>Current log</h2>
                  <button type="button" class="secondary" data-log-toggle aria-pressed="false">Pause log</button>
                </div>
                <p class="muted" data-log-meta>{_esc(_log_meta_text(log_payload))}</p>
                <pre class="log-viewer" data-log-output>{_esc(log_payload.get("content") or "")}</pre>
              </div>
            </section>
            <details class="panel diagnostics issue-diagnostics">
              <summary>Diagnostics: recent events, run history, workspace metadata, and raw issue metadata</summary>
              <div class="panel-grid diagnostics-grid">
                <div>
                  <h2>Recent events</h2>
                  <div data-issue-events>{_issue_events_table(payload["recent_events"])}</div>
                </div>
                <div>
                  <h2>Runs</h2>
                  <div data-issue-runs>{_issue_runs_table(payload["recent_runs"])}</div>
                </div>
                <div>
                  <h2>Workspace metadata</h2>
                  <dl class="metadata">{workspace_rows or "<dt>Workspace</dt><dd>none</dd>"}</dl>
                </div>
                <div>
                  <h2>Raw issue metadata</h2>
                  <dl class="metadata">
                    <dt>Phase</dt><dd><code>{_esc(issue.phase)}</code></dd>
                    <dt>Status</dt><dd>{_esc(issue.status)}</dd>
                    <dt>Current run</dt><dd>{_esc(issue.current_run_id or "none")}</dd>
                    <dt>Updated</dt><dd>{_esc(issue.updated_at)}</dd>
                  </dl>
                </div>
              </div>
            </details>
            """
