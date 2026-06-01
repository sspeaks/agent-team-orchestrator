# Agent Team Orchestrator

A local-first state-machine orchestrator for coordinating AI engineering agents through issue intake, research, planning, implementation, validation, and review.

The project stores state locally in SQLite and writes per-issue artifacts to disk. It does not require Azure DevOps permissions; Azure DevOps support is future optional adapter work for importing or syncing work items.

## Prerequisites

- Python 3.10 or newer.
- Git, for target repositories and isolated worktrees.
- GitHub Copilot CLI for real agent runs. Use `AGENT_TEAM_RUNNER=dry-run` for local development, tests, and smoke checks that must not invoke Copilot.
- No runtime Python dependencies beyond the standard library.

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

```bash
export AGENT_TEAM_RUNNER=dry-run

agent-team init
agent-team issue create \
  --repo /path/to/target-repo \
  --description "Research the failure, write a plan, and stop before implementation."
agent-team issue edit 1 --title "Investigate flaky migration validation failure"
agent-team issue advance 1 --to needs_research --message "publish draft"
agent-team worker once
agent-team worker once
agent-team issue approve-plan 1
agent-team worker once
agent-team dashboard
agent-team issue show 1
agent-team serve --worker-concurrency 3
```

Use `AGENT_TEAM_HOME=/path/to/state` to change where SQLite state and artifacts are stored. By default, state lives under `~/.local/share/agent-team-orchestrator`.
Use `AGENT_TEAM_WORKTREES_DIR=/path/to/worktrees` to change where isolated per-issue Git worktrees are stored. By default, worktrees live under `$AGENT_TEAM_HOME/worktrees`.
When the web server runs under WSL, the Open in VS Code button uses `WSL_DISTRO_NAME` to build a VS Code Remote-WSL link for Windows browsers. Set `AGENT_TEAM_VSCODE_WSL_DISTRO=Ubuntu-22.04` to override the distro name, or set `AGENT_TEAM_VSCODE_WSL_DISTRO=` to force local file links.

## Development and tests

Use dry-run mode for checks that should not invoke Copilot:

```bash
AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m agent_team.cli init
PYTHONDONTWRITEBYTECODE=1 AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m unittest discover -s tests
```

After `python3 -m pip install -e .`, the same commands can use the `agent-team` console script.

## Safety model

- The default runner is `copilot-cli`, which invokes Copilot CLI for phase work with least-privilege, phase-specific tool approvals.
- Broad Copilot permissions require the explicit `AGENT_TEAM_COPILOT_PERMISSION_MODE=yolo` escape hatch and should only be used in isolated throwaway environments.
- The orchestrator owns state transitions; runners may suggest a next state but cannot bypass validation.
- Copilot output must include a valid `Recommendation: ...`; `blocked` recommendations or missing recommendations leave the issue blocked instead of silently advancing.
- CLI-created local issues start as editable `draft` backlog items unless created with `--ready`; the web create form defaults to runnable and can be unchecked to keep an editable draft. Workers and web Run-next ignore drafts until a manager publishes them to `needs_research`.
- Copilot CLI implementation and later repo-backed phases use a persistent isolated Git worktree per issue. Research and planning use the target checkout only as read-only source context and do not create workspace metadata.
- Issues with a missing, non-Git, or dirty target repo directory are blocked before Copilot implementation creates the first isolated workspace. Existing issue worktrees are reused across validation, review, merge, and merge-conflict-resolution so implementation changes remain available until merge.
- Merge finalization is serialized per source Git repository with a local file lock under `$AGENT_TEAM_HOME/locks`, so concurrent workers do not run overlapping local merges, PR branch pushes, or cleanup operations for the same source checkout.
- Plan approval is an explicit human gate: plan output lands in `awaiting_plan_approval`, and implementation cannot start until you run `issue approve-plan`.
- Merge approval is also an explicit human gate: successful review lands in `awaiting_merge_approval`, and the worktree is not finalized or cleaned up until you run `issue approve-merge` and then run the merge phase. The approval can use `--mode auto`, `--mode local`, or `--mode pull-request`.
- Critical open-ended questions use `awaiting_human_input`. Agents can recommend this state only with a structured `Human input request`; the request is persisted in SQLite, projected to `human_input.jsonl` and `human_input.md`, and later prompts receive the answered decision history as quoted context.
- Every run and state transition is recorded in SQLite and `history.jsonl`. Phase artifacts such as `research.md` and `plan.md` are the clean deliverables; a per-run log file is created under each issue's `logs/` directory as soon as the phase starts, and Copilot output streams there while the phase is running.
- Worker, direct-run, and web startup paths recover interrupted runs before scheduling new work. Recovery only reclaims expired locks or lock owners that are definitely dead on the same host, preserves issue worktrees, archives prior phase artifacts before reruns, and routes ambiguous merge states to `blocked` instead of guessing.
- The web interface binds to `127.0.0.1` by default, uses same-origin/CSRF checks for POST controls, and refuses non-loopback binds unless `--unsafe-allow-remote` is passed. It still has no authentication; do not expose it on a shared network without adding protection.
- `agent-team serve` runs the web UI and autonomous ready-queue worker loop in one local process. Shutdown stops scheduling new batches and lets already-started issue runs drain according to the existing runner behavior; hard kills may leave leases active until their TTL expires.
- For development/tests, set `AGENT_TEAM_RUNNER=dry-run` to avoid invoking any AI tool.

