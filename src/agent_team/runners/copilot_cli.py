from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Mapping

from agent_team.blocked_summary import extract_blocked_summary, summarize_blocked_reason
from agent_team.artifacts import (
    HUMAN_INPUT_JSONL_ARTIFACT,
    HUMAN_INPUT_MARKDOWN_ARTIFACT,
    PLAN_FEEDBACK_ARTIFACT,
    PLAN_PRIOR_ARTIFACT,
    UNBLOCK_CONTEXT_ARTIFACT,
)
from agent_team.config import CopilotModelSelection
from agent_team.models import AgentResult, HumanInputRequestDraft, Issue
from agent_team.runners.base import AgentRunner
from agent_team.state_machine import default_next_phase, validate_human_input_resume_phase


PHASE_AGENTS = {
    "research": "agent-team-research",
    "plan": "agent-team-plan",
    "implementation": "agent-team-implementation",
    "validation": "agent-team-validation",
    "review": "agent-team-review",
    "merge_conflict_resolution": "agent-team-merge-conflict-resolution",
}

PHASE_RECOMMENDATIONS = {
    "research": {"ready_for_plan", "awaiting_human_input", "blocked"},
    "plan": {"ready_for_implementation", "awaiting_plan_approval", "awaiting_human_input", "blocked"},
    "implementation": {"ready_for_validation", "awaiting_human_input", "blocked"},
    "validation": {"ready_for_review", "ready_for_implementation", "awaiting_human_input", "blocked"},
    "review": {"awaiting_merge_approval", "done", "ready_for_implementation", "awaiting_human_input", "blocked"},
    "merge_conflict_resolution": {
        "ready_for_validation",
        "ready_for_implementation",
        "awaiting_human_input",
        "blocked",
    },
}


@dataclass(frozen=True)
class PhasePermissionPolicy:
    allow_tools: tuple[str, ...]
    deny_tools: tuple[str, ...] = ()
    allow_urls: tuple[str, ...] = ()
    deny_urls: tuple[str, ...] = ()


READ_ONLY_ALLOW_TOOLS = (
    "read",
    "shell(git status)",
    "shell(git status:*)",
    "shell(git diff)",
    # Covers read-only diff summaries such as `git diff --stat`.
    "shell(git diff:*)",
    "shell(git log)",
    "shell(git log:*)",
    "shell(git show)",
    "shell(git show:*)",
    "shell(git grep:*)",
    "shell(git ls-files)",
    "shell(git ls-files:*)",
    "shell(rg:*)",
    "shell(ls)",
    "shell(ls:*)",
    "shell(pwd)",
    "shell(head:*)",
    "shell(tail:*)",
    "shell(wc:*)",
    "shell(cat:*)",
    "shell(grep:*)",
)

VALIDATION_ALLOW_TOOLS = READ_ONLY_ALLOW_TOOLS + (
    "shell(python -m unittest)",
    "shell(python -m unittest:*)",
    "shell(python3 -m unittest)",
    "shell(python3 -m unittest:*)",
    "shell(python -m pytest)",
    "shell(python -m pytest:*)",
    "shell(python3 -m pytest)",
    "shell(python3 -m pytest:*)",
    "shell(pytest)",
    "shell(pytest:*)",
    "shell(npm test)",
    "shell(npm test:*)",
    "shell(npm run test)",
    "shell(npm run test:*)",
    "shell(go test)",
    "shell(go test:*)",
    "shell(dotnet test)",
    "shell(dotnet test:*)",
    "shell(cargo test)",
    "shell(cargo test:*)",
    "shell(make test)",
    "shell(make test:*)",
)

WORKTREE_CHECK_ALLOW_TOOLS = VALIDATION_ALLOW_TOOLS + (
    "shell(python:*)",
    "shell(python3:*)",
    "shell(npm run:*)",
    "shell(node:*)",
)

UNSAFE_DENY_TOOLS = (
    "shell(git push)",
    "shell(git push:*)",
    "shell(gh pr create:*)",
    "shell(gh pr merge:*)",
    "shell(rm -rf:*)",
    "shell(sudo:*)",
)

RESEARCH_ALLOW_URLS = (
    "https://*",
    "http://*",
)

