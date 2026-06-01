from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    home: Path
    db_path: Path
    artifacts_dir: Path
    worktrees_dir: Path
    locks_dir: Path | None = None
    runner: str = "copilot-cli"
    copilot_command: str = "copilot"
    copilot_args: tuple[str, ...] = ()
    copilot_permission_mode: str = "phase"
    copilot_plugin_dir: Path | None = None
    runner_timeout_seconds: int = 1800
    lock_ttl_seconds: int = 1800
    web_workers: int = 1
    worker_concurrency: int = 1
    worker_interval_seconds: int = 60
    vscode_wsl_distro: str | None = None
    merge_mode: str = "auto"
    pr_remote: str | None = None
    pr_branch_prefix: str = "agent-team/issue-"


def load_config() -> AppConfig:
    home = Path(
        os.environ.get(
            "AGENT_TEAM_HOME",
            Path.home() / ".local" / "share" / "agent-team-orchestrator",
        )
    ).expanduser()
    copilot_args = _build_copilot_args()
    copilot_permission_mode = _copilot_permission_mode()
    copilot_plugin_dir = os.environ.get("AGENT_TEAM_COPILOT_PLUGIN_DIR")
    runner_timeout_seconds = int(os.environ.get("AGENT_TEAM_RUNNER_TIMEOUT_SECONDS", "1800"))
    lock_ttl_seconds = int(os.environ.get("AGENT_TEAM_LOCK_TTL_SECONDS", str(runner_timeout_seconds)))
    web_workers = _env_positive_int("AGENT_TEAM_WEB_WORKERS", 1)
    worker_concurrency = _env_positive_int("AGENT_TEAM_WORKER_CONCURRENCY", 1)
    worker_interval_seconds = _env_non_negative_int("AGENT_TEAM_WORKER_INTERVAL_SECONDS", 60)
    vscode_wsl_distro = _vscode_wsl_distro()
    merge_mode = _merge_mode()
    pr_remote = _env_optional_string("AGENT_TEAM_PR_REMOTE")
    pr_branch_prefix = _env_optional_string("AGENT_TEAM_PR_BRANCH_PREFIX") or "agent-team/issue-"
    return AppConfig(
        home=home,
        db_path=home / "state.db",
        artifacts_dir=home / "issues",
        worktrees_dir=Path(os.environ.get("AGENT_TEAM_WORKTREES_DIR", home / "worktrees")).expanduser(),
        locks_dir=home / "locks",
        runner=os.environ.get("AGENT_TEAM_RUNNER", "copilot-cli"),
        copilot_command=os.environ.get("AGENT_TEAM_COPILOT_COMMAND", "copilot"),
        copilot_args=tuple(copilot_args),
        copilot_permission_mode=copilot_permission_mode,
        copilot_plugin_dir=Path(copilot_plugin_dir).expanduser() if copilot_plugin_dir else None,
        runner_timeout_seconds=runner_timeout_seconds,
        lock_ttl_seconds=lock_ttl_seconds,
        web_workers=web_workers,
        worker_concurrency=worker_concurrency,
        worker_interval_seconds=worker_interval_seconds,
        vscode_wsl_distro=vscode_wsl_distro,
        merge_mode=merge_mode,
        pr_remote=pr_remote,
        pr_branch_prefix=pr_branch_prefix,
    )


def ensure_home(config: AppConfig) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    config.artifacts_dir.mkdir(parents=True, exist_ok=True)
    config.worktrees_dir.mkdir(parents=True, exist_ok=True)
    (config.locks_dir or config.home / "locks").mkdir(parents=True, exist_ok=True)


def _build_copilot_args() -> list[str]:
    args: list[str] = []
    available_tools = os.environ.get("AGENT_TEAM_COPILOT_AVAILABLE_TOOLS")
    excluded_tools = os.environ.get("AGENT_TEAM_COPILOT_EXCLUDED_TOOLS")
    allow_tool = os.environ.get("AGENT_TEAM_COPILOT_ALLOW_TOOL")
    deny_tool = os.environ.get("AGENT_TEAM_COPILOT_DENY_TOOL")
    allow_url = os.environ.get("AGENT_TEAM_COPILOT_ALLOW_URL")
    deny_url = os.environ.get("AGENT_TEAM_COPILOT_DENY_URL")
    extra_args = os.environ.get("AGENT_TEAM_COPILOT_ARGS")
    if available_tools:
        args.append(f"--available-tools={available_tools}")
    if excluded_tools:
        args.append(f"--excluded-tools={excluded_tools}")
    if allow_tool:
        args.append(f"--allow-tool={allow_tool}")
    if deny_tool:
        args.append(f"--deny-tool={deny_tool}")
    if allow_url:
        args.append(f"--allow-url={allow_url}")
    if deny_url:
        args.append(f"--deny-url={deny_url}")
    if _env_bool("AGENT_TEAM_COPILOT_ALLOW_ALL_TOOLS"):
        args.append("--allow-all-tools")
    if _env_bool("AGENT_TEAM_COPILOT_ALLOW_ALL_URLS"):
        args.append("--allow-all-urls")
    if extra_args:
        args.extend(shlex.split(extra_args))
    return args


def _vscode_wsl_distro() -> str | None:
    if "AGENT_TEAM_VSCODE_WSL_DISTRO" in os.environ:
        return os.environ["AGENT_TEAM_VSCODE_WSL_DISTRO"].strip() or None
    return os.environ.get("WSL_DISTRO_NAME", "").strip() or None


def _copilot_permission_mode() -> str:
    value = os.environ.get("AGENT_TEAM_COPILOT_PERMISSION_MODE", "phase").strip().lower()
    if value not in {"phase", "yolo"}:
        raise ValueError("AGENT_TEAM_COPILOT_PERMISSION_MODE must be one of: phase, yolo")
    return value


def _merge_mode() -> str:
    value = os.environ.get("AGENT_TEAM_MERGE_MODE", "auto").strip().lower().replace("-", "_")
    if value not in {"auto", "local", "pull_request"}:
        raise ValueError("AGENT_TEAM_MERGE_MODE must be one of: auto, local, pull_request")
    return value


def _env_optional_string(name: str) -> str | None:
    if name not in os.environ:
        return None
    return os.environ[name].strip() or None


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def _env_positive_int(name: str, default: int) -> int:
    value = _env_int(name, default)
    if value < 1:
        raise ValueError(f"{name} must be at least 1")
    return value


def _env_non_negative_int(name: str, default: int) -> int:
    value = _env_int(name, default)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