## Default Copilot CLI phase mapping

The `copilot-cli` runner uses packaged custom agents instead of relying on Copilot CLI slash commands in non-interactive prompts. The runner loads the bundled local plugin and invokes `copilot --agent agent-team-orchestrator:<name> -p <prompt> --no-ask-user` with phase-specific `--allow-tool`/`--deny-tool` approvals. Configured extra args are appended last so advanced local runs can add or override approvals deliberately.

| Orchestrator phase | Custom agent | Purpose |
|---|---|---|
| `research` | `agent-team-research` | Deep investigation and context gathering |
| `plan` | `agent-team-plan` | Read-only implementation planning |
| `implementation` | `agent-team-implementation` | Implement the approved plan; delegates independent subtasks in parallel when appropriate |
| `validation` | `agent-team-validation` | Run checks and summarize validation results |
| `review` | `agent-team-review` | Review the implementation without editing code |
| `merge_conflict_resolution` | `agent-team-merge-conflict-resolution` | Resolve merge conflict markers in the isolated worktree; delegates independent conflicts in parallel when appropriate |

The plan stage still stops at `awaiting_plan_approval`; implementation does not run until a human approves the plan. The normal `merge` phase is deterministic Git/worktree logic rather than a Copilot prompt; Copilot is only used again if a merge conflict is prepared for `merge_conflict_resolution`.

## Worktree isolation

For Copilot CLI runs, the orchestrator creates a detached Git worktree for each issue under `$AGENT_TEAM_HOME/worktrees` (or `AGENT_TEAM_WORKTREES_DIR`) when implementation starts. Research and planning do not create worktrees; they receive the target checkout as source context. Research still blocks if the source checkout is dirty before the run or changes during the read-only run. Planning snapshots the current source state, including local changes, and if the checkout changes while planning is in progress the generated plan is discarded and the issue is requeued to `ready_for_plan` so the next plan run starts from the latest source checkout state. From implementation onward, Copilot runs with both its current working directory and `--add-dir` set to the isolated workspace path. Every Copilot phase also receives `--add-dir` for the per-issue artifact directory so agents can read prior artifacts and write the current phase artifact without `--allow-all-paths`. If the issue target repo points at a subdirectory inside a Git repository, the worktree is created at the Git root and Copilot runs in the corresponding subdirectory.

