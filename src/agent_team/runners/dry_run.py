from __future__ import annotations

from agent_team.models import AgentResult, Issue
from agent_team.runners.base import AgentRunner
from agent_team.state_machine import default_next_phase


class DryRunRunner(AgentRunner):
    name = "dry-run"

    def run(self, phase: str, issue: Issue, context: dict[str, str]) -> AgentResult:
        next_phase = default_next_phase(phase)
        repo = issue.repo_path or "<no repo>"
        markdown = f"""# {phase.title()} Result

Dry-run runner completed the `{phase}` phase for issue `{issue.id}`.

## Issue

- Title: {issue.title}
- Repository: {repo}
- Current phase: {issue.phase}

## Simulated Output

This is deterministic placeholder output for orchestrator development and tests.
Replace the runner with `copilot-cli` when you are ready to invoke a real agent.

## Suggested next phase

`{next_phase}`
"""
        return AgentResult(
            status="success",
            summary=f"Dry-run {phase} completed for issue {issue.id}",
            artifact_markdown=markdown,
            suggested_next_phase=next_phase,
        )

