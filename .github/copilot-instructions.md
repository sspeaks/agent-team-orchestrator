# Copilot instructions for agent-team-orchestrator

## Commands

- Run directly from the checkout with `PYTHONPATH=src`; after `python3 -m pip install -e .`, the `agent-team` console script is available.
- Full test suite: `PYTHONPATH=src python3 -m unittest discover -s tests`
- Single test file: `PYTHONPATH=src python3 -m unittest tests.test_copilot_runner`
- Single test method: `PYTHONPATH=src python3 -m unittest tests.test_copilot_runner.CopilotCliRunnerTests.test_command_allows_repo_directory`
- CLI smoke check without invoking Copilot: `AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m agent_team.cli init`

## Architecture

- This is a Python >=3.10, local-first orchestrator with no runtime dependencies beyond the standard library. State lives in SQLite and per-issue artifacts under `AGENT_TEAM_HOME` (`~/.local/share/agent-team-orchestrator` by default).
- `agent_team.cli` is the entry point. It loads `AppConfig`, creates the state/artifact directories, initializes `IssueStore`, then dispatches CLI, worker, dashboard, and web commands.
- `IssueStore` in `db.py` owns the SQLite schema for issues, runs, events, lock leases, and issue transitions. All phase changes must go through `state_machine.validate_transition`.
- `state_machine.py` is the canonical phase graph: ready phases, running phases, valid transitions, and default next phases. Keep this file in sync with CLI phase choices, runner recommendation parsing, prompt files, packaged agent files, and tests when adding or renaming phases.
- `Orchestrator` is the central coordinator. For each runnable issue it acquires a lease, moves to the running phase, creates a run, prepares workspace context, invokes the runner, writes artifacts/logs/history, records run completion, and transitions to the next phase.
- The `merge` phase is deterministic Git/worktree logic, not an AI runner phase. `WorkspaceManager.merge_and_cleanup` merges approved issue worktree commits back to the source branch, archives workspace metadata, and routes conflicts to `ready_for_merge_conflict_resolution`.
- Runners implement `AgentRunner`. `DryRunRunner` is deterministic for tests and local development. `CopilotCliRunner` invokes the bundled Copilot plugin agents with `copilot --plugin-dir ... --agent agent-team-orchestrator:<agent> -p <prompt> --no-ask-user`, phase-specific `--allow-tool`/`--deny-tool` approvals, `--add-dir` for the execution repo/worktree and issue artifact directory, plus configured extra args. `AGENT_TEAM_COPILOT_PERMISSION_MODE=yolo` is the explicit broad-permission escape hatch.
- Copilot phases use prompt templates in `src/agent_team/prompts/` and custom agent definitions in `src/agent_team/copilot_plugin/agents/`. The runner expects each final artifact to contain exactly one allowed `Recommendation:` value; missing or invalid recommendations block the issue.
- `WorkspaceManager` creates a persistent detached Git worktree per issue under `AGENT_TEAM_WORKTREES_DIR` or `$AGENT_TEAM_HOME/worktrees`. Copilot runs in `workspace_repo_path`; the original `repo_path` is only the source checkout used to create or merge the worktree.
- `ArtifactStore` writes `issue.json`, phase artifacts, run logs, `history.jsonl`, workspace metadata, plan rejection context, and merge metadata under `$AGENT_TEAM_HOME/issues/<issue_id>/`.
- The terminal dashboard is a renderer over `IssueStore.dashboard_summary`. The web UI is a standard-library `ThreadingHTTPServer` app that shares the same store/artifacts and queues runs through `WebJobManager`.

## Repository conventions

- Use `AGENT_TEAM_RUNNER=dry-run` for development or tests that should not invoke Copilot. Tests construct `AppConfig` with `TemporaryDirectory` state and fake runners/Copilot scripts instead of touching the user's real state.
- Target repos must be clean before first workspace creation because uncommitted/untracked files are not copied to Git worktrees. Existing issue worktrees are intentionally reused across later phases; do not reset or delete them except through successful merge cleanup.
- Adding a phase requires synchronized edits across `state_machine.py`, `CopilotCliRunner.PHASE_AGENTS`, `CopilotCliRunner.PHASE_RECOMMENDATIONS`, `src/agent_team/prompts/<phase>.md`, packaged agent markdown, CLI choices, artifact listing if it should appear in the UI, and state/runner/web tests.
- Prompt templates are rendered with `str.format` in `CopilotCliRunner._build_prompt`; escape literal braces in prompt markdown as `{{` and `}}`, and add any new placeholders to `_build_prompt`.
- Plan approval and merge approval are human gates. A plan recommendation of `ready_for_implementation` is normalized to `awaiting_plan_approval`; a review recommendation of `done` is normalized to `awaiting_merge_approval`.
- Web output must escape user, artifact, and error content with `_esc`. POST actions require CSRF and same-origin checks, and manual run/approval/transition actions must reject active issue locks.
- Artifact downloads must go through `ArtifactStore.list_issue_artifacts` and `read_issue_artifact` so path traversal checks and the allow-list stay centralized.
- Tests use `unittest`. Git-dependent tests are guarded with `@unittest.skipUnless(shutil.which("git"), "git is required for workspace tests")`; keep that pattern for new Git worktree scenarios.
