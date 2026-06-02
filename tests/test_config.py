from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from agent_team.config import AppConfig, DEFAULT_CONFIG_FILENAME, ensure_home, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._empty_cwd = tempfile.TemporaryDirectory()
        self._empty_cwd_context = self._cwd(Path(self._empty_cwd.name))
        self._empty_cwd_context.__enter__()

    def tearDown(self) -> None:
        self._empty_cwd_context.__exit__(None, None, None)
        self._empty_cwd.cleanup()

    @contextmanager
    def _cwd(self, path: Path) -> Iterator[None]:
        previous = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)

    def _write_config(self, directory: str | Path, content: str, name: str = "agent-team.config.jsonc") -> Path:
        path = Path(directory) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_explicit_jsonc_config_loads_comments_and_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                tmp,
                """
                {
                  // Line comments are ignored.
                  "home": "~/agent-team-jsonc-home",
                  "worktrees_dir": "~/agent-team-jsonc-worktrees",
                  "runner": "dry-run",
                  "runner_timeout_seconds": 2400,
                  "lock_ttl_seconds": 120,
                  "human_input": {
                    "mode": "eager"
                  },
                  "copilot": {
                    "command": "copilot-dev",
                    "permission_mode": "yolo",
                    "plugin_dir": "~/agent-team-jsonc-plugin",
                    "available_tools": ["read", "edit"],
                    "excluded_tools": "web_search",
                    "allow_tool": "shell(npm test)",
                    "deny_tool": "shell(git push)",
                    "allow_url": "https://example.test/*",
                    "deny_url": "http://*",
                    "allow_all_tools": true,
                    "allow_all_urls": true,
                    "extra_args": ["--model", "gpt-5.5", "--note=http://example.test//not-comment/*"]
                  },
                  "web": {
                    "host": "127.0.0.2",
                    "port": 9876,
                    "web_workers": 2,
                    "unsafe_allow_remote": true,
                    "vscode_wsl_distro": ""
                  },
                  "worker": {
                    "worker_concurrency": 3,
                    "worker_interval_seconds": 0
                  }
                }
                """,
            )

            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)

        self.assertEqual(config.home, Path("~/agent-team-jsonc-home").expanduser())
        self.assertEqual(config.worktrees_dir, Path("~/agent-team-jsonc-worktrees").expanduser())
        self.assertEqual(config.runner, "dry-run")
        self.assertEqual(config.runner_timeout_seconds, 2400)
        self.assertEqual(config.lock_ttl_seconds, 120)
        self.assertEqual(config.human_input_mode, "eager")
        self.assertEqual(config.copilot_command, "copilot-dev")
        self.assertEqual(config.copilot_permission_mode, "yolo")
        self.assertEqual(config.copilot_plugin_dir, Path("~/agent-team-jsonc-plugin").expanduser())
        self.assertEqual(
            config.copilot_args,
            (
                "--available-tools=read,edit",
                "--excluded-tools=web_search",
                "--allow-tool=shell(npm test)",
                "--deny-tool=shell(git push)",
                "--allow-url=https://example.test/*",
                "--deny-url=http://*",
                "--allow-all-tools",
                "--allow-all-urls",
                "--model",
                "gpt-5.5",
                "--note=http://example.test//not-comment/*",
            ),
        )
        self.assertEqual(config.web_host, "127.0.0.2")
        self.assertEqual(config.web_port, 9876)
        self.assertEqual(config.web_workers, 2)
        self.assertTrue(config.web_unsafe_allow_remote)
        self.assertEqual(config.worker_concurrency, 3)
        self.assertEqual(config.worker_interval_seconds, 0)
        self.assertIsNone(config.vscode_wsl_distro)

    def test_jsonc_trailing_commas_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                tmp,
                """
                {
                  "runner": "dry-run",
                  "copilot": {
                    "extra_args": ["--flag",],
                  },
                }
                """,
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)

        self.assertEqual(config.runner, "dry-run")
        self.assertEqual(config.copilot_args, ("--flag",))

    def test_default_config_file_is_discovered_in_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_config(root, '{"home": "./state", "runner": "dry-run"}')
            with patch.dict(os.environ, {}, clear=True), self._cwd(root):
                config = load_config()

        self.assertEqual(config.home, Path("state"))
        self.assertEqual(config.runner, "dry-run")

    def test_config_file_can_be_selected_with_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, '{"runner": "dry-run"}', "selected.jsonc")
            with patch.dict(os.environ, {"AGENT_TEAM_CONFIG_FILE": str(path)}, clear=True):
                config = load_config()

        self.assertEqual(config.runner, "dry-run")

    def test_explicit_config_path_takes_precedence_over_environment_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = self._write_config(tmp, '{"runner": "copilot-cli"}', "env.jsonc")
            explicit_path = self._write_config(tmp, '{"runner": "dry-run"}', "explicit.jsonc")
            with patch.dict(os.environ, {"AGENT_TEAM_CONFIG_FILE": str(env_path)}, clear=True):
                config = load_config(explicit_path)

        self.assertEqual(config.runner, "dry-run")

    def test_missing_default_config_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True), self._cwd(Path(tmp)):
                config = load_config()

        self.assertEqual(config.runner, "copilot-cli")

    def test_missing_explicit_config_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.jsonc"
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "Config file not found"):
                    load_config(missing)

    def test_invalid_config_files_are_rejected(self) -> None:
        cases = (
            ("invalid.jsonc", "{", "Invalid JSONC"),
            ("array.jsonc", "[]", "must contain a JSON object"),
            ("unknown.jsonc", '{"unknown": true}', "Unknown config key 'unknown'"),
            ("unknown-nested.jsonc", '{"copilot": {"unknown": true}}', "Unknown config key 'copilot.unknown'"),
            (
                "unknown-human-input.jsonc",
                '{"human_input": {"unknown": true}}',
                "Unknown config key 'human_input.unknown'",
            ),
            ("bad-section.jsonc", '{"web": []}', "web must be an object"),
            ("bad-human-input-section.jsonc", '{"human_input": []}', "human_input must be an object"),
            ("bad-int.jsonc", '{"runner_timeout_seconds": 0}', "runner_timeout_seconds must be at least 1"),
            ("bad-bool.jsonc", '{"web": {"unsafe_allow_remote": "yes"}}', "unsafe_allow_remote must be a boolean"),
            ("bad-extra.jsonc", '{"copilot": {"extra_args": [1]}}', "copilot.extra_args"),
            ("bad-comment.jsonc", '{"runner": "dry-run" /* unterminated', "Unterminated block comment"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for name, content, message in cases:
                with self.subTest(name=name):
                    path = self._write_config(tmp, content, name)
                    with patch.dict(os.environ, {}, clear=True):
                        with self.assertRaisesRegex(ValueError, message):
                            load_config(path)

    def test_configured_runner_timeout_drives_default_lock_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, '{"runner_timeout_seconds": 2400}')
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)

        self.assertEqual(config.runner_timeout_seconds, 2400)
        self.assertEqual(config.lock_ttl_seconds, 2400)

    def test_environment_overrides_config_file_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                tmp,
                """
                {
                  "home": "/tmp/file-home",
                  "runner": "copilot-cli",
                  "runner_timeout_seconds": 1200,
                  "human_input": {"mode": "autonomous"},
                  "copilot": {
                    "allow_tool": "shell(file-test)",
                    "allow_all_tools": true,
                    "extra_args": ["--model", "file-model"]
                  },
                  "web": {"web_workers": 2},
                  "worker": {"worker_concurrency": 2, "worker_interval_seconds": 15}
                }
                """,
            )
            env = {
                "AGENT_TEAM_HOME": "/tmp/env-home",
                "AGENT_TEAM_RUNNER": "dry-run",
                "AGENT_TEAM_RUNNER_TIMEOUT_SECONDS": "2400",
                "AGENT_TEAM_HUMAN_INPUT_MODE": "eager",
                "AGENT_TEAM_COPILOT_ALLOW_TOOL": "shell(env-test)",
                "AGENT_TEAM_COPILOT_ALLOW_ALL_TOOLS": "false",
                "AGENT_TEAM_COPILOT_ARGS": "--model env-model",
                "AGENT_TEAM_WEB_WORKERS": "5",
                "AGENT_TEAM_WORKER_CONCURRENCY": "6",
                "AGENT_TEAM_WORKER_INTERVAL_SECONDS": "7",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config(path)

        self.assertEqual(config.home, Path("/tmp/env-home"))
        self.assertEqual(config.runner, "dry-run")
        self.assertEqual(config.runner_timeout_seconds, 2400)
        self.assertEqual(config.lock_ttl_seconds, 2400)
        self.assertEqual(config.human_input_mode, "eager")
        self.assertEqual(config.web_workers, 5)
        self.assertEqual(config.worker_concurrency, 6)
        self.assertEqual(config.worker_interval_seconds, 7)
        self.assertNotIn("--allow-tool=shell(file-test)", config.copilot_args)
        self.assertIn("--allow-tool=shell(env-test)", config.copilot_args)
        self.assertNotIn("--allow-all-tools", config.copilot_args)
        self.assertNotIn("file-model", config.copilot_args)
        self.assertEqual(config.copilot_args[-2:], ("--model", "env-model"))

    def test_copilot_pass_through_env_values_replace_config_file_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                tmp,
                """
                {
                  "copilot": {
                    "available_tools": "file-available",
                    "excluded_tools": "file-excluded",
                    "allow_tool": "file-allow",
                    "deny_tool": "file-deny",
                    "allow_url": "https://file-allow.example/*",
                    "deny_url": "https://file-deny.example/*",
                    "extra_args": ["--model", "file-model"]
                  }
                }
                """,
            )
            env = {
                "AGENT_TEAM_COPILOT_AVAILABLE_TOOLS": "env-available",
                "AGENT_TEAM_COPILOT_EXCLUDED_TOOLS": "env-excluded",
                "AGENT_TEAM_COPILOT_ALLOW_TOOL": "env-allow",
                "AGENT_TEAM_COPILOT_DENY_TOOL": "env-deny",
                "AGENT_TEAM_COPILOT_ALLOW_URL": "https://env-allow.example/*",
                "AGENT_TEAM_COPILOT_DENY_URL": "https://env-deny.example/*",
                "AGENT_TEAM_COPILOT_ARGS": "--model env-model --env-flag",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config(path)

        self.assertEqual(
            config.copilot_args,
            (
                "--available-tools=env-available",
                "--excluded-tools=env-excluded",
                "--allow-tool=env-allow",
                "--deny-tool=env-deny",
                "--allow-url=https://env-allow.example/*",
                "--deny-url=https://env-deny.example/*",
                "--model",
                "env-model",
                "--env-flag",
            ),
        )

    def test_empty_copilot_pass_through_env_values_clear_config_file_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(
                tmp,
                """
                {
                  "copilot": {
                    "available_tools": "file-available",
                    "excluded_tools": "file-excluded",
                    "allow_tool": "file-allow",
                    "deny_tool": "file-deny",
                    "allow_url": "https://file-allow.example/*",
                    "deny_url": "https://file-deny.example/*",
                    "extra_args": ["--model", "file-model"]
                  }
                }
                """,
            )
            env = {
                "AGENT_TEAM_COPILOT_AVAILABLE_TOOLS": "",
                "AGENT_TEAM_COPILOT_EXCLUDED_TOOLS": "",
                "AGENT_TEAM_COPILOT_ALLOW_TOOL": "",
                "AGENT_TEAM_COPILOT_DENY_TOOL": "",
                "AGENT_TEAM_COPILOT_ALLOW_URL": "",
                "AGENT_TEAM_COPILOT_DENY_URL": "",
                "AGENT_TEAM_COPILOT_ARGS": "",
            }
            with patch.dict(os.environ, env, clear=True):
                config = load_config(path)

        self.assertEqual(config.copilot_args, ())

    def test_config_can_disable_wsl_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, '{"web": {"vscode_wsl_distro": null}}')
            with patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}, clear=True):
                config = load_config(path)

        self.assertIsNone(config.vscode_wsl_distro)

    def test_example_config_is_valid_and_actual_config_is_ignored(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        example_path = repo_root / "agent-team.config.example.jsonc"
        gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

        self.assertTrue(example_path.is_file())
        self.assertIn("/agent-team.config.jsonc", gitignore)
        with patch.dict(os.environ, {}, clear=True):
            config = load_config(example_path)
        self.assertEqual(config.runner, "copilot-cli")
        self.assertEqual(config.human_input_mode, "balanced")

    def test_example_config_allows_uncommenting_single_top_level_default(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        example_path = repo_root / "agent-team.config.example.jsonc"
        text = example_path.read_text(encoding="utf-8").replace(
            '  // "runner": "copilot-cli",',
            '  "runner": "dry-run",',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, text)
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)

        self.assertEqual(config.runner, "dry-run")

    def test_example_config_allows_uncommenting_single_nested_default(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        example_path = repo_root / "agent-team.config.example.jsonc"
        text = example_path.read_text(encoding="utf-8")
        text = text.replace('  // "web": {', '  "web": {')
        text = text.replace('    // "port": 8765,', '    "port": 9876,')
        text = text.replace('  // },\n\n  // "worker": {', '  },\n\n  // "worker": {')
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, text)
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)

        self.assertEqual(config.web_port, 9876)

    def test_example_config_allows_uncommenting_human_input_mode(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        example_path = repo_root / "agent-team.config.example.jsonc"
        text = example_path.read_text(encoding="utf-8")
        text = text.replace('  // "human_input": {', '  "human_input": {')
        text = text.replace('    // "mode": "balanced"', '    "mode": "autonomous"')
        text = text.replace('  // },\n\n  // "web": {', '  },\n\n  // "web": {')
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, text)
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)

        self.assertEqual(config.human_input_mode, "autonomous")

    def test_default_config_tests_are_not_affected_by_repo_local_config(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        local_config = repo_root / DEFAULT_CONFIG_FILENAME
        created = False
        if not local_config.exists():
            local_config.write_text('{"runner": "dry-run"}', encoding="utf-8")
            created = True
        try:
            with patch.dict(os.environ, {}, clear=True):
                config = load_config()
        finally:
            if created:
                local_config.unlink()

        self.assertEqual(config.runner, "copilot-cli")

    def test_copilot_permission_args_from_env(self) -> None:
        env = {
            "AGENT_TEAM_COPILOT_AVAILABLE_TOOLS": "bash,edit,view",
            "AGENT_TEAM_COPILOT_ALLOW_TOOL": "shell(git:*),write",
            "AGENT_TEAM_COPILOT_DENY_TOOL": "shell(git push)",
            "AGENT_TEAM_COPILOT_ALLOW_URL": "https://docs.github.com/*",
            "AGENT_TEAM_COPILOT_DENY_URL": "https://example.invalid/*",
            "AGENT_TEAM_COPILOT_ARGS": "--model gpt-5.5",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        self.assertNotIn("--yolo", config.copilot_args)
        self.assertIn("--available-tools=bash,edit,view", config.copilot_args)
        self.assertIn("--allow-tool=shell(git:*),write", config.copilot_args)
        self.assertIn("--deny-tool=shell(git push)", config.copilot_args)
        self.assertIn("--allow-url=https://docs.github.com/*", config.copilot_args)
        self.assertIn("--deny-url=https://example.invalid/*", config.copilot_args)
        self.assertIn("--model", config.copilot_args)
        self.assertIn("gpt-5.5", config.copilot_args)

    def test_default_runner_is_copilot_cli(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.runner, "copilot-cli")

    def test_human_input_mode_defaults_to_balanced(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.human_input_mode, "balanced")

    def test_human_input_mode_can_be_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, '{"human_input": {"mode": "autonomous"}}')
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(path)

        self.assertEqual(config.human_input_mode, "autonomous")

    def test_human_input_mode_environment_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, '{"human_input": {"mode": "autonomous"}}')
            with patch.dict(os.environ, {"AGENT_TEAM_HUMAN_INPUT_MODE": "EAGER"}, clear=True):
                config = load_config(path)

        self.assertEqual(config.human_input_mode, "eager")

    def test_human_input_mode_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_config(tmp, '{"human_input": {"mode": "often"}}')
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "human_input.mode/AGENT_TEAM_HUMAN_INPUT_MODE"):
                    load_config(path)

        with patch.dict(os.environ, {"AGENT_TEAM_HUMAN_INPUT_MODE": "often"}, clear=True):
            with self.assertRaisesRegex(ValueError, "human_input.mode/AGENT_TEAM_HUMAN_INPUT_MODE"):
                load_config()

    def test_lock_ttl_defaults_to_runner_timeout(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.lock_ttl_seconds, config.runner_timeout_seconds)
        self.assertEqual(config.lock_ttl_seconds, 1800)

    def test_lock_ttl_tracks_runner_timeout_override_by_default(self) -> None:
        with patch.dict(os.environ, {"AGENT_TEAM_RUNNER_TIMEOUT_SECONDS": "2400"}, clear=True):
            config = load_config()
        self.assertEqual(config.runner_timeout_seconds, 2400)
        self.assertEqual(config.lock_ttl_seconds, 2400)

    def test_lock_ttl_can_be_overridden_independently(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_TEAM_RUNNER_TIMEOUT_SECONDS": "2400",
                "AGENT_TEAM_LOCK_TTL_SECONDS": "2500",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.runner_timeout_seconds, 2400)
        self.assertEqual(config.lock_ttl_seconds, 2500)

    def test_runtime_worker_defaults_can_be_overridden(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_TEAM_WEB_WORKERS": "2",
                "AGENT_TEAM_WORKER_CONCURRENCY": "3",
                "AGENT_TEAM_WORKER_INTERVAL_SECONDS": "15",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.web_workers, 2)
        self.assertEqual(config.worker_concurrency, 3)
        self.assertEqual(config.worker_interval_seconds, 15)

    def test_merge_pr_defaults_can_be_overridden(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_TEAM_MERGE_MODE": "pull-request",
                "AGENT_TEAM_PR_REMOTE": " upstream ",
                "AGENT_TEAM_PR_BRANCH_PREFIX": "bots/pr-",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.merge_mode, "pull_request")
        self.assertEqual(config.pr_remote, "upstream")
        self.assertEqual(config.pr_branch_prefix, "bots/pr-")

    def test_merge_mode_rejects_invalid_value(self) -> None:
        with patch.dict(os.environ, {"AGENT_TEAM_MERGE_MODE": "remote-only"}, clear=True):
            with self.assertRaisesRegex(ValueError, "AGENT_TEAM_MERGE_MODE"):
                load_config()

    def test_runtime_worker_env_values_must_be_valid(self) -> None:
        invalid_cases = (
            ("AGENT_TEAM_WEB_WORKERS", "0", "at least 1"),
            ("AGENT_TEAM_WORKER_CONCURRENCY", "-1", "at least 1"),
            ("AGENT_TEAM_WORKER_INTERVAL_SECONDS", "-1", "non-negative"),
            ("AGENT_TEAM_WORKER_CONCURRENCY", "many", "integer"),
        )
        for name, value, message in invalid_cases:
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {name: value}, clear=True):
                    with self.assertRaisesRegex(ValueError, f"{name}.*{message}"):
                        load_config()

    def test_vscode_wsl_distro_can_be_configured_explicitly(self) -> None:
        with patch.dict(os.environ, {"AGENT_TEAM_VSCODE_WSL_DISTRO": " Ubuntu-22.04 "}, clear=True):
            config = load_config()
        self.assertEqual(config.vscode_wsl_distro, "Ubuntu-22.04")

    def test_vscode_wsl_distro_defaults_to_wsl_environment(self) -> None:
        with patch.dict(os.environ, {"WSL_DISTRO_NAME": " Ubuntu "}, clear=True):
            config = load_config()
        self.assertEqual(config.vscode_wsl_distro, "Ubuntu")

    def test_vscode_wsl_distro_explicit_value_takes_precedence(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_TEAM_VSCODE_WSL_DISTRO": "Debian",
                "WSL_DISTRO_NAME": "Ubuntu",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.vscode_wsl_distro, "Debian")

    def test_vscode_wsl_distro_blank_explicit_value_disables_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_TEAM_VSCODE_WSL_DISTRO": " ",
                "WSL_DISTRO_NAME": "Ubuntu",
            },
            clear=True,
        ):
            config = load_config()
        self.assertIsNone(config.vscode_wsl_distro)

    def test_copilot_defaults_to_phase_permission_mode(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertEqual(config.copilot_permission_mode, "phase")
        self.assertNotIn("--yolo", config.copilot_args)

    def test_copilot_permission_mode_can_use_yolo_escape_hatch(self) -> None:
        with patch.dict(os.environ, {"AGENT_TEAM_COPILOT_PERMISSION_MODE": "yolo"}, clear=True):
            config = load_config()
        self.assertEqual(config.copilot_permission_mode, "yolo")

    def test_copilot_permission_mode_rejects_invalid_values(self) -> None:
        with patch.dict(os.environ, {"AGENT_TEAM_COPILOT_PERMISSION_MODE": "open"}, clear=True):
            with self.assertRaisesRegex(ValueError, "AGENT_TEAM_COPILOT_PERMISSION_MODE"):
                load_config()

    def test_worktrees_dir_defaults_under_home(self) -> None:
        with patch.dict(os.environ, {"AGENT_TEAM_HOME": "/tmp/agent-team-test"}, clear=True):
            config = load_config()
        self.assertEqual(config.worktrees_dir, Path("/tmp/agent-team-test/worktrees"))
        self.assertEqual(config.locks_dir, Path("/tmp/agent-team-test/locks"))

    def test_worktrees_dir_can_be_overridden(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_TEAM_HOME": "/tmp/agent-team-test",
                "AGENT_TEAM_WORKTREES_DIR": "/tmp/custom-worktrees",
            },
            clear=True,
        ):
            config = load_config()
        self.assertEqual(config.worktrees_dir, Path("/tmp/custom-worktrees"))

    def test_copilot_plugin_dir_can_be_overridden(self) -> None:
        with patch.dict(os.environ, {"AGENT_TEAM_COPILOT_PLUGIN_DIR": "/tmp/custom-copilot-plugin"}, clear=True):
            config = load_config()
        self.assertEqual(config.copilot_plugin_dir, Path("/tmp/custom-copilot-plugin"))

    def test_ensure_home_creates_worktrees_and_locks_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = AppConfig(
                home=home,
                db_path=home / "state.db",
                artifacts_dir=home / "issues",
                worktrees_dir=home / "worktrees",
                locks_dir=home / "locks",
            )
            ensure_home(config)
            self.assertTrue(config.worktrees_dir.is_dir())
            self.assertTrue(config.locks_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