Worktrees are persistent by design. Validation, review, merge, and merge-conflict-resolution runs for the same issue require and reuse the implementation-created worktree instead of resetting it, so agent changes remain available until a human approves the merge. Successful implementation and merge-conflict-resolution iterations are committed in the isolated worktree before validation, which keeps review/rework loops visible in Git history. Snapshot commit subjects summarize the change, while commit bodies retain orchestration metadata (Issue, Phase, Run ID, Next Phase, and summary details). Workspace metadata is written to each issue's `workspace.json` artifact and is shown by `agent-team issue show` and the web issue detail page. Issue detail surfaces also show phase artifacts and logs, including merge-conflict-resolution reports when they exist. If a repo-backed issue is manually advanced to a post-implementation phase without workspace metadata, that phase blocks instead of creating a misleading new workspace.

Before first worktree creation, the source checkout must be clean because uncommitted or untracked files are not copied into a Git worktree. Commit or stash local changes before running the issue. Created worktrees are detached at the source `HEAD`; after review passes, use the web issue detail page's Open in VS Code button when workspace metadata exists or inspect the workspace path, then run `agent-team issue approve-merge <id>` to record human approval. The next merge run first prepares the reviewed work in the isolated worktree by creating a final safety-net commit when needed and merging the current target branch into the worktree to surface conflicts before any source checkout merge or remote push. If the target branch cannot be inferred from `workspace.json`, pass `--branch <name>` to `approve-merge`.

If `agent-team serve` is hosted in WSL and the web UI is opened from Windows, the Open in VS Code button emits a Remote-WSL URI when a distro name is available so Windows VS Code opens the isolated worktree through the WSL extension. Install the VS Code WSL extension on Windows for this link to work. The distro is detected from `WSL_DISTRO_NAME`; override it with `AGENT_TEAM_VSCODE_WSL_DISTRO=<distro>`, or set `AGENT_TEAM_VSCODE_WSL_DISTRO=` to restore local `vscode://file/...` links for POSIX workspace paths.

If the merge detects conflicts, it leaves the conflict markers in the isolated worktree, writes `merge.md`, and moves the issue to `ready_for_merge_conflict_resolution`. Run the next ready issue to let Copilot resolve the conflicts in that worktree. Conflict fixes write `merge_conflict_resolution.md`, then go back through validation, review, merge approval, and merge before the issue can close.

## Merge finalization and pull requests

After human merge approval, the deterministic `merge` phase finalizes the reviewed work in one of three modes:

- `auto` is the default. If the source repo has no remotes, it preserves the existing local `git merge --no-ff --log` behavior, writes `workspace.merged.json`, and removes the issue worktree after the merge succeeds. If the repo has a supported push remote, it pushes a deterministic branch and opens or reuses a hosted PR instead of merging the protected target branch locally. If remotes exist but none are supported, the issue blocks with guidance rather than silently falling back to a local merge.
- `local` always uses the existing local merge path, even when remotes exist.
- `pull-request` requires a supported remote and blocks when no supported PR provider can be used.

Use `agent-team issue approve-merge <id> --mode pull-request --remote origin` to force a specific PR remote, or use the web approval form's Finalization mode and PR remote controls. `AGENT_TEAM_MERGE_MODE` can set the default mode (`auto`, `local`, or `pull_request`), `AGENT_TEAM_PR_REMOTE` can set a default remote name, and `AGENT_TEAM_PR_BRANCH_PREFIX` defaults deterministic PR branches to `agent-team/issue-<id>`.

The first supported providers are GitHub and Azure DevOps Services. GitHub remotes are detected from common SSH and HTTPS forms such as `git@github.com:owner/repo.git` and `https://github.com/owner/repo.git`; PR creation uses the `gh` CLI and your existing `gh auth login` state. Azure DevOps Services remotes are detected from `dev.azure.com`, `visualstudio.com`, and `ssh.dev.azure.com:v3` repository URLs; PR creation uses `az repos pr ...`, so install and authenticate the Azure DevOps CLI extension before using that provider. Azure DevOps Server/on-prem and unknown providers are unsupported for PR mode.

PR finalization writes `pull_request.json` with provider, remote, source branch, target branch, head commit, PR URL/id/number, and cleanup metadata, and `merge.md` clearly records that the local issue was finalized by opening or reusing a PR. The orchestrator marks the local issue `done` after PR metadata is persisted and the worktree is removed; that means "PR opened by the orchestrator", not "the hosted PR was merged". Hosted PR review, status polling, and automatic PR completion remain outside this orchestrator.

