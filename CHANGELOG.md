# Changelog

## 0.2.0

- Add `--with-model-refresh` to copy a trusted Codex `refresh-models.py`.
- Generate a `codex-with-models` launcher for each destination, using its Python, Codex configuration directory, and existing CLI.
- Include the script and launcher in backups, restore, concurrent-edit checks, and rollback.
- Add tests for refresh-before-launch behavior, failure handling, idempotence, restore, and quoted arguments and paths.

## 0.1.0

- Sync portable Codex and Claude Code defaults between local and SSH machines.
- Preview changed keys without printing configuration values.
- Preserve machine-specific integrations and project trust.
- Support opt-in credential transfer and shared or per-harness custom API keys.
- Remap home paths and transfer the selected Codex model catalog.
- Add private backups, restore, concurrent-edit detection, and per-destination rollback.
