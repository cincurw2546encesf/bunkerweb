# Copilot Instructions for BunkerWeb

Use [AGENTS.md](../AGENTS.md) as the canonical, up-to-date instruction file for this repository.

## Copilot Focus

- Start with [AGENTS.md](../AGENTS.md), then open component-specific guidance as needed.
- Prefer links to existing docs instead of repeating long instructions.
- Keep changes minimal and aligned with existing patterns in the touched component.

## High-Value Reminders

- Scheduler orchestration and worker job execution are split (`src/scheduler/` vs `src/worker/`).
- UI data access should go through API client layers, not direct DB access patterns.
- Validate settings/config changes through the existing generation pipeline; avoid bypass paths.
- Run `pre-commit run --all-files` after code edits.

## Primary References

- [AGENTS.md](../AGENTS.md)
- [CLAUDE.md](../CLAUDE.md)
- [BUILD.md](../BUILD.md)
- [CONTRIBUTING.md](../CONTRIBUTING.md)
- [docs/quickstart-guide.md](../docs/quickstart-guide.md)
