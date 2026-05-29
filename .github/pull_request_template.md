## Summary

Describe the change and its user-visible impact.

## Validation

- [ ] `PYTHONDONTWRITEBYTECODE=1 AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m unittest discover -s tests`
- [ ] `AGENT_TEAM_RUNNER=dry-run PYTHONPATH=src python3 -m agent_team.cli init`
- [ ] Documentation updated or not needed
- [ ] No secrets, credentials, private absolute paths, or sensitive logs included

## Notes for reviewers

Call out risks, follow-up work, or compatibility concerns.
