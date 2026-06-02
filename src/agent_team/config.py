from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .human_input_policy import DEFAULT_HUMAN_INPUT_MODE, normalize_human_input_mode


DEFAULT_CONFIG_FILENAME = "agent-team.config.jsonc"
_DEFAULT_HOME = Path.home() / ".local" / "share" / "agent-team-orchestrator"
_TOP_LEVEL_KEYS = {
    "home",
    "worktrees_dir",
    "runner",
    "runner_timeout_seconds",
    "lock_ttl_seconds",
    "copilot",
    "human_input",
    "web",
    "worker",
}
_COPILOT_KEYS = {
    "command",
    "permission_mode",
    "plugin_dir",
    "available_tools",
    "excluded_tools",
    "allow_tool",
    "deny_tool",
    "allow_url",
    "deny_url",
    "allow_all_tools",
    "allow_all_urls",
    "extra_args",
}
_COPILOT_PASSTHROUGH_FLAGS = (
    ("available_tools", "--available-tools", "AGENT_TEAM_COPILOT_AVAILABLE_TOOLS"),
    ("excluded_tools", "--excluded-tools", "AGENT_TEAM_COPILOT_EXCLUDED_TOOLS"),
    ("allow_tool", "--allow-tool", "AGENT_TEAM_COPILOT_ALLOW_TOOL"),
    ("deny_tool", "--deny-tool", "AGENT_TEAM_COPILOT_DENY_TOOL"),
    ("allow_url", "--allow-url", "AGENT_TEAM_COPILOT_ALLOW_URL"),
    ("deny_url", "--deny-url", "AGENT_TEAM_COPILOT_DENY_URL"),
)
_WEB_KEYS = {
    "host",
    "port",
    "web_workers",
    "unsafe_allow_remote",
    "vscode_wsl_distro",
}
_WORKER_KEYS = {
    "worker_concurrency",
    "worker_interval_seconds",
}
_HUMAN_INPUT_KEYS = {
    "mode",
}


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
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_workers: int = 1
    web_unsafe_allow_remote: bool = False
    worker_concurrency: int = 1
    worker_interval_seconds: int = 60
    human_input_mode: str = DEFAULT_HUMAN_INPUT_MODE
    vscode_wsl_distro: str | None = None
    merge_mode: str = "auto"
    pr_remote: str | None = None
    pr_branch_prefix: str = "agent-team/issue-"


