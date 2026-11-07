# Changelog

## Unreleased

- Refuse a Claude sync that would remove the target's `env.ANTHROPIC_BASE_URL`; `--allow-gateway-drop` opts out.
- Require a source `ANTHROPIC_BASE_URL` when a custom Claude API key is selected.

## 0.3.0

- Add `launchers` to install `claude-ag` / `codex-ag` shell aliases wired to a gateway, choosing per machine the first `--gateway` that answers HTTP (e.g. IPv4, then IPv6).
- Create only what is missing: Claude launcher settings (opus `claude-opus-5-5`, sonnet/subagents `dsv41-flash-uncensored[1m]`, haiku `glm-5.3[1m]`, bypass permissions), the Codex provider entry, API key, and model catalog. Existing aliases, settings, and default providers stay untouched.
- Add `--verify` to run each launcher once in an interactive shell and expect `pong`.
- Keep shell rc file permissions on write; launcher changes are backed up and restorable.

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
