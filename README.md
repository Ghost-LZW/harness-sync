# harness-sync

[中文文档](README.zh-CN.md)

Keep your Codex and Claude Code defaults consistent across your laptop and SSH machines.

A small Python CLI with no runtime dependencies. Uses your existing OpenSSH configuration, previews changes by default, and backs up every update. Nothing needs to be installed on the remote machine beyond Python 3.11+.

## Install

Requires Python 3.11+ on both ends and OpenSSH on the machine running the CLI. macOS and Linux are supported.

```sh
pipx install git+https://github.com/Ghost-LZW/harness-sync.git

# Or use a checkout without installing anything:
git clone https://github.com/Ghost-LZW/harness-sync.git
cd harness-sync
python3 harness_sync.py --help
```

## Sync

```sh
# Preview: read local defaults and compare them to two SSH machines.
harness-sync sync --to devbox --to alice@server.example.com

# Apply portable defaults, preserving each target's credentials.
harness-sync sync --to devbox --to alice@server.example.com --apply

# Also use this machine's inline credentials and supported credential files.
harness-sync sync --to devbox --with-secrets --apply

# Pull defaults from an SSH machine to this computer.
harness-sync sync --from devbox --to local --apply

# Copy from one remote machine to another through this computer.
harness-sync sync --from devbox --to staging --harness codex --apply

# Use an explicit SSH port.
harness-sync sync --to ssh://alice@server.example.com:2222 --apply
```

`local` means the computer running the tool. Other endpoints are SSH aliases, `user@host`, or `ssh://user@host:port`. Your normal SSH config, keys, agent, and jump hosts apply. Connections use batch mode; establish SSH access first. No shell profile, SSH configuration, or CLI installation is modified.

`--harness codex` or `--harness claude` limits the sync to one harness. Repeating `--to` applies one source to multiple destinations. All destinations are inspected and planned before the first write. A failed destination does not undo successful updates on earlier destinations.

A Claude sync stops before writing anything when the source settings have no `env.ANTHROPIC_BASE_URL` but a target has one, because the target would otherwise fall back to the default API. Pass `--allow-gateway-drop` if that is intended. A custom Claude API key always requires a source `ANTHROPIC_BASE_URL`, so the key is never sent to an unintended endpoint.

## Choose your own API key

Supply keys through an environment variable or private file. No literal API-key argument is provided, so keys do not end up in process arguments or shell history. The tool reads these inputs on the computer running it, even when `--from` points to an SSH machine.

```sh
# Use a key stored in a private file for both harnesses.
harness-sync sync --to devbox --api-key-file ~/.config/my-gateway/api-key --apply

# Use different keys for each harness. These variable names must already be set.
harness-sync sync --to devbox \
  --codex-api-key-env CODEX_GATEWAY_KEY \
  --claude-api-key-env CLAUDE_GATEWAY_KEY \
  --apply

# Set only Claude's key, keeping Codex out of this operation.
harness-sync sync --to devbox --harness claude \
  --claude-api-key-file ~/.config/my-gateway/claude-key --apply
```

Available options:

| Scope | Environment variable | Private file |
| --- | --- | --- |
| Both | `--api-key-env VAR` | `--api-key-file PATH` |
| Codex | `--codex-api-key-env VAR` | `--codex-api-key-file PATH` |
| Claude | `--claude-api-key-env VAR` | `--claude-api-key-file PATH` |

A harness-specific key takes precedence over a shared key. An explicit key takes precedence over credentials copied with `--with-secrets`. Custom keys work without `--with-secrets`.

For Codex, custom-key mode requires a custom `model_provider` in the source configuration. It writes the key to the target's `api-key` file and sets that provider's `auth` command to `/bin/cat` with the target path. This requires a Codex version supporting provider `auth` commands. For Claude, it sets `env.ANTHROPIC_AUTH_TOKEN` in `settings.json` and removes a conflicting `ANTHROPIC_API_KEY` or `apiKeyHelper` setting. Provider URLs come from the source configuration. Exported variables or wrapper scripts outside these files can still override harness behavior.

## Refresh Codex models before launch

If your source Codex directory contains a trusted `refresh-models.py`, opt in to copying it and creating a launcher:

```sh
harness-sync sync --to devbox --harness codex --with-model-refresh
harness-sync sync --to devbox --harness codex --with-model-refresh --apply

# On devbox: refresh the catalog, then start Codex with your arguments.
~/.local/bin/codex-with-models
~/.local/bin/codex-with-models --version
```

This copies `refresh-models.py` byte-for-byte into the target's Codex directory and installs `~/.local/bin/codex-with-models` with mode `0700`. The launcher sets `CODEX_HOME` to the synchronized directory, runs the script using the target's Python, and forwards all arguments to the existing Codex CLI. It uses `~/.local/bin/codex` when executable, otherwise a `codex` found on the target's PATH. It preserves the original CLI entry, so package-manager updates can continue to replace it. Use the full launcher path when `~/.local/bin` is absent from PATH. Ordinary `codex` does not run this launcher.