## Unattended tool permissions

Copilot CLI can use read-only tools automatically, but write tools, non-read-only shell commands, URL access, and other potentially risky tools need approval. Because this orchestrator runs Copilot CLI with `--no-ask-user`, the default `AGENT_TEAM_COPILOT_PERMISSION_MODE=phase` supplies phase-specific approval policies instead of `--yolo` or `--allow-all-tools`.

Default phase policies approve narrowly read-only repository/artifact inspection commands for `research`, `plan`, and `review`; add explicit test/check commands such as `python3 -m unittest`, `pytest`, `npm test`, `npm run test`, and ecosystem test runners for `validation`; and add `write` only for `implementation` and `merge_conflict_resolution`, which operate in isolated worktrees. Validation intentionally excludes arbitrary interpreter and package-script approvals such as `python3:*`, `node:*`, and `npm run:*`; add a specific `AGENT_TEAM_COPILOT_ALLOW_TOOL` override when a repo needs another check command. Broad shell approvals for commands that can mutate in-place, such as `find:*` and `sed:*`, are intentionally excluded from read-only policies. Phase mode also denies clearly unsafe operations such as `git push`, `gh pr create`, `gh pr merge`, `rm -rf`, and `sudo`.

Research is the only phase that also allows web URL access by default (`--allow-url=https://*,http://*`) so the custom research agent can match Copilot CLI `/research` behavior with current documentation and GitHub/web-backed investigation. This does not grant write tools or broad path access, and the read-only source mutation guard still applies. Operators can narrow or block URL access for a run with the existing URL pass-through flags below, such as `AGENT_TEAM_COPILOT_DENY_URL`.

Set `AGENT_TEAM_COPILOT_PERMISSION_MODE=yolo` only as an explicit escape hatch. Yolo mode emits `--yolo` and skips the phase policy, while still adding the repo/workspace and per-issue artifact directories with `--add-dir`.

The runner passes these environment variables through to Copilot CLI:

| Environment variable | Copilot CLI flag |
|---|---|
| `AGENT_TEAM_COPILOT_PERMISSION_MODE=phase|yolo` | select least-privilege phase policies or explicit `--yolo` escape hatch |
| `AGENT_TEAM_COPILOT_AVAILABLE_TOOLS` | `--available-tools=...` |
| `AGENT_TEAM_COPILOT_EXCLUDED_TOOLS` | `--excluded-tools=...` |
| `AGENT_TEAM_COPILOT_ALLOW_TOOL` | `--allow-tool=...` |
| `AGENT_TEAM_COPILOT_DENY_TOOL` | `--deny-tool=...` |
| `AGENT_TEAM_COPILOT_ALLOW_URL` | `--allow-url=...` |
| `AGENT_TEAM_COPILOT_DENY_URL` | `--deny-url=...` |
| `AGENT_TEAM_COPILOT_ALLOW_ALL_TOOLS=true` | `--allow-all-tools` |
| `AGENT_TEAM_COPILOT_ALLOW_ALL_URLS=true` | `--allow-all-urls` |
| `AGENT_TEAM_COPILOT_PLUGIN_DIR` | override the bundled custom-agent plugin directory |
| `AGENT_TEAM_COPILOT_ARGS` | extra raw Copilot CLI args |

These advanced pass-throughs are appended after the default phase policy and can weaken the least-privilege defaults. Prefer additive approvals for a known missing command, for example:

```bash
export AGENT_TEAM_COPILOT_ALLOW_TOOL='shell(npm run lint),shell(npm run lint:*)'
export AGENT_TEAM_COPILOT_DENY_TOOL='shell(git push),shell(git push:*),shell(rm -rf:*)'
```

Avoid `--allow-all`/`--yolo` outside an isolated throwaway environment.

## CLI overview

