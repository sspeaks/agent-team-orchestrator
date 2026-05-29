from __future__ import annotations

import re


MAX_BLOCKED_SUMMARY_CHARS = 280
BLOCKED_SUMMARY_FALLBACK = (
    "Blocked because no clear reason was recorded. Review the latest run, logs, and artifacts to decide how to resume."
)

_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]+\)")
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?](?=\s|$)")


def summarize_blocked_reason(text: str | None, *, limit: int = MAX_BLOCKED_SUMMARY_CHARS) -> str:
    """Return a short, display-safe blocked reason from verbose agent or system text."""
    cleaned = _clean_text(text or "")
    if not cleaned:
        return BLOCKED_SUMMARY_FALLBACK
    summary = _first_sentences(cleaned, max_sentences=2)
    return _truncate(summary, limit) or BLOCKED_SUMMARY_FALLBACK


def _clean_text(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if _is_noise_line(line):
            continue
        stripped = _strip_markdown(line)
        stripped = _strip_known_label(stripped)
        if not stripped or _is_noise_line(stripped):
            continue
        lines.append(stripped)

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))

    return re.sub(r"\s+", " ", " ".join(paragraphs)).strip()


def _strip_markdown(line: str) -> str:
    line = re.sub(r"^>+\s*", "", line).strip()
    line = re.sub(r"^#{1,6}\s*", "", line).strip()
    line = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
    line = re.sub(r"^\[[ xX]\]\s+", "", line).strip()
    line = _LINK_RE.sub(r"\1", line)
    line = line.replace("`", "")
    line = re.sub(r"(?<!\w)(?:\*\*|__)(.+?)(?:\*\*|__)(?!\w)", r"\1", line)
    line = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", line)
    line = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", line)
    return line.strip()


def _strip_known_label(line: str) -> str:
    return re.sub(r"^(?:blocked\s+summary|blocked\s+reason|blocker|summary)\s*:\s*", "", line, flags=re.I).strip()


def _is_noise_line(line: str) -> bool:
    lowered = line.lower().strip()
    heading = line.lstrip("#").strip().lstrip("0123456789. )").strip().lower().strip("*_")
    if not lowered:
        return True
    if lowered.startswith("<!--"):
        return True
    if lowered.startswith(("recommendation:", "**recommendation:", "final recommendation:")):
        return True
    if heading.startswith("recommendation:") or heading == "recommendation":
        return True
    if heading in {
        "executive summary",
        "summary",
        "blocked reason",
        "blocker",
        "summary of changes",
        "files changed",
        "tests/checks run",
        "deviations from the plan",
        "remaining risks",
        "human input request",
    }:
        return True
    if lowered.startswith("traceback (most recent call last):"):
        return True
    if re.match(r'^\s*file "[^"]+", line \d+', lowered):
        return True
    if re.match(r"^at\s+\S+\s+\(.+:\d+:\d+\)$", lowered):
        return True
    return False


def _first_sentences(text: str, *, max_sentences: int) -> str:
    sentences = [match.group(0).strip() for match in _SENTENCE_RE.finditer(text)]
    if sentences:
        return " ".join(sentences[:max_sentences]).strip()
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    cleaned = text.strip()
    if limit <= 0:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    shortened = cleaned[: max(0, limit - 3)].rstrip()
    boundary = shortened.rfind(" ")
    if boundary > limit // 2:
        shortened = shortened[:boundary].rstrip()
    return f"{shortened}..."