This is an explicit code-transfer option: inspect and trust the source script. No refresh script is bundled or executed by `harness-sync`; it runs later when you use the launcher. The script must be portable and compatible with the target's provider, credentials, Python, and CLI installation. Its own return code controls launch behavior: zero starts Codex, nonzero stops. A script that returns zero after retaining a valid cached catalog can allow startup when discovery is temporarily unavailable.

The source must select `model_catalog_json`, and Codex must already be installed on the target. Credentials are still opt-in through `--with-secrets` or custom-key options. The source script is required, not an existing source launcher: each destination launcher is generated for that machine. Script and launcher updates are backed up and restored like configuration files. Omit `--with-model-refresh` to leave them alone; this option does not add cron jobs or modify shell profiles. Model lists may differ between CLI versions when the refresh script requests a version-specific catalog.

## What gets synchronized

The source controls portable defaults. Stale portable settings on the target are removed; machine-specific sections stay on that target. This includes permission and sandbox defaults: syncing a source configured with broad permissions carries those choices to the destination. Preview first.

| Configuration | Synced | Kept on each target |
| --- | --- | --- |
| Codex `config.toml` | Model, reasoning effort, service tier, provider definitions, approval/sandbox defaults, portable features, profiles, other defaults | `projects`, `desktop`, `marketplaces`, `plugins`, `mcp_servers`, `hooks`, `notify`, `tui`; the hooks feature switch |
| Claude `settings.json` | Model, model settings, environment defaults, permissions, UI/update preferences, other defaults | `hooks`, `enabledPlugins`, `extraKnownMarketplaces`, `skillOverrides` |
| Codex model catalog | The JSON file referenced by `model_catalog_json`, normalized to `models.json` on the destination | Unreferenced catalogs |
| Codex refresh helper | Source `refresh-models.py` and a generated `codex-with-models` launcher only with `--with-model-refresh` | Existing helpers when the flag is omitted |
| Credentials | Supported inline credentials and files only with `--with-secrets`, or a selected custom API key | Target credentials by default |

Supported credential files are Codex `api-key`, Codex `auth.json`, and Claude `.credentials.json`. Keychain entries, browser logins, shell environment files, arbitrary files used by an auth command, and external secret helpers are not copied. Without `--with-secrets`, secret-like settings retain their corresponding target values; source-only secret-like settings are omitted. Arbitrary secrets in unusually named fields cannot be identified reliably. Treat configuration files as private.

Home and harness-directory paths inside portable setting values are remapped to the destination. Other absolute paths remain unchanged and must already exist on the destination. Source hooks, skills, plugins, MCP binaries, desktop integrations, project trust, project overrides, instructions, history, sessions, and caches are outside the sync scope. Existing target integrations are retained as-is; the tool does not repair them.

Codex `CODEX_HOME` and Claude `CLAUDE_CONFIG_DIR` are honored on each machine when visible to its Python process. SSH workers run as non-interactive commands; variables set only in an interactive shell may be absent. `--local-home PATH` selects isolated local `.codex` and `.claude` directories, useful for testing.

TOML is parsed and serialized with semantic validation. Comments and original formatting are not retained after a changed Codex config is written. JSON is formatted with two-space indentation. Unchanged configs are not rewritten.

## Backups and recovery

```sh
harness-sync backups --target devbox

# Substitute an ID printed by the previous command. Preview first.
harness-sync restore --target devbox --backup BACKUP_ID
harness-sync restore --target devbox --backup BACKUP_ID --apply
```

Backups live on the destination under `${XDG_STATE_HOME:-~/.local/state}/harness-sync/backups/`. Each backup records the prior content and which files did not exist. Restoring removes files created by the original sync and creates a new backup so the restore can itself be undone. Backup IDs are machine-local. No automatic pruning is performed.

Configuration files and refresh scripts are written with mode `0600`; the optional executable launcher and backup directories use `0700`. The tool checks for concurrent edits before writing, serializes its own writes with a destination lock, and uses atomic replacement for each file. If a normal write fails, it attempts to roll back that destination. A process kill, power loss, or a writer ignoring the lock can still interrupt a multi-file operation; use the backup to recover. Symlink configuration files are rejected rather than replaced.

## Privacy

This repository contains only tool code, documentation, and synthetic test fixtures. Running the tool reads real private configuration directly from the selected machines; it does not export a profile or put configuration into the checkout. Preview output lists changed key names, never values or content diffs. No telemetry is sent.

SSH encrypts configuration in transit. Remote worker arguments contain the public tool code; configuration and credentials travel over stdin. Backups can contain credentials and should remain private. Do not commit your real configuration, key files, or backups. The `.gitignore` provides a few common exclusions, not a complete secret detector.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m pip install .
harness-sync --version
```

Tests cover credential opt-in, custom keys, path mapping, stale-default removal, machine-specific preservation, preview privacy, idempotence, backups/restores, concurrent edits, rollback, TOML round trips, the SSH worker protocol, and model-refresh launch order, failure handling, and argument/path quoting. CI runs on macOS and Linux with Python 3.11 and 3.13.

Codex user-default behavior: [official configuration documentation](https://developers.openai.com/codex/config-basic/).

MIT licensed. Maintained by Lan Zongwei · i@soullan.com