PHASE_PERMISSION_POLICIES = {
    "research": PhasePermissionPolicy(
        READ_ONLY_ALLOW_TOOLS,
        UNSAFE_DENY_TOOLS,
        allow_urls=RESEARCH_ALLOW_URLS,
    ),
    "plan": PhasePermissionPolicy(READ_ONLY_ALLOW_TOOLS, UNSAFE_DENY_TOOLS),
    "implementation": PhasePermissionPolicy(WORKTREE_CHECK_ALLOW_TOOLS + ("write",), UNSAFE_DENY_TOOLS),
    "validation": PhasePermissionPolicy(VALIDATION_ALLOW_TOOLS, UNSAFE_DENY_TOOLS),
    "review": PhasePermissionPolicy(READ_ONLY_ALLOW_TOOLS, UNSAFE_DENY_TOOLS),
    "merge_conflict_resolution": PhasePermissionPolicy(WORKTREE_CHECK_ALLOW_TOOLS + ("write",), UNSAFE_DENY_TOOLS),
}

if set(PHASE_PERMISSION_POLICIES) != set(PHASE_AGENTS):
    missing = sorted(set(PHASE_AGENTS) - set(PHASE_PERMISSION_POLICIES))
    extra = sorted(set(PHASE_PERMISSION_POLICIES) - set(PHASE_AGENTS))
    raise RuntimeError(f"Phase permission policies must match phase agents; missing={missing}, extra={extra}")


@dataclass(frozen=True)
class _RecommendationDiagnostic:
    next_phase: str | None
    reason: str
    detected_value: str | None
    allowed_values: tuple[str, ...]


