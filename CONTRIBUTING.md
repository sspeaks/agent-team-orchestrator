# Contributing

Thank you for considering a contribution to Agent Team Orchestrator. This project is a local-first Python CLI for coordinating AI engineering agents through issue workflows.

## Before you start

- Use focused changes that solve one problem at a time.
- Open an issue first for large design changes or behavior changes.
- Do not include secrets, credentials, private absolute paths, or sensitive logs in issues, pull requests, tests, or documentation.

## Development setup

Prerequisites: Python 3.10 or newer and Git.

```bash
python3 -m pip install -e .
```

Run the test suite and dry-run smoke check before submitting code changes:

```bash
PYTHONDONTWRITEBYTECODE=1 AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m unittest discover -s tests
AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m agent_team.cli init
```

## Pull requests

- Keep pull requests small and explain the user-visible impact.
- Add or update tests for behavior changes.
- Update documentation when commands, workflow, or safety expectations change.
- Prefer standard-library solutions unless a new dependency is clearly justified.

## Community standards

Be respectful, constructive, and focused on the project. A separate code of conduct is intentionally deferred until maintainers configure a truthful enforcement contact or repository moderation process.
