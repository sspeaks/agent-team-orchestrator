from __future__ import annotations

import contextlib
import html
import io
import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agent_team.cli as cli_module
import agent_team.web as web_module
from agent_team.cli import build_parser
from agent_team.config import AppConfig
from agent_team.models import HumanInputRequestDraft, utc_now_iso
from agent_team.orchestrator import Orchestrator
from agent_team.web import AgentTeamWebApp, LOG_TAIL_BYTES, WebJob, serve_web, serve_web_and_worker
from agent_team.web_html import _render_closed_synopsis


class WebRenderingTests(unittest.TestCase):
    def test_server_closed_synopsis_does_not_link_unsafe_pull_request_url(self) -> None:
        rendered = _render_closed_synopsis(
            {
                "summary": "Finalized by PR",
                "pull_request": {
                    "number": 7,
                    "url": "javascript:alert(1)",
                    "source_branch": "agent-team/issue-7",
                    "target_branch": "main",
                    "status": "OPEN",
                },
            }
        )

        self.assertIn("Pull request #7", rendered)
        self.assertNotIn("<a ", rendered)
        self.assertNotIn("javascript:alert", rendered)

    def test_server_closed_synopsis_links_http_pull_request_url(self) -> None:
        rendered = _render_closed_synopsis(
            {
                "summary": "Finalized by PR",
                "pull_request": {
                    "number": 7,
                    "url": "https://github.com/owner/repo/pull/7",
                    "source_branch": "agent-team/issue-7",
                    "target_branch": "main",
                    "status": "OPEN",
                },
            }
        )

        self.assertIn('<a href="https://github.com/owner/repo/pull/7" rel="noreferrer">Pull request #7</a>', rendered)

    def test_server_closed_synopsis_does_not_link_credential_bearing_pull_request_url(self) -> None:
        rendered = _render_closed_synopsis(
            {
                "summary": "Finalized by PR",
                "pull_request": {
                    "number": 7,
                    "url": "https://user:secret@github.com/owner/repo/pull/7",
                    "source_branch": "agent-team/issue-7",
                    "target_branch": "main",
                    "status": "OPEN",
                },
            }
        )

        self.assertIn("Pull request #7", rendered)
        self.assertNotIn("<a ", rendered)
        self.assertNotIn("user:secret", rendered)

    def test_browser_closed_synopsis_checks_pr_url_scheme_before_linking(self) -> None:
        script = (Path(web_module.__file__).with_name("web_static") / "app.js").read_text(encoding="utf-8")

        self.assertIn("function isSafeHttpUrl", script)
        self.assertIn("if (isSafeHttpUrl(pullRequest.url))", script)
        self.assertIn("!/^https?:\\/\\//i.test(text)", script)
        self.assertIn("!parsed.username", script)
        self.assertIn("!parsed.password", script)
        self.assertNotIn("if (pullRequest.url) {\n      var link", script)

    def test_browser_closed_synopsis_rejects_pr_url_query_and_fragment(self) -> None:
        script = (Path(web_module.__file__).with_name("web_static") / "app.js").read_text(encoding="utf-8")

        self.assertIn("!parsed.search", script)
        self.assertIn("!parsed.hash", script)