class CopilotCliRunner(AgentRunner):
    name = "copilot-cli"

    def __init__(
        self,
        command: str = "copilot",
        timeout_seconds: int = 1800,
        extra_args: tuple[str, ...] = (),
        plugin_dir: Path | None = None,
        permission_mode: str = "phase",
        model: str | None = None,
        reasoning_effort: str | None = None,
        phase_overrides: Mapping[str, CopilotModelSelection] | None = None,
    ) -> None:
        if permission_mode not in {"phase", "yolo"}:
            raise ValueError("permission_mode must be one of: phase, yolo")
        normalized_phase_overrides = dict(phase_overrides or {})
        invalid_phases = sorted(set(normalized_phase_overrides) - set(PHASE_AGENTS))
        if invalid_phases:
            valid_phases = ", ".join(sorted(PHASE_AGENTS))
            raise ValueError(
                "copilot.phase_overrides contains unsupported phase(s): "
                f"{', '.join(invalid_phases)}; expected one of: {valid_phases}"
            )
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.extra_args = extra_args
        self.permission_mode = permission_mode
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.phase_overrides = normalized_phase_overrides
        self.plugin_dir = (plugin_dir or self._default_plugin_dir()).expanduser()
        self.plugin_name = self._plugin_name(self.plugin_dir)

    def run(self, phase: str, issue: Issue, context: dict[str, str]) -> AgentResult:
        prompt = self._build_prompt(phase, issue, context)
        execution_repo_path, path_error = self._resolve_execution_repo_path(phase, issue, context)
        if path_error is not None:
            return AgentResult(
                status="blocked",
                summary=path_error,
                artifact_markdown=CopilotCliRunner._system_blocked_artifact(path_error),
                suggested_next_phase="blocked",
                error=path_error,
                blocked_summary=summarize_blocked_reason(path_error),
            )

        source_snapshot, source_guard_error = self._source_snapshot_for_read_only_phase(phase, context)
        if source_guard_error is not None:
            return AgentResult(
                status="blocked",
                summary=source_guard_error,
                artifact_markdown=CopilotCliRunner._system_blocked_artifact(source_guard_error),
                suggested_next_phase="blocked",
                error=source_guard_error,
                blocked_summary=summarize_blocked_reason(source_guard_error),
            )
        artifact_dir = self._artifact_dir_from_context(context)
        command = self._build_command(phase, prompt, execution_repo_path, artifact_dir)
        completed = self._run_command(command, execution_repo_path, context)
        output = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        artifact = self._artifact_markdown(output, context)
        mutation_error = self._source_mutation_error(phase, source_snapshot)
        if mutation_error is not None:
            if phase == "plan":
                return AgentResult(
                    status="requeued",
                    summary=mutation_error,
                    artifact_markdown=self._requeued_plan_artifact(mutation_error),
                    suggested_next_phase="ready_for_plan",
                    error=mutation_error,
                    raw_stdout=None if completed.logged else output,
                    raw_stderr=stderr,
                )
            return AgentResult(
                status="blocked",
                summary=mutation_error,
                artifact_markdown=self._blocked_artifact(artifact, mutation_error),
                suggested_next_phase="blocked",
                error=mutation_error,
                raw_stdout=None if completed.logged else output,
                raw_stderr=stderr,
                blocked_summary=summarize_blocked_reason(mutation_error),
            )
        if completed.returncode != 0:
            return AgentResult(
                status="failed",
                summary=f"Copilot CLI {phase} failed with exit code {completed.returncode}",
                artifact_markdown=artifact or "Copilot CLI failed without output.",
                suggested_next_phase="blocked",
                error=stderr or output or f"exit code {completed.returncode}",
                raw_stdout=None if completed.logged else output,
                raw_stderr=stderr,
                blocked_summary=summarize_blocked_reason(stderr or output or f"Copilot CLI {phase} failed."),
            )
        recommendation = self._recommendation_diagnostic(phase, artifact)
        recommended_next_phase = recommendation.next_phase
        if recommended_next_phase is None:
            message = self._recommendation_error_message(phase, recommendation)
            return AgentResult(
                status="blocked",
                summary=message,
                artifact_markdown=self._recommendation_blocked_artifact(artifact, message),
                suggested_next_phase="blocked",
                error=message,
                raw_stdout=None if completed.logged else output,
                raw_stderr=stderr,
                blocked_summary=summarize_blocked_reason(message),
            )
        if recommended_next_phase == "blocked":
            blocked_summary = self._blocked_summary_from_artifact(artifact)
            return AgentResult(
                status="blocked",
                summary=f"Copilot CLI {phase} recommended blocked for issue {issue.id}",
                artifact_markdown=artifact,
                suggested_next_phase="blocked",
                error=f"Copilot CLI {phase} recommended blocked",
                raw_stdout=None if completed.logged else output,
                raw_stderr=stderr,
                blocked_summary=blocked_summary,
            )
        return AgentResult(
            status="success",
            summary=f"Copilot CLI {phase} recommended {recommended_next_phase} for issue {issue.id}",
            artifact_markdown=artifact,
            suggested_next_phase=recommended_next_phase,
            raw_stdout=None if completed.logged else output,
            raw_stderr=stderr,
        )

    def _run_command(self, command: list[str], repo_path: Path | None, context: dict[str, str]) -> "_CompletedCopilotRun":
        log_path = context.get("run_log")
        if not log_path:
            completed = subprocess.run(
                command,
                cwd=str(repo_path) if repo_path is not None else None,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            return _CompletedCopilotRun(completed.returncode, completed.stdout or "", completed.stderr or "", False)

        return self._run_command_with_live_log(command, repo_path, Path(log_path))

    def _run_command_with_live_log(
        self,
        command: list[str],
        repo_path: Path | None,
        log_path: Path,
    ) -> "_CompletedCopilotRun":
        process = subprocess.Popen(
            command,
            cwd=str(repo_path) if repo_path is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        deadline = time.monotonic() + self.timeout_seconds
        output_parts: list[str] = []
        timed_out = False

        with log_path.open("a", encoding="utf-8") as log_handle:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    timed_out = True
                    process.kill()
                    break
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    chunk = self._read_available(fd)
                    if chunk:
                        output_parts.append(chunk)
                        log_handle.write(chunk)
                        log_handle.flush()
            while True:
                chunk = self._read_available(fd)
                if not chunk:
                    break
                output_parts.append(chunk)
                log_handle.write(chunk)
                log_handle.flush()
            log_handle.write(f"\n```\n\n- Finished: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")

        process.stdout.close()
        returncode = process.wait()
        output = "".join(output_parts)
        if timed_out:
            output += f"\nCopilot CLI timed out after {self.timeout_seconds} seconds."
            returncode = returncode if returncode != 0 else -1
        return _CompletedCopilotRun(returncode, output, "", True)

    @staticmethod
    def _read_available(fd: int) -> str:
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _build_command(
        self,
        phase: str,
        prompt: str,
        repo_path: Path | None,
        artifact_dir: Path | None = None,
    ) -> list[str]:
        command = [
            self.command,
            "--plugin-dir",
            str(self.plugin_dir),
            "--agent",
            self._agent_selector(PHASE_AGENTS[phase]),
            "-p",
            prompt,
            "--no-ask-user",
        ]
        command.extend(self._permission_args(phase))
        for add_dir in self._add_dirs(repo_path, artifact_dir):
            command.extend(["--add-dir", str(add_dir)])
        command.extend(self._model_args(phase))
        command.extend(self.extra_args)
        return command

    def _model_args(self, phase: str) -> list[str]:
        override = self.phase_overrides.get(phase)
        model = override.model if override is not None and override.model is not None else self.model
        reasoning_effort = (
            override.reasoning_effort
            if override is not None and override.reasoning_effort is not None
            else self.reasoning_effort
        )
        args: list[str] = []
        if model is not None:
            args.extend(["--model", model])
        if reasoning_effort is not None:
            args.extend(["--reasoning-effort", reasoning_effort])
        return args

    def _permission_args(self, phase: str) -> list[str]:
        if self.permission_mode == "yolo":
            return ["--yolo"]
        policy = PHASE_PERMISSION_POLICIES[phase]
        args: list[str] = []
        if policy.allow_tools:
            args.append(f"--allow-tool={','.join(policy.allow_tools)}")
        if policy.deny_tools:
            args.append(f"--deny-tool={','.join(policy.deny_tools)}")
        if policy.allow_urls:
            args.append(f"--allow-url={','.join(policy.allow_urls)}")
        if policy.deny_urls:
            args.append(f"--deny-url={','.join(policy.deny_urls)}")
        return args

    @staticmethod
    def _artifact_dir_from_context(context: dict[str, str]) -> Path | None:
        raw_artifact_dir = context.get("artifacts_dir", "").strip()
        if not raw_artifact_dir:
            return None
        return Path(raw_artifact_dir).expanduser()

    @staticmethod
    def _add_dirs(*paths: Path | None) -> list[Path]:
        add_dirs: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            if path is None:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            add_dirs.append(path)
        return add_dirs

    @staticmethod
    def _default_plugin_dir() -> Path:
        if hasattr(resources, "files"):
            return Path(str(resources.files("agent_team").joinpath("copilot_plugin")))
        return Path(__file__).resolve().parent.parent / "copilot_plugin"

    @staticmethod
    def _plugin_name(plugin_dir: Path) -> str | None:
        for relative_path in (
            "plugin.json",
            ".plugin/plugin.json",
            ".github/plugin/plugin.json",
            ".claude-plugin/plugin.json",
        ):
            manifest_path = plugin_dir / relative_path
            if not manifest_path.is_file():
                continue
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            name = data.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _agent_selector(self, agent_id: str) -> str:
        if self.plugin_name:
            return f"{self.plugin_name}:{agent_id}"
        return agent_id

    @staticmethod
    def _resolve_execution_repo_path(phase: str, issue: Issue, context: dict[str, str]) -> tuple[Path | None, str | None]:
        if not issue.repo_path and not context.get("source_repo_path", "").strip() and not context.get(
            "workspace_repo_path", ""
        ).strip():
            return None, f"Target repo path is required for Copilot CLI {phase} phase."
        if phase in {"research", "plan"}:
            raw_source_path = context.get("source_repo_path", "").strip()
            if raw_source_path:
                return CopilotCliRunner._validated_directory(
                    raw_source_path,
                    f"Source repo path does not exist or is not a directory: {raw_source_path}",
                )
            raw_workspace_path = context.get("workspace_repo_path", "").strip()
            if raw_workspace_path:
                return CopilotCliRunner._validated_directory(
                    raw_workspace_path,
                    f"Isolated workspace path does not exist or is not a directory: {raw_workspace_path}",
                )
            return None, None

        raw_path = context.get("workspace_repo_path", "").strip()
        if issue.repo_path and not raw_path:
            return None, f"Isolated workspace path was not provided for target repo: {issue.repo_path}"
        if not raw_path:
            return None, None
        return CopilotCliRunner._validated_directory(
            raw_path,
            f"Isolated workspace path does not exist or is not a directory: {raw_path}",
        )

    @staticmethod
    def _validated_directory(raw_path: str, error_message: str) -> tuple[Path | None, str | None]:
        path = Path(raw_path).expanduser()
        if not path.is_dir():
            return None, error_message
        return path.resolve(), None

    @staticmethod
    def _source_snapshot(phase: str, context: dict[str, str]) -> "_SourceSnapshot | None":
        snapshot, _ = CopilotCliRunner._source_snapshot_for_read_only_phase(phase, context)
        return snapshot

    @staticmethod
    def _source_snapshot_for_read_only_phase(
        phase: str, context: dict[str, str]
    ) -> "tuple[_SourceSnapshot | None, str | None]":
        if phase not in {"research", "plan"}:
            return None, None
        raw_path = context.get("source_repo_path", "").strip()
        if not raw_path:
            return None, None
        path = Path(raw_path).expanduser()
        if not path.is_dir():
            return None, f"Source repo path does not exist or is not a directory: {raw_path}"
        root = CopilotCliRunner._git_output(path, "rev-parse", "--show-toplevel")
        if root is None:
            return None, (
                f"Source repo could not be inspected before read-only {phase} phase: {path.resolve()}. "
                "Ensure it is a Git checkout with at least one commit before rerunning."
            )
        root_path = Path(root).resolve()
        head = CopilotCliRunner._git_output(root_path, "rev-parse", "HEAD")
        status = CopilotCliRunner._git_output(root_path, "status", "--porcelain")
        if head is None or status is None:
            return None, (
                f"Source repo could not be inspected before read-only {phase} phase: {root_path}. "
                "Inspect the checkout before rerunning."
            )
        if phase == "research" and status:
            return None, (
                f"Source repo must be clean before read-only {phase} phase: {root_path}. "
                "Commit, stash, or restore local changes before rerunning."
            )
        content_fingerprint = CopilotCliRunner._source_content_fingerprint(root_path)
        if content_fingerprint is None:
            return None, (
                f"Source repo could not be inspected before read-only {phase} phase: {root_path}. "
                "Inspect the checkout before rerunning."
            )
        return _SourceSnapshot(root_path, head, status, content_fingerprint), None

    @staticmethod
    def _source_mutation_error(phase: str, snapshot: "_SourceSnapshot | None") -> str | None:
        if snapshot is None:
            return None
        head = CopilotCliRunner._git_output(snapshot.repo_root, "rev-parse", "HEAD")
        status = CopilotCliRunner._git_output(snapshot.repo_root, "status", "--porcelain")
        content_fingerprint = CopilotCliRunner._source_content_fingerprint(snapshot.repo_root)
        if head is None or status is None or content_fingerprint is None:
            return (
                f"Source repo could not be inspected after read-only {phase} phase: {snapshot.repo_root}. "
                "Inspect the checkout before rerunning."
            )
        if (
            head != snapshot.head
            or status != snapshot.status
            or content_fingerprint != snapshot.content_fingerprint
        ):
            return (
                f"Source repo changed during read-only {phase} phase: {snapshot.repo_root}. "
                "Inspect or restore the checkout before rerunning."
            )
        return None

    @staticmethod
    def _source_content_fingerprint(repo_path: Path) -> str | None:
        diff_cached = CopilotCliRunner._git_bytes(repo_path, "diff", "--no-ext-diff", "--binary", "--cached", "--")
        diff_worktree = CopilotCliRunner._git_bytes(repo_path, "diff", "--no-ext-diff", "--binary", "--")
        untracked_files = CopilotCliRunner._git_bytes(
            repo_path, "ls-files", "--others", "--exclude-standard", "-z"
        )
        if diff_cached is None or diff_worktree is None or untracked_files is None:
            return None
        digest = hashlib.sha256()
        for label, payload in (("cached", diff_cached), ("worktree", diff_worktree)):
            digest.update(label.encode("ascii"))
            digest.update(b"\0")
            digest.update(str(len(payload)).encode("ascii"))
            digest.update(b"\0")
            digest.update(payload)
            digest.update(b"\0")
        for raw_relative_path in sorted(path for path in untracked_files.split(b"\0") if path):
            relative_path = os.fsdecode(raw_relative_path)
            path = repo_path / relative_path
            digest.update(b"untracked\0")
            digest.update(raw_relative_path)
            digest.update(b"\0")
            try:
                stat_result = path.lstat()
                digest.update(oct(stat_result.st_mode & 0o7777).encode("ascii"))
                digest.update(b"\0")
                if path.is_symlink():
                    digest.update(b"symlink\0")
                    digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
                elif path.is_file():
                    digest.update(b"file\0")
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                else:
                    digest.update(b"other\0")
            except OSError:
                return None
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _git_output(repo_path: Path, *args: str) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo_path), *args],
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

    @staticmethod
    def _git_bytes(repo_path: Path, *args: str) -> bytes | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(repo_path), *args],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    @staticmethod
    def _blocked_artifact(existing_artifact: str, message: str) -> str:
        prefix = existing_artifact.rstrip()
        blocked = CopilotCliRunner._system_blocked_artifact(message)
        return f"{prefix}\n\n{blocked}" if prefix else blocked

    @staticmethod
    def _system_blocked_artifact(message: str) -> str:
        return (
            f"{message}\n\n"
            f"Blocked summary: {summarize_blocked_reason(message)}\n"
            "Recommendation: `blocked`"
        )

    @staticmethod
    def _recommendation_blocked_artifact(existing_artifact: str, message: str) -> str:
        diagnostic = (
            "## Orchestrator diagnostic\n\n"
            "Copilot CLI completed successfully, but the phase artifact could not be routed "
            "because it did not include exactly one phase-allowed final Recommendation.\n\n"
            f"{message}\n\n"
            f"Blocked summary: {summarize_blocked_reason(message)}\n"
            "Recommendation: `blocked`"
        )
        prefix = existing_artifact.rstrip()
        if not prefix:
            return diagnostic
        return f"{prefix}\n\n---\n\n{diagnostic}"

    @staticmethod
    def _requeued_plan_artifact(message: str) -> str:
        return (
            "# Plan discarded and requeued\n\n"
            "The generated plan was discarded because the source repository changed during planning. "
            "The issue was requeued so the next plan run starts from the current source checkout state.\n\n"
            f"{message}\n\n"
            "Recommendation: `ready_for_plan`"
        )

    @staticmethod
    def _recommended_next_phase(phase: str, artifact_markdown: str) -> str | None:
        return CopilotCliRunner._recommendation_diagnostic(phase, artifact_markdown).next_phase

    @staticmethod
    def _recommendation_diagnostic(phase: str, artifact_markdown: str) -> _RecommendationDiagnostic:
        allowed = tuple(sorted(PHASE_RECOMMENDATIONS[phase]))
        allowed_pattern = re.compile(rf"`?\b({'|'.join(re.escape(value) for value in allowed)})\b`?", re.IGNORECASE)
        lines = artifact_markdown.splitlines()
        for index in range(len(lines) - 1, -1, -1):
            if "recommendation" not in lines[index].lower():
                continue
            window = "\n".join(lines[index : min(len(lines), index + 4)])
            match = allowed_pattern.search(window)
            if match is None:
                continue
            recommendation = match.group(1).lower()
            if phase == "plan" and recommendation == "ready_for_implementation":
                recommendation = default_next_phase(phase)
            elif phase == "review" and recommendation == "done":
                recommendation = default_next_phase(phase)
            return _RecommendationDiagnostic(recommendation, "valid", match.group(1), allowed)

        if allowed_pattern.search(artifact_markdown) is None:
            detected_value = CopilotCliRunner._detected_invalid_recommendation_value(lines)
            if detected_value is not None:
                return _RecommendationDiagnostic(None, "invalid", detected_value, allowed)
        return _RecommendationDiagnostic(None, "missing", None, allowed)

    @staticmethod
    def _blocked_summary_from_artifact(artifact_markdown: str) -> str | None:
        return extract_blocked_summary(artifact_markdown)

    @staticmethod
    def _detected_invalid_recommendation_value(lines: list[str]) -> str | None:
        for index in range(len(lines) - 1, -1, -1):
            label_value = CopilotCliRunner._recommendation_label_value(lines[index])
            if label_value is None:
                continue
            candidates = [label_value]
            if not label_value.strip():
                candidates.extend(lines[index + 1 : min(len(lines), index + 4)])
            for candidate in candidates:
                token = CopilotCliRunner._recommendation_value_token(candidate)
                if token:
                    return token.lower()
        return None

    @staticmethod
    def _human_input_request_from_artifact(phase: str, artifact_markdown: str) -> HumanInputRequestDraft:
        sections = CopilotCliRunner._human_input_sections(artifact_markdown)
        if len(sections) != 1:
            raise ValueError(
                "Expected exactly one '## Human input request' section when recommending awaiting_human_input"
            )
        fields = CopilotCliRunner._human_input_fields(sections[0])
        required = ("requested_by_phase", "resume_phase", "question", "rationale", "requested_decision")
        missing = [field for field in required if not _joined_field(fields, field)]
        if missing:
            raise ValueError(f"Human input request is missing required field(s): {', '.join(missing)}")
        requested_by_phase = _strip_inline_markdown(_joined_field(fields, "requested_by_phase"))
        resume_phase = _strip_inline_markdown(_joined_field(fields, "resume_phase"))
        if requested_by_phase != phase:
            raise ValueError(
                f"Human input request phase {requested_by_phase!r} does not match current phase {phase!r}"
            )
        validate_human_input_resume_phase(requested_by_phase, resume_phase)
        options = tuple(_strip_inline_markdown(value) for value in fields.get("options", ()) if value.strip())
        return HumanInputRequestDraft(
            requested_by_phase=requested_by_phase,
            resume_phase=resume_phase,
            question=_joined_field(fields, "question"),
            rationale=_joined_field(fields, "rationale"),
            requested_decision=_joined_field(fields, "requested_decision"),
            options=options,
            context=_joined_field(fields, "context") or None,
        )

    @staticmethod
    def _human_input_sections(artifact_markdown: str) -> list[str]:
        lines = artifact_markdown.splitlines()
        starts = [
            index
            for index, line in enumerate(lines)
            if re.match(r"^\s*#{2,6}\s+Human input request\s*$", line, re.IGNORECASE)
        ]
        sections: list[str] = []
        for start in starts:
            end = len(lines)
            for index in range(start + 1, len(lines)):
                if re.match(r"^\s*#{1,6}\s+\S", lines[index]) or (
                    CopilotCliRunner._recommendation_label_value(lines[index]) is not None
                ):
                    end = index
                    break
            sections.append("\n".join(lines[start + 1 : end]))
        return sections

    @staticmethod
    def _human_input_fields(section: str) -> dict[str, list[str]]:
        aliases = {
            "requested by phase": "requested_by_phase",
            "resume phase": "resume_phase",
            "question": "question",
            "rationale": "rationale",
            "why this requires a human": "rationale",
            "requested decision": "requested_decision",
            "options": "options",
            "context": "context",
        }
        pattern = re.compile(
            r"^(?:[-*]\s*)?"
            r"(Requested by phase|Resume phase|Question|Rationale|Why this requires a human|Requested decision|Options|Context)"
            r"\s*:\s*(.*)$",
            re.IGNORECASE,
        )
        fields: dict[str, list[str]] = {}
        current_key: str | None = None
        for raw_line in section.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            if CopilotCliRunner._recommendation_label_value(stripped) is not None:
                break
            match = pattern.match(stripped)
            if match is not None:
                key = aliases[match.group(1).lower()]
                if key in fields and key != "options":
                    raise ValueError(f"Human input request field {match.group(1)!r} appears more than once")
                fields.setdefault(key, [])
                value = _strip_inline_markdown(match.group(2).strip())
                if value:
                    fields[key].append(value)
                current_key = key
                continue
            if current_key is None:
                continue
            continuation = stripped
            if current_key == "options":
                continuation = re.sub(r"^(?:[-*]\s*)", "", continuation).strip()
            if continuation:
                fields.setdefault(current_key, []).append(_strip_inline_markdown(continuation))
        return fields

    @staticmethod
    def _recommendation_label_value(line: str) -> str | None:
        stripped = line.strip()
        stripped = re.sub(r"^(?:>+\s*)?(?:#{1,6}\s*)?(?:[-*+]\s*)?(?:\d+[.)]\s*)?", "", stripped)
        match = re.match(
            r"^(?:[*_`]+)?\s*recommendation\s*(?:[*_`]+)?\s*(?::\s*(.*)|\s*)$",
            stripped,
            re.IGNORECASE,
        )
        if match is None:
            return None
        return match.group(1) or ""

    @staticmethod
    def _recommendation_value_token(text: str) -> str | None:
        match = re.search(r"[\s>*_`\"'(\[]*([A-Za-z][A-Za-z0-9_-]*)", text.strip())
        if match is None:
            return None
        return match.group(1)

    @staticmethod
    def _recommendation_error_message(phase: str, diagnostic: _RecommendationDiagnostic) -> str:
        allowed_values = ", ".join(diagnostic.allowed_values)
        if diagnostic.reason == "invalid" and diagnostic.detected_value:
            return (
                f"Copilot CLI {phase} provided invalid Recommendation '{diagnostic.detected_value}'; "
                f"expected one of: {allowed_values}"
            )
        return f"Copilot CLI {phase} did not provide a valid Recommendation; expected one of: {allowed_values}"

    @staticmethod
    def _artifact_markdown(stdout: str, context: dict[str, str]) -> str:
        phase_artifact = context.get("phase_artifact")
        if phase_artifact:
            path = Path(phase_artifact)
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    return CopilotCliRunner._strip_run_header(content)
        return CopilotCliRunner._extract_final_answer(stdout)

    @staticmethod
    def _extract_final_answer(stdout: str) -> str:
        lines = stdout.strip().splitlines()
        for index in range(len(lines) - 1, -1, -1):
            line = lines[index].strip()
            if line.lower().startswith(("## 1.", "# 1.", "1. ")):
                return "\n".join(lines[index:]).strip()
        return stdout.strip()

    @staticmethod
    def _strip_run_header(content: str) -> str:
        return re.sub(r"\A<!-- run_id: .*? -->\s*", "", content, count=1, flags=re.DOTALL).strip()

    @staticmethod
    def _optional_artifact_text(path_text: str, max_chars: int = 20_000) -> str:
        if not path_text:
            return ""
        path = Path(path_text)
        if not path.is_file():
            return ""
        content = path.read_text(encoding="utf-8").strip()
        if len(content) <= max_chars:
            return content
        return content[:max_chars] + "\n\n[truncated]"

    @staticmethod
    def _build_prompt(phase: str, issue: Issue, context: dict[str, str]) -> str:
        template = context.get("prompt_template", "")
        artifacts_dir = context.get("artifacts_dir", "")
        phase_artifact = context.get("phase_artifact", "")
        plan_feedback_artifact = f"{artifacts_dir}/{PLAN_FEEDBACK_ARTIFACT}" if artifacts_dir else ""
        plan_prior_artifact = f"{artifacts_dir}/{PLAN_PRIOR_ARTIFACT}" if artifacts_dir else ""
        human_input_jsonl_artifact = f"{artifacts_dir}/{HUMAN_INPUT_JSONL_ARTIFACT}" if artifacts_dir else ""
        human_input_artifact = f"{artifacts_dir}/{HUMAN_INPUT_MARKDOWN_ARTIFACT}" if artifacts_dir else ""
        unblock_context_artifact = f"{artifacts_dir}/{UNBLOCK_CONTEXT_ARTIFACT}" if artifacts_dir else ""
        review_artifact = f"{artifacts_dir}/review.md" if artifacts_dir else ""
        merge_conflict_resolution_artifact = (
            f"{artifacts_dir}/merge_conflict_resolution.md" if artifacts_dir else ""
        )
        rendered = template.format(
            issue_id=issue.id,
            title=issue.title,
            description=issue.description,
            repo_path=issue.repo_path or "",
            source_repo_path=context.get("source_repo_path", issue.repo_path or ""),
            workspace_repo_path=context.get("workspace_repo_path", ""),
            workspace_root=context.get("workspace_root", ""),
            phase=phase,
            artifacts_dir=artifacts_dir,
            phase_artifact=phase_artifact,
            research_artifact=f"{artifacts_dir}/research.md" if artifacts_dir else "",
            plan_artifact=f"{artifacts_dir}/plan.md" if artifacts_dir else "",
            plan_feedback_artifact=plan_feedback_artifact,
            plan_prior_artifact=plan_prior_artifact,
            plan_rejection_feedback=CopilotCliRunner._optional_artifact_text(plan_feedback_artifact),
            human_input_jsonl_artifact=human_input_jsonl_artifact,
            human_input_artifact=human_input_artifact,
            human_input_context=CopilotCliRunner._optional_artifact_text(human_input_artifact, max_chars=12_000),
            unblock_context_artifact=unblock_context_artifact,
            unblock_context=CopilotCliRunner._optional_artifact_text(unblock_context_artifact, max_chars=12_000),
            implementation_artifact=f"{artifacts_dir}/implementation.md" if artifacts_dir else "",
            validation_artifact=f"{artifacts_dir}/validation.md" if artifacts_dir else "",
            review_artifact=review_artifact,
            review_feedback=CopilotCliRunner._optional_artifact_text(review_artifact),
            merge_artifact=f"{artifacts_dir}/merge.md" if artifacts_dir else "",
            merge_conflict_resolution_artifact=merge_conflict_resolution_artifact,
        )
        return rendered


def _joined_field(fields: dict[str, list[str]], key: str) -> str:
    return "\n".join(value for value in fields.get(key, ()) if value.strip()).strip()


def _strip_inline_markdown(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned.startswith("`") and cleaned.endswith("`"):
        cleaned = cleaned[1:-1].strip()
    return cleaned


class _CompletedCopilotRun:
    def __init__(self, returncode: int, stdout: str, stderr: str, logged: bool) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.logged = logged


@dataclass(frozen=True)
class _SourceSnapshot:
    repo_root: Path
    head: str
    status: str
    content_fingerprint: str
