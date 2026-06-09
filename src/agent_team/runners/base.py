from __future__ import annotations

from abc import ABC, abstractmethod

from agent_team.models import AgentResult, Issue


class AgentRunner(ABC):
    name: str

    @abstractmethod
    def run(self, phase: str, issue: Issue, context: dict[str, str]) -> AgentResult:
        raise NotImplementedError

    def cancel_run(self, run_id: str, reason: str) -> bool:
        return False