class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.config = AppConfig(
            home=self.home,
            db_path=self.home / "state.db",
            artifacts_dir=self.home / "issues",
            worktrees_dir=self.home / "worktrees",
            runner="dry-run",
            lock_ttl_seconds=60,
        )
        self.app = AgentTeamWebApp(self.config, max_workers=1)
        self.server = self.app.build_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.store = self.app.store
        self.artifacts = self.app.artifacts

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.app.shutdown()
        self.temp.cleanup()

    def get(self, path: str, headers: dict[str, str] | None = None) -> str:
        request = urllib.request.Request(self.base_url + path, headers=headers or {})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def get_json(self, path: str, headers: dict[str, str] | None = None) -> tuple[dict[str, object], object]:
        request = urllib.request.Request(self.base_url + path, headers=headers or {})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload, response.headers

    def post(
        self,
        path: str,
        values: dict[str, str],
        headers: dict[str, str] | None = None,
        include_csrf: bool = True,
    ) -> str:
        values = dict(values)
        if include_csrf:
            values["_csrf_token"] = self.app.csrf_token
        body = urllib.parse.urlencode(values).encode("utf-8")
        request_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=request_headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8")

    def write_cli_config(self, payload: dict[str, object]) -> Path:
        path = self.home / "cli-config.jsonc"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_create_issue_dashboard_and_list_render_escaped_issue(self) -> None:
        html = self.post(
            "/issues",
            {
                "title": "",
                "description": "Research <b>web issue</b> through the web form. Details.",
                "repo_path": "/tmp/repo",
                "priority": "2",
                "tags": "web,test",
            },
        )
        self.assertIn("Research &lt;b&gt;web issue&lt;/b&gt; through the web form.", html)
        self.assertNotIn("Research <b>web issue</b> through the web form.", html)

        issue = self.store.list_issues()[0]
        self.assertEqual(issue.title, "Research <b>web issue</b> through the web form.")
        self.assertEqual(issue.phase, "draft")
        self.assertEqual(issue.priority, 2)
        self.assertEqual(issue.repo_path, "/tmp/repo")
        self.assertEqual(issue.tags, "web,test")
        snapshot = json.loads((self.config.artifacts_dir / str(issue.id) / "issue.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["title"], "Research <b>web issue</b> through the web form.")
        self.assertEqual(snapshot["phase"], "draft")

        self.assertIn("Research &lt;b&gt;web issue&lt;/b&gt; through the web form.", self.get("/issues"))
        dashboard = self.get("/")
        self.assertIn("Manager cockpit", dashboard)
        self.assertIn("Approval needed", dashboard)
        self.assertIn("Draft backlog", dashboard)
        self.assertIn("Ready to run", dashboard)
        self.assertIn('data-summary-card="active_work"', dashboard)
        self.assertIn("Issue counts", dashboard)
        self.assertIn("Run activity", dashboard)
        self.assertIn("Queued browser actions", dashboard)
        self.assertIn("data-dashboard-diagnostics", dashboard)
        self.assertIn("<details class=\"panel diagnostics\" data-dashboard-diagnostics>", dashboard)
        self.assertNotIn("<h2>Web jobs</h2>", dashboard)
        self.assertIn("draft", dashboard)

    def test_web_create_can_make_issue_ready(self) -> None:
        self.post(
            "/issues",
            {
                "description": "Run immediately",
                "repo_path": "/tmp/repo",
                "priority": "2",
                "ready": "1",
            },
        )

        issue = self.store.list_issues()[0]
        self.assertEqual(issue.title, "Run immediately")
        self.assertEqual(issue.phase, "needs_research")

    def test_issue_detail_escapes_artifact_content(self) -> None:
        issue = self.store.create_issue("artifact issue", "desc")
        self.artifacts.write_issue_snapshot(issue)
        self.artifacts.write_phase_artifact(issue.id, "research", "run-1", "<script>alert(1)</script>")

        html = self.get(f"/issues/{issue.id}")
        self.assertIn("research artifact", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

        artifact = self.get(f"/artifacts/{issue.id}/research.md")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", artifact)
        self.assertNotIn("<script>alert(1)</script>", artifact)

    def test_issue_detail_links_phase_artifacts_in_timeline(self) -> None:
        issue = self.store.create_issue("timeline artifact issue", "desc", ready=True)
        self.artifacts.write_phase_artifact(issue.id, "research", "run-1", "Research body <script>timeline</script>")
        self.artifacts.write_phase_artifact(issue.id, "plan", "run-2", "Plan body that should stay out of detail")

        html = self.get(f"/issues/{issue.id}")

        top = html[: html.index("Primary controls")]
        artifact_section = html[html.index("Artifacts and logs") :]
        self.assertIn('<nav class="timeline" aria-label="Workflow progress" data-phase-timeline>', top)
        self.assertIn(
            f'<a class="phase-step current has-artifact" href="/artifacts/{issue.id}/research.md"',
            top,
        )
        self.assertIn(f'aria-label="Open research artifact"', top)
        self.assertIn(
            f'<a class="phase-step pending has-artifact" href="/artifacts/{issue.id}/plan.md"',
            top,
        )
        self.assertNotIn(f'href="/artifacts/{issue.id}/implementation.md"', top)
        self.assertLess(html.index(f'href="/artifacts/{issue.id}/research.md"'), html.index("Artifacts and logs"))
        self.assertIn(f'href="/artifacts/{issue.id}/research.md"', artifact_section)
        self.assertIn(f'href="/artifacts/{issue.id}/plan.md"', artifact_section)
        self.assertNotIn("Research body", html)
        self.assertNotIn("&lt;script&gt;timeline&lt;/script&gt;", html)

    def test_issue_detail_artifacts_section_is_scroll_bounded(self) -> None:
        issue = self.store.create_issue("long artifact issue", "desc", ready=True)
        self.artifacts.write_phase_artifact(issue.id, "research", "run-research", "Research body")
        self.artifacts.write_phase_artifact(issue.id, "plan", "run-plan", "Plan body")
        for index in range(8):
            self.artifacts.run_log_path(issue.id, "research", f"run-{index}").write_text(
                f"research log {index}",
                encoding="utf-8",
            )

        html = self.get(f"/issues/{issue.id}")

        self.assertIn('<div class="artifact-list-viewer" data-issue-artifacts>', html)
        artifact_section = html[
            html.index('<div class="artifact-list-viewer" data-issue-artifacts>') : html.index(
                '<details class="panel diagnostics issue-diagnostics">'
            )
        ]
        self.assertIn(f'href="/artifacts/{issue.id}/research.md"', artifact_section)
        self.assertIn(f'href="/artifacts/{issue.id}/plan.md"', artifact_section)
        self.assertIn(f'href="/artifacts/{issue.id}/logs/research-run-0.md"', artifact_section)
        self.assertIn(f'href="/artifacts/{issue.id}/logs/research-run-7.md"', artifact_section)

    def test_issue_detail_places_current_log_before_primary_controls_for_non_merge_approval(self) -> None:
        issue = self.store.create_issue("current log layout issue", "desc", ready=True)

        html = self.get(f"/issues/{issue.id}")

        self.assertLess(html.index("Current log"), html.index("Primary controls"))
        self.assertLess(html.index("Current log"), html.index("Issue context"))
        self.assertIn("data-log-toggle", html)
        self.assertIn("data-log-meta", html)
        self.assertIn("data-log-output", html)

    def test_issue_detail_places_review_artifact_and_merge_controls_before_current_log(self) -> None:
        issue = self.store.create_issue("merge review layout issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_workspace_metadata(issue.id, {"source_branch": "main"})
        review_content = "Review says merge is ready\n<script>alert('review')</script>"
        self.artifacts.write_phase_artifact(issue.id, "review", "run-1", review_content)

        html = self.get(f"/issues/{issue.id}")

        self.assertIn("Review artifact", html)
        self.assertIn("Review says merge is ready", html)
        self.assertIn("&lt;script&gt;alert(&#x27;review&#x27;)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert('review')</script>", html)
        self.assertIn(f'href="/artifacts/{issue.id}/review.md"', html)
        self.assertIn("Open full review artifact", html)
        self.assertLess(html.index("Review artifact"), html.index("Primary controls"))
        self.assertLess(html.index("Review artifact"), html.index("Current log"))
        self.assertLess(html.index("Primary controls"), html.index("Current log"))
        self.assertLess(html.index("/actions/approve-merge"), html.index("Current log"))

    def test_issue_detail_shows_missing_review_artifact_notice_before_current_log(self) -> None:
        issue = self.store.create_issue("missing review artifact issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_workspace_metadata(issue.id, {"source_branch": "main"})

        html = self.get(f"/issues/{issue.id}")

        self.assertIn("Review artifact", html)
        self.assertIn("the review artifact is not available yet", html)
        self.assertIn(f"/issues/{issue.id}/actions/approve-merge", html)
        self.assertLess(html.index("the review artifact is not available yet"), html.index("Primary controls"))
        self.assertLess(html.index("the review artifact is not available yet"), html.index("Current log"))
        self.assertLess(html.index("Primary controls"), html.index("Current log"))

    def test_issue_detail_links_existing_phase_artifacts_when_blocked(self) -> None:
        issue = self.store.create_issue("blocked artifact issue", "desc", ready=True)
        self.artifacts.write_phase_artifact(issue.id, "research", "run-1", "Blocked issue research")
        self.store.transition_issue(issue.id, "blocked")

        html = self.get(f"/issues/{issue.id}")

        top = html[: html.index("Primary controls")]
        self.assertIn('<span class="phase-step attention">Blocked</span>', top)
        self.assertIn(
            f'<a class="phase-step pending has-artifact" href="/artifacts/{issue.id}/research.md"',
            top,
        )

    def test_issue_detail_api_and_route_surface_merge_conflict_resolution_artifact(self) -> None:
        issue = self.store.create_issue("conflict artifact issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_merge")
        self.store.transition_issue(issue.id, "merging")
        self.store.transition_issue(issue.id, "ready_for_merge_conflict_resolution")
        self.artifacts.write_phase_artifact(issue.id, "merge", "run-merge", "Merge body")
        self.artifacts.write_phase_artifact(
            issue.id,
            "merge_conflict_resolution",
            "run-1",
            "Resolved <script>alert(1)</script> conflict",
        )

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        artifacts = {artifact["relative_path"]: artifact for artifact in payload["artifacts"]}
        artifact = artifacts["merge_conflict_resolution.md"]
        top = html[: html.index("Primary controls")]
        timeline_by_label = {step["label"]: step for step in payload["phase_timeline"]}

        self.assertIn("merge conflict resolution artifact", html)
        self.assertIn("merge_conflict_resolution.md", html)
        self.assertEqual(artifact["label"], "merge conflict resolution artifact")
        self.assertEqual(artifact["kind"], "phase")
        self.assertIn(
            f'<a class="phase-step done has-artifact" href="/artifacts/{issue.id}/merge.md"',
            top,
        )
        self.assertIn(
            f'<a class="phase-step current has-artifact" href="/artifacts/{issue.id}/merge_conflict_resolution.md"',
            top,
        )
        self.assertEqual(
            timeline_by_label["Merge"]["artifact"],
            {
                "label": "merge artifact",
                "relative_path": "merge.md",
                "url": f"/artifacts/{issue.id}/merge.md",
            },
        )
        self.assertEqual(
            timeline_by_label["Conflict resolution"]["artifact"],
            {
                "label": "merge conflict resolution artifact",
                "relative_path": "merge_conflict_resolution.md",
                "url": f"/artifacts/{issue.id}/merge_conflict_resolution.md",
            },
        )
        self.assertNotIn("content", timeline_by_label["Conflict resolution"]["artifact"])

        artifact_page = self.get(f"/artifacts/{issue.id}/merge_conflict_resolution.md")
        self.assertIn("Resolved &lt;script&gt;alert(1)&lt;/script&gt; conflict", artifact_page)
        self.assertNotIn("Resolved <script>alert(1)</script> conflict", artifact_page)

    def test_issue_detail_done_without_conflict_artifact_omits_conflict_resolution_step(self) -> None:
        issue = self.store.create_issue("clean merge issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_merge")
        self.store.transition_issue(issue.id, "merging")
        self.store.transition_issue(issue.id, "done")

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        top = html[: html.index("Primary controls")]
        timeline_by_label = {step["label"]: step for step in payload["phase_timeline"]}

        self.assertNotIn("Conflict resolution", timeline_by_label)
        self.assertNotIn("Conflict resolution", top)
        self.assertEqual(timeline_by_label["Merge"]["status"], "done")
        self.assertEqual(timeline_by_label["Done"]["status"], "current")

    def test_issue_detail_surfaces_plan_review_for_plan_approval(self) -> None:
        issue = self.store.create_issue("plan review issue", "desc")
        self._move_to_plan_approval(issue.id)
        plan_content = "Review this plan\n<script>alert('plan')</script>"
        self.artifacts.write_phase_artifact(issue.id, "plan", "run-1", plan_content)

        html = self.get(f"/issues/{issue.id}")

        self.assertIn("Plan review", html)
        self.assertIn("Review this plan", html)
        self.assertIn("&lt;script&gt;alert(&#x27;plan&#x27;)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert('plan')</script>", html)
        self.assertIn(f'href="/artifacts/{issue.id}/plan.md"', html)
        self.assertIn("Open full plan artifact", html)
        self.assertIn(f"/issues/{issue.id}/actions/approve-plan", html)
        self.assertIn(f"/issues/{issue.id}/actions/reject-plan", html)
        self.assertLess(html.index("Plan review"), html.index("Primary controls"))
        self.assertLess(html.index("Plan review"), html.index("Artifacts and logs"))

    def test_issue_detail_shows_missing_plan_review_notice(self) -> None:
        issue = self.store.create_issue("missing plan issue", "desc")
        self._move_to_plan_approval(issue.id)

        html = self.get(f"/issues/{issue.id}")

        self.assertIn("Plan review", html)
        self.assertIn("the plan artifact is not available yet", html)
        self.assertIn(f"/issues/{issue.id}/actions/approve-plan", html)
        self.assertIn(f"/issues/{issue.id}/actions/reject-plan", html)

    def test_issue_detail_and_api_surface_human_input_request(self) -> None:
        issue = self.store.create_issue("human input detail", "desc")
        self._move_to_human_input(issue.id)

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        timeline_by_label = {step["label"]: step for step in payload["phase_timeline"]}

        self.assertIn("Human input needed", html)
        self.assertIn("Which &amp; option &lt;now&gt;?", html)
        self.assertNotIn("Which & option <now>?", html)
        self.assertIn("User controlled &lt;context&gt;", html)
        self.assertIn(f"/issues/{issue.id}/actions/answer-human-input", html)
        self.assertIn("Answer human input", html)
        self.assertIn(f"/issues/{issue.id}/actions/stop", html)
        self.assertIn("Stop issue", html)
        self.assertNotIn(f"/issues/{issue.id}/actions/transition", html)
        actions = {control["action"] for control in payload["manager_controls"]}
        self.assertIn(f"/issues/{issue.id}/actions/stop", actions)
        self.assertEqual(payload["human_input"]["pending"]["question"], "Which & option <now>?")
        self.assertEqual(payload["human_input"]["pending"]["resume_phase"], "needs_research")
        self.assertEqual(timeline_by_label["Human input"]["status"], "current")
        self.assertEqual(timeline_by_label["Human input"]["artifact"]["relative_path"], "human_input.md")

    def test_dashboard_api_returns_manager_buckets_and_json_headers(self) -> None:
        draft = self.store.create_issue("draft issue", "desc")
        ready = self.store.create_issue("ready issue", "desc", ready=True)
        approval = self.store.create_issue("approval issue", "desc", ready=True)
        self._move_to_plan_approval(approval.id)
        human_input = self.store.create_issue("human input issue", "desc", ready=True)
        self._move_to_human_input(human_input.id)
        blocked = self.store.create_issue("blocked issue", "desc", ready=True)
        self.store.transition_issue(blocked.id, "blocked")

        payload, headers = self.get_json("/api/dashboard")

        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertIn("charset=utf-8", headers.get("Content-Type", ""))
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        summary = payload["summary"]
        self.assertGreaterEqual(summary["ready"], 1)
        self.assertEqual(summary["draft"], 1)
        self.assertEqual(summary["approval_needed"], 1)
        self.assertEqual(summary["human_input_needed"], 1)
        self.assertEqual(summary["blocked"], 1)
        self.assertIn(draft.id, {item["id"] for item in payload["draft_issues"]})
        self.assertNotIn(draft.id, {item["id"] for item in payload["ready_issues"]})
        self.assertIn(ready.id, {item["id"] for item in payload["ready_issues"]})
        self.assertEqual(payload["approval_issues"][0]["title"], "approval issue")
        self.assertEqual(payload["human_input_needed"][0]["title"], "human input issue")
        self.assertEqual(payload["runtime"]["mode"], "web-only")
        self.assertEqual(payload["runtime"]["web_workers"], 1)
        dashboard = self.get("/")
        self.assertIn("Mode: web-only - queued browser actions 1", dashboard)
        self.assertIn("<summary>Diagnostics: recent events, issue counts, open issues, and queued browser actions</summary>", dashboard)
        self.assertIn("Human input", self.get("/"))

    def test_dashboard_surfaces_recently_merged_in_html_api_and_js(self) -> None:
        repo = "/tmp/repo-a"
        issue = self.store.create_issue("merged dashboard issue", "desc", repo_path=repo, ready=True)
        self._close_with_merge(issue.id, "merge-dashboard", "Merged issue into main at abc123")
        query = urllib.parse.urlencode({"repo": repo})

        dashboard = self.get(f"/?{query}")
        payload, _headers = self.get_json(f"/api/dashboard?{query}")
        script = self.get("/static/app.js")

        self.assertIn("Recently finalized", dashboard)
        self.assertIn('data-dashboard-list="recently_merged"', dashboard)
        self.assertIn("merged dashboard issue", dashboard)
        self.assertIn("Merged issue into main at abc123", dashboard)
        self.assertEqual(payload["summary"]["recently_merged"], 1)
        self.assertEqual(payload["recently_merged"][0]["issue_id"], issue.id)
        self.assertEqual(payload["recently_merged"][0]["run_id"], "merge-dashboard")
        self.assertIn("renderRecentlyMerged", script)
        self.assertIn('data-dashboard-list="recently_merged"', script)

    def test_repo_context_selector_scopes_dashboard_list_and_new_issue_form(self) -> None:
        repo_a = '/tmp/repo "a" & <x>'
        repo_b = "/tmp/repo-b"
        issue_a = self.store.create_issue("repo a issue", "desc", repo_path=repo_a, ready=True)
        issue_b = self.store.create_issue("repo b issue", "desc", repo_path=repo_b, ready=True)
        query = urllib.parse.urlencode({"repo": repo_a})

        dashboard = self.get(f"/?{query}")
        payload, _headers = self.get_json(f"/api/dashboard?{query}")
        issue_list = self.get(f"/issues?{query}")
        new_issue = self.get(f"/issues/new?{query}")

        escaped_repo = html.escape(repo_a, quote=True)
        self.assertIn("Repo context", dashboard)
        self.assertIn(f'<option value="{escaped_repo}" selected>{escaped_repo}</option>', dashboard)
        self.assertIn("repo a issue", dashboard)
        self.assertNotIn("repo b issue", dashboard)
        self.assertIn(f'href="/issues?{html.escape(query, quote=True)}"', dashboard)
        self.assertIn(f'action="/actions/run-next?{html.escape(query, quote=True)}"', dashboard)
        self.assertIn(f'"dashboard_api_url": "/api/dashboard?{query}"', dashboard)
        self.assertEqual({item["id"] for item in payload["ready_issues"]}, {issue_a.id})
        self.assertEqual(payload["summary"]["ready"], 1)
        self.assertIn("repo a issue", issue_list)
        self.assertNotIn("repo b issue", issue_list)
        self.assertIn(f'<input type="hidden" name="repo" value="{escaped_repo}">', issue_list)
        self.assertIn(f'value="{escaped_repo}" placeholder="/path/to/repo" required', new_issue)
        self.assertIn('<input name="ready" type="checkbox" value="1" checked>', new_issue)
        self.assertNotIn('name="title"', new_issue)
        self.assertNotIn(issue_b.id, {item["id"] for item in payload["ready_issues"]})

        unknown_query = urllib.parse.urlencode({"repo": "/tmp/unknown"})
        unknown_dashboard = self.get(f"/?{unknown_query}")
        self.assertIn('<option value="/tmp/unknown" selected>/tmp/unknown</option>', unknown_dashboard)

    def test_repo_context_create_prefill_can_be_used_or_overridden(self) -> None:
        repo_a = "/tmp/repo-a"
        repo_b = "/tmp/repo-b"
        query = urllib.parse.urlencode({"repo": repo_a})

        self.post(
            f"/issues?{query}",
            {"description": "uses context", "priority": "3"},
        )
        self.post(
            f"/issues?{query}",
            {"description": "blank uses context", "repo_path": "", "priority": "3"},
        )
        self.post(
            f"/issues?{query}",
            {"description": "overrides context", "repo_path": repo_b, "priority": "3"},
        )

        issues = {issue.title: issue for issue in self.store.list_issues()}
        self.assertEqual(issues["uses context"].repo_path, repo_a)
        self.assertEqual(issues["blank uses context"].repo_path, repo_a)
        self.assertEqual(issues["overrides context"].repo_path, repo_b)

        with self.assertRaises(urllib.error.HTTPError) as missing_repo:
            self.post("/issues", {"description": "missing target repo", "priority": "3"})
        self.assertEqual(missing_repo.exception.code, 400)
        self.assertIn("target repo is required", missing_repo.exception.read().decode("utf-8"))

    def test_repo_context_run_next_and_job_visibility_are_scoped(self) -> None:
        repo_a = "/tmp/repo-a"
        repo_b = "/tmp/repo-b"
        repo_b_first = self.store.create_issue("repo b first", "desc", repo_path=repo_b, priority=1, ready=True)
        repo_a_second = self.store.create_issue("repo a second", "desc", repo_path=repo_a, priority=5, ready=True)
        query_a = urllib.parse.urlencode({"repo": repo_a})
        query_b = urllib.parse.urlencode({"repo": repo_b})

        self.post(f"/actions/run-next?{query_a}", {})
        self.assertTrue(self.app.jobs.wait_for_idle(5))

        self.assertEqual(self.store.get_issue(repo_a_second.id).phase, "ready_for_plan")
        self.assertEqual(self.store.get_issue(repo_b_first.id).phase, "needs_research")
        payload_a, _headers = self.get_json(f"/api/dashboard?{query_a}")
        scoped_job_ids = {job["id"] for job in payload_a["jobs"]}
        self.assertTrue(any(job["repo_path"] == repo_a for job in payload_a["jobs"]))

        global_job = self.app.jobs.submit_run_next()
        self.assertTrue(self.app.jobs.wait_for_idle(5))
        self.assertEqual(self.store.get_issue(repo_b_first.id).phase, "ready_for_plan")
        payload_b, _headers = self.get_json(f"/api/dashboard?{query_b}")
        self.assertNotIn(global_job.id, {job["id"] for job in payload_b["jobs"]})
        payload_all, _headers = self.get_json("/api/dashboard")
        self.assertIn(global_job.id, {job["id"] for job in payload_all["jobs"]})

        repo_a_issue_job = self.store.create_issue("repo a issue job", "desc", repo_path=repo_a, ready=True)
        issue_job = self.app.jobs.submit_run_issue(repo_a_issue_job.id)
        self.assertTrue(self.app.jobs.wait_for_idle(5))
        payload_a, _headers = self.get_json(f"/api/dashboard?{query_a}")
        self.assertIn(issue_job.id, {job["id"] for job in payload_a["jobs"]})
        self.assertTrue(scoped_job_ids.issubset({job["id"] for job in payload_a["jobs"]}))

    def test_dashboard_api_manager_buckets_are_not_limited_by_open_issue_sample(self) -> None:
        for index in range(101):
            self.store.create_issue(f"ready filler {index}", "desc", priority=1, ready=True)
        draft = self.store.create_issue("draft outside generic sample", "desc", priority=2)
        approval = self.store.create_issue("approval outside generic sample", "desc", priority=2, ready=True)
        self._move_to_plan_approval(approval.id)
        blocked = self.store.create_issue("blocked outside generic sample", "desc", priority=2, ready=True)
        self.store.transition_issue(blocked.id, "blocked")

        payload, _headers = self.get_json("/api/dashboard")

        summary = payload["summary"]
        self.assertEqual(summary["ready"], 101)
        self.assertEqual(summary["draft"], 1)
        self.assertEqual(summary["approval_needed"], 1)
        self.assertEqual(summary["blocked"], 1)
        self.assertIn(draft.id, {item["id"] for item in payload["draft_issues"]})
        self.assertIn(approval.id, {item["id"] for item in payload["approval_issues"]})
        self.assertIn(blocked.id, {item["id"] for item in payload["blocked_issues"]})

    def test_web_app_startup_recovers_stale_running_issue(self) -> None:
        issue = self.store.create_issue("stale issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "dead-run"))
        self.store.transition_issue(issue.id, "researching", "dead-run")
        self.store.create_run("dead-run", issue.id, "research", "dry-run")
        self._expire_lock(issue.id)

        recovered_app = AgentTeamWebApp(self.config, max_workers=1)
        try:
            recovered = recovered_app.store.get_issue(issue.id)
            self.assertEqual(recovered.phase, "needs_research")
            self.assertIsNone(recovered.current_run_id)
        finally:
            recovered_app.shutdown()

    def test_web_run_control_recovers_targeted_stale_issue_before_queueing(self) -> None:
        issue = self.store.create_issue("stale run issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "dead-run"))
        self.store.transition_issue(issue.id, "researching", "dead-run")
        self.store.create_run("dead-run", issue.id, "research", "dry-run")
        self._expire_lock(issue.id)

        self.post(f"/issues/{issue.id}/actions/run", {})

        self.assertTrue(self.app.jobs.wait_for_idle(5))
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")
        self.assertEqual([run["status"] for run in self.store.list_runs(issue.id)], ["interrupted", "success"])

    def test_web_plan_approval_recovers_stale_completed_plan_lock(self) -> None:
        issue = self.store.create_issue("stale plan approval", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "plan-run"))
        self.store.transition_issue(issue.id, "planning", "plan-run")
        self.store.create_run("plan-run", issue.id, "plan", "dry-run")
        self.store.complete_run(
            "plan-run",
            issue.id,
            "success",
            "plan completed",
            None,
            next_phase="awaiting_plan_approval",
        )
        self.store.transition_issue(issue.id, "awaiting_plan_approval", "plan-run")
        self._expire_lock(issue.id)

        self.post(f"/issues/{issue.id}/actions/approve-plan", {"message": "approved after recovery"})

        recovered = self.store.get_issue(issue.id)
        self.assertEqual(recovered.phase, "ready_for_implementation")
        self.assertIsNone(recovered.current_run_id)
        self.assertIsNone(recovered.lock_expires_at)

    def test_issue_api_returns_compact_state_without_artifact_bodies(self) -> None:
        issue = self.store.create_issue("api issue </script>", "desc", ready=True)
        self.artifacts.write_phase_artifact(issue.id, "research", "run-1", "<script>alert(1)</script>")

        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        html = self.get(f"/issues/{issue.id}")

        self.assertEqual(payload["issue"]["title"], "api issue </script>")
        self.assertEqual(payload["issue"]["phase"], "needs_research")
        self.assertIn("phase_timeline", payload)
        self.assertIn("Ready to run the research agent.", payload["next_action"])
        timeline_by_label = {step["label"]: step for step in payload["phase_timeline"]}
        self.assertEqual(
            timeline_by_label["Research"]["artifact"],
            {
                "label": "research artifact",
                "relative_path": "research.md",
                "url": f"/artifacts/{issue.id}/research.md",
            },
        )
        self.assertNotIn("content", timeline_by_label["Research"]["artifact"])
        self.assertNotIn("artifact", timeline_by_label["Plan"])
        self.assertEqual(payload["artifacts"][0]["relative_path"], "research.md")
        self.assertNotIn("content", payload["artifacts"][0])
        self.assertIsNone(payload["blocked_reason"])
        self.assertIsNone(payload["closed_synopsis"])
        self.assertIn('<script id="agent-team-bootstrap" type="application/json">', html)
        self.assertIn('data-log-toggle aria-pressed="false"', html)
        self.assertNotIn("api issue </script>", html)

    def test_closed_issue_detail_surfaces_bounded_synopsis(self) -> None:
        issue = self.store.create_issue("closed <title>", "Investigate <intent> and merge it.", ready=True)
        self.artifacts.write_phase_artifact(
            issue.id,
            "plan",
            "run-plan",
            "1. Executive Summary\n\nBuild the <recently> merged section.\n\n2. Proposed approach\n\nFULL_PLAN_SENTINEL\n",
        )
        self.artifacts.write_phase_artifact(
            issue.id,
            "implementation",
            "run-impl",
            "1. Summary of changes\n\nAdded dashboard synopsis <safe>.\n\n2. Files changed\n\nFULL_BODY_SENTINEL\n",
        )
        self.artifacts.write_phase_artifact(issue.id, "merge", "merge-synopsis", "Merge artifact <details>")
        self.artifacts.run_log_path(issue.id, "merge", "merge-synopsis").write_text("Merge log", encoding="utf-8")
        self.artifacts.write_merged_workspace_metadata(
            issue.id,
            {
                "merged_at": "2026-01-01T00:02:00+00:00",
                "merge_target_branch": "main<&>",
                "merge_commit": "abc<commit>",
                "worktree_commit": "def456",
            },
        )
        self._close_with_merge(issue.id, "merge-synopsis", "Merged issue into main at abc<script>")

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        synopsis = payload["closed_synopsis"]
        prominent_html = html[: html.index("Phase timeline")]

        self.assertIsNotNone(synopsis)
        self.assertIn("Closed synopsis", prominent_html)
        self.assertLess(html.index("Closed synopsis"), html.index("Phase timeline"))
        self.assertIn("Build the &lt;recently&gt; merged section.", prominent_html)
        self.assertIn("Added dashboard synopsis &lt;safe&gt;.", prominent_html)
        self.assertIn("Merged issue into main at abc&lt;script&gt;", prominent_html)
        self.assertIn("main&lt;&amp;&gt;", prominent_html)
        self.assertNotIn("Build the <recently> merged section.", html)
        self.assertNotIn("FULL_BODY_SENTINEL", html)
        self.assertEqual(synopsis["summary"], "Build the <recently> merged section.")
        self.assertEqual(synopsis["change_excerpt"], "Added dashboard synopsis <safe>.")
        self.assertNotIn("FULL_BODY_SENTINEL", synopsis["change_excerpt"])
        self.assertNotIn("FULL_PLAN_SENTINEL", synopsis["summary"])
        self.assertNotIn("content", synopsis)
        self.assertEqual(synopsis["target_branch"], "main<&>")
        self.assertEqual(synopsis["merge_commit"], "abc<commit>")
        self.assertEqual(
            {link["relative_path"] for link in synopsis["links"]},
            {
                "plan.md",
                "implementation.md",
                "merge.md",
                "logs/merge-merge-synopsis.md",
                "workspace.merged.json",
            },
        )

    def test_closed_issue_synopsis_falls_back_without_artifacts(self) -> None:
        issue = self.store.create_issue("fallback closed issue", "Fallback <description>.", ready=True)
        self._close_with_merge(issue.id, "merge-fallback", "Fallback merge summary <ok>")

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        synopsis = payload["closed_synopsis"]

        self.assertEqual(synopsis["source"], "issue")
        self.assertEqual(synopsis["summary"], "Fallback <description>.")
        self.assertEqual(synopsis["merge_summary"], "Fallback merge summary <ok>")
        self.assertIn("Fallback &lt;description&gt;.", html)
        self.assertIn("Fallback merge summary &lt;ok&gt;", html)
        self.assertNotIn("Fallback <description>.", html)

    def test_issue_detail_surfaces_blocked_run_reason(self) -> None:
        issue = self.store.create_issue("blocked run issue", "desc", ready=True)
        self._block_with_run(
            issue.id,
            run_id="run-blocked",
            summary="Need human <triage>",
            error="Runner failed on <unsafe> output",
        )

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]

        self.assertIn("Blocked reason", html)
        self.assertLess(html.index("Blocked reason"), html.index("Phase timeline"))
        self.assertLess(html.index("Blocked reason"), html.index("Primary controls"))
        self.assertIn("Need human &lt;triage&gt;", html)
        self.assertIn("Runner failed on &lt;unsafe&gt; output", html)
        self.assertNotIn("Need human <triage>", html)
        self.assertIn(f'href="/artifacts/{issue.id}/research.md"', html)
        self.assertIn(f'href="/artifacts/{issue.id}/logs/research-run-blocked.md"', html)
        self.assertEqual(reason["source"], "run")
        self.assertEqual(reason["summary"], "Need human <triage>")
        self.assertEqual(reason["error"], "Runner failed on <unsafe> output")
        self.assertEqual(reason["artifact"]["relative_path"], "research.md")
        self.assertEqual(reason["log"]["relative_path"], "logs/research-run-blocked.md")
        self.assertNotIn("content", reason["artifact"])
        self.assertNotIn("content", reason["log"])

    def test_issue_detail_uses_persisted_blocked_summary_as_primary_reason(self) -> None:
        issue = self.store.create_issue("blocked summary issue", "desc", ready=True)
        self._block_with_run(
            issue.id,
            run_id="run-summary",
            summary="Verbose runner output includes stack traces and internal retry metadata.",
            error="Runner failed on <unsafe> output",
            blocked_summary="Credentials are missing. Add them and rerun research.",
        )

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]
        prominent_html = html[: html.index("Phase timeline")]
        primary_html = prominent_html[: prominent_html.index("<details")]

        self.assertEqual(payload["issue"]["blocked_summary"], "Credentials are missing. Add them and rerun research.")
        self.assertEqual(reason["summary"], "Credentials are missing. Add them and rerun research.")
        self.assertEqual(reason["technical_summary"], "Verbose runner output includes stack traces and internal retry metadata.")
        self.assertIn("Credentials are missing. Add them and rerun research.", primary_html)
        self.assertNotIn("Runner failed", primary_html)
        self.assertIn("<summary>Technical details</summary>", prominent_html)
        self.assertIn("Runner failed on &lt;unsafe&gt; output", prominent_html)

        dashboard = self.get("/")
        dashboard_payload, _headers = self.get_json("/api/dashboard")
        self.assertEqual(
            dashboard_payload["blocked_issues"][0]["blocked_summary"],
            "Credentials are missing. Add them and rerun research.",
        )
        self.assertIn("Credentials are missing. Add them and rerun research.", dashboard)

    def test_issue_detail_keeps_agent_summary_when_run_error_is_generic(self) -> None:
        issue = self.store.create_issue("agent summary issue", "desc", ready=True)
        run_id = "run-agent-summary"
        phase = "research"
        artifact_markdown = (
            "1. Summary\n\n"
            "Verbose artifact preface should stay secondary.\n\n"
            "Blocked summary: Source checkout credentials are missing. Add them and rerun research.\n"
            "Recommendation: `blocked`\n"
        )
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, run_id))
        self.store.transition_issue(issue.id, "researching", run_id, "Starting research")
        self.store.create_run(run_id, issue.id, phase, "copilot")
        artifact_path = self.artifacts.write_phase_artifact(issue.id, phase, run_id, artifact_markdown)
        self.store.complete_run(
            run_id,
            issue.id,
            "blocked",
            f"Copilot CLI {phase} recommended blocked for issue {issue.id}",
            str(artifact_path),
            f"Copilot CLI {phase} recommended blocked",
        )
        self.store.transition_issue(
            issue.id,
            "blocked",
            run_id,
            f"Copilot CLI {phase} recommended blocked for issue {issue.id}",
            blocked_summary="Source checkout credentials are missing. Add them and rerun research.",
        )
        self.store.release_lock(issue.id, "worker", run_id)

        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]

        self.assertEqual(
            reason["summary"],
            "Source checkout credentials are missing. Add them and rerun research.",
        )
        self.assertIn("Verbose artifact preface", reason["artifact_excerpt"])

    def test_recovered_terminal_run_exposes_artifact_blocked_summary(self) -> None:
        issue = self.store.create_issue("recovered blocked summary issue", "desc", ready=True)
        blocked_summary = "The source checkout credentials are missing. Add them and rerun research."
        self.assertTrue(self.store.acquire_lock(issue.id, "legacy-worker", 60, "failed-run"))
        self.store.transition_issue(issue.id, "researching", "failed-run")
        self.store.create_run("failed-run", issue.id, "research", "copilot")
        artifact_path = self.artifacts.write_phase_artifact(
            issue.id,
            "research",
            "failed-run",
            (
                "Verbose recovery diagnostics should stay secondary.\n\n"
                f"Blocked summary: {blocked_summary}\n"
                "Recommendation: `blocked`\n"
            ),
        )
        self.store.complete_run(
            "failed-run",
            issue.id,
            "failed",
            "Copilot CLI failed with generic recovery details.",
            str(artifact_path),
            "Generic subprocess failure details.",
        )
        self._expire_lock(issue.id)

        result = Orchestrator(self.store, self.artifacts, self.config).recover_interrupted_issue(issue.id)
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")

        self.assertIsNotNone(result)
        self.assertEqual(payload["issue"]["blocked_summary"], blocked_summary)
        self.assertEqual(payload["blocked_reason"]["summary"], blocked_summary)
        self.assertNotIn("Recovered terminal", payload["blocked_reason"]["summary"])

    def test_blocked_run_exposes_primary_retry_transition(self) -> None:
        issue = self.store.create_issue("retry blocked issue", "desc", ready=True)
        self._block_with_run(issue.id, run_id="run-retry", summary="Transient research failure")

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]
        suggested = reason["suggested_transition"]
        controls = payload["manager_controls"]
        retry_control = next(control for control in controls if control.get("button") == "Retry research")
        generic_transition = next(control for control in controls if control.get("button") == "Transition")
        retry_fields = {field["name"]: field for field in retry_control["fields"]}

        self.assertEqual(suggested["agent_phase"], "research")
        self.assertEqual(suggested["ready_phase"], "needs_research")
        self.assertEqual(suggested["label"], "Needs research (needs_research)")
        self.assertEqual(suggested["run_id"], "run-retry")
        self.assertEqual(retry_control["group"], "primary")
        self.assertTrue(retry_control["action"].endswith(f"/issues/{issue.id}/actions/transition"))
        self.assertEqual(retry_fields["next_phase"], {"type": "hidden", "name": "next_phase", "value": "needs_research"})
        self.assertEqual(
            retry_fields["message"],
            {"type": "hidden", "name": "message", "value": "Retrying research after blocked run"},
        )
        self.assertNotEqual(generic_transition.get("group"), "primary")
        self.assertIn("retry the research phase from the primary control", payload["next_action"])
        self.assertIn("Suggested retry: Needs research (needs_research)", html)
        self.assertLess(html.index("Retry research"), html.index("Advanced actions: override phase"))

        self.post(
            f"/issues/{issue.id}/actions/transition",
            {
                "next_phase": retry_fields["next_phase"]["value"],
                "message": retry_fields["message"]["value"],
            },
        )

        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")
        content = self.artifacts.unblock_context_path(issue.id).read_text(encoding="utf-8")
        self.assertIn("Resume phase: `needs_research`", content)
        self.assertIn("Retrying research after blocked run", content)

    def test_blocked_advanced_transition_with_message_writes_unblock_context(self) -> None:
        issue = self.store.create_issue("advanced blocked issue", "desc", ready=True)
        self._block_with_run(issue.id, run_id="run-advanced", summary="Research needs direction")

        self.post(
            f"/issues/{issue.id}/actions/transition",
            {
                "next_phase": "ready_for_plan",
                "message": "Skip research and plan the cached-path fix.",
            },
        )

        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")
        artifacts = {artifact.relative_path: artifact for artifact in self.artifacts.list_issue_artifacts(issue.id)}
        self.assertEqual(artifacts["unblock_context.md"].label, "unblock guidance")
        self.assertEqual(artifacts["unblock_context.md"].kind, "unblock_context")
        content = self.artifacts.unblock_context_path(issue.id).read_text(encoding="utf-8")
        self.assertIn("Resume phase: `ready_for_plan`", content)
        self.assertIn("Skip research and plan the cached-path fix.", content)

    def test_successful_run_blocked_transition_still_suggests_run_phase(self) -> None:
        issue = self.store.create_issue("success run blocked issue", "desc")
        self._move_to_plan_approval(issue.id)
        self.store.transition_issue(issue.id, "ready_for_implementation")
        run_id = "run-success-blocked"
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, run_id))
        self.store.transition_issue(issue.id, "implementing", run_id)
        self.store.create_run(run_id, issue.id, "implementation", "dry-run")
        self.store.complete_run(
            run_id,
            issue.id,
            "success",
            "Implementation finished with an unusable next phase",
            None,
            next_phase="not_a_phase",
        )
        self.store.transition_issue(issue.id, "blocked", run_id, "Invalid stored next phase")
        self.store.release_lock(issue.id, "worker", run_id)

        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]
        suggested = reason["suggested_transition"]
        retry_control = next(control for control in payload["manager_controls"] if control.get("button") == "Retry implementation")
        retry_fields = {field["name"]: field for field in retry_control["fields"]}

        self.assertEqual(reason["source"], "transition")
        self.assertEqual(reason["run_id"], run_id)
        self.assertEqual(suggested["agent_phase"], "implementation")
        self.assertEqual(suggested["ready_phase"], "ready_for_implementation")
        self.assertEqual(retry_fields["next_phase"]["value"], "ready_for_implementation")

    def test_issue_detail_surfaces_manual_block_reason(self) -> None:
        issue = self.store.create_issue("manual block issue", "desc", ready=True)
        self.store.transition_issue(issue.id, "blocked", message="Blocked by manager <needs context>")
        self.store.add_event(issue.id, "lock.released", "later unrelated event")

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]

        self.assertIn("Blocked reason", html)
        self.assertIn("Blocked by manager &lt;needs context&gt;", html)
        self.assertNotIn("Blocked by manager <needs context>", html)
        self.assertNotIn("later unrelated event", html[: html.index("Phase timeline")])
        self.assertEqual(reason["source"], "manual_transition")
        self.assertEqual(reason["summary"], "Blocked by manager <needs context>")
        self.assertIsNone(reason["suggested_transition"])
        self.assertIsNone(reason["artifact"])
        self.assertIsNone(reason["log"])
        self.assertIn("no automatic retry target was found", payload["next_action"])
        self.assertFalse(any(control.get("button", "").startswith("Retry ") for control in payload["manager_controls"]))
        self.assertIn("Advanced actions: override phase", html)

    def test_issue_detail_prefers_current_manual_block_over_old_run_reason(self) -> None:
        issue = self.store.create_issue("current block issue", "desc", ready=True)
        self._block_with_run(
            issue.id,
            run_id="run-old-block",
            summary="Old agent blocker <stale>",
            error="Old detailed error",
        )
        self.store.transition_issue(issue.id, "ready_for_plan", message="Manager resumed work")
        self.store.transition_issue(issue.id, "blocked", message="Current manual blocker <fresh>")

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]
        prominent_html = html[: html.index("Phase timeline")]

        self.assertEqual(reason["source"], "manual_transition")
        self.assertEqual(reason["summary"], "Current manual blocker <fresh>")
        self.assertIsNone(reason["suggested_transition"])
        self.assertIn("Current manual blocker &lt;fresh&gt;", prominent_html)
        self.assertNotIn("Old agent blocker", prominent_html)

    def test_issue_detail_uses_artifact_excerpt_for_generic_copilot_block(self) -> None:
        issue = self.store.create_issue("generic copilot block issue", "desc", ready=True)
        run_id = "run-generic-block"
        phase = "research"
        real_reason = "Validation cannot continue because the deployment slot is missing <prod> credentials."
        artifact_markdown = (
            "1. Summary\n\n"
            f"{real_reason}\n\n"
            f"{'x' * 800} FULL_BODY_SENTINEL\n\n"
            "6. Recommendation: `blocked`\n"
        )
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, run_id))
        self.store.transition_issue(issue.id, "researching", run_id, "Starting research")
        self.store.create_run(run_id, issue.id, phase, "copilot")
        artifact_path = self.artifacts.write_phase_artifact(issue.id, phase, run_id, artifact_markdown)
        self.artifacts.run_log_path(issue.id, phase, run_id).write_text("Blocked run log", encoding="utf-8")
        summary = f"Copilot CLI {phase} recommended blocked for issue {issue.id}"
        error = f"Copilot CLI {phase} recommended blocked"
        self.store.complete_run(run_id, issue.id, "blocked", summary, str(artifact_path), error)
        self.store.transition_issue(issue.id, "blocked", run_id, summary)
        self.store.release_lock(issue.id, "worker", run_id)

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]
        prominent_html = html[: html.index("Phase timeline")]

        self.assertEqual(reason["source"], "run")
        self.assertEqual(reason["run_summary"], summary)
        self.assertIsNone(reason["error"])
        self.assertIn("deployment slot is missing <prod> credentials", reason["summary"])
        self.assertIn("deployment slot is missing <prod> credentials", reason["artifact_excerpt"])
        self.assertNotIn("FULL_BODY_SENTINEL", reason["summary"])
        self.assertNotIn("content", reason["artifact"])
        self.assertIn("deployment slot is missing &lt;prod&gt; credentials", prominent_html)
        primary_html = prominent_html[: prominent_html.index("<details")]
        self.assertNotIn("Copilot CLI research recommended blocked", primary_html)

    def test_issue_detail_uses_artifact_excerpt_for_legacy_recommendation_parse_block(self) -> None:
        issue = self.store.create_issue("legacy recommendation block issue", "desc", ready=True)
        run_id = "run-legacy-recommendation-block"
        phase = "research"
        real_reason = "Research stopped because the source checkout needs <manual> repair."
        artifact_markdown = f"1. Summary\n\n{real_reason}\n\n6. Recommendation: `blocked`\n"
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, run_id))
        self.store.transition_issue(issue.id, "researching", run_id, "Starting research")
        self.store.create_run(run_id, issue.id, phase, "copilot")
        artifact_path = self.artifacts.write_phase_artifact(issue.id, phase, run_id, artifact_markdown)
        self.artifacts.run_log_path(issue.id, phase, run_id).write_text("Legacy parse block log", encoding="utf-8")
        summary = f"Copilot CLI {phase} did not provide a valid Recommendation"
        self.store.complete_run(run_id, issue.id, "blocked", summary, str(artifact_path), summary)
        self.store.transition_issue(issue.id, "blocked", run_id, summary)
        self.store.release_lock(issue.id, "worker", run_id)

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]
        prominent_html = html[: html.index("Phase timeline")]

        self.assertEqual(reason["source"], "run")
        self.assertEqual(reason["run_summary"], summary)
        self.assertEqual(reason["error"], summary)
        self.assertIn("source checkout needs <manual> repair", reason["summary"])
        self.assertIn("source checkout needs <manual> repair", reason["artifact_excerpt"])
        self.assertEqual(reason["artifact"]["relative_path"], "research.md")
        self.assertEqual(reason["log"]["relative_path"], f"logs/{phase}-{run_id}.md")
        self.assertIn("source checkout needs &lt;manual&gt; repair", prominent_html)
        primary_html = prominent_html[: prominent_html.index("<details")]
        self.assertNotIn("Copilot CLI research did not provide a valid Recommendation", primary_html)

    def test_issue_detail_keeps_new_recommendation_diagnostic_detail(self) -> None:
        issue = self.store.create_issue("new recommendation block issue", "desc", ready=True)
        run_id = "run-new-recommendation-block"
        phase = "research"
        real_reason = "Research cannot proceed until the <prod> branch is restored."
        artifact_markdown = f"1. Summary\n\n{real_reason}\n\n6. Recommendation: `ready_for_validation`\n"
        self.assertTrue(self.store.acquire_lock(issue.id, "worker", 60, run_id))
        self.store.transition_issue(issue.id, "researching", run_id, "Starting research")
        self.store.create_run(run_id, issue.id, phase, "copilot")
        artifact_path = self.artifacts.write_phase_artifact(issue.id, phase, run_id, artifact_markdown)
        self.artifacts.run_log_path(issue.id, phase, run_id).write_text("New parse block log", encoding="utf-8")
        summary = (
            f"Copilot CLI {phase} provided invalid Recommendation 'ready_for_validation'; "
            "expected one of: blocked, ready_for_plan"
        )
        error = summary + " <unsafe>"
        self.store.complete_run(run_id, issue.id, "blocked", summary, str(artifact_path), error)
        self.store.transition_issue(issue.id, "blocked", run_id, summary)
        self.store.release_lock(issue.id, "worker", run_id)

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        reason = payload["blocked_reason"]
        prominent_html = html[: html.index("Phase timeline")]

        self.assertEqual(reason["source"], "run")
        self.assertEqual(reason["run_summary"], summary)
        self.assertEqual(reason["error"], error)
        self.assertIn("<prod> branch is restored", reason["summary"])
        self.assertIn("<prod> branch is restored", reason["artifact_excerpt"])
        self.assertEqual(reason["artifact"]["relative_path"], "research.md")
        self.assertEqual(reason["log"]["relative_path"], f"logs/{phase}-{run_id}.md")
        self.assertIn("&lt;prod&gt; branch is restored", prominent_html)
        self.assertIn("provided invalid Recommendation", prominent_html)
        self.assertIn("&lt;unsafe&gt;", prominent_html)
        self.assertNotIn("<prod> branch is restored", html)
        self.assertNotIn("<unsafe>", html)

    def test_issue_api_live_controls_change_after_phase_transition(self) -> None:
        issue = self.store.create_issue("live controls issue", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.store.transition_issue(issue.id, "planning")

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        actions = {control["action"] for control in payload["manager_controls"]}
        self.assertIn('data-action-stack', html)
        self.assertNotIn(f"/issues/{issue.id}/actions/approve-plan", actions)
        self.assertNotIn("/actions/approve-plan", html)

        self.store.transition_issue(issue.id, "awaiting_plan_approval")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        actions = {control["action"] for control in payload["manager_controls"]}
        reject_control = next(
            control for control in payload["manager_controls"] if control["action"].endswith("/actions/reject-plan")
        )
        updated_html = self.get(f"/issues/{issue.id}")

        self.assertEqual(payload["csrf_token"], self.app.csrf_token)
        self.assertIn("manager_controls_signature", payload)
        self.assertIn("data-controls-signature=", updated_html)
        self.assertIn(f"/issues/{issue.id}/actions/approve-plan", actions)
        self.assertIn(f"/issues/{issue.id}/actions/reject-plan", actions)
        self.assertEqual(reject_control["fields"][0]["name"], "feedback")
        self.assertTrue(reject_control["fields"][0]["required"])

    def test_issue_live_transition_to_merge_approval_reloads_detail_layout(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the live-layout JavaScript regression test")
        script = self.get("/static/app.js")
        issue_payload = {
            "generated_at": "now",
            "issue": {
                "title": "merge approval live layout issue",
                "phase": "awaiting_merge_approval",
                "status": "open",
                "current_run_id": None,
                "lock_owner": None,
                "lock_expires_at": None,
            },
            "active_job": None,
            "closed_synopsis": None,
            "blocked_reason": None,
            "next_action": "Review the completed work, then approve merge or send it back.",
            "csrf_token": "fresh-token",
            "manager_controls": [],
            "manager_controls_signature": "[]",
            "phase_timeline": [],
            "recent_events": [],
            "recent_runs": [],
            "artifacts": [],
        }
        test_script = f"""
const assert = require("assert");
const appScript = {json.dumps(script)};
const issuePayload = {json.dumps(issue_payload)};
const logPayload = {{
  generated_at: "now",
  issue_id: 1,
  log: {{ exists: false, relative_path: null, content: "", size_bytes: 0, truncated: false }}
}};
const phaseNode = {{ textContent: "reviewing" }};
const liveStatus = {{
  textContent: "",
  classList: {{ toggle() {{}} }}
}};
const actionStack = {{
  replaceCount: 0,
  getAttribute() {{ return "old-controls"; }},
  querySelectorAll() {{ return []; }},
  replaceChildren() {{ this.replaceCount += 1; }}
}};
let reloadCount = 0;
global.document = {{
  hidden: false,
  getElementById(id) {{
    return id === "agent-team-bootstrap"
      ? {{ textContent: JSON.stringify({{ page: "issue", issue_id: 1, csrf_token: "stale-token" }}) }}
      : null;
  }},
  querySelector(selector) {{
    if (selector === "[data-issue-phase]") return phaseNode;
    if (selector === "[data-live-status]") return liveStatus;
    if (selector === "[data-action-stack]") return actionStack;
    return null;
  }},
  createElement(tag) {{ return {{ tagName: tag, classList: {{ toggle() {{}} }} }}; }},
  createTextNode(text) {{ return {{ textContent: String(text) }}; }}
}};
global.window = {{
  setTimeout() {{}},
  location: {{ reload() {{ reloadCount += 1; }} }}
}};
global.fetch = function (path) {{
  const payload = path.indexOf("/logs/current") === -1 ? issuePayload : logPayload;
  return Promise.resolve({{
    ok: true,
    status: 200,
    json() {{ return Promise.resolve(payload); }}
  }});
}};
eval(appScript);
setTimeout(() => {{
  assert.strictEqual(reloadCount, 1);
  assert.strictEqual(actionStack.replaceCount, 0);
  assert.strictEqual(phaseNode.textContent, "reviewing");
  assert.strictEqual(liveStatus.textContent, "Merge approval is ready; refreshing page layout.");
}}, 0);
"""
        result = subprocess.run([node, "-e", test_script], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_issue_live_controls_preserve_dirty_form_fields_when_unchanged(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the live-control JavaScript regression test")
        issue = self.store.create_issue("dirty controls issue", "desc")
        self._move_to_plan_approval(issue.id)
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        script = self.get("/static/app.js")
        test_script = f"""
const assert = require("assert");
const appScript = {json.dumps(script)};
const issuePayload = {{
  generated_at: "now",
  issue: {{
    title: "dirty controls issue",
    phase: "awaiting_plan_approval",
    status: "open",
    current_run_id: null,
    lock_owner: null,
    lock_expires_at: null
  }},
  active_job: null,
  next_action: "Review the generated plan, then approve or reject it.",
  csrf_token: "fresh-token",
  manager_controls: {json.dumps(payload["manager_controls"])},
  manager_controls_signature: {json.dumps(payload["manager_controls_signature"])},
  phase_timeline: [],
  recent_events: [],
  recent_runs: [],
  artifacts: [],
  blocked_reason: {{
    source: "run",
    summary: "Blocked <summary>",
    error: "Detailed <error>",
    run_id: "run-js",
    phase: "research",
    status: "blocked",
    started_at: null,
    completed_at: "later",
    artifact: {{ label: "Open blocked artifact", url: "/artifacts/{issue.id}/research.md" }},
    log: null
  }}
}};
const logPayload = {{
  generated_at: "now",
  issue_id: {issue.id},
  log: {{ exists: false, relative_path: null, content: "", size_bytes: 0, truncated: false }}
}};
const csrfField = {{ value: "stale-token" }};
const dirtyTextarea = {{ value: "typed rejection feedback" }};
const actionStack = {{
  attrs: {{ "data-controls-signature": issuePayload.manager_controls_signature }},
  replaceCount: 0,
  getAttribute(name) {{ return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }},
  setAttribute(name, value) {{ this.attrs[name] = String(value); }},
  querySelectorAll(selector) {{ return selector === 'input[name="_csrf_token"]' ? [csrfField] : []; }},
  replaceChildren() {{
    this.replaceCount += 1;
    dirtyTextarea.value = "";
  }}
}};
const liveStatus = {{
  textContent: "",
  classList: {{ toggle() {{}} }}
}};
const blockedReason = fakeElement("section");
function fakeElement(tag) {{
  return {{
    tagName: tag,
    className: "",
    textContent: "",
    children: [],
    classList: {{ toggle() {{}} }},
    append() {{ this.children.push.apply(this.children, arguments); }},
    setAttribute(name, value) {{ this[name] = String(value); }},
    replaceChildren() {{ this.children = Array.prototype.slice.call(arguments); }},
    querySelectorAll() {{ return []; }}
  }};
}}
global.document = {{
  hidden: false,
  getElementById(id) {{
    return id === "agent-team-bootstrap"
      ? {{ textContent: JSON.stringify({{ page: "issue", issue_id: {issue.id}, csrf_token: "stale-token" }}) }}
      : null;
  }},
  querySelector(selector) {{
    if (selector === "[data-action-stack]") return actionStack;
    if (selector === "[data-live-status]") return liveStatus;
    if (selector === "[data-blocked-reason]") return blockedReason;
    return null;
  }},
  createElement: fakeElement,
  createTextNode(text) {{ return {{ textContent: String(text) }}; }}
}};
global.window = {{ setTimeout() {{}} }};
global.fetch = function (path) {{
  const payload = path.indexOf("/logs/current") === -1 ? issuePayload : logPayload;
  return Promise.resolve({{
    ok: true,
    status: 200,
    json() {{ return Promise.resolve(payload); }}
  }});
}};
eval(appScript);
setTimeout(() => {{
  assert.strictEqual(actionStack.replaceCount, 0);
  assert.strictEqual(dirtyTextarea.value, "typed rejection feedback");
  assert.strictEqual(csrfField.value, "fresh-token");
  assert.strictEqual(blockedReason.hidden, false);
  assert.strictEqual(blockedReason.className, "panel attention blocked-reason-panel");
  function collectText(node) {{
    return (node.textContent || "") + (node.children || []).map(collectText).join("");
  }}
  function collectLinks(node) {{
    const links = [];
    if (node.href) links.push(node.href);
    (node.children || []).forEach((child) => links.push.apply(links, collectLinks(child)));
    return links;
  }}
  const blockedText = collectText(blockedReason);
  assert(blockedText.includes("Blocked <summary>"));
  assert(blockedText.includes("Detailed <error>"));
  assert(collectLinks(blockedReason).includes("/artifacts/{issue.id}/research.md"));
}}, 0);
"""
        result = subprocess.run([node, "-e", test_script], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_issue_live_controls_honor_explicit_group_override(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the live-control JavaScript regression test")
        script = self.get("/static/app.js")
        controls = [
            {
                "action": "/issues/1/actions/transition",
                "method": "post",
                "button": "Retry research",
                "group": "primary",
                "fields": [
                    {"type": "hidden", "name": "next_phase", "value": "needs_research"},
                    {"type": "hidden", "name": "message", "value": "Retrying research after blocked run"},
                ],
            },
            {
                "action": "/issues/1/actions/transition",
                "method": "post",
                "button": "Transition",
                "fields": [],
            },
            {
                "action": "/issues/1/actions/delete",
                "method": "post",
                "button": "Delete issue (irreversible)",
                "fields": [],
            },
        ]
        test_script = f"""
const assert = require("assert");
const appScript = {json.dumps(script)};
const issuePayload = {{
  generated_at: "now",
  issue: {{
    title: "grouped controls issue",
    phase: "blocked",
    status: "open",
    current_run_id: null,
    lock_owner: null,
    lock_expires_at: null
  }},
  active_job: null,
  closed_synopsis: null,
  blocked_reason: null,
  next_action: "Retry research.",
  csrf_token: "fresh-token",
  manager_controls: {json.dumps(controls)},
  manager_controls_signature: "new-controls",
  phase_timeline: [],
  recent_events: [],
  recent_runs: [],
  artifacts: []
}};
const logPayload = {{
  generated_at: "now",
  issue_id: 1,
  log: {{ exists: false, relative_path: null, content: "", size_bytes: 0, truncated: false }}
}};
function fakeElement(tag) {{
  return {{
    tagName: tag,
    className: "",
    textContent: "",
    children: [],
    attrs: {{}},
    classList: {{ toggle() {{}} }},
    append() {{ this.children.push.apply(this.children, arguments); }},
    replaceChildren() {{ this.children = Array.prototype.slice.call(arguments); }},
    setAttribute(name, value) {{ this.attrs[name] = String(value); }},
    getAttribute(name) {{ return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }},
    querySelectorAll() {{ return []; }}
  }};
}}
const actionStack = fakeElement("div");
actionStack.attrs["data-controls-signature"] = "old-controls";
const liveStatus = fakeElement("p");
global.document = {{
  hidden: false,
  getElementById(id) {{
    return id === "agent-team-bootstrap"
      ? {{ textContent: JSON.stringify({{ page: "issue", issue_id: 1, csrf_token: "stale-token" }}) }}
      : null;
  }},
  querySelector(selector) {{
    if (selector === "[data-action-stack]") return actionStack;
    if (selector === "[data-live-status]") return liveStatus;
    return null;
  }},
  createElement: fakeElement,
  createTextNode(text) {{ return {{ textContent: String(text) }}; }}
}};
global.window = {{ setTimeout() {{}} }};
global.fetch = function (path) {{
  const payload = path.indexOf("/logs/current") === -1 ? issuePayload : logPayload;
  return Promise.resolve({{
    ok: true,
    status: 200,
    json() {{ return Promise.resolve(payload); }}
  }});
}};
function collectText(node) {{
  if (!node) return "";
  return (node.textContent || "") + (node.children || []).map(collectText).join("");
}}
eval(appScript);
setTimeout(() => {{
  assert.strictEqual(actionStack.children.length, 3);
  assert.strictEqual(actionStack.children[0].className, "control-group primary-action-group");
  assert.strictEqual(actionStack.children[1].className, "advanced-actions");
  assert.strictEqual(actionStack.children[2].className, "danger-zone");
  assert(collectText(actionStack.children[0]).includes("Retry research"));
  assert(!collectText(actionStack.children[1]).includes("Retry research"));
  assert(collectText(actionStack.children[1]).includes("Transition"));
  assert(collectText(actionStack.children[2]).includes("Delete issue (irreversible)"));
  assert.strictEqual(actionStack.attrs["data-controls-signature"], "new-controls");
}}, 0);
"""
        result = subprocess.run([node, "-e", test_script], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_issue_live_timeline_renders_artifact_links(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for the live-timeline JavaScript regression test")
        script = self.get("/static/app.js")
        issue_payload = {
            "generated_at": "now",
            "issue": {
                "title": "timeline links issue",
                "phase": "ready_for_plan",
                "status": "open",
                "current_run_id": None,
                "lock_owner": None,
                "lock_expires_at": None,
            },
            "active_job": None,
            "next_action": "Ready to run the plan agent.",
            "csrf_token": "fresh-token",
            "manager_controls": [],
            "manager_controls_signature": "[]",
            "phase_timeline": [
                {
                    "label": "Research",
                    "status": "done",
                    "phases": ["needs_research", "researching"],
                    "artifact": {
                        "label": "research artifact",
                        "relative_path": "research.md",
                        "url": "/artifacts/1/research.md",
                    },
                },
                {"label": "Plan", "status": "current", "phases": ["ready_for_plan", "planning"]},
            ],
            "recent_events": [],
            "recent_runs": [],
            "artifacts": [],
        }
        test_script = f"""
const assert = require("assert");
const appScript = {json.dumps(script)};
const issuePayload = {json.dumps(issue_payload)};
const logPayload = {{
  generated_at: "now",
  issue_id: 1,
  log: {{ exists: false, relative_path: null, content: "", size_bytes: 0, truncated: false }}
}};
const timeline = {{
  children: [],
  replaceChildren() {{ this.children = Array.prototype.slice.call(arguments); }}
}};
const liveStatus = {{
  textContent: "",
  classList: {{ toggle() {{}} }}
}};
function fakeElement(tag) {{
  return {{
    tagName: tag,
    className: "",
    textContent: "",
    children: [],
    attrs: {{}},
    href: "",
    title: "",
    classList: {{ toggle() {{}} }},
    append() {{ this.children.push.apply(this.children, arguments); }},
    setAttribute(name, value) {{ this.attrs[name] = String(value); }},
    replaceChildren() {{ this.children = Array.prototype.slice.call(arguments); }},
    querySelectorAll() {{ return []; }}
  }};
}}
global.document = {{
  hidden: false,
  getElementById(id) {{
    return id === "agent-team-bootstrap"
      ? {{ textContent: JSON.stringify({{ page: "issue", issue_id: 1, csrf_token: "stale-token" }}) }}
      : null;
  }},
  querySelector(selector) {{
    if (selector === "[data-phase-timeline]") return timeline;
    if (selector === "[data-live-status]") return liveStatus;
    return null;
  }},
  createElement: fakeElement,
  createTextNode(text) {{ return {{ textContent: String(text) }}; }}
}};
global.window = {{ setTimeout() {{}} }};
global.fetch = function (path) {{
  const payload = path.indexOf("/logs/current") === -1 ? issuePayload : logPayload;
  return Promise.resolve({{
    ok: true,
    status: 200,
    json() {{ return Promise.resolve(payload); }}
  }});
}};
eval(appScript);
setTimeout(() => {{
  assert.strictEqual(timeline.children.length, 2);
  assert.strictEqual(timeline.children[0].tagName, "a");
  assert.strictEqual(timeline.children[0].className, "phase-step done has-artifact");
  assert.strictEqual(timeline.children[0].href, "/artifacts/1/research.md");
  assert.strictEqual(timeline.children[0].textContent, "Research");
  assert.strictEqual(timeline.children[0].title, "Open research artifact");
  assert.strictEqual(timeline.children[0].attrs["aria-label"], "Open research artifact");
  assert.strictEqual(timeline.children[1].tagName, "span");
  assert.strictEqual(timeline.children[1].className, "phase-step current");
}}, 0);
"""
        result = subprocess.run([node, "-e", test_script], capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_current_log_api_handles_missing_active_log_and_utf8_tail(self) -> None:
        issue = self.store.create_issue("log issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "owner", 60, "run-current"))
        self.store.transition_issue(issue.id, "researching")
        self.store.create_run("run-current", issue.id, "research", "dry-run")

        payload, _headers = self.get_json(f"/api/issues/{issue.id}/logs/current")
        self.assertFalse(payload["log"]["exists"])
        self.assertEqual(payload["log"]["relative_path"], "logs/research-run-current.md")
        self.assertEqual(payload["log"]["content"], "")

        path = self.artifacts.run_log_path(issue.id, "research", "run-current")
        path.write_bytes(b"x" * 10 + bytes([0xC3, 0xA9]) + b"z" * (LOG_TAIL_BYTES - 1))
        payload, _headers = self.get_json(f"/api/issues/{issue.id}/logs/current")

        self.assertTrue(payload["log"]["exists"])
        self.assertTrue(payload["log"]["truncated"])
        self.assertIn(chr(0xFFFD), payload["log"]["content"])

    def test_current_log_api_handles_log_deleted_between_selection_and_tail_read(self) -> None:
        issue = self.store.create_issue("race log issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "owner", 60, "run-race"))
        self.store.transition_issue(issue.id, "researching")
        self.store.create_run("run-race", issue.id, "research", "dry-run")
        path = self.artifacts.run_log_path(issue.id, "research", "run-race")
        path.write_text("race log", encoding="utf-8")
        original_tail = self.app.artifacts.read_issue_artifact_tail

        def delete_before_read(issue_id: int, relative_path: str, max_bytes: int = LOG_TAIL_BYTES):
            path.unlink()
            return original_tail(issue_id, relative_path, max_bytes)

        self.app.artifacts.read_issue_artifact_tail = delete_before_read
        payload, _headers = self.get_json(f"/api/issues/{issue.id}/logs/current")

        self.assertFalse(payload["log"]["exists"])
        self.assertEqual(payload["log"]["relative_path"], "logs/research-run-race.md")
        self.assertEqual(payload["log"]["content"], "")
        self.assertEqual(payload["log"]["size_bytes"], 0)

    def test_current_log_prefers_active_run_over_newer_log_file(self) -> None:
        issue = self.store.create_issue("current log issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "owner", 60, "run-current"))
        self.store.transition_issue(issue.id, "researching")
        self.store.create_run("run-current", issue.id, "research", "dry-run")
        self.artifacts.run_log_path(issue.id, "research", "run-current").write_text("current log", encoding="utf-8")
        self.artifacts.run_log_path(issue.id, "plan", "run-newer").write_text("newer log", encoding="utf-8")

        payload, _headers = self.get_json(f"/api/issues/{issue.id}/logs/current")

        self.assertEqual(payload["log"]["relative_path"], "logs/research-run-current.md")
        self.assertEqual(payload["log"]["content"], "current log")

    def test_static_js_and_job_api_are_available(self) -> None:
        request = urllib.request.Request(self.base_url + "/static/app.js")
        with urllib.request.urlopen(request, timeout=5) as response:
            script = response.read().decode("utf-8")
            self.assertEqual(response.headers.get_content_type(), "application/javascript")
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("fetchJson", script)
        self.assertIn("renderControls", script)
        self.assertIn("queued browser actions", script)

        css_request = urllib.request.Request(self.base_url + "/static/styles.css")
        with urllib.request.urlopen(css_request, timeout=5) as response:
            styles = response.read().decode("utf-8")
            self.assertEqual(response.headers.get_content_type(), "text/css")
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn(".danger-zone", styles)
        self.assertIn(".diagnostics-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));", styles)
        self.assertIn(".diagnostics-grid > * { min-width: 0; }", styles)
        self.assertIn(".diagnostics [data-dashboard-list] { max-width: 100%; overflow-x: auto; }", styles)
        self.assertIn(".diagnostics table { table-layout: fixed; }", styles)
        self.assertIn(".artifact-list-viewer { max-height: 18rem; overflow: auto; }", styles)
        self.assertIn(
            ".diagnostics th, .diagnostics td, .diagnostics code, .diagnostics .muted { overflow-wrap: anywhere; }",
            styles,
        )
        self.assertIn(".diagnostics code { white-space: normal; }", styles)
        self.assertIn('<link rel="stylesheet" href="/static/styles.css">', self.get("/"))

        issue = self.store.create_issue("job api issue", "desc", ready=True)
        job = self.app.jobs.submit_run_issue(issue.id)
        self.assertTrue(self.app.jobs.wait_for_idle(5))
        payload, _headers = self.get_json(f"/api/jobs/{job.id}")
        self.assertEqual(payload["id"], job.id)
        self.assertEqual(payload["issue_id"], issue.id)
        self.assertEqual(payload["status"], "succeeded")

    def test_artifact_route_escapes_content_and_rejects_unknown_paths(self) -> None:
        issue = self.store.create_issue("artifact route issue", "desc")
        self.artifacts.write_issue_snapshot(issue)
        self.artifacts.write_phase_artifact(issue.id, "research", "run-1", "<script>alert(1)</script>")

        html = self.get(f"/artifacts/{issue.id}/research.md")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)

        for path in (f"/artifacts/{issue.id}/logs%2Fmissing.md", f"/artifacts/{issue.id}/..%2F..%2Fetc%2Fpasswd"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.get(path)
                self.assertEqual(caught.exception.code, 400)

    def test_workspace_metadata_is_shown_and_available_as_artifact(self) -> None:
        issue = self.store.create_issue("workspace issue", "desc")
        self.artifacts.write_workspace_metadata(
            issue.id,
            {
                "workspace_repo_path": "/tmp/worktrees/<workspace>",
                "worktree_root": "/tmp/worktrees/root",
            },
        )

        detail = self.get(f"/issues/{issue.id}")
        self.assertIn("Workspace", detail)
        self.assertIn("/tmp/worktrees/&lt;workspace&gt;", detail)
        artifact = self.get(f"/artifacts/{issue.id}/workspace.json")
        self.assertIn("/tmp/worktrees/&lt;workspace&gt;", artifact)

    def test_merge_approval_renders_open_in_vscode_link(self) -> None:
        issue = self.store.create_issue("vscode merge issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_workspace_metadata(
            issue.id,
            {
                "source_branch": "main",
                "workspace_repo_path": "/tmp/work trees/<workspace>?review#now",
            },
        )
        expected_href = "vscode://file/tmp/work%20trees/%3Cworkspace%3E%3Freview%23now"

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        vscode_control = next(
            control for control in payload["manager_controls"] if control.get("button") == "Open in VS Code"
        )

        self.assertIn(f'<a class="button" href="{expected_href}">Open in VS Code</a>', html)
        self.assertIn("/actions/approve-merge", html)
        self.assertIn('value="main"', html)
        self.assertEqual(
            vscode_control,
            {
                "action": expected_href,
                "href": expected_href,
                "method": "get",
                "kind": "link",
                "button": "Open in VS Code",
            },
        )

    def test_merge_approval_uses_remote_wsl_vscode_link_when_distro_is_configured(self) -> None:
        self.app.config = replace(self.app.config, vscode_wsl_distro="Ubuntu-22.04")
        issue = self.store.create_issue("vscode wsl merge issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_workspace_metadata(
            issue.id,
            {
                "source_branch": "main",
                "workspace_repo_path": "/home/dev/work tree/<workspace>?review#now",
            },
        )
        expected_href = (
            "vscode://vscode-remote/wsl+Ubuntu-22.04"
            "/home/dev/work%20tree/%3Cworkspace%3E%3Freview%23now"
        )

        html = self.get(f"/issues/{issue.id}")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        vscode_control = next(
            control for control in payload["manager_controls"] if control.get("button") == "Open in VS Code"
        )

        self.assertIn(f'<a class="button" href="{expected_href}">Open in VS Code</a>', html)
        self.assertEqual(vscode_control["href"], expected_href)
        self.assertEqual(vscode_control["action"], expected_href)

    def test_merge_approval_omits_open_in_vscode_for_missing_or_invalid_workspace_path(self) -> None:
        cases = (
            None,
            {"worktree_root": "/tmp/worktrees/root"},
            {"workspace_repo_path": ""},
            {"workspace_repo_path": "relative/path"},
        )
        for metadata in cases:
            with self.subTest(metadata=metadata):
                issue = self.store.create_issue("vscode omitted issue", "desc")
                self._move_to_merge_approval(issue.id)
                if metadata is not None:
                    self.artifacts.write_workspace_metadata(issue.id, metadata)

                html = self.get(f"/issues/{issue.id}")
                payload, _headers = self.get_json(f"/api/issues/{issue.id}")
                labels = {control.get("button") for control in payload["manager_controls"]}

                self.assertNotIn("Open in VS Code", labels)
                self.assertNotIn("vscode://file/", html)
                self.assertIn("/actions/approve-merge", html)

    def test_vscode_file_uri_normalizes_windows_paths(self) -> None:
        self.assertEqual(
            web_module._vscode_file_uri(r"C:\Users\manager\work tree\repo?x#y"),
            "vscode://file/C:/Users/manager/work%20tree/repo%3Fx%23y",
        )

    def test_vscode_workspace_uri_uses_remote_wsl_for_posix_paths(self) -> None:
        self.assertEqual(
            web_module._vscode_workspace_uri(
                "/home/dev/work tree/<repo>?review#now",
                "Ubuntu-22.04",
            ),
            "vscode://vscode-remote/wsl+Ubuntu-22.04/home/dev/work%20tree/%3Crepo%3E%3Freview%23now",
        )
        self.assertEqual(
            web_module._vscode_workspace_uri("/mnt/c/Users/dev/repo", "Ubuntu Preview"),
            "vscode://vscode-remote/wsl+Ubuntu%20Preview/mnt/c/Users/dev/repo",
        )

    def test_vscode_workspace_uri_preserves_local_behavior_without_wsl_distro(self) -> None:
        self.assertEqual(
            web_module._vscode_workspace_uri("/home/dev/repo", ""),
            "vscode://file/home/dev/repo",
        )
        self.assertEqual(
            web_module._vscode_workspace_uri(r"C:\Users\manager\repo", "Ubuntu"),
            "vscode://file/C:/Users/manager/repo",
        )

    def test_invalid_issue_id_returns_error_page(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/issues/999")
        self.assertEqual(caught.exception.code, 404)
        body = caught.exception.read().decode("utf-8")
        self.assertIn("Issue not found: 999", body)

    def test_plan_approval_and_manual_transition_use_state_machine(self) -> None:
        issue = self.store.create_issue("approval issue", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.store.transition_issue(issue.id, "planning")
        self.store.transition_issue(issue.id, "awaiting_plan_approval")

        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.post(f"/issues/{issue.id}/actions/transition", {"next_phase": "done"})
        self.assertEqual(invalid.exception.code, 400)

        self.post(
            f"/issues/{issue.id}/actions/approve-plan",
            {"message": "approved from test"},
        )
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_implementation")

        self.post(
            f"/issues/{issue.id}/actions/transition",
            {"next_phase": "blocked", "message": "manual block"},
        )
        self.assertEqual(self.store.get_issue(issue.id).phase, "blocked")

    def test_approve_plan_rejects_wrong_phase(self) -> None:
        issue = self.store.create_issue("not ready", "desc")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(f"/issues/{issue.id}/actions/approve-plan", {})
        self.assertEqual(caught.exception.code, 400)

    def test_answer_human_input_action_records_answer_and_resumes(self) -> None:
        issue = self.store.create_issue("human input answer", "desc")
        self._move_to_human_input(issue.id)

        self.post(f"/issues/{issue.id}/actions/answer-human-input", {"answer": "Use option B"})

        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")
        requests = self.store.list_human_input_requests(issue.id)
        self.assertEqual(requests[0].status, "answered")
        self.assertEqual(requests[0].answer, "Use option B")
        self.assertIn(
            "Use option B",
            (self.config.artifacts_dir / str(issue.id) / "human_input.md").read_text(encoding="utf-8"),
        )

    def test_answer_human_input_rejects_wrong_phase_empty_and_transition_bypass(self) -> None:
        issue = self.store.create_issue("human input validation", "desc")
        with self.assertRaises(urllib.error.HTTPError) as wrong_phase:
            self.post(f"/issues/{issue.id}/actions/answer-human-input", {"answer": "answer"})
        self.assertEqual(wrong_phase.exception.code, 400)

        self._move_to_human_input(issue.id)
        with self.assertRaises(urllib.error.HTTPError) as empty:
            self.post(f"/issues/{issue.id}/actions/answer-human-input", {"answer": "   "})
        self.assertEqual(empty.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as bypass:
            self.post(f"/issues/{issue.id}/actions/transition", {"next_phase": "needs_research"})
        self.assertEqual(bypass.exception.code, 400)
        self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_human_input")

    def test_stop_action_blocks_ready_approval_and_human_input_issues(self) -> None:
        ready = self.store.create_issue("ready stop", "desc", ready=True)
        approval = self.store.create_issue("approval stop", "desc", ready=True)
        self._move_to_plan_approval(approval.id)
        human_input = self.store.create_issue("human stop", "desc")
        self._move_to_human_input(human_input.id)

        ready_html = self.post(f"/issues/{ready.id}/actions/stop", {"message": "Pause ready from web."})
        self.post(f"/issues/{approval.id}/actions/stop", {})
        self.post(f"/issues/{human_input.id}/actions/stop", {"message": "Pause human input from web."})

        self.assertEqual(self.store.get_issue(ready.id).phase, "blocked")
        self.assertEqual(self.store.get_issue(ready.id).blocked_summary, "Pause ready from web.")
        self.assertIn(f"Issue {ready.id} stopped at blocked", ready_html)
        approval_issue = self.store.get_issue(approval.id)
        self.assertEqual(approval_issue.phase, "blocked")
        self.assertEqual(approval_issue.blocked_summary, "Issue stopped by manager")
        human_issue = self.store.get_issue(human_input.id)
        self.assertEqual(human_issue.phase, "blocked")
        self.assertEqual(human_issue.blocked_summary, "Pause human input from web.")
        requests = self.store.list_human_input_requests(human_input.id)
        self.assertEqual(requests[0].status, "stopped")
        self.assertIsNone(self.store.get_pending_human_input_request(human_input.id))
        payload, _headers = self.get_json(f"/api/issues/{human_input.id}")
        self.assertIsNone(payload["human_input"]["pending"])
        self.assertEqual(payload["human_input"]["requests"][0]["status"], "stopped")

    def test_stop_action_rejects_draft_blocked_done_lock_and_active_job(self) -> None:
        draft = self.store.create_issue("draft stop", "desc")
        with self.assertRaises(urllib.error.HTTPError) as draft_error:
            self.post(f"/issues/{draft.id}/actions/stop", {})
        self.assertEqual(draft_error.exception.code, 400)

        blocked = self.store.create_issue("blocked stop", "desc", ready=True)
        self.store.transition_issue(blocked.id, "blocked")
        self.post(f"/issues/{blocked.id}/actions/stop", {"message": "already stopped"})
        self.assertEqual(self.store.get_issue(blocked.id).phase, "blocked")

        done = self.store.create_issue("done stop", "desc")
        self._close_with_merge(done.id, "run-done-stop", "done")
        with self.assertRaises(urllib.error.HTTPError) as done_error:
            self.post(f"/issues/{done.id}/actions/stop", {})
        self.assertEqual(done_error.exception.code, 400)

        locked = self.store.create_issue("locked stop", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(locked.id, "test-owner", 60, "run-1"))
        with self.assertRaises(urllib.error.HTTPError) as lock_error:
            self.post(f"/issues/{locked.id}/actions/stop", {})
        self.assertEqual(lock_error.exception.code, 409)

        queued = self.store.create_issue("queued stop", "desc", ready=True)
        now = utc_now_iso()
        with self.app.jobs._lock:
            self.app.jobs._jobs["queued-stop"] = WebJob(
                id="queued-stop",
                action="Run issue",
                issue_id=queued.id,
                status="queued",
                message="Queued",
                created_at=now,
                updated_at=now,
            )
        with self.assertRaises(urllib.error.HTTPError) as job_error:
            self.post(f"/issues/{queued.id}/actions/stop", {})
        self.assertEqual(job_error.exception.code, 409)

    def test_transition_to_human_input_without_request_is_rejected(self) -> None:
        issue = self.store.create_issue("manual human input", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")

        html = self.get(f"/issues/{issue.id}")
        self.assertNotIn('<option value="awaiting_human_input"', html)
        with self.assertRaises(urllib.error.HTTPError) as bypass:
            self.post(f"/issues/{issue.id}/actions/transition", {"next_phase": "awaiting_human_input"})

        self.assertEqual(bypass.exception.code, 400)
        self.assertEqual(self.store.get_issue(issue.id).phase, "researching")
        self.assertIsNone(self.store.get_pending_human_input_request(issue.id))

    def test_approve_merge_action_records_request_and_advances(self) -> None:
        issue = self.store.create_issue("merge issue", "desc")
        self._move_to_merge_approval(issue.id)

        self.post(
            f"/issues/{issue.id}/actions/approve-merge",
            {"branch": "main", "message": "merge approved"},
        )

        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_merge")
        request = self.artifacts.read_merge_request(issue.id)
        self.assertIsNotNone(request)
        self.assertEqual(request["target_branch"], "main")
        self.assertEqual(request["message"], "merge approved")
        self.assertEqual(request["mode"], "auto")

    def test_approve_merge_action_defaults_to_configured_merge_mode(self) -> None:
        self.app.config = replace(self.app.config, merge_mode="local")
        issue = self.store.create_issue("merge issue", "desc")
        self._move_to_merge_approval(issue.id)

        self.post(
            f"/issues/{issue.id}/actions/approve-merge",
            {"branch": "main", "message": "merge approved"},
        )

        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_merge")
        request = self.artifacts.read_merge_request(issue.id)
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["mode"], "local")

    def test_approve_merge_action_records_explicit_pull_request_mode_and_remote(self) -> None:
        issue = self.store.create_issue("merge issue", "desc")
        self._move_to_merge_approval(issue.id)

        self.post(
            f"/issues/{issue.id}/actions/approve-merge",
            {"branch": "main", "message": "open a PR", "mode": "pull-request", "remote": "upstream"},
        )

        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_merge")
        request = self.artifacts.read_merge_request(issue.id)
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request["mode"], "pull_request")
        self.assertEqual(request["remote_name"], "upstream")

    def test_approve_merge_rejects_wrong_phase(self) -> None:
        issue = self.store.create_issue("not ready", "desc")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(f"/issues/{issue.id}/actions/approve-merge", {})
        self.assertEqual(caught.exception.code, 400)

    def test_reject_plan_action_transitions_and_saves_feedback(self) -> None:
        issue = self.store.create_issue("reject issue", "desc")
        self._move_to_plan_approval(issue.id)
        self.artifacts.write_phase_artifact(issue.id, "plan", "run-1", "Draft plan content")

        self.post(f"/issues/{issue.id}/actions/reject-plan", {"feedback": "Use a smaller scope."})

        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")
        self.assertIn(
            "Use a smaller scope.",
            (self.config.artifacts_dir / str(issue.id) / "plan_feedback.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "Draft plan content",
            (self.config.artifacts_dir / str(issue.id) / "plan_prior.md").read_text(encoding="utf-8"),
        )
        events = self.store.list_events(issue.id)
        self.assertEqual(events[-1]["event_type"], "plan.rejected")

    def test_transition_back_to_plan_with_message_saves_feedback(self) -> None:
        issue = self.store.create_issue("generic reject issue", "desc")
        self._move_to_plan_approval(issue.id)
        self.artifacts.write_phase_artifact(issue.id, "plan", "run-1", "Draft plan content")

        self.post(
            f"/issues/{issue.id}/actions/transition",
            {"next_phase": "ready_for_plan", "message": "Use a smaller scope."},
        )

        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")
        self.assertIn(
            "Use a smaller scope.",
            (self.config.artifacts_dir / str(issue.id) / "plan_feedback.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(self.store.list_events(issue.id)[-1]["event_type"], "plan.rejected")

    def test_reject_plan_requires_approval_phase_and_feedback(self) -> None:
        issue = self.store.create_issue("not ready", "desc")
        with self.assertRaises(urllib.error.HTTPError) as wrong_phase:
            self.post(f"/issues/{issue.id}/actions/reject-plan", {"feedback": "Use a smaller scope."})
        self.assertEqual(wrong_phase.exception.code, 400)

        self._move_to_plan_approval(issue.id)
        with self.assertRaises(urllib.error.HTTPError) as empty_feedback:
            self.post(f"/issues/{issue.id}/actions/reject-plan", {"feedback": "   "})
        self.assertEqual(empty_feedback.exception.code, 400)

    def test_issue_detail_renders_reject_plan_form(self) -> None:
        issue = self.store.create_issue("reject form issue", "desc")
        self._move_to_plan_approval(issue.id)

        html = self.get(f"/issues/{issue.id}")

        self.assertIn("/actions/reject-plan", html)
        self.assertIn("Rejection feedback", html)
        self.assertIn("Reject plan", html)

    def test_issue_detail_renders_approve_merge_form(self) -> None:
        issue = self.store.create_issue("merge form issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_workspace_metadata(issue.id, {"source_branch": "main"})

        html = self.get(f"/issues/{issue.id}")

        self.assertIn("/actions/approve-merge", html)
        self.assertIn("Target branch", html)
        self.assertIn('value="main"', html)
        self.assertIn("Finalization mode", html)
        self.assertIn('<option value="auto" selected>', html)
        self.assertIn("PR remote (optional)", html)
        self.assertIn("Approve merge", html)

    def test_issue_detail_approve_merge_form_defaults_to_configured_mode_and_remote(self) -> None:
        self.app.config = replace(self.app.config, merge_mode="local", pr_remote="upstream")
        issue = self.store.create_issue("merge form issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.artifacts.write_workspace_metadata(issue.id, {"source_branch": "main"})

        html = self.get(f"/issues/{issue.id}")

        self.assertIn('<option value="local" selected>', html)
        self.assertIn('name="remote" value="upstream"', html)

    def test_post_requires_csrf_token(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(
                "/issues",
                {"description": "csrf issue", "priority": "3"},
                include_csrf=False,
            )
        self.assertEqual(caught.exception.code, 403)
        self.assertIn("Invalid CSRF token", caught.exception.read().decode("utf-8"))

    def test_post_rejects_null_malformed_and_cross_origin_headers(self) -> None:
        for origin in ("null", "not-a-url", "http://evil.example"):
            with self.subTest(origin=origin):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.post(
                        "/issues",
                        {"description": "origin issue", "priority": "3"},
                        headers={"Origin": origin},
                    )
                self.assertEqual(caught.exception.code, 403)

        html = self.post(
            "/issues",
            {"description": "same origin issue", "repo_path": "/tmp/repo", "priority": "3"},
            headers={"Origin": self.base_url},
        )
        self.assertIn("same origin issue", html)

    def test_default_web_rejects_spoofed_host_header(self) -> None:
        for path in ("/", "/api/dashboard"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.get(path, headers={"Host": "evil.example"})
                self.assertEqual(caught.exception.code, 403)

    def test_remote_opt_in_preserves_same_origin_post_check(self) -> None:
        remote_app = AgentTeamWebApp(self.config, max_workers=1, allow_remote=True)
        server = remote_app.build_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        remote_url = f"http://127.0.0.1:{server.server_port}"
        try:
            remote_host = "remote.example:8765"
            body = urllib.parse.urlencode(
                {
                    "_csrf_token": remote_app.csrf_token,
                    "description": "remote issue",
                    "repo_path": "/tmp/repo",
                    "priority": "3",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                remote_url + "/issues",
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Host": remote_host,
                    "Origin": f"http://{remote_host}",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
            self.assertEqual(remote_app.store.list_issues()[-1].title, "remote issue")

            blocked = urllib.request.Request(
                remote_url + "/issues",
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Host": remote_host,
                    "Origin": "http://evil.example",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(blocked, timeout=5)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            remote_app.shutdown()

    def test_manual_web_transitions_reject_active_locks(self) -> None:
        issue = self.store.create_issue("locked issue", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.store.transition_issue(issue.id, "planning")
        self.store.transition_issue(issue.id, "awaiting_plan_approval")
        self.assertTrue(self.store.acquire_lock(issue.id, "test-owner", 60, "run-1"))

        with self.assertRaises(urllib.error.HTTPError) as approval:
            self.post(f"/issues/{issue.id}/actions/approve-plan", {})
        self.assertEqual(approval.exception.code, 409)

        with self.assertRaises(urllib.error.HTTPError) as transition:
            self.post(f"/issues/{issue.id}/actions/transition", {"next_phase": "blocked"})
        self.assertEqual(transition.exception.code, 409)

        with self.assertRaises(urllib.error.HTTPError) as reject:
            self.post(f"/issues/{issue.id}/actions/reject-plan", {"feedback": "Use a smaller scope."})
        self.assertEqual(reject.exception.code, 409)

    def test_blocked_retry_transition_rejects_active_lock(self) -> None:
        issue = self.store.create_issue("locked blocked retry issue", "desc", ready=True)
        self._block_with_run(issue.id, run_id="run-blocked-retry-lock", summary="Blocked before retry")
        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        retry_control = next(control for control in payload["manager_controls"] if control.get("button") == "Retry research")
        retry_fields = {field["name"]: field for field in retry_control["fields"]}
        self.assertTrue(self.store.acquire_lock(issue.id, "test-owner", 60, "active-run"))

        with self.assertRaises(urllib.error.HTTPError) as transition:
            self.post(
                f"/issues/{issue.id}/actions/transition",
                {
                    "next_phase": retry_fields["next_phase"]["value"],
                    "message": retry_fields["message"]["value"],
                },
            )

        self.assertEqual(transition.exception.code, 409)

    def test_approve_merge_rejects_active_lock(self) -> None:
        issue = self.store.create_issue("locked merge issue", "desc")
        self._move_to_merge_approval(issue.id)
        self.assertTrue(self.store.acquire_lock(issue.id, "test-owner", 60, "run-1"))

        with self.assertRaises(urllib.error.HTTPError) as approval:
            self.post(f"/issues/{issue.id}/actions/approve-merge", {})
        self.assertEqual(approval.exception.code, 409)

    def test_issue_run_control_rejects_active_lock(self) -> None:
        issue = self.store.create_issue("locked run issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(issue.id, "test-owner", 60, "run-1"))

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(f"/issues/{issue.id}/actions/run", {})
        self.assertEqual(caught.exception.code, 409)

    def test_issue_run_control_queues_and_completes_dry_run(self) -> None:
        issue = self.store.create_issue("run issue", "desc", ready=True)
        self.post(f"/issues/{issue.id}/actions/run", {})

        self.assertTrue(self.app.jobs.wait_for_idle(5))
        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "ready_for_plan")
        detail = self.get(f"/issues/{issue.id}")
        self.assertIn("Dry-run research completed", detail)

    def test_run_next_control_queues_and_completes_dry_run(self) -> None:
        draft = self.store.create_issue("draft next issue", "desc")
        issue = self.store.create_issue("next issue", "desc", ready=True)
        self.post("/actions/run-next", {})

        self.assertTrue(self.app.jobs.wait_for_idle(5))
        self.assertEqual(self.store.get_issue(draft.id).phase, "draft")
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")

    def test_draft_issue_detail_has_submit_control_but_no_run_control(self) -> None:
        issue = self.store.create_issue("draft issue", "desc")

        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        html = self.get(f"/issues/{issue.id}")

        self.assertIn("submit it for research", payload["next_action"])
        actions = {control["action"] for control in payload["manager_controls"]}
        submit_control = next(
            control for control in payload["manager_controls"] if control["action"].endswith("/actions/submit-for-research")
        )
        submit_fields = {field["name"]: field for field in submit_control["fields"]}
        self.assertEqual(submit_control["button"], "Submit for research")
        self.assertEqual(submit_control["group"], "primary")
        self.assertEqual(submit_fields["message"]["type"], "hidden")
        self.assertEqual(submit_fields["message"]["value"], "Submitted draft for research")
        edit_control = next(control for control in payload["manager_controls"] if control["action"].endswith("/edit"))
        self.assertEqual(edit_control["kind"], "link")
        self.assertEqual(edit_control["button"], "Edit draft")
        self.assertIn(f"/issues/{issue.id}/actions/submit-for-research", actions)
        self.assertIn(f"/issues/{issue.id}/edit", actions)
        self.assertNotIn(f"/issues/{issue.id}/actions/run", actions)
        transition = next(control for control in payload["manager_controls"] if control["action"].endswith("/actions/transition"))
        options = transition["fields"][0]["options"]
        self.assertEqual(options, [{"value": "needs_research", "label": "Needs research (needs_research)"}])
        self.assertIn(f"/issues/{issue.id}/actions/submit-for-research", html)
        self.assertIn("Submit for research", html)
        self.assertIn(f"/issues/{issue.id}/edit", html)
        self.assertIn("Edit draft", html)
        self.assertNotIn(f"/issues/{issue.id}/actions/run", html)
        self.assertLess(html.index("Submit for research"), html.index("Advanced actions: override phase"))

    def test_web_submit_for_research_publishes_draft_snapshot_and_event(self) -> None:
        issue = self.store.create_issue("draft issue", "desc")

        html_text = self.post(
            f"/issues/{issue.id}/actions/submit-for-research",
            {"message": "Submit from draft button"},
        )

        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "needs_research")
        self.assertEqual(updated.status, "open")
        snapshot = json.loads((self.config.artifacts_dir / str(issue.id) / "issue.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["phase"], "needs_research")
        self.assertEqual(snapshot["status"], "open")
        events = self.store.list_events(issue.id)
        self.assertEqual(events[-1]["event_type"], "issue.transitioned")
        self.assertEqual(events[-1]["message"], "Submit from draft button")
        self.assertIn("Submitted draft for research", html_text)

    def test_web_submit_for_research_rejects_non_draft_or_closed_issue(self) -> None:
        ready = self.store.create_issue("ready issue", "desc", ready=True)
        closed_draft = self.store.create_issue("closed draft", "desc")
        with self.store.connect() as conn:
            conn.execute("UPDATE issues SET status = 'closed' WHERE id = ?", (closed_draft.id,))

        for issue in (ready, closed_draft):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post(f"/issues/{issue.id}/actions/submit-for-research", {})
            self.assertEqual(caught.exception.code, 400)

    def test_non_draft_issue_detail_does_not_expose_edit_control(self) -> None:
        issue = self.store.create_issue("ready issue", "desc", ready=True)

        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        html = self.get(f"/issues/{issue.id}")

        actions = {control["action"] for control in payload["manager_controls"]}
        self.assertNotIn(f"/issues/{issue.id}/edit", actions)
        self.assertNotIn(f"/issues/{issue.id}/edit", html)
        self.assertNotIn("Edit draft", html)

    def test_web_edit_draft_get_renders_prefilled_escaped_values(self) -> None:
        issue = self.store.create_issue(
            "<b>draft</b>",
            "line & <desc>",
            repo_path="/tmp/<repo>",
            priority=2,
            tags="tag<&",
        )

        html_text = self.get(f"/issues/{issue.id}/edit")

        self.assertIn("Edit draft issue", html_text)
        self.assertIn("<strong>Title:</strong> &lt;b&gt;draft&lt;/b&gt;", html_text)
        self.assertNotIn('name="title"', html_text)
        self.assertIn("line &amp; &lt;desc&gt;", html_text)
        self.assertIn("value=\"/tmp/&lt;repo&gt;\"", html_text)
        self.assertIn("value=\"2\"", html_text)
        self.assertIn("value=\"tag&lt;&amp;\"", html_text)
        self.assertNotIn("<b>draft</b>", html_text)

    def test_web_edit_draft_updates_fields_snapshot_event_and_preserves_draft(self) -> None:
        issue = self.store.create_issue("draft", "desc", repo_path="/old/repo", priority=3, tags="old")

        html_text = self.post(
            f"/issues/{issue.id}/actions/edit?repo=/old/repo",
            {
                "description": "updated <desc>",
                "repo_path": "/new/repo",
                "priority": "1",
                "tags": "new,tags",
                "ready": "1",
            },
        )

        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.title, "updated <desc>")
        self.assertEqual(updated.description, "updated <desc>")
        self.assertEqual(updated.repo_path, "/new/repo")
        self.assertEqual(updated.priority, 1)
        self.assertEqual(updated.tags, "new,tags")
        self.assertEqual(updated.phase, "draft")
        self.assertEqual(updated.status, "open")
        snapshot = json.loads((self.config.artifacts_dir / str(issue.id) / "issue.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["title"], "updated <desc>")
        self.assertEqual(snapshot["repo_path"], "/new/repo")
        events = self.store.list_events(issue.id)
        self.assertEqual([event["event_type"] for event in events], ["issue.created", "issue.edited"])
        self.assertIn("title, description, repo_path, priority, tags", events[-1]["message"])
        self.assertIn("Draft issue edited", html_text)
        self.assertIn("updated &lt;desc&gt;", html_text)
        self.assertNotIn("updated <desc>", html_text)
        self.assertIn("updated &lt;desc&gt;", html_text)

    def test_web_edit_preserves_title_when_description_is_unchanged(self) -> None:
        issue = self.store.create_issue("custom cli title", "desc", repo_path="/old/repo", priority=3, tags="old")

        self.post(
            f"/issues/{issue.id}/actions/edit",
            {
                "description": "desc",
                "repo_path": "/new/repo",
                "priority": "2",
                "tags": "new",
            },
        )

        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.title, "custom cli title")
        self.assertEqual(updated.repo_path, "/new/repo")
        self.assertEqual(updated.priority, 2)
        self.assertEqual(updated.tags, "new")
        self.assertNotIn("title", self.store.list_events(issue.id)[-1]["message"])

    def test_web_edit_draft_rejects_invalid_state_lock_job_and_validation(self) -> None:
        draft = self.store.create_issue("draft", "desc")
        with self.assertRaises(urllib.error.HTTPError) as missing_description:
            self.post(
                f"/issues/{draft.id}/actions/edit",
                {"description": "", "priority": "3"},
            )
        self.assertEqual(missing_description.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as bad_priority:
            self.post(
                f"/issues/{draft.id}/actions/edit",
                {"description": "desc", "priority": "high"},
            )
        self.assertEqual(bad_priority.exception.code, 400)

        ready = self.store.create_issue("ready", "desc", ready=True)
        with self.assertRaises(urllib.error.HTTPError) as get_non_draft:
            self.get(f"/issues/{ready.id}/edit")
        self.assertEqual(get_non_draft.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as post_non_draft:
            self.post(
                f"/issues/{ready.id}/actions/edit",
                {"description": "desc", "priority": "3"},
            )
        self.assertEqual(post_non_draft.exception.code, 400)

        locked = self.store.create_issue("locked", "desc")
        self.assertTrue(self.store.acquire_lock(locked.id, "test-owner", 60, "run-1"))
        with self.assertRaises(urllib.error.HTTPError) as lock_error:
            self.post(
                f"/issues/{locked.id}/actions/edit",
                {"description": "desc", "priority": "3"},
            )
        self.assertEqual(lock_error.exception.code, 409)

        queued = self.store.create_issue("queued", "desc")
        now = utc_now_iso()
        with self.app.jobs._lock:
            self.app.jobs._jobs["queued-edit"] = WebJob(
                id="queued-edit",
                action="Run issue",
                issue_id=queued.id,
                status="queued",
                message="Queued",
                created_at=now,
                updated_at=now,
            )
        with self.assertRaises(urllib.error.HTTPError) as job_error:
            self.post(
                f"/issues/{queued.id}/actions/edit",
                {"description": "desc", "priority": "3"},
            )
        self.assertEqual(job_error.exception.code, 409)

    def test_reset_to_draft_control_is_separate_from_generic_transitions(self) -> None:
        issue = self.store.create_issue("reset controls issue", "desc", ready=True)

        payload, _headers = self.get_json(f"/api/issues/{issue.id}")
        html = self.get(f"/issues/{issue.id}")

        actions = {control["action"] for control in payload["manager_controls"]}
        self.assertIn(f"/issues/{issue.id}/actions/reset-to-draft", actions)
        self.assertIn(f"/issues/{issue.id}/actions/stop", actions)
        stop_control = next(control for control in payload["manager_controls"] if control["action"].endswith("/actions/stop"))
        self.assertEqual(stop_control["button"], "Stop issue")
        self.assertEqual(stop_control["fields"][0]["label"], "Stop reason")
        transition = next(control for control in payload["manager_controls"] if control["action"].endswith("/actions/transition"))
        transition_options = {option["value"] for option in transition["fields"][0]["options"]}
        self.assertNotIn("draft", transition_options)
        reset_control = next(
            control for control in payload["manager_controls"] if control["action"].endswith("/actions/reset-to-draft")
        )
        self.assertEqual(reset_control["fields"][0]["placeholder"], f"RESET {issue.id}")
        self.assertIn("/actions/reset-to-draft", html)
        self.assertIn("Reset to draft (destructive)", html)
        self.assertIn("/actions/stop", html)
        self.assertIn("Stop issue", html)
        self.assertIn("Danger zone: reset or delete this issue", html)
        self.assertIn("Advanced actions: override phase", html)

    def test_stop_control_is_absent_for_draft_blocked_and_done_issues(self) -> None:
        draft = self.store.create_issue("draft no stop", "desc")
        blocked = self.store.create_issue("blocked no stop", "desc", ready=True)
        self.store.transition_issue(blocked.id, "blocked")
        done = self.store.create_issue("done no stop", "desc")
        self._close_with_merge(done.id, "run-done-no-stop", "done")

        for issue in (draft, blocked, self.store.get_issue(done.id)):
            payload, _headers = self.get_json(f"/api/issues/{issue.id}")
            html = self.get(f"/issues/{issue.id}")
            actions = {control["action"] for control in payload["manager_controls"]}
            self.assertNotIn(f"/issues/{issue.id}/actions/stop", actions)
            self.assertNotIn(f"/issues/{issue.id}/actions/stop", html)

    def test_delete_control_is_available_for_draft_and_non_draft_issues(self) -> None:
        draft = self.store.create_issue("draft delete controls issue", "desc")
        ready = self.store.create_issue("ready delete controls issue", "desc", ready=True)

        for issue in (draft, ready):
            payload, _headers = self.get_json(f"/api/issues/{issue.id}")
            html = self.get(f"/issues/{issue.id}")
            actions = {control["action"] for control in payload["manager_controls"]}
            delete_control = next(
                control for control in payload["manager_controls"] if control["action"].endswith("/actions/delete")
            )

            self.assertIn(f"/issues/{issue.id}/actions/delete", actions)
            self.assertEqual(delete_control["fields"][0]["placeholder"], f"DELETE {issue.id}")
            self.assertIn("/actions/delete", html)
            self.assertIn("Delete issue (irreversible)", html)
            self.assertIn("Danger zone: reset or delete this issue", html)

    def test_web_reset_to_draft_requires_confirmation_and_clears_state(self) -> None:
        issue = self.store.create_issue("reset issue", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.store.create_run("run-old", issue.id, "research", "dry-run")
        self.artifacts.write_phase_artifact(issue.id, "research", "run-old", "old research")
        self.artifacts.run_log_path(issue.id, "research", "run-old").write_text("old log", encoding="utf-8")

        with self.assertRaises(urllib.error.HTTPError) as bad_confirmation:
            self.post(f"/issues/{issue.id}/actions/reset-to-draft", {"confirmation": f"RESET {issue.id + 1}"})
        self.assertEqual(bad_confirmation.exception.code, 400)

        self.post(
            f"/issues/{issue.id}/actions/reset-to-draft",
            {"confirmation": f"RESET {issue.id}", "message": "restart"},
        )

        updated = self.store.get_issue(issue.id)
        self.assertEqual(updated.phase, "draft")
        self.assertEqual(updated.status, "open")
        self.assertEqual(self.store.list_runs(issue.id), [])
        self.assertEqual([event["event_type"] for event in self.store.list_events(issue.id)], ["issue.reset_to_draft"])
        self.assertEqual(self.artifacts.list_issue_artifacts(issue.id), [])
        detail = self.get(f"/issues/{issue.id}")
        self.assertIn("Draft backlog", detail)
        self.assertIn("No runs yet", detail)
        self.assertIn("No artifacts or logs yet", detail)

        self.post(
            f"/issues/{issue.id}/actions/transition",
            {"next_phase": "needs_research", "message": "publish after reset"},
        )
        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")

    def test_web_reset_rejects_active_lock_and_active_job(self) -> None:
        locked = self.store.create_issue("locked reset issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(locked.id, "test-owner", 60, "run-1"))

        with self.assertRaises(urllib.error.HTTPError) as lock_error:
            self.post(f"/issues/{locked.id}/actions/reset-to-draft", {"confirmation": f"RESET {locked.id}"})
        self.assertEqual(lock_error.exception.code, 409)

        queued = self.store.create_issue("queued reset issue", "desc", ready=True)
        now = utc_now_iso()
        with self.app.jobs._lock:
            self.app.jobs._jobs["queued-reset"] = WebJob(
                id="queued-reset",
                action="Run issue",
                issue_id=queued.id,
                status="queued",
                message="Queued",
                created_at=now,
                updated_at=now,
            )

        with self.assertRaises(urllib.error.HTTPError) as job_error:
            self.post(f"/issues/{queued.id}/actions/reset-to-draft", {"confirmation": f"RESET {queued.id}"})
        self.assertEqual(job_error.exception.code, 409)

    def test_web_delete_requires_confirmation_removes_state_and_404s_deleted_issue(self) -> None:
        issue = self.store.create_issue("delete issue", "desc", ready=True)
        self.store.transition_issue(issue.id, "researching")
        self.store.transition_issue(issue.id, "ready_for_plan")
        self.store.create_run("run-old", issue.id, "research", "dry-run")
        self.artifacts.write_phase_artifact(issue.id, "research", "run-old", "old research")
        self.artifacts.run_log_path(issue.id, "research", "run-old").write_text("old log", encoding="utf-8")
        now = utc_now_iso()
        with self.app.jobs._lock:
            self.app.jobs._jobs["completed-delete"] = WebJob(
                id="completed-delete",
                action="Run issue",
                issue_id=issue.id,
                status="succeeded",
                message="Completed",
                created_at=now,
                updated_at=now,
            )

        with self.assertRaises(urllib.error.HTTPError) as bad_confirmation:
            self.post(f"/issues/{issue.id}/actions/delete", {"confirmation": f"DELETE {issue.id + 1}"})
        self.assertEqual(bad_confirmation.exception.code, 400)
        self.assertEqual(self.store.get_issue(issue.id).phase, "ready_for_plan")
        self.assertTrue((self.config.artifacts_dir / str(issue.id)).is_dir())

        html = self.post(
            f"/issues/{issue.id}/actions/delete",
            {"confirmation": f"DELETE {issue.id}", "message": "cleanup"},
        )

        self.assertIn(f"Deleted issue {issue.id}", html)
        with self.assertRaisesRegex(KeyError, "Issue not found"):
            self.store.get_issue(issue.id)
        self.assertEqual(self.store.list_runs(issue.id), [])
        self.assertEqual(self.store.list_events(issue.id), [])
        self.assertFalse((self.config.artifacts_dir / str(issue.id)).exists())
        self.assertEqual(self.app.jobs.list_jobs(issue.id), [])
        for path in (
            f"/issues/{issue.id}",
            f"/api/issues/{issue.id}",
            f"/artifacts/{issue.id}/research.md",
        ):
            with self.assertRaises(urllib.error.HTTPError) as missing:
                self.get(path)
            self.assertEqual(missing.exception.code, 404)

    def test_web_delete_rejects_active_lock_and_active_job(self) -> None:
        locked = self.store.create_issue("locked delete issue", "desc", ready=True)
        self.assertTrue(self.store.acquire_lock(locked.id, "test-owner", 60, "run-1"))

        with self.assertRaises(urllib.error.HTTPError) as lock_error:
            self.post(f"/issues/{locked.id}/actions/delete", {"confirmation": f"DELETE {locked.id}"})
        self.assertEqual(lock_error.exception.code, 409)

        queued = self.store.create_issue("queued delete issue", "desc", ready=True)
        now = utc_now_iso()
        with self.app.jobs._lock:
            self.app.jobs._jobs["queued-delete"] = WebJob(
                id="queued-delete",
                action="Run issue",
                issue_id=queued.id,
                status="queued",
                message="Queued",
                created_at=now,
                updated_at=now,
            )

        with self.assertRaises(urllib.error.HTTPError) as job_error:
            self.post(f"/issues/{queued.id}/actions/delete", {"confirmation": f"DELETE {queued.id}"})
        self.assertEqual(job_error.exception.code, 409)

    def test_web_transition_publishes_draft(self) -> None:
        issue = self.store.create_issue("draft issue", "desc")

        self.post(
            f"/issues/{issue.id}/actions/transition",
            {"next_phase": "needs_research", "message": "publish draft"},
        )

        self.assertEqual(self.store.get_issue(issue.id).phase, "needs_research")

    def test_cli_web_help_includes_host_and_port(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(output):
            build_parser().parse_args(["web", "--help"])
        self.assertEqual(caught.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("--host", help_text)
        self.assertIn("--port", help_text)
        self.assertIn("--web-workers", help_text)
        self.assertIn("--workers", help_text)
        self.assertIn("--unsafe-allow-remote", help_text)

    def test_cli_web_workers_alias_is_preserved(self) -> None:
        args = build_parser().parse_args(["web", "--workers", "2"])
        self.assertEqual(args.web_workers, 2)
        args = build_parser().parse_args(["web", "--web-workers", "3"])
        self.assertEqual(args.web_workers, 3)

    def test_cli_top_level_config_argument_is_parsed_before_subcommand(self) -> None:
        args = build_parser().parse_args(["--config", "agent-team.config.jsonc", "init"])
        self.assertEqual(args.config, "agent-team.config.jsonc")
        self.assertEqual(args.command, "init")

    def test_cli_web_uses_config_defaults_when_flags_are_omitted(self) -> None:
        config_path = self.write_cli_config(
            {
                "home": str(self.home / "cli-web-state"),
                "runner": "dry-run",
                "web": {
                    "host": "127.0.0.2",
                    "port": 9001,
                    "web_workers": 3,
                    "unsafe_allow_remote": True,
                },
            }
        )

        with mock.patch.object(cli_module, "serve_web", return_value=0) as serve:
            exit_code = cli_module.main(["--config", str(config_path), "web"])

        self.assertEqual(exit_code, 0)
        called_config, host, port, workers, unsafe = serve.call_args.args
        self.assertEqual(called_config.web_host, "127.0.0.2")
        self.assertEqual(host, "127.0.0.2")
        self.assertEqual(port, 9001)
        self.assertEqual(workers, 3)
        self.assertTrue(unsafe)

    def test_cli_web_flags_override_config_defaults(self) -> None:
        config_path = self.write_cli_config(
            {
                "home": str(self.home / "cli-web-override-state"),
                "runner": "dry-run",
                "web": {
                    "host": "127.0.0.2",
                    "port": 9001,
                    "web_workers": 3,
                    "unsafe_allow_remote": True,
                },
            }
        )

        with mock.patch.object(cli_module, "serve_web", return_value=0) as serve:
            exit_code = cli_module.main(
                [
                    "--config",
                    str(config_path),
                    "web",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--web-workers",
                    "2",
                    "--no-unsafe-allow-remote",
                ]
            )

        self.assertEqual(exit_code, 0)
        _, host, port, workers, unsafe = serve.call_args.args
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, 0)
        self.assertEqual(workers, 2)
        self.assertFalse(unsafe)

    def test_cli_serve_help_includes_web_and_worker_options(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stdout(output):
            build_parser().parse_args(["serve", "--help"])
        self.assertEqual(caught.exception.code, 0)
        help_text = output.getvalue()
        for option in (
            "--host",
            "--port",
            "--web-workers",
            "--worker-concurrency",
            "--interval",
            "--unsafe-allow-remote",
        ):
            self.assertIn(option, help_text)

    def test_cli_serve_uses_config_defaults_and_cli_overrides(self) -> None:
        config_path = self.write_cli_config(
            {
                "home": str(self.home / "cli-serve-state"),
                "runner": "dry-run",
                "web": {
                    "host": "127.0.0.2",
                    "port": 9002,
                    "web_workers": 3,
                    "unsafe_allow_remote": False,
                },
                "worker": {
                    "worker_concurrency": 4,
                    "worker_interval_seconds": 15,
                },
            }
        )

        with mock.patch.object(cli_module, "serve_web_and_worker", return_value=0) as serve:
            exit_code = cli_module.main(
                [
                    "--config",
                    str(config_path),
                    "serve",
                    "--port",
                    "0",
                    "--worker-concurrency",
                    "2",
                    "--unsafe-allow-remote",
                ]
            )

        self.assertEqual(exit_code, 0)
        called_config = serve.call_args.args[0]
        self.assertEqual(called_config.worker_concurrency, 4)
        self.assertEqual(serve.call_args.kwargs["host"], "127.0.0.2")
        self.assertEqual(serve.call_args.kwargs["port"], 0)
        self.assertEqual(serve.call_args.kwargs["web_workers"], 3)
        self.assertEqual(serve.call_args.kwargs["worker_concurrency"], 2)
        self.assertEqual(serve.call_args.kwargs["interval_seconds"], 15)
        self.assertTrue(serve.call_args.kwargs["unsafe_allow_remote"])

    def test_web_rejects_non_loopback_bind_without_explicit_unsafe_opt_in(self) -> None:
        with self.assertRaises(ValueError):
            serve_web(self.config, host="0.0.0.0", port=0, workers=1)

    def test_serve_rejects_non_loopback_bind_without_explicit_unsafe_opt_in(self) -> None:
        with self.assertRaises(ValueError):
            serve_web_and_worker(
                self.config,
                host="0.0.0.0",
                port=0,
                web_workers=1,
                worker_concurrency=1,
                interval_seconds=1,
                install_signal_handlers=False,
            )

    def test_serve_starts_web_and_autonomous_worker_until_stopped(self) -> None:
        issue = self.store.create_issue("served issue", "desc", ready=True)
        started: queue.Queue[int] = queue.Queue()
        stop_event = threading.Event()
        errors: list[BaseException] = []

        def run_service() -> None:
            try:
                serve_web_and_worker(
                    self.config,
                    host="127.0.0.1",
                    port=0,
                    web_workers=2,
                    worker_concurrency=1,
                    interval_seconds=60,
                    stop_event=stop_event,
                    on_started=lambda server: started.put(server.server_port),
                    install_signal_handlers=False,
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_service)
        thread.start()
        try:
            port = started.get(timeout=5)
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 5
            payload: dict[str, object] | None = None
            while time.monotonic() < deadline:
                request = urllib.request.Request(base_url + "/api/dashboard")
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if self.store.get_issue(issue.id).phase == "awaiting_plan_approval":
                    break
                time.sleep(0.05)

            self.assertIsNotNone(payload)
            self.assertEqual(self.store.get_issue(issue.id).phase, "awaiting_plan_approval")
            runtime = payload["runtime"]
            self.assertEqual(runtime["mode"], "serve")
            self.assertEqual(runtime["web_workers"], 2)
            self.assertEqual(runtime["worker_concurrency"], 1)
            self.assertEqual(runtime["worker_interval_seconds"], 60)
            with urllib.request.urlopen(base_url + "/", timeout=5) as response:
                html = response.read().decode("utf-8")
            self.assertIn("Mode: serve - autonomous workers 1 - poll interval 60s - queued browser actions 2", html)
        finally:
            stop_event.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

    def test_serve_shutdown_waits_for_worker_drain(self) -> None:
        started: queue.Queue[int] = queue.Queue()
        stop_event = threading.Event()
        worker_started = threading.Event()
        worker_draining = threading.Event()
        release_worker = threading.Event()
        service_finished = threading.Event()
        return_codes: list[int] = []
        errors: list[BaseException] = []

        def fake_run_worker_loop(
            store: object,
            artifacts: object,
            config: AppConfig,
            interval_seconds: int,
            concurrency: int,
            stop_event: threading.Event,
            on_result: object = None,
        ) -> None:
            worker_started.set()
            if not stop_event.wait(5):
                raise AssertionError("worker was not asked to stop")
            worker_draining.set()
            if not release_worker.wait(5):
                raise AssertionError("worker was not released")

        def run_service() -> None:
            try:
                return_codes.append(
                    serve_web_and_worker(
                        self.config,
                        host="127.0.0.1",
                        port=0,
                        web_workers=1,
                        worker_concurrency=1,
                        interval_seconds=60,
                        stop_event=stop_event,
                        on_started=lambda server: started.put(server.server_port),
                        install_signal_handlers=False,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                service_finished.set()

        with mock.patch.object(web_module, "run_worker_loop", fake_run_worker_loop):
            with mock.patch.object(web_module, "WORKER_THREAD_JOIN_SECONDS", 0.01):
                thread = threading.Thread(target=run_service)
                thread.start()
                try:
                    started.get(timeout=5)
                    self.assertTrue(worker_started.wait(5))
                    stop_event.set()
                    self.assertTrue(worker_draining.wait(5))
                    self.assertFalse(service_finished.wait(1))
                    release_worker.set()
                    thread.join(timeout=5)
                finally:
                    release_worker.set()
                    thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        self.assertEqual(return_codes, [0])

    def test_serve_shutdown_surfaces_late_worker_errors(self) -> None:
        started: queue.Queue[int] = queue.Queue()
        stop_event = threading.Event()
        worker_started = threading.Event()
        worker_draining = threading.Event()
        fail_worker = threading.Event()
        service_finished = threading.Event()
        return_codes: list[int] = []
        errors: list[BaseException] = []

        def fake_run_worker_loop(
            store: object,
            artifacts: object,
            config: AppConfig,
            interval_seconds: int,
            concurrency: int,
            stop_event: threading.Event,
            on_result: object = None,
        ) -> None:
            worker_started.set()
            if not stop_event.wait(5):
                raise AssertionError("worker was not asked to stop")
            worker_draining.set()
            if not fail_worker.wait(5):
                raise AssertionError("worker failure was not triggered")
            raise RuntimeError("worker failed while draining")

        def run_service() -> None:
            try:
                return_codes.append(
                    serve_web_and_worker(
                        self.config,
                        host="127.0.0.1",
                        port=0,
                        web_workers=1,
                        worker_concurrency=1,
                        interval_seconds=60,
                        stop_event=stop_event,
                        on_started=lambda server: started.put(server.server_port),
                        install_signal_handlers=False,
                    )
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                service_finished.set()

        with mock.patch.object(web_module, "run_worker_loop", fake_run_worker_loop):
            with mock.patch.object(web_module, "WORKER_THREAD_JOIN_SECONDS", 0.01):
                thread = threading.Thread(target=run_service)
                thread.start()
                try:
                    started.get(timeout=5)
                    self.assertTrue(worker_started.wait(5))
                    stop_event.set()
                    self.assertTrue(worker_draining.wait(5))
                    self.assertFalse(service_finished.wait(1))
                    fail_worker.set()
                    thread.join(timeout=5)
                finally:
                    fail_worker.set()
                    thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(return_codes, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(str(errors[0]), "worker failed while draining")

    def _move_to_plan_approval(self, issue_id: int) -> None:
        if self.store.get_issue(issue_id).phase == "draft":
            self.store.transition_issue(issue_id, "needs_research")
        self.store.transition_issue(issue_id, "researching")
        self.store.transition_issue(issue_id, "ready_for_plan")
        self.store.transition_issue(issue_id, "planning")
        self.store.transition_issue(issue_id, "awaiting_plan_approval")

    def _move_to_merge_approval(self, issue_id: int) -> None:
        self._move_to_plan_approval(issue_id)
        self.store.transition_issue(issue_id, "ready_for_implementation")
        self.store.transition_issue(issue_id, "implementing")
        self.store.transition_issue(issue_id, "ready_for_validation")
        self.store.transition_issue(issue_id, "validating")
        self.store.transition_issue(issue_id, "ready_for_review")
        self.store.transition_issue(issue_id, "reviewing")
        self.store.transition_issue(issue_id, "awaiting_merge_approval")

    def _move_to_human_input(self, issue_id: int) -> None:
        if self.store.get_issue(issue_id).phase == "draft":
            self.store.transition_issue(issue_id, "needs_research")
        self.assertTrue(self.store.acquire_lock(issue_id, "worker", 60, "run-human"))
        self.store.transition_issue(issue_id, "researching", "run-human")
        self.store.create_run("run-human", issue_id, "research", "dry-run")
        request = self.store.complete_run_and_request_human_input(
            "run-human",
            issue_id,
            "needs human input",
            None,
            HumanInputRequestDraft(
                requested_by_phase="research",
                resume_phase="needs_research",
                question="Which & option <now>?",
                rationale="The decision affects correctness.",
                requested_decision="Choose the safe option.",
                options=("A", "B"),
                context="User controlled <context>",
            ),
        )
        self.store.release_lock(issue_id, "worker", "run-human")
        self.artifacts.append_human_input_request(request)
        self.artifacts.write_human_input_summary(issue_id, self.store.list_human_input_requests(issue_id))

    def _close_with_merge(self, issue_id: int, run_id: str, summary: str) -> None:
        self._move_to_merge_approval(issue_id)
        self.store.transition_issue(issue_id, "ready_for_merge")
        self.assertTrue(self.store.acquire_lock(issue_id, "worker", 60, run_id))
        self.store.transition_issue(issue_id, "merging", run_id)
        self.store.create_run(run_id, issue_id, "merge", "dry-run")
        self.store.complete_run(run_id, issue_id, "success", summary, None, next_phase="done")
        self.store.transition_issue(issue_id, "done", run_id)
        self.store.release_lock(issue_id, "worker", run_id)

    def _expire_lock(self, issue_id: int) -> None:
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE issues SET lock_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", issue_id),
            )

    def _block_with_run(
        self,
        issue_id: int,
        run_id: str,
        summary: str,
        error: str | None = None,
        blocked_summary: str | None = None,
    ) -> None:
        self.assertTrue(self.store.acquire_lock(issue_id, "worker", 60, run_id))
        self.store.transition_issue(issue_id, "researching", run_id, "Starting research")
        self.store.create_run(run_id, issue_id, "research", "dry-run")
        artifact_path = self.artifacts.write_phase_artifact(issue_id, "research", run_id, "Blocked artifact details")
        self.artifacts.run_log_path(issue_id, "research", run_id).write_text("Blocked run log", encoding="utf-8")
        self.store.complete_run(run_id, issue_id, "blocked", summary, str(artifact_path), error)
        self.store.transition_issue(issue_id, "blocked", run_id, summary, blocked_summary=blocked_summary)
        self.store.release_lock(issue_id, "worker", run_id)


if __name__ == "__main__":
    unittest.main()