```bash
agent-team init
agent-team issue create --repo /path/to/repo --description "..."
agent-team issue create --repo /path/to/repo --description "..." --ready
agent-team issue edit 1 --title "..." --description "..." --repo /path/to/repo
agent-team issue edit 1 --title ""
agent-team issue edit 1 --clear-repo --clear-tags
agent-team issue advance 1 --to needs_research --message "publish draft"
agent-team issue list
agent-team issue show 1
agent-team issue approve-plan 1
agent-team issue answer-human-input 1 --answer "Use option B and keep the existing API stable."
agent-team issue answer-human-input 1 --answer-file decision.txt
agent-team issue approve-merge 1
agent-team issue approve-merge 1 --branch main
agent-team issue approve-merge 1 --mode pull-request --remote origin
agent-team issue advance 1 --to ready_for_implementation
agent-team issue delete 1 --confirm "DELETE 1"
agent-team run --issue 1 --phase research
agent-team run --issue 1 --phase merge
agent-team serve --host 127.0.0.1 --port 8765 --web-workers 1 --worker-concurrency 3 --interval 60
agent-team worker once
agent-team worker once --concurrency 3
agent-team worker loop --interval 60
agent-team dashboard
agent-team web --host 127.0.0.1 --port 8765
```

`issue create --title` and `issue edit --title` are optional title overrides; edit with `--title ""` to regenerate the title from the description.

## Workflow

```text
draft -> needs_research -> ready_for_plan -> awaiting_plan_approval
  -> ready_for_implementation
  -> ready_for_validation -> ready_for_review -> awaiting_merge_approval
  -> ready_for_merge -> done
```

Validation and review may loop back to `ready_for_implementation`; merge conflicts may route to `ready_for_merge_conflict_resolution`, then back through validation and review; any agent phase may pause at `awaiting_human_input` and resume to its stored phase after `agent-team issue answer-human-input`; any phase may return `blocked`.

`draft` is a local backlog phase, not an agent phase. `agent-team issue create` defaults to `draft`; titles are generated from descriptions unless an optional CLI title override is provided. Use `agent-team issue edit <id>` for optional title overrides or regeneration, and use the CLI or web edit page to revise draft description, target repo, priority, and tags before publishing. Use `--ready` to create an immediately runnable issue or publish later with `agent-team issue advance <id> --to needs_research --message "publish draft"`.

When an issue is moved out of `blocked`, a non-empty manual transition message is saved as `unblock_context.md` and included in later agent prompts as user-provided context.

Issue deletion is irreversible and removes the issue row, runs, events, artifacts, logs, and issue workspaces. Use `agent-team issue delete <id> --confirm "DELETE <id>"` or the matching web UI control.

## Dashboard

Use the terminal dashboard to see work item progress:

```bash
agent-team dashboard
```

It shows:

- issue counts by status/phase
- currently active locks/runs
- issues awaiting human input
- draft backlog issues
- open issues with current phase
- recent runs
- recent events

## Web interface

Start the normal local control surface with:

```bash
agent-team serve --worker-concurrency 3
```

By default it listens on `http://127.0.0.1:8765` and uses the same `AGENT_TEAM_HOME` state directory as the CLI. `serve` starts both the browser UI and a continuous autonomous ready-queue worker loop. Use `--web-workers` to set how many explicitly queued browser actions can run, `--worker-concurrency` to set how many autonomous issue runs can run at once, and `--interval` to set the idle poll interval. The dashboard and `/api/dashboard` show the current runtime mode and these concurrency settings.

Use `agent-team web` when you want only the UI without the autonomous scheduler. Its `--web-workers` option controls queued browser actions triggered from the UI, such as "Run next ready issue"; the older `--workers` spelling remains as a compatibility alias. `--host` accepts loopback addresses by default for both `web` and `serve`; non-loopback binds require `--unsafe-allow-remote`, which has no built-in authentication and should only be used behind your own network/auth protections.

Default runtime settings can also be configured with `AGENT_TEAM_WEB_WORKERS`, `AGENT_TEAM_WORKER_CONCURRENCY`, and `AGENT_TEAM_WORKER_INTERVAL_SECONDS`.