def load_config(config_path: str | Path | None = None) -> AppConfig:
    file_config = _load_config_file(config_path)
    copilot_config = _section(file_config, "copilot")
    human_input_config = _section(file_config, "human_input")
    web_config = _section(file_config, "web")
    worker_config = _section(file_config, "worker")

    home = _path_setting(
        os.environ.get("AGENT_TEAM_HOME"),
        _config_path(file_config, "home", _DEFAULT_HOME),
    )
    worktrees_dir = _path_setting(
        os.environ.get("AGENT_TEAM_WORKTREES_DIR"),
        _config_path(file_config, "worktrees_dir", home / "worktrees"),
    )
    copilot_plugin_dir = _optional_path_setting(
        os.environ.get("AGENT_TEAM_COPILOT_PLUGIN_DIR"),
        _config_optional_path(copilot_config, "plugin_dir", None),
    )
    runner = _env_string("AGENT_TEAM_RUNNER", _config_string(file_config, "runner", "copilot-cli"))
    copilot_args = _build_copilot_args(copilot_config)
    copilot_permission_mode = _copilot_permission_mode(
        _env_string(
            "AGENT_TEAM_COPILOT_PERMISSION_MODE",
            _config_string(copilot_config, "permission_mode", "phase"),
        )
    )
    copilot_command = _env_string(
        "AGENT_TEAM_COPILOT_COMMAND",
        _config_string(copilot_config, "command", "copilot"),
    )
    runner_timeout_seconds = _env_positive_int(
        "AGENT_TEAM_RUNNER_TIMEOUT_SECONDS",
        _config_positive_int(file_config, "runner_timeout_seconds", 1800),
    )
    lock_ttl_seconds = _resolve_lock_ttl_seconds(file_config, runner_timeout_seconds)
    web_host = _config_string(web_config, "host", "127.0.0.1")
    web_port = _config_port(web_config, "port", 8765)
    web_workers = _env_positive_int("AGENT_TEAM_WEB_WORKERS", _config_positive_int(web_config, "web_workers", 1))
    web_unsafe_allow_remote = _config_bool(web_config, "unsafe_allow_remote", False)
    worker_concurrency = _env_positive_int(
        "AGENT_TEAM_WORKER_CONCURRENCY",
        _config_positive_int(worker_config, "worker_concurrency", 1),
    )
    worker_interval_seconds = _env_non_negative_int(
        "AGENT_TEAM_WORKER_INTERVAL_SECONDS",
        _config_non_negative_int(worker_config, "worker_interval_seconds", 60),
    )
    human_input_mode = normalize_human_input_mode(
        _env_string(
            "AGENT_TEAM_HUMAN_INPUT_MODE",
            _config_string(human_input_config, "mode", DEFAULT_HUMAN_INPUT_MODE),
        ),
        "human_input.mode/AGENT_TEAM_HUMAN_INPUT_MODE",
    )
    vscode_wsl_distro = _vscode_wsl_distro(web_config)
    merge_mode = _merge_mode()
    pr_remote = _env_optional_string("AGENT_TEAM_PR_REMOTE")
    pr_branch_prefix = _env_optional_string("AGENT_TEAM_PR_BRANCH_PREFIX") or "agent-team/issue-"
    return AppConfig(
        home=home,
        db_path=home / "state.db",
        artifacts_dir=home / "issues",
        worktrees_dir=worktrees_dir,
        locks_dir=home / "locks",
        runner=runner,
        copilot_command=copilot_command,
        copilot_args=tuple(copilot_args),
        copilot_permission_mode=copilot_permission_mode,
        copilot_plugin_dir=copilot_plugin_dir,
        runner_timeout_seconds=runner_timeout_seconds,
        lock_ttl_seconds=lock_ttl_seconds,
        web_host=web_host,
        web_port=web_port,
        web_workers=web_workers,
        web_unsafe_allow_remote=web_unsafe_allow_remote,
        worker_concurrency=worker_concurrency,
        worker_interval_seconds=worker_interval_seconds,
        human_input_mode=human_input_mode,
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


def _load_config_file(config_path: str | Path | None) -> dict[str, Any]:
    path, required = _select_config_path(config_path)
    if not path.exists():
        if required:
            raise ValueError(f"Config file not found: {path}")
        return {}
    if path.is_dir():
        raise ValueError(f"Config file is a directory: {path}")
    try:
        source = path.read_text(encoding="utf-8")
        parsed = json.loads(_strip_jsonc_trailing_commas(_strip_jsonc_comments(source)))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSONC in config file {path}: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Config file {path} must contain a JSON object")
    _validate_config_keys(parsed, path)
    return parsed


def _select_config_path(config_path: str | Path | None) -> tuple[Path, bool]:
    if config_path is not None:
        return Path(config_path).expanduser(), True
    env_path = os.environ.get("AGENT_TEAM_CONFIG_FILE")
    if env_path and env_path.strip():
        return Path(env_path).expanduser(), True
    return Path(DEFAULT_CONFIG_FILENAME), False


def _strip_jsonc_comments(source: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            output.extend((" ", " "))
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue
        if char == "/" and next_char == "*":
            output.extend((" ", " "))
            index += 2
            closed = False
            while index < len(source):
                char = source[index]
                next_char = source[index + 1] if index + 1 < len(source) else ""
                if char == "*" and next_char == "/":
                    output.extend((" ", " "))
                    index += 2
                    closed = True
                    break
                output.append(char if char in "\r\n" else " ")
                index += 1
            if not closed:
                raise ValueError("Unterminated block comment in config file")
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _strip_jsonc_trailing_commas(source: str) -> str:
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(source) and source[lookahead] in " \t\r\n":
                lookahead += 1
            if lookahead < len(source) and source[lookahead] in "}]":
                output.append(" ")
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _validate_config_keys(config: Mapping[str, Any], path: Path) -> None:
    _reject_unknown_keys(config, _TOP_LEVEL_KEYS, path)
    if "copilot" in config:
        _reject_unknown_keys(_section(config, "copilot"), _COPILOT_KEYS, path, "copilot")
    if "human_input" in config:
        _reject_unknown_keys(_section(config, "human_input"), _HUMAN_INPUT_KEYS, path, "human_input")
    if "web" in config:
        _reject_unknown_keys(_section(config, "web"), _WEB_KEYS, path, "web")
    if "worker" in config:
        _reject_unknown_keys(_section(config, "worker"), _WORKER_KEYS, path, "worker")


def _reject_unknown_keys(
    config: Mapping[str, Any],
    allowed: set[str],
    path: Path,
    prefix: str | None = None,
) -> None:
    for key in config:
        if key not in allowed:
            dotted = f"{prefix}.{key}" if prefix else key
            raise ValueError(f"Unknown config key {dotted!r} in {path}")


def _section(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _build_copilot_args(copilot_config: Mapping[str, Any]) -> list[str]:
    args: list[str] = []
    for key, flag, env_name in _COPILOT_PASSTHROUGH_FLAGS:
        _append_copilot_value(args, key, flag, env_name, copilot_config)
    if _copilot_bool_setting(copilot_config, "allow_all_tools", "AGENT_TEAM_COPILOT_ALLOW_ALL_TOOLS"):
        args.append("--allow-all-tools")
    if _copilot_bool_setting(copilot_config, "allow_all_urls", "AGENT_TEAM_COPILOT_ALLOW_ALL_URLS"):
        args.append("--allow-all-urls")
    args.extend(_copilot_extra_args(copilot_config))
    return args


def _append_copilot_value(
    args: list[str],
    key: str,
    flag: str,
    env_name: str,
    config: Mapping[str, Any],
) -> None:
    if env_name in os.environ:
        value = os.environ[env_name]
    else:
        value = _config_string_or_list(config, key, None)
    if value:
        args.append(f"{flag}={value}")


def _config_string_or_list(config: Mapping[str, Any], key: str, default: str | None) -> str | None:
    if key not in config:
        return default
    value = config[key]
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ",".join(value)
    raise ValueError(f"copilot.{key} must be a string or an array of strings")


def _config_extra_args(config: Mapping[str, Any]) -> tuple[str, ...]:
    if "extra_args" not in config:
        return ()
    value = config["extra_args"]
    if isinstance(value, str):
        return tuple(shlex.split(value))
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError("copilot.extra_args must be a string or an array of strings")


def _copilot_extra_args(config: Mapping[str, Any]) -> tuple[str, ...]:
    if "AGENT_TEAM_COPILOT_ARGS" in os.environ:
        return tuple(shlex.split(os.environ["AGENT_TEAM_COPILOT_ARGS"]))
    return _config_extra_args(config)


def _copilot_bool_setting(config: Mapping[str, Any], key: str, env_name: str) -> bool:
    if env_name in os.environ:
        return _env_bool(env_name)
    return _config_bool(config, key, False)


def _vscode_wsl_distro(web_config: Mapping[str, Any]) -> str | None:
    if "AGENT_TEAM_VSCODE_WSL_DISTRO" in os.environ:
        return os.environ["AGENT_TEAM_VSCODE_WSL_DISTRO"].strip() or None
    if "vscode_wsl_distro" in web_config:
        value = web_config["vscode_wsl_distro"]
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("web.vscode_wsl_distro must be a string or null")
        return value.strip() or None
    return os.environ.get("WSL_DISTRO_NAME", "").strip() or None


def _copilot_permission_mode(raw_value: str) -> str:
    value = raw_value.strip().lower()
    if value not in {"phase", "yolo"}:
        raise ValueError("copilot.permission_mode/AGENT_TEAM_COPILOT_PERMISSION_MODE must be one of: phase, yolo")
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


def _env_string(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _path_setting(raw_value: str | None, default: Path) -> Path:
    if raw_value is None:
        return default.expanduser()
    return Path(raw_value).expanduser()


def _optional_path_setting(raw_value: str | None, default: Path | None) -> Path | None:
    if raw_value is None:
        return default.expanduser() if default is not None else None
    return Path(raw_value).expanduser() if raw_value else None


def _config_string(config: Mapping[str, Any], key: str, default: str) -> str:
    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _config_path(config: Mapping[str, Any], key: str, default: Path) -> Path:
    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string path")
    return Path(value).expanduser()


def _config_optional_path(config: Mapping[str, Any], key: str, default: Path | None) -> Path | None:
    if key not in config:
        return default
    value = config[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"copilot.{key} must be a string path or null")
    return Path(value).expanduser() if value else None


def _config_bool(config: Mapping[str, Any], key: str, default: bool) -> bool:
    if key not in config:
        return default
    value = config[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _config_positive_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = _config_int(config, key, default)
    if value < 1:
        raise ValueError(f"{key} must be at least 1")
    return value


def _config_non_negative_int(config: Mapping[str, Any], key: str, default: int) -> int:
    value = _config_int(config, key, default)
    if value < 0:
        raise ValueError(f"{key} must be non-negative")
    return value


def _config_port(config: Mapping[str, Any], key: str, default: int) -> int:
    value = _config_non_negative_int(config, key, default)
    if value > 65535:
        raise ValueError(f"{key} must be no greater than 65535")
    return value


def _config_int(config: Mapping[str, Any], key: str, default: int) -> int:
    if key not in config:
        return default
    value = config[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _resolve_lock_ttl_seconds(config: Mapping[str, Any], runner_timeout_seconds: int) -> int:
    if "AGENT_TEAM_LOCK_TTL_SECONDS" in os.environ:
        return _env_positive_int("AGENT_TEAM_LOCK_TTL_SECONDS", runner_timeout_seconds)
    if "lock_ttl_seconds" in config:
        return _config_positive_int(config, "lock_ttl_seconds", runner_timeout_seconds)
    return runner_timeout_seconds
