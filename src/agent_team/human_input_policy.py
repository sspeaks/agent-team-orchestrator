from __future__ import annotations

DEFAULT_HUMAN_INPUT_MODE = "balanced"
HUMAN_INPUT_MODES = ("autonomous", "balanced", "eager")


def normalize_human_input_mode(raw_value: str, setting_name: str = "human_input.mode") -> str:
    value = raw_value.strip().lower()
    if value not in HUMAN_INPUT_MODES:
        allowed = ", ".join(HUMAN_INPUT_MODES)
        raise ValueError(f"{setting_name} must be one of: {allowed}")
    return value


def human_input_policy_prompt(mode: str) -> str:
    normalized = normalize_human_input_mode(mode)
    mode_text = {
        "autonomous": (
            "Ask only when an autonomous assumption would be unsafe or would block correctness, "
            "safety, scope, data handling, or the phase's ability to proceed."
        ),
        "balanced": (
            "Ask when two or more viable options materially affect architecture, public API or CLI "
            "behavior, data handling, security or safety, operational behavior, or user workflow; "
            "otherwise make a reasonable assumption and document it."
        ),
        "eager": (
            "Ask earlier for nontrivial design or product tradeoffs before committing to an approach, "
            "while still making routine engineering decisions autonomously."
        ),
    }
    return (
        f"Active mode: `{normalized}`.\n"
        f"{mode_text[normalized]}\n"
        "Human input is for structured manager decisions, not routine clarifications. Do not ask for "
        "facts available from the repo, docs, tests, or local investigation; low-impact style preferences; "
        "or decisions that can safely wait for plan or merge approval. When asking, recommend "
        "`awaiting_human_input` and include exactly one `## Human input request` section."
    )
