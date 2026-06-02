# Agent Team Orchestrator

A local-first CLI and web cockpit for coordinating AI engineering agents through issue intake, research, planning, implementation, validation, review, and merge finalization.

Agent Team Orchestrator keeps its state on your machine. It stores issue state in SQLite, writes per-issue artifacts and logs under `AGENT_TEAM_HOME`, and uses isolated Git worktrees for implementation and later repo-backed phases so agents do not edit the source checkout directly.

## What it does

- Tracks local issues through a validated phase workflow with reviewable artifacts at each step.
- Runs either deterministic dry-run phases or GitHub Copilot CLI custom agents for real work.
- Requires human approval before implementation starts and before reviewed work is finalized.
- Reuses a persistent per-issue worktree from implementation through validation, review, merge, and merge-conflict resolution.
- Provides a terminal dashboard plus a local web UI for manager decisions and queue control.

## Prerequisites

- Python 3.10 or newer.
- Git, for target repositories and isolated worktrees.
- GitHub Copilot CLI for real agent runs. Use dry-run mode for local smoke checks and tests that must not invoke Copilot.
- Optional: `gh` or Azure DevOps CLI authentication when using hosted pull-request finalization.

There are no runtime Python dependencies beyond the standard library.

## Installation

Install from a checkout when you want the `agent-team` console script:

```bash
git clone <repository-url>
cd agent-team-orchestrator
python3 -m pip install -e .
agent-team init
```

You can also run directly from the checkout without installing:

```bash
PYTHONPATH=src python3 -m agent_team.cli init
```

## Quick start

Copy the example config, then uncomment only the values you need. For a safe smoke run, set `runner` to `"dry-run"`.
Each `worker once` invocation drains ready work until the queue is idle or the next human gate is reached.

```bash
cp agent-team.config.example.jsonc agent-team.config.jsonc

agent-team init
agent-team issue create \
  --repo /path/to/target-repo \
  --description "Fix the flaky validation failure" \
  --ready

agent-team worker once          # research + planning, then awaits plan approval
agent-team issue approve-plan 1 # use the id printed by issue create
agent-team worker once          # implementation + validation + review, then awaits merge approval
agent-team issue approve-merge 1 --mode local
agent-team worker once          # merge finalization
```

For an always-on local control surface, run:

```bash
agent-team serve --worker-concurrency 3
```

Use `agent-team --help`, `agent-team issue --help`, and subcommand `--help` output for the full command reference.

## Workflow

```text
draft -> needs_research -> ready_for_plan -> awaiting_plan_approval
  -> ready_for_implementation
  -> ready_for_validation -> ready_for_review -> awaiting_merge_approval
  -> ready_for_merge -> done
```

Validation and review can loop back to implementation. Merge conflicts route through `ready_for_merge_conflict_resolution` before returning to validation and review. Any agent phase can pause at `awaiting_human_input` or stop at `blocked` when it cannot proceed safely.

## Configuration

`agent-team.config.example.jsonc` is the detailed configuration reference. Copy it to the ignored local file `agent-team.config.jsonc` and uncomment persistent defaults as needed.

Config discovery order is:

1. `agent-team --config PATH <subcommand>`
2. `AGENT_TEAM_CONFIG_FILE=/path/to/agent-team.config.jsonc`
3. `agent-team.config.jsonc` in the current working directory
4. Built-in defaults

Environment variables remain supported as compatibility overrides, and command flags such as `serve --host`, `serve --port`, `worker once --concurrency`, and `worker loop --concurrency` affect only that invocation.

Copilot model and reasoning effort can be configured globally or per Copilot-backed phase in the example config. Defaults remain unset, model IDs depend on the local Copilot CLI/account, raw `extra_args` are appended last for advanced overrides, and custom-agent `model` frontmatter can override CLI model selection.

## Safety highlights

- The default runner is `copilot-cli`; dry-run mode is deterministic and does not invoke Copilot.
- Default Copilot runs use phase-specific least-privilege tool approvals. The `copilot.permission_mode = "yolo"` escape hatch should only be used in isolated throwaway environments.
- Plan approval and merge approval are explicit human gates.
- Before the first implementation worktree is created, the target source checkout must be a clean Git repository because uncommitted and untracked files are not copied into worktrees.
- Research and planning use the target checkout as source context. Implementation and later repo-backed phases run in the isolated issue worktree.
- The web UI binds to `127.0.0.1` by default, uses same-origin/CSRF checks for POSTs, and has no authentication. Do not expose it on a shared network without your own protection.
- Merge finalization can run locally or open/reuse a hosted PR when a supported GitHub or Azure DevOps Services remote is available. Opening a PR marks the local issue done; hosted PR review and merge remain outside the orchestrator.
- Every run, transition, artifact, and log remains local under the configured state directory.

## Development and tests

Use dry-run mode for checks that should not invoke Copilot:

```bash
AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m agent_team.cli init
PYTHONDONTWRITEBYTECODE=1 AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m unittest discover -s tests
```

After `python3 -m pip install -e .`, the same commands can use the `agent-team` console script.

## More information

| Need | Source |
|---|---|
| Command reference | `agent-team --help` and subcommand `--help` |
| Configuration options | `agent-team.config.example.jsonc` |
| Contributor workflow | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Support expectations | [`SUPPORT.md`](SUPPORT.md) |
| Security reporting | [`SECURITY.md`](SECURITY.md) |
| License | [`LICENSE`](LICENSE) |
