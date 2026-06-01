import unittest
import tempfile
from pathlib import Path

from agent_team.models import Issue
from agent_team.runners.copilot_cli import PHASE_AGENTS, PHASE_PERMISSION_POLICIES, CopilotCliRunner


class CopilotCliRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issue = Issue(
            id=1,
            title="title",
            description="desc",
            source="local",
            external_id=None,
            repo_path="/tmp/repo",
            phase="needs_research",
            status="open",
            priority=3,
            tags=None,
            lock_owner=None,
            lock_expires_at=None,
            current_run_id=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

    def test_research_uses_plain_custom_agent_prompt(self) -> None:
        prompt = CopilotCliRunner._build_prompt("research", self.issue, {"prompt_template": "Body {title}"})
        self.assertEqual(prompt, "Body title")

    def test_plan_uses_plain_custom_agent_prompt(self) -> None:
        prompt = CopilotCliRunner._build_prompt("plan", self.issue, {"prompt_template": "Body {title}"})
        self.assertEqual(prompt, "Body title")

    def test_implementation_uses_plain_custom_agent_prompt(self) -> None:
        prompt = CopilotCliRunner._build_prompt("implementation", self.issue, {"prompt_template": "Body {title}"})
        self.assertEqual(prompt, "Body title")

    def test_review_uses_plain_custom_agent_prompt(self) -> None:
        prompt = CopilotCliRunner._build_prompt("review", self.issue, {"prompt_template": "Body {title}"})
        self.assertEqual(prompt, "Body title")

    def test_validation_uses_plain_prompt(self) -> None:
        prompt = CopilotCliRunner._build_prompt("validation", self.issue, {"prompt_template": "Body {title}"})
        self.assertEqual(prompt, "Body title")

    def test_merge_conflict_resolution_uses_plain_custom_agent_prompt(self) -> None:
        prompt = CopilotCliRunner._build_prompt(
            "merge_conflict_resolution",
            self.issue,
            {"prompt_template": "Body {title}"},
        )
        self.assertEqual(prompt, "Body title")

    def test_packaged_prompts_render_merge_conflict_resolution_artifact(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "src" / "agent_team" / "prompts"
        context = {
            "artifacts_dir": "/tmp/artifacts",
            "workspace_repo_path": "/tmp/workspace",
            "workspace_root": "/tmp/workspace",
            "source_repo_path": "/tmp/repo",
        }

        for phase in ("validation", "review"):
            with self.subTest(phase=phase):
                template = (prompts_dir / f"{phase}.md").read_text(encoding="utf-8")
                prompt = CopilotCliRunner._build_prompt(
                    phase,
                    self.issue,
                    {**context, "prompt_template": template},
                )

                self.assertIn("/tmp/artifacts/merge_conflict_resolution.md", prompt)

    def test_packaged_prompts_render_unblock_context_artifact_and_content(self) -> None:
        prompts_dir = Path(__file__).resolve().parents[1] / "src" / "agent_team" / "prompts"
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            (artifacts_dir / "unblock_context.md").write_text(
                "Use the cached path to unblock the run.",
                encoding="utf-8",
            )
            context = {
                "artifacts_dir": str(artifacts_dir),
                "workspace_repo_path": "/tmp/workspace",
                "workspace_root": "/tmp/workspace",
                "source_repo_path": "/tmp/repo",
            }

            for phase in ("research", "plan", "implementation", "validation", "review", "merge_conflict_resolution"):
                with self.subTest(phase=phase):
                    template = (prompts_dir / f"{phase}.md").read_text(encoding="utf-8")
                    prompt = CopilotCliRunner._build_prompt(
                        phase,
                        self.issue,
                        {**context, "prompt_template": template},
                    )

                    self.assertIn(str(artifacts_dir / "unblock_context.md"), prompt)
                    self.assertIn("Use the cached path to unblock the run.", prompt)

    def test_prompt_can_render_workspace_paths(self) -> None:
        prompt = CopilotCliRunner._build_prompt(
            "implementation",
            self.issue,
            {
                "prompt_template": (
                    "Target {repo_path} Source {source_repo_path} "
                    "Workspace {workspace_repo_path} Root {workspace_root}"
                ),
                "source_repo_path": "/tmp/source",
                "workspace_repo_path": "/tmp/worktrees/issue-1/subdir",
                "workspace_root": "/tmp/worktrees/issue-1",
            },
        )
        self.assertIn("Source /tmp/source", prompt)
        self.assertIn("Workspace /tmp/worktrees/issue-1/subdir", prompt)
        self.assertIn("Root /tmp/worktrees/issue-1", prompt)

    def test_extra_args_are_stored(self) -> None:
        runner = CopilotCliRunner(extra_args=("--allow-tool=write",))
        self.assertEqual(runner.extra_args, ("--allow-tool=write",))

    def test_invalid_permission_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "permission_mode"):
            CopilotCliRunner(permission_mode="open")

    def test_blocked_recommendation_blocks_next_phase(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "research",
            "**Recommendation**: `blocked` until the repository is accessible.",
        )
        self.assertEqual(recommended, "blocked")

    def test_bold_colon_recommendation_is_accepted(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "research",
            "**Recommendation:** `ready_for_plan`. The target repo is accessible.",
        )
        self.assertEqual(recommended, "ready_for_plan")

    def test_heading_recommendation_uses_following_value(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "research",
            "## 6. Recommendation\n\n`ready_for_plan`",
        )
        self.assertEqual(recommended, "ready_for_plan")

    def test_plan_ready_recommendation_still_requires_approval(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "plan",
            "7. Recommendation: `ready_for_implementation`",
        )
        self.assertEqual(recommended, "awaiting_plan_approval")

    def test_agent_authored_plan_requeue_recommendation_is_not_accepted(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "plan",
            "7. Recommendation: `ready_for_plan`",
        )
        self.assertIsNone(recommended)

    def test_review_done_recommendation_routes_to_merge_approval(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "review",
            "6. Recommendation: `done`",
        )
        self.assertEqual(recommended, "awaiting_merge_approval")

    def test_review_merge_approval_recommendation_is_accepted(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "review",
            "6. Recommendation: `awaiting_merge_approval`",
        )
        self.assertEqual(recommended, "awaiting_merge_approval")

    def test_review_ready_for_implementation_recommendation_requeues_implementation(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "review",
            "6. Recommendation: `ready_for_implementation`",
        )
        self.assertEqual(recommended, "ready_for_implementation")

    def test_merge_conflict_resolution_recommends_validation(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase(
            "merge_conflict_resolution",
            "5. Recommendation: `ready_for_validation`",
        )
        self.assertEqual(recommended, "ready_for_validation")

    def test_allowed_phases_can_recommend_human_input(self) -> None:
        for phase in PHASE_AGENTS:
            with self.subTest(phase=phase):
                recommended = CopilotCliRunner._recommended_next_phase(
                    phase,
                    "Recommendation: `awaiting_human_input`",
                )
                self.assertEqual(recommended, "awaiting_human_input")

    def test_human_input_request_parser_validates_structured_section(self) -> None:
        artifact = """
## Human input request

- Requested by phase: `implementation`
- Resume phase: `ready_for_implementation`
- Question: Should the migration rewrite existing state?
- Rationale: Data loss risk is material.
- Requested decision: Approve or reject destructive rewrite.
- Options:
  - Rewrite existing state
  - Preserve existing state
- Context: Existing deployments contain customer data.

Recommendation: `awaiting_human_input`
"""
        request = CopilotCliRunner._human_input_request_from_artifact("implementation", artifact)

        self.assertEqual(request.requested_by_phase, "implementation")
        self.assertEqual(request.resume_phase, "ready_for_implementation")
        self.assertIn("migration", request.question)
        self.assertEqual(request.requested_decision, "Approve or reject destructive rewrite.")
        self.assertEqual(request.options, ("Rewrite existing state", "Preserve existing state"))
        self.assertEqual(request.context, "Existing deployments contain customer data.")
        self.assertNotIn("Recommendation", request.requested_decision)
        self.assertTrue(all("Recommendation" not in option for option in request.options))
        self.assertNotIn("Recommendation", request.context)

    def test_human_input_request_parser_stops_at_recommendation_after_options(self) -> None:
        artifact = """
## Human input request

- Requested by phase: `validation`
- Resume phase: `ready_for_validation`
- Question: Which compatibility target should validation use?
- Rationale: The answer changes the validation matrix.
- Requested decision: Select one compatibility target.
- Options:
  - Current production
  - Next preview

Recommendation: `awaiting_human_input`
"""
        request = CopilotCliRunner._human_input_request_from_artifact("validation", artifact)

        self.assertEqual(request.requested_decision, "Select one compatibility target.")
        self.assertEqual(request.options, ("Current production", "Next preview"))
        self.assertIsNone(request.context)

    def test_human_input_request_parser_stops_at_recommendation_without_optional_fields(self) -> None:
        artifact = """
## Human input request

- Requested by phase: `review`
- Resume phase: `ready_for_review`
- Question: Is the altered merge intent acceptable?
- Rationale: The change affects the reviewed behavior.
- Requested decision: Approve or reject the altered merge intent.

Recommendation: `awaiting_human_input`
"""
        request = CopilotCliRunner._human_input_request_from_artifact("review", artifact)

        self.assertEqual(request.requested_decision, "Approve or reject the altered merge intent.")
        self.assertEqual(request.options, ())
        self.assertIsNone(request.context)

    def test_human_input_request_parser_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing required"):
            CopilotCliRunner._human_input_request_from_artifact(
                "plan",
                "## Human input request\n\n- Requested by phase: `plan`\n\nRecommendation: `awaiting_human_input`",
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            CopilotCliRunner._human_input_request_from_artifact(
                "plan",
                """
## Human input request
- Requested by phase: `implementation`
- Resume phase: `ready_for_implementation`
- Question: Q
- Rationale: R
- Requested decision: D
""",
            )
        with self.assertRaisesRegex(ValueError, "Invalid human-input resume phase"):
            CopilotCliRunner._human_input_request_from_artifact(
                "plan",
                """
## Human input request
- Requested by phase: `plan`
- Resume phase: `ready_for_review`
- Question: Q
- Rationale: R
- Requested decision: D
""",
            )

    def test_plan_prompt_includes_rejection_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            (artifacts_dir / "plan_feedback.md").write_text("Use a smaller scope.", encoding="utf-8")
            template = (Path(__file__).parent.parent / "src" / "agent_team" / "prompts" / "plan.md").read_text(
                encoding="utf-8"
            )

            prompt = CopilotCliRunner._build_prompt(
                "plan",
                self.issue,
                {
                    "prompt_template": template,
                    "artifacts_dir": str(artifacts_dir),
                    "phase_artifact": str(artifacts_dir / "plan.md"),
                },
            )

        self.assertIn(str(artifacts_dir / "plan_feedback.md"), prompt)
        self.assertIn(str(artifacts_dir / "plan_prior.md"), prompt)
        self.assertIn("Use a smaller scope.", prompt)
        self.assertIn("1. Executive Summary", prompt)
        self.assertIn("what will change and why", prompt)

    def test_plan_prompt_and_agent_require_final_recommendation(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        prompt_text = (repo_root / "src" / "agent_team" / "prompts" / "plan.md").read_text(encoding="utf-8")
        agent_text = (
            repo_root / "src" / "agent_team" / "copilot_plugin" / "agents" / "agent-team-plan.agent.md"
        ).read_text(encoding="utf-8")

        for text in (prompt_text, agent_text):
            with self.subTest(text=text[:40]):
                lowered = text.lower()
                self.assertIn("all seven required sections", lowered)
                self.assertIn("exactly one", lowered)
                self.assertIn("recommendation:", lowered)
                self.assertIn("ready_for_implementation", text)
                self.assertIn("awaiting_human_input", text)
                self.assertIn("blocked", text)
                self.assertIn("will block", lowered)
                self.assertNotIn("awaiting_plan_approval", text)

    def test_prompt_includes_human_input_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            (artifacts_dir / "human_input.md").write_text("Answer: use option B", encoding="utf-8")
            template = "Human {human_input_artifact} {human_input_jsonl_artifact}\n{human_input_context}"

            prompt = CopilotCliRunner._build_prompt(
                "implementation",
                self.issue,
                {
                    "prompt_template": template,
                    "artifacts_dir": str(artifacts_dir),
                    "phase_artifact": str(artifacts_dir / "implementation.md"),
                },
            )

        self.assertIn(str(artifacts_dir / "human_input.md"), prompt)
        self.assertIn(str(artifacts_dir / "human_input.jsonl"), prompt)
        self.assertIn("Answer: use option B", prompt)

    def test_prompt_includes_unblock_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            (artifacts_dir / "unblock_context.md").write_text("Try the cached path first", encoding="utf-8")
            template = "Unblock {unblock_context_artifact}\n{unblock_context}"

            prompt = CopilotCliRunner._build_prompt(
                "implementation",
                self.issue,
                {
                    "prompt_template": template,
                    "artifacts_dir": str(artifacts_dir),
                    "phase_artifact": str(artifacts_dir / "implementation.md"),
                },
            )

        self.assertIn(str(artifacts_dir / "unblock_context.md"), prompt)
        self.assertIn("Try the cached path first", prompt)

    def test_implementation_prompt_includes_prior_review_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            (artifacts_dir / "review.md").write_text(
                "<!-- run_id: abc; updated_at: now -->\n\n"
                "2. Critical findings\nFix the retry loop.\n\n"
                "6. Recommendation: `ready_for_implementation`\n",
                encoding="utf-8",
            )
            template = (
                Path(__file__).parent.parent / "src" / "agent_team" / "prompts" / "implementation.md"
            ).read_text(encoding="utf-8")

            prompt = CopilotCliRunner._build_prompt(
                "implementation",
                self.issue,
                {
                    "prompt_template": template,
                    "artifacts_dir": str(artifacts_dir),
                    "phase_artifact": str(artifacts_dir / "implementation.md"),
                },
            )

        self.assertIn(str(artifacts_dir / "review.md"), prompt)
        self.assertIn("Fix the retry loop.", prompt)
        self.assertIn("address every review finding", prompt)

    def test_validation_and_review_prompts_include_optional_merge_conflict_context(self) -> None:
        prompts_dir = Path(__file__).parent.parent / "src" / "agent_team" / "prompts"
        with tempfile.TemporaryDirectory() as tmp:
            artifacts_dir = Path(tmp)
            for phase in ("validation", "review"):
                with self.subTest(phase=phase):
                    template = (prompts_dir / f"{phase}.md").read_text(encoding="utf-8")
                    prompt = CopilotCliRunner._build_prompt(
                        phase,
                        self.issue,
                        {
                            "prompt_template": template,
                            "artifacts_dir": str(artifacts_dir),
                            "phase_artifact": str(artifacts_dir / f"{phase}.md"),
                        },
                    )

                    self.assertIn(str(artifacts_dir / "merge.md"), prompt)
                    self.assertIn(str(artifacts_dir / "merge_conflict_resolution.md"), prompt)
                    self.assertIn("when present", prompt)

    def test_artifact_markdown_prefers_phase_artifact(self) -> None:
        with self.subTest("phase artifact"):
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                artifact = Path(tmp) / "plan.md"
                artifact.write_text("<!-- run_id: abc; updated_at: now -->\n\n# Clean plan\n", encoding="utf-8")
                markdown = CopilotCliRunner._artifact_markdown(
                    "I’ll do work.\n\n# noisy transcript",
                    {"phase_artifact": str(artifact)},
                )
            self.assertEqual(markdown, "# Clean plan")

    def test_artifact_markdown_extracts_final_numbered_section(self) -> None:
        markdown = CopilotCliRunner._artifact_markdown(
            "I’ll inspect files.\n\n● Read file\n\n1. Executive Summary\nWhat changes and why.\n",
            {},
        )
        self.assertEqual(markdown, "1. Executive Summary\nWhat changes and why.")

    def test_missing_recommendation_is_not_accepted(self) -> None:
        recommended = CopilotCliRunner._recommended_next_phase("research", "Looks good.")
        self.assertIsNone(recommended)

    def test_missing_recommendation_diagnostic_lists_allowed_values(self) -> None:
        diagnostic = CopilotCliRunner._recommendation_diagnostic("research", "Looks good.")
        message = CopilotCliRunner._recommendation_error_message("research", diagnostic)

        self.assertEqual(diagnostic.reason, "missing")
        self.assertIsNone(diagnostic.next_phase)
        self.assertIsNone(diagnostic.detected_value)
        self.assertEqual(diagnostic.allowed_values, ("awaiting_human_input", "blocked", "ready_for_plan"))
        self.assertEqual(
            message,
            "Copilot CLI research did not provide a valid Recommendation; "
            "expected one of: awaiting_human_input, blocked, ready_for_plan",
        )

    def test_invalid_recommendation_diagnostic_reports_detected_value(self) -> None:
        diagnostic = CopilotCliRunner._recommendation_diagnostic(
            "implementation",
            "6. Recommendation: `ready_for_implementation`.",
        )
        message = CopilotCliRunner._recommendation_error_message("implementation", diagnostic)

        self.assertEqual(diagnostic.reason, "invalid")
        self.assertIsNone(diagnostic.next_phase)
        self.assertEqual(diagnostic.detected_value, "ready_for_implementation")
        self.assertEqual(diagnostic.allowed_values, ("awaiting_human_input", "blocked", "ready_for_validation"))
        self.assertEqual(
            message,
            "Copilot CLI implementation provided invalid Recommendation 'ready_for_implementation'; "
            "expected one of: awaiting_human_input, blocked, ready_for_validation",
        )

    def test_heading_invalid_recommendation_reports_following_value(self) -> None:
        diagnostic = CopilotCliRunner._recommendation_diagnostic(
            "implementation",
            "## 6. Recommendation\n\n`ready_for_implementation`,",
        )

        self.assertEqual(diagnostic.reason, "invalid")
        self.assertEqual(diagnostic.detected_value, "ready_for_implementation")

    def test_prose_recommendation_does_not_create_invalid_token(self) -> None:
        diagnostic = CopilotCliRunner._recommendation_diagnostic(
            "implementation",
            "My recommendation is ready_for_implementation after more testing.",
        )

        self.assertEqual(diagnostic.reason, "missing")
        self.assertIsNone(diagnostic.detected_value)

    def test_command_allows_repo_directory(self) -> None:
        runner = CopilotCliRunner(extra_args=("--allow-tool=read",))
        command = runner._build_command("research", "prompt", Path("/tmp/repo"), Path("/tmp/artifacts/1"))
        self.assertIn("--add-dir", command)
        self.assertIn("/tmp/repo", command)
        self.assertIn("/tmp/artifacts/1", command)
        self.assertIn("--agent", command)
        self.assertEqual(command[command.index("--agent") + 1], "agent-team-orchestrator:agent-team-research")
        self.assertIn("--plugin-dir", command)
        self.assertNotIn("--yolo", command)
        self.assertNotIn("--allow-all", command)
        self.assertNotIn("--allow-all-tools", command)
        self.assertEqual(command[-1], "--allow-tool=read")

    def test_default_commands_use_least_privilege_phase_policies(self) -> None:
        runner = CopilotCliRunner()
        for phase in PHASE_AGENTS:
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "prompt", Path("/tmp/repo"))
                self.assertNotIn("--yolo", command)
                self.assertNotIn("--allow-all", command)
                self.assertNotIn("--allow-all-tools", command)
                self.assertNotIn("--allow-all-urls", command)
                self.assertTrue(any(arg.startswith("--allow-tool=") for arg in command))
                self.assertTrue(any(arg.startswith("--deny-tool=") for arg in command))
                self.assertIn("shell(git push)", self._permission_values(command, "--deny-tool"))
                self.assertIn("shell(git push:*)", self._permission_values(command, "--deny-tool"))
                if phase == "research":
                    self.assertIn("https://*", self._permission_values(command, "--allow-url"))
                    self.assertIn("http://*", self._permission_values(command, "--allow-url"))
                else:
                    self.assertEqual([], self._permission_values(command, "--allow-url"))

    def test_read_only_phase_policies_do_not_allow_write(self) -> None:
        runner = CopilotCliRunner()
        for phase in ("research", "plan", "review"):
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "prompt", Path("/tmp/repo"))
                self.assertNotIn("write", self._permission_values(command, "--allow-tool"))

    def test_review_phase_policy_allows_git_status_and_diff_inspection(self) -> None:
        runner = CopilotCliRunner()
        command = runner._build_command("review", "prompt", Path("/tmp/repo"))
        allowed = set(self._permission_values(command, "--allow-tool"))

        self.assertTrue(
            {
                "shell(git status)",
                "shell(git status:*)",
                "shell(git diff)",
                "shell(git diff:*)",
            }.issubset(allowed)
        )
        self.assertNotIn("shell(git:*)", allowed)
        self.assertNotIn("write", allowed)

    def test_all_phase_policies_allow_git_status_and_diff_inspection(self) -> None:
        runner = CopilotCliRunner()
        expected = {
            "shell(git status)",
            "shell(git status:*)",
            "shell(git diff)",
            "shell(git diff:*)",
        }

        for phase in PHASE_AGENTS:
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "prompt", Path("/tmp/repo"))
                allowed = set(self._permission_values(command, "--allow-tool"))
                self.assertTrue(expected.issubset(allowed))

    def test_all_phases_allow_abstract_read_tool(self) -> None:
        runner = CopilotCliRunner()
        for phase in PHASE_AGENTS:
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "prompt", Path("/tmp/repo"))
                self.assertIn("read", self._permission_values(command, "--allow-tool"))

    def test_read_only_phases_allow_basic_file_inspection_tools(self) -> None:
        runner = CopilotCliRunner()
        for phase in ("research", "plan", "review"):
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "prompt", Path("/tmp/repo"))
                allowed = self._permission_values(command, "--allow-tool")
                self.assertIn("shell(cat:*)", allowed)
                self.assertIn("shell(grep:*)", allowed)

    def test_only_research_phase_policy_allows_url_access(self) -> None:
        runner = CopilotCliRunner()
        for phase in PHASE_AGENTS:
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "prompt", Path("/tmp/repo"))
                allowed_urls = self._permission_values(command, "--allow-url")
                if phase == "research":
                    self.assertEqual(["https://*", "http://*"], allowed_urls)
                else:
                    self.assertEqual([], allowed_urls)

    def test_read_only_phase_policies_do_not_allow_mutating_shell_inspection_forms(self) -> None:
        runner = CopilotCliRunner()
        disallowed = {
            "shell(find:*)",
            "shell(find -delete:*)",
            "shell(find -exec:*)",
            "shell(sed:*)",
            "shell(sed -i:*)",
        }
        for phase in ("research", "plan", "review"):
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "prompt", Path("/tmp/repo"))
                allowed = set(self._permission_values(command, "--allow-tool"))
                self.assertTrue(disallowed.isdisjoint(allowed))

    def test_validation_allows_check_commands_without_write(self) -> None:
        runner = CopilotCliRunner()
        command = runner._build_command("validation", "prompt", Path("/tmp/repo"))
        allowed = self._permission_values(command, "--allow-tool")
        self.assertIn("shell(python3 -m unittest)", allowed)
        self.assertIn("shell(python3 -m unittest:*)", allowed)
        self.assertIn("shell(pytest:*)", allowed)
        self.assertIn("shell(npm test)", allowed)
        self.assertIn("shell(npm run test:*)", allowed)
        self.assertIn("shell(go test:*)", allowed)
        self.assertNotIn("write", allowed)

    def test_validation_policy_rejects_arbitrary_runtime_execution(self) -> None:
        runner = CopilotCliRunner()
        command = runner._build_command("validation", "prompt", Path("/tmp/repo"))
        allowed = self._permission_values(command, "--allow-tool")

        for broad_runtime in (
            "shell(python:*)",
            "shell(python3:*)",
            "shell(node:*)",
            "shell(npm run:*)",
        ):
            with self.subTest(broad_runtime=broad_runtime):
                self.assertNotIn(broad_runtime, allowed)

    def test_write_phases_allow_write(self) -> None:
        runner = CopilotCliRunner()
        for phase in ("implementation", "merge_conflict_resolution"):
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "prompt", Path("/tmp/repo"))
                self.assertIn("write", self._permission_values(command, "--allow-tool"))

    def test_yolo_permission_mode_emits_explicit_escape_hatch(self) -> None:
        runner = CopilotCliRunner(permission_mode="yolo")
        command = runner._build_command("implementation", "prompt", Path("/tmp/repo"), Path("/tmp/artifacts/1"))
        self.assertIn("--yolo", command)
        self.assertFalse(any(arg.startswith("--allow-tool=") for arg in command))
        self.assertFalse(any(arg.startswith("--deny-tool=") for arg in command))
        self.assertFalse(any(arg.startswith("--allow-url=") for arg in command))
        self.assertFalse(any(arg.startswith("--deny-url=") for arg in command))
        self.assertIn("/tmp/repo", command)
        self.assertIn("/tmp/artifacts/1", command)

    def test_run_blocks_without_target_repo_context(self) -> None:
        runner = CopilotCliRunner(command="/bin/false")
        issue = Issue(
            **{
                **self.issue.__dict__,
                "repo_path": None,
            }
        )

        result = runner.run("research", issue, {"prompt_template": "Body {title}"})

        self.assertEqual(result.status, "blocked")
        self.assertIn("Target repo path is required", result.summary)
        self.assertEqual(result.suggested_next_phase, "blocked")

    def test_all_phases_have_custom_agent_commands(self) -> None:
        runner = CopilotCliRunner()
        for phase, agent_name in PHASE_AGENTS.items():
            with self.subTest(phase=phase):
                command = runner._build_command(phase, "Body title", None)
                self.assertEqual(command[command.index("--agent") + 1], f"agent-team-orchestrator:{agent_name}")
                self.assertNotIn("/research", command)
                self.assertNotIn("/plan", command)
                self.assertNotIn("/fleet", command)
                self.assertNotIn("/review", command)

    def test_all_phases_have_permission_policies(self) -> None:
        self.assertEqual(set(PHASE_PERMISSION_POLICIES), set(PHASE_AGENTS))

    def test_packaged_plugin_manifest_names_agent_namespace(self) -> None:
        runner = CopilotCliRunner()
        self.assertEqual(runner.plugin_name, "agent-team-orchestrator")
        self.assertTrue((runner.plugin_dir / "plugin.json").is_file())

    def test_packaged_custom_agents_exist_with_recommendations(self) -> None:
        runner = CopilotCliRunner()
        for phase, agent_name in PHASE_AGENTS.items():
            with self.subTest(phase=phase):
                agent_file = runner.plugin_dir / "agents" / f"{agent_name}.agent.md"
                self.assertTrue(agent_file.is_file(), f"Missing packaged agent file: {agent_file}")
                text = agent_file.read_text(encoding="utf-8")
                self.assertIn("phase artifact", text.lower())
                self.assertIn("Recommendation:", text)
                for recommendation in ("blocked",):
                    self.assertIn(recommendation, text)

    def test_plan_agent_requires_executive_summary_first(self) -> None:
        runner = CopilotCliRunner()
        agent_file = runner.plugin_dir / "agents" / "agent-team-plan.agent.md"
        text = agent_file.read_text(encoding="utf-8")
        summary_index = text.index("1. Executive Summary")
        approach_index = text.index("2. Proposed approach")

        self.assertLess(summary_index, approach_index)
        summary_block = text[summary_index:approach_index].lower()
        self.assertIn("manager-friendly", summary_block)
        self.assertIn("what will change and why", summary_block)
        self.assertIn("7. Recommendation", text)
        self.assertIn("ready_for_implementation", text)
        self.assertIn("awaiting_human_input", text)
        self.assertIn("blocked", text)

    def test_research_agent_instructs_parallel_web_synthesis(self) -> None:
        runner = CopilotCliRunner()
        agent_file = runner.plugin_dir / "agents" / "agent-team-research.agent.md"
        text = agent_file.read_text(encoding="utf-8").lower()
        self.assertIn("/research", text)
        self.assertIn("parallel research", text)
        self.assertIn("subagents/tasks", text)
        self.assertIn("search the web", text)
        self.assertIn("github search", text)
        self.assertIn("synthesize", text)
        self.assertIn("external sources", text)
        self.assertIn("research orchestrator", text)
        self.assertIn("not a planner", text)
        self.assertIn("codebase pattern discovery", text)
        self.assertIn("self-directed research questions", text)
        self.assertIn("answer each question with evidence", text)
        self.assertIn("url citations", text)
        self.assertIn("confidence assessment", text)
        self.assertIn("follow-up waves", text)

    def test_research_prompt_reinforces_report_not_plan(self) -> None:
        prompts_dir = Path(__file__).parent.parent / "src" / "agent_team" / "prompts"
        text = (prompts_dir / "research.md").read_text(encoding="utf-8").lower()
        self.assertIn("evidence-backed research report", text)
        self.assertIn("not an implementation plan", text)
        self.assertIn("codebase pattern discovery", text)
        self.assertIn("self-answered questions", text)

    def test_phase_agents_include_detailed_workflows(self) -> None:
        runner = CopilotCliRunner()
        expectations = {
            "agent-team-plan": (
                "/plan",
                "planning workflow",
                "read-only planning checks in parallel",
                "implementation sequence",
                "plan quality bar",
            ),
            "agent-team-implementation": (
                "/fleet",
                "implementation workflow",
                "independent implementation threads in parallel",
                "integrate subagent results",
                "run the smallest relevant checks",
            ),
            "agent-team-validation": (
                "validation workflow",
                "targeted checks first",
                "validation threads in parallel",
                "classify it as implementation-caused",
            ),
            "agent-team-review": (
                "/review",
                "review workflow",
                "high-signal code review",
                "review threads in parallel",
                "do not report low-value style",
            ),
            "agent-team-merge-conflict-resolution": (
                "conflict-resolution workflow",
                "/fleet",
                "inventory every file containing conflict markers",
                "resolve them in parallel",
                "prefer reconciled combined solutions",
            ),
        }
        for agent_name, required_phrases in expectations.items():
            with self.subTest(agent_name=agent_name):
                agent_file = runner.plugin_dir / "agents" / f"{agent_name}.agent.md"
                text = agent_file.read_text(encoding="utf-8").lower()
                for phrase in required_phrases:
                    self.assertIn(phrase, text)

    def test_validation_and_review_agents_read_optional_merge_conflict_context(self) -> None:
        runner = CopilotCliRunner()
        for agent_name in ("agent-team-validation", "agent-team-review"):
            with self.subTest(agent_name=agent_name):
                agent_file = runner.plugin_dir / "agents" / f"{agent_name}.agent.md"
                text = agent_file.read_text(encoding="utf-8").lower()
                self.assertIn("merge_conflict_resolution.md", text)
                self.assertIn("when `merge.md`", text)
                self.assertIn("post-conflict", text)

    def test_all_prompt_templates_render_without_slash_prefixes(self) -> None:
        prompts_dir = Path(__file__).parent.parent / "src" / "agent_team" / "prompts"
        for phase in PHASE_AGENTS:
            with self.subTest(phase=phase):
                template = (prompts_dir / f"{phase}.md").read_text(encoding="utf-8")
                prompt = CopilotCliRunner._build_prompt(
                    phase,
                    self.issue,
                    {
                        "prompt_template": template,
                        "artifacts_dir": "/tmp/artifacts/1",
                        "phase_artifact": f"/tmp/artifacts/1/{phase}.md",
                        "workspace_repo_path": "/tmp/worktrees/issue-1/repo",
                        "workspace_root": "/tmp/worktrees/issue-1",
                    },
                )
                self.assertFalse(prompt.startswith("/"))
                self.assertNotIn("{issue_id}", prompt)
                self.assertNotIn("{phase_artifact}", prompt)

    def test_research_and_plan_tolerate_missing_workspace(self) -> None:
        self.assertEqual(CopilotCliRunner._resolve_execution_repo_path("research", self.issue, {}), (None, None))
        self.assertEqual(CopilotCliRunner._resolve_execution_repo_path("plan", self.issue, {}), (None, None))

    def test_workspace_required_phase_without_workspace_blocks_without_running_copilot(self) -> None:
        runner = CopilotCliRunner()
        result = runner.run("implementation", self.issue, {"prompt_template": "Body {title}"})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.suggested_next_phase, "blocked")
        self.assertIn("Isolated workspace path was not provided", result.summary)

    def test_run_blocks_invalid_recommendation_with_diagnostic_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            script = Path(tmp) / "fake-copilot"
            script.write_text(
                """#!/usr/bin/env python3
print("1. Summary\\n\\nChanged the files.\\n\\n6. Recommendation: `ready_for_implementation`.")
""",
                encoding="utf-8",
            )
            script.chmod(0o755)

            result = CopilotCliRunner(command=str(script)).run(
                "implementation",
                self.issue,
                {"prompt_template": "Body {title}", "workspace_repo_path": str(workspace)},
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.suggested_next_phase, "blocked")
        self.assertEqual(
            result.summary,
            "Copilot CLI implementation provided invalid Recommendation 'ready_for_implementation'; "
            "expected one of: awaiting_human_input, blocked, ready_for_validation",
        )
        self.assertEqual(result.error, result.summary)
        self.assertIn("Changed the files.", result.artifact_markdown)
        self.assertIn("## Orchestrator diagnostic", result.artifact_markdown)
        self.assertIn("Recommendation: `blocked`", result.artifact_markdown)
        self.assertEqual(CopilotCliRunner._recommended_next_phase("implementation", result.artifact_markdown), "blocked")

    def test_plan_run_blocks_missing_recommendation_with_diagnostic_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake-copilot"
            script.write_text(
                """#!/usr/bin/env python3
print('''1. Executive Summary

Plan the routing hardening.

2. Proposed approach

Update plan instructions and add coverage.

3. Files/components to change

Prompt, agent, and tests.

4. Test plan

Run targeted unit tests.

5. Risks and rollback

Rollback by reverting the prompt and test changes.

6. Required human approvals

None.
''')
""",
                encoding="utf-8",
            )
            script.chmod(0o755)

            result = CopilotCliRunner(command=str(script)).run(
                "plan",
                self.issue,
                {"prompt_template": "Body {title}"},
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.suggested_next_phase, "blocked")
        self.assertIn("Copilot CLI plan did not provide a valid Recommendation", result.summary)
        self.assertIn("awaiting_human_input", result.summary)
        self.assertIn("awaiting_plan_approval", result.summary)
        self.assertIn("blocked", result.summary)
        self.assertIn("ready_for_implementation", result.summary)
        self.assertEqual(result.error, result.summary)
        self.assertIn("Plan the routing hardening.", result.artifact_markdown)
        self.assertIn("6. Required human approvals", result.artifact_markdown)
        self.assertIn("## Orchestrator diagnostic", result.artifact_markdown)
        self.assertIn("Recommendation: `blocked`", result.artifact_markdown)
        self.assertEqual(CopilotCliRunner._recommended_next_phase("plan", result.artifact_markdown), "blocked")

    def test_nonexistent_workspace_path_blocks_without_running_copilot(self) -> None:
        runner = CopilotCliRunner()
        result = runner.run(
            "research",
            self.issue,
            {"prompt_template": "Body {title}", "workspace_repo_path": "/path/that/does/not/exist"},
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.suggested_next_phase, "blocked")
        self.assertIn("Isolated workspace path does not exist", result.summary)

    def test_source_mutation_guard_detects_read_only_phase_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
            (repo / "README.md").write_text("# test\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")
            snapshot = CopilotCliRunner._source_snapshot("research", {"source_repo_path": str(repo)})

            (repo / "README.md").write_text("# changed\n", encoding="utf-8")

            message = CopilotCliRunner._source_mutation_error("research", snapshot)
        self.assertIsNotNone(message)
        self.assertIn("Source repo changed during read-only research phase", message)

    def test_source_guard_blocks_dirty_baseline_before_read_only_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
            (repo / "README.md").write_text("# test\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")
            (repo / "README.md").write_text("# dirty baseline\n", encoding="utf-8")

            runner = CopilotCliRunner(command="/path/that/does/not/exist")
            result = runner.run(
                "research",
                self.issue,
                {"prompt_template": "Body {title}", "source_repo_path": str(repo)},
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.suggested_next_phase, "blocked")
        self.assertIn("Source repo must be clean before read-only research phase", result.summary)

    def test_source_guard_allows_dirty_baseline_before_plan_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
            (repo / "README.md").write_text("# test\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")
            (repo / "README.md").write_text("# dirty baseline\n", encoding="utf-8")
            called = Path(tmp) / "called"
            script = Path(tmp) / "fake-copilot"
            script.write_text(
                f"""#!/usr/bin/env python3
import pathlib

pathlib.Path({str(called)!r}).write_text("called", encoding="utf-8")
print("1. Result\\n\\nRecommendation: `ready_for_implementation`")
""",
                encoding="utf-8",
            )
            script.chmod(0o755)

            snapshot, error = CopilotCliRunner._source_snapshot_for_read_only_phase(
                "plan", {"source_repo_path": str(repo)}
            )
            result = CopilotCliRunner(command=str(script)).run(
                "plan",
                self.issue,
                {"prompt_template": "Body {title}", "source_repo_path": str(repo)},
            )
            called_exists = called.is_file()

        self.assertIsNone(error)
        self.assertIsNotNone(snapshot)
        self.assertIn("README.md", snapshot.status)
        self.assertTrue(called_exists)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.suggested_next_phase, "awaiting_plan_approval")

    def test_plan_source_mutation_requeues_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
            (repo / "README.md").write_text("# test\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")
            script = Path(tmp) / "fake-copilot"
            script.write_text(
                """#!/usr/bin/env python3
import pathlib

pathlib.Path("README.md").write_text("# changed during plan\\n", encoding="utf-8")
print("1. Result\\n\\nRecommendation: `ready_for_implementation`")
""",
                encoding="utf-8",
            )
            script.chmod(0o755)

            result = CopilotCliRunner(command=str(script)).run(
                "plan",
                self.issue,
                {"prompt_template": "Body {title}", "source_repo_path": str(repo)},
            )

        self.assertEqual(result.status, "requeued")
        self.assertEqual(result.suggested_next_phase, "ready_for_plan")
        self.assertIn("Source repo changed during read-only plan phase", result.summary)
        self.assertIn("Plan discarded and requeued", result.artifact_markdown)
        self.assertIn("Recommendation: `ready_for_plan`", result.artifact_markdown)
        self.assertNotIn("Recommendation: `blocked`", result.artifact_markdown)
        self.assertIn("Recommendation: `ready_for_implementation`", result.raw_stdout or "")

    def test_plan_source_mutation_detects_dirty_baseline_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
            (repo / "README.md").write_text("# test\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")
            (repo / "README.md").write_text("# dirty baseline\n", encoding="utf-8")
            baseline_status = self._git(repo, "status", "--porcelain")
            script = Path(tmp) / "fake-copilot"
            script.write_text(
                """#!/usr/bin/env python3
import pathlib

pathlib.Path("README.md").write_text("# changed during dirty plan\\n", encoding="utf-8")
print("1. Result\\n\\nRecommendation: `ready_for_implementation`")
""",
                encoding="utf-8",
            )
            script.chmod(0o755)

            result = CopilotCliRunner(command=str(script)).run(
                "plan",
                self.issue,
                {"prompt_template": "Body {title}", "source_repo_path": str(repo)},
            )
            final_status = self._git(repo, "status", "--porcelain")

        self.assertEqual(baseline_status, final_status)
        self.assertEqual(result.status, "requeued")
        self.assertEqual(result.suggested_next_phase, "ready_for_plan")
        self.assertIn("Source repo changed during read-only plan phase", result.summary)

    def test_plan_source_snapshot_detects_dirty_untracked_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
            (repo / "README.md").write_text("# test\n", encoding="utf-8")
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "-m", "initial")
            (repo / "notes.txt").write_text("dirty baseline\n", encoding="utf-8")
            baseline_status = self._git(repo, "status", "--porcelain")

            snapshot = CopilotCliRunner._source_snapshot("plan", {"source_repo_path": str(repo)})
            (repo / "notes.txt").write_text("changed during plan\n", encoding="utf-8")
            final_status = self._git(repo, "status", "--porcelain")
            message = CopilotCliRunner._source_mutation_error("plan", snapshot)

        self.assertEqual(baseline_status, final_status)
        self.assertIsNotNone(message)
        self.assertIn("Source repo changed during read-only plan phase", message)

    def test_source_guard_blocks_non_git_source_before_read_only_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = CopilotCliRunner(command="/path/that/does/not/exist")
            result = runner.run(
                "plan",
                self.issue,
                {"prompt_template": "Body {title}", "source_repo_path": tmp},
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.suggested_next_phase, "blocked")
        self.assertIn("Source repo could not be inspected before read-only plan phase", result.summary)

    @staticmethod
    def _permission_values(command, flag: str):
        values = []
        prefix = f"{flag}="
        for arg in command:
            if arg.startswith(prefix):
                values.extend(arg[len(prefix) :].split(","))
        return values

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        import subprocess

        completed = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
        return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