The web UI is a manager cockpit for the local agent team. It live-refreshes dashboard and issue-detail state without a full page reload. The dashboard leads with manager decisions: active work, approval gates, human-input requests, blocked issues, drafts, ready work, recently finalized issues, and compact run activity. Recent events, phase counts, open issue dumps, and queued browser action history are still available in a collapsed diagnostics section.

Use the repo context selector in the header to focus the dashboard and issue list on one known target repo. Known repos are discovered from existing issue target repo paths. In a selected repo context, dashboard counts, lists, recent run/event diagnostics, queued browser action visibility, and the "Run next ready issue" action are scoped to that repo, and the create form pre-fills the target repo. Switch back to "All repos" to see aggregate data and create issues without an automatic repo prefill.

The web UI lets you:

- view manager-first dashboard cards for active work, approval gates, human input, blocked issues, draft backlog, ready work, recently finalized issues, and compact run activity
- create local issues with description, target repo, priority, and tags; generated titles appear in issue lists and detail pages; "Make runnable now" is checked by default to start in `needs_research`, and unchecking it keeps the issue as an editable draft
- edit local draft issues with description, target repo, priority, and tags before publishing
- list and filter issues, then open detail pages organized around status, next action, primary controls, workflow progress, evidence, collapsed diagnostics, advanced phase overrides, and a distinct danger zone
- queue a run for the next ready issue or for the current issue's next runnable phase
- answer human-input requests, approve plans, approve worktree finalization, and manually transition issues through the existing state-machine validation

Generic manual transitions into or out of `awaiting_human_input` are rejected so every pause has a structured request and the decision log cannot be bypassed. Use the issue page's answer form or `agent-team issue answer-human-input`; answering marks the pending request answered, records an event, updates `human_input.jsonl`/`human_input.md`, and moves the issue back to the stored resume phase.

The browser polls reserved read-only endpoints under `/api/` plus packaged static assets under `/static/app.js` and `/static/styles.css` for live updates and styling. Those endpoints are implementation details for the local UI and remain protected by the same host checks as the HTML pages; form-based mutations still use CSRF validation.

## Concurrent processing

More than one work item can be processed at a time:

```bash
agent-team serve --worker-concurrency 3 --interval 60 --web-workers 1
agent-team worker once --concurrency 3
agent-team worker loop --interval 60 --concurrency 3
```

`serve` is the recommended control surface: it keeps the web UI live while continuously draining ready work. `worker once` drains ready work until the queue is idle while keeping up to the configured number of issue runs active. When one run finishes, the worker immediately refills the freed slot with the next eligible issue instead of waiting for the rest of the current set to finish. `worker loop` repeats that same drain cycle and sleeps only after the ready queue is idle; it is useful for diagnostics or when you intentionally want a worker without the web UI.

Concurrency is bounded and protected by per-issue lease locks. Ready issue selection preserves priority ordering and rotates within each priority bucket based on the last successful scheduling time. Multiple workers may process different ready issues at the same time, but drafts are ignored by `serve`, `worker once`, `worker loop`, concurrent processing, and the web Run-next action until published. Only one worker can hold the lock for a given issue. Copilot CLI issues targeting the same original repo still receive distinct per-issue implementation worktrees, so concurrent implementation and later agent runs do not edit the same checkout. The deterministic merge phase is additionally serialized per source Git repository with a file lock, allowing same-repo issues to queue safely during merge while unrelated repos can still merge independently.

`--web-workers` controls explicitly queued browser actions such as "Run next ready issue" button clicks. `--worker-concurrency` controls autonomous ready-queue issue runs. If you run `agent-team serve` and a separate `agent-team worker loop` against the same `AGENT_TEAM_HOME`, their effective ready-queue concurrency is additive; leases prevent duplicate work on the same issue, but total load increases.

On `SIGINT` or `SIGTERM`, `serve` stops scheduling new batches, shuts down the HTTP server and queued browser actions, and waits for the current worker batch to drain. It does not force-cancel in-flight Copilot subprocesses. If the process is hard-killed, existing lock TTL recovery still applies.
