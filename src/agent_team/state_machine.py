from __future__ import annotations

READY_PHASES = {
    "needs_research": "research",
    "ready_for_plan": "plan",
    "ready_for_implementation": "implementation",
    "ready_for_validation": "validation",
    "ready_for_review": "review",
    "ready_for_merge": "merge",
    "ready_for_merge_conflict_resolution": "merge_conflict_resolution",
}

HUMAN_INPUT_RESUME_PHASES_BY_AGENT = {
    "research": "needs_research",
    "plan": "ready_for_plan",
    "implementation": "ready_for_implementation",
    "validation": "ready_for_validation",
    "review": "ready_for_review",
    "merge_conflict_resolution": "ready_for_merge_conflict_resolution",
}

RUNNING_PHASES = {
    "research": "researching",
    "plan": "planning",
    "implementation": "implementing",
    "validation": "validating",
    "review": "reviewing",
    "merge": "merging",
    "merge_conflict_resolution": "resolving_merge_conflicts",
}

_READY_PHASES_BY_AGENT = {agent_phase: ready_phase for ready_phase, agent_phase in READY_PHASES.items()}
_AGENT_PHASES_BY_RUNNING = {running_phase: agent_phase for agent_phase, running_phase in RUNNING_PHASES.items()}
_READY_PHASES_BY_RUNNING = {
    running_phase: _READY_PHASES_BY_AGENT[agent_phase]
    for running_phase, agent_phase in _AGENT_PHASES_BY_RUNNING.items()
}

VALID_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"needs_research"},
    "needs_research": {"researching", "blocked"},
    "researching": {"ready_for_plan", "awaiting_human_input", "blocked"},
    "ready_for_plan": {"planning", "blocked"},
    "planning": {"awaiting_plan_approval", "ready_for_plan", "awaiting_human_input", "blocked"},
    "awaiting_plan_approval": {"ready_for_implementation", "ready_for_plan", "blocked"},
    "ready_for_implementation": {"implementing", "blocked"},
    "implementing": {"ready_for_validation", "awaiting_human_input", "blocked"},
    "ready_for_validation": {"validating", "blocked"},
    "validating": {"ready_for_review", "ready_for_implementation", "awaiting_human_input", "blocked"},
    "ready_for_review": {"reviewing", "blocked"},
    "reviewing": {
        "awaiting_merge_approval",
        "ready_for_implementation",
        "ready_for_merge_conflict_resolution",
        "awaiting_human_input",
        "blocked",
    },
    "awaiting_merge_approval": {"ready_for_merge", "ready_for_review", "ready_for_implementation", "blocked"},
    "ready_for_merge": {"merging", "blocked"},
    "merging": {"done", "awaiting_pr_closure", "ready_for_merge_conflict_resolution", "blocked"},
    "awaiting_pr_closure": {"done", "ready_for_merge_conflict_resolution", "ready_for_validation", "blocked"},
    "ready_for_merge_conflict_resolution": {"resolving_merge_conflicts", "blocked"},
    "resolving_merge_conflicts": {"ready_for_validation", "ready_for_implementation", "awaiting_human_input", "blocked"},
    "awaiting_human_input": {
        "needs_research",
        "ready_for_plan",
        "ready_for_implementation",
        "ready_for_validation",
        "ready_for_review",
        "ready_for_merge_conflict_resolution",
        "blocked",
    },
    "blocked": {
        "needs_research",
        "ready_for_plan",
        "awaiting_plan_approval",
        "ready_for_implementation",
        "ready_for_validation",
        "ready_for_review",
        "awaiting_merge_approval",
        "awaiting_pr_closure",
        "ready_for_merge",
        "ready_for_merge_conflict_resolution",
    },
    "done": set(),
}

DEFAULT_NEXT_PHASE: dict[str, str] = {
    "research": "ready_for_plan",
    "plan": "awaiting_plan_approval",
    "implementation": "ready_for_validation",
    "validation": "ready_for_review",
    "review": "awaiting_merge_approval",
    "merge": "done",
    "merge_conflict_resolution": "ready_for_validation",
}

TERMINAL_PHASES = {"done", "blocked"}


def validate_transition(current: str, next_phase: str) -> None:
    allowed = VALID_TRANSITIONS.get(current)
    if allowed is None:
        raise ValueError(f"Unknown phase: {current}")
    if next_phase not in allowed:
        allowed_display = ", ".join(sorted(allowed)) or "<none>"
        raise ValueError(f"Invalid transition {current!r} -> {next_phase!r}; allowed: {allowed_display}")


def allowed_transitions(current: str) -> tuple[str, ...]:
    allowed = VALID_TRANSITIONS.get(current)
    if allowed is None:
        raise ValueError(f"Unknown phase: {current}")
    return tuple(sorted(allowed))


def runnable_phase_for(issue_phase: str) -> str | None:
    return READY_PHASES.get(issue_phase)


def ready_phase_for_agent_phase(agent_phase: str) -> str | None:
    return _READY_PHASES_BY_AGENT.get(agent_phase)


def is_running_phase(issue_phase: str) -> bool:
    return issue_phase in _AGENT_PHASES_BY_RUNNING


def agent_phase_for_running_phase(issue_phase: str) -> str | None:
    return _AGENT_PHASES_BY_RUNNING.get(issue_phase)


def ready_phase_for_running_phase(issue_phase: str) -> str | None:
    return _READY_PHASES_BY_RUNNING.get(issue_phase)


def running_phase_for(agent_phase: str) -> str:
    try:
        return RUNNING_PHASES[agent_phase]
    except KeyError as exc:
        raise ValueError(f"Unknown agent phase: {agent_phase}") from exc


def default_next_phase(agent_phase: str) -> str:
    try:
        return DEFAULT_NEXT_PHASE[agent_phase]
    except KeyError as exc:
        raise ValueError(f"Unknown agent phase: {agent_phase}") from exc


def human_input_resume_phase_for_agent_phase(agent_phase: str) -> str:
    try:
        return HUMAN_INPUT_RESUME_PHASES_BY_AGENT[agent_phase]
    except KeyError as exc:
        raise ValueError(f"Human input is not supported for agent phase: {agent_phase}") from exc


def validate_human_input_resume_phase(requested_by_phase: str, resume_phase: str) -> None:
    expected = human_input_resume_phase_for_agent_phase(requested_by_phase)
    if resume_phase != expected:
        raise ValueError(
            f"Invalid human-input resume phase {resume_phase!r} for {requested_by_phase!r}; expected {expected!r}"
        )
    validate_transition("awaiting_human_input", resume_phase)
