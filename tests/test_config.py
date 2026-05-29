import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_team.config import AppConfig, ensure_home, load_config


class ConfigTests(unittest.TestCase):
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
