import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import harness_sync as hs


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source"
        self.target = Path(self.temp.name) / "target"
        for home in (self.source, self.target):
            (home / ".codex").mkdir(parents=True)
            (home / ".claude").mkdir()
        self.put(self.source, "codex/config.toml", {
            "model": "example-model", "model_provider": "example",
            "model_reasoning_effort": "high", "service_tier": "fast",
            "approval_policy": "never", "sandbox_mode": "danger-full-access",
            "model_providers": {"example": {
                "base_url": "https://gateway.example.test/v1", "wire_api": "responses",
                "auth": {"command": "/bin/cat", "args": [str(self.source / ".codex/api-key")]},
                "http_headers": {"Authorization": "fixture-source-secret"},
            }},
            "model_catalog_json": str(self.source / ".codex/models.json"),
            "projects": {"/example/source-project": {"trust_level": "trusted"}},
            "notify": ["/example/mac-notifier"],
            "mcp_servers": {"source-only": {"command": "/example/mac-app"}},
            "features": {"hooks": True, "example": True},
        })
        self.put(self.source, "claude/settings.json", {
            "model": "example-opus", "env": {"ANTHROPIC_AUTH_TOKEN": "fixture-source-secret", "ANTHROPIC_BASE_URL": "https://gateway.example.test"},
            "hooks": {"SessionStart": ["source hook"]},
            "modelSettings": {"example-opus": {"effortLevel": "high"}},
            "permissions": {"defaultMode": "bypassPermissions"},
        })
        self.put(self.target, "codex/config.toml", {
            "model": "old-model", "model_context_window": 99999,
            "projects": {"/example/target-project": {"trust_level": "trusted"}},
            "mcp_servers": {"target-only": {"command": "target-tool"}},
            "features": {"hooks": False},
        })
        self.put(self.target, "claude/settings.json", {
            "model": "old-opus", "env": {"ANTHROPIC_AUTH_TOKEN": "fixture-target-secret", "OLD_OPTION": "1"},
            "hooks": {"Stop": ["target hook"]},
        })
        (self.source / ".codex/models.json").write_text('{"models": []}')
        (self.source / ".codex/api-key").write_text("fixture-source-key\n")

    def put(self, home, label, data):
        p = hs.file_path(label, hs.roots(str(home)))
        p.write_bytes(hs.toml_dump(data) if label.endswith("toml") else json.dumps(data).encode())

    def snapshot(self, home, secrets=False):
        return hs.inspect(str(home), secrets=secrets)

    def plan(self, secrets=False, keys=None):
        return hs.build_plan(self.snapshot(self.source, secrets), self.snapshot(self.target, secrets), secrets=secrets, api_keys=keys)

    def read_plan(self, plan, label):
        return hs.parse_config(label, hs.decode(plan["writes"][label]))

    def test_defaults_replace_stale_settings_preserve_machine_state(self):
        plan = self.plan()
        codex = self.read_plan(plan, "codex/config.toml")
        self.assertEqual(codex["model"], "example-model")
        self.assertNotIn("model_context_window", codex)
        self.assertIn("/example/target-project", codex["projects"])
        self.assertEqual(list(codex["mcp_servers"]), ["target-only"])
        self.assertNotIn("notify", codex)
        self.assertFalse(codex["features"]["hooks"])
        provider = codex["model_providers"]["example"]
        self.assertEqual(provider["auth"]["args"], [str(self.target / ".codex/api-key")])
        self.assertNotIn("http_headers", provider)
        claude = self.read_plan(plan, "claude/settings.json")
        self.assertEqual(claude["env"]["ANTHROPIC_AUTH_TOKEN"], "fixture-target-secret")
        self.assertNotIn("OLD_OPTION", claude["env"])
        self.assertEqual(claude["hooks"], {"Stop": ["target hook"]})
        self.assertNotIn("codex/api-key", plan["writes"])

    def test_explicit_secret_transfer_and_preview_does_not_print_values(self):
        plan = self.plan(secrets=True)
        self.assertEqual(hs.decode(plan["writes"]["codex/api-key"]), b"fixture-source-key\n")
        claude = self.read_plan(plan, "claude/settings.json")
        self.assertEqual(claude["env"]["ANTHROPIC_AUTH_TOKEN"], "fixture-source-secret")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            hs.show_plan("example-ssh-alias", plan)
        for value in ("fixture-source-secret", "fixture-source-key", "gateway.example.test"):
            self.assertNotIn(value, stream.getvalue())

    def test_custom_keys_override_source_auth_without_bulk_secret_transfer(self):
        plan = self.plan(keys={"codex": "fixture-custom-codex", "claude": "fixture-custom-claude"})
        self.assertEqual(hs.decode(plan["writes"]["codex/api-key"]), b"fixture-custom-codex\n")
        codex = self.read_plan(plan, "codex/config.toml")
        self.assertNotIn("http_headers", codex["model_providers"]["example"])
        claude = self.read_plan(plan, "claude/settings.json")
        self.assertEqual(claude["env"]["ANTHROPIC_AUTH_TOKEN"], "fixture-custom-claude")
        # A shared source key must never overwrite an explicit replacement.
        plan = self.plan(secrets=True, keys={"codex": "fixture-override"})
        self.assertEqual(hs.decode(plan["writes"]["codex/api-key"]), b"fixture-override\n")

    def test_env_and_file_key_sources_and_override_precedence(self):
        keyfile = Path(self.temp.name) / "fixture-key"
        keyfile.write_text("fixture-file-key\n")
        args = type("Args", (), {
            "api_key_env": "FIXTURE_API_KEY", "api_key_file": None,
            "codex_api_key_env": None, "codex_api_key_file": str(keyfile),
            "claude_api_key_env": None, "claude_api_key_file": None, "harness": "both",
        })
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "fixture-env-key"}):
            self.assertEqual(hs.load_api_keys(args), {"codex": "fixture-file-key", "claude": "fixture-env-key"})
        with patch.dict(os.environ, {"FIXTURE_API_KEY": "invalid key"}):
            with self.assertRaises(hs.SyncError):
                hs.load_api_keys(args)

    def test_apply_backup_restore_and_idempotence(self):
        originals = {label: hs.read_file(hs.file_path(label, hs.roots(str(self.target)))) for label in hs.ALLOWED}
        result = hs.apply_plan(self.plan(secrets=True), str(self.target))
        self.assertTrue(result["backup"])
        self.assertEqual(self.plan(secrets=True)["writes"], {})
        for label in result["updated"]:
            self.assertEqual(hs.file_path(label, hs.roots(str(self.target))).stat().st_mode & 0o777, 0o600)
        backup = hs.state_dir(str(self.target)) / "backups" / result["backup"]
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        restore = hs.restore_plan(result["backup"], str(self.target))
        restored = hs.apply_plan(restore, str(self.target))
        self.assertTrue(restored["backup"])
        for label, data in originals.items():
            self.assertEqual(hs.read_file(hs.file_path(label, hs.roots(str(self.target)))), data)

    def test_concurrent_edit_is_rejected_before_any_write(self):
        plan = self.plan()
        p = self.target / ".claude/settings.json"
        p.write_text('{"model": "concurrent-change"}')
        before = (self.target / ".codex/config.toml").read_bytes()
        with self.assertRaisesRegex(hs.SyncError, "changed during sync"):
            hs.apply_plan(plan, str(self.target))
        self.assertEqual((self.target / ".codex/config.toml").read_bytes(), before)

    def test_custom_key_sync_is_idempotent(self):
        keys = {"codex": "fixture-custom-key", "claude": "fixture-other-key"}
        source = self.snapshot(self.source)
        target = hs.inspect(str(self.target), api_key=True)
        plan = hs.build_plan(source, target, api_keys=keys)
        hs.apply_plan(plan, str(self.target))
        target = hs.inspect(str(self.target), api_key=True)
        self.assertEqual(hs.build_plan(source, target, api_keys=keys)["writes"], {})

    def test_dangling_target_catalog_can_be_repaired(self):
        self.put(self.target, "codex/config.toml", {"model_catalog_json": "/nonexistent/example/models.json"})
        target = hs.inspect(str(self.target), catalog=False)
        plan = hs.build_plan(self.snapshot(self.source), target)
        self.assertIn("codex/models.json", plan["writes"])

    def test_missing_custom_provider_is_rejected(self):
        self.put(self.source, "codex/config.toml", {"model": "example-model"})
        with self.assertRaisesRegex(hs.SyncError, "custom model_provider"):
            self.plan(keys={"codex": "fixture-key"})

    def test_partial_write_failure_rolls_back(self):
        plan = self.plan(secrets=True)
        originals = {k: hs.read_file(hs.file_path(k, hs.roots(str(self.target)))) for k in plan["writes"]}
        real = hs.atomic_write
        def fail_one(p, data, mode=0o600):
            if p == self.target / ".claude/settings.json":
                raise OSError("fixture write failure")
            real(p, data, mode)
        with patch.object(hs, "atomic_write", side_effect=fail_one):
            with self.assertRaises(OSError):
                hs.apply_plan(plan, str(self.target))
        for label, data in originals.items():
            self.assertEqual(hs.read_file(hs.file_path(label, hs.roots(str(self.target)))), data)

    def test_invalid_json_does_not_echo_secrets(self):
        with self.assertRaises(hs.SyncError) as e:
            hs.parse_config("claude/settings.json", b'{"fixture-secret-value"')
        self.assertNotIn("fixture-secret-value", str(e.exception))

    def test_token_budgets_are_settings_and_auth_tokens_are_secrets(self):
        source = {"max_tokens": 1000, "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "10000", "ANTHROPIC_AUTH_TOKEN": "fixture-secret", "accessToken": "fixture-oauth"}
        result = hs.protect_secrets(source, {}, False)
        self.assertEqual(result, {"max_tokens": 1000, "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "10000"})

    def test_symlinks_and_path_traversal_are_rejected(self):
        key = self.target / ".codex/api-key"
        key.symlink_to(self.source / ".codex/api-key")
        with self.assertRaises(hs.SyncError):
            self.snapshot(self.target, secrets=True)
        with self.assertRaises(hs.SyncError):
            hs.file_path("codex/../../arbitrary-file", hs.roots(str(self.target)))
        with self.assertRaises(hs.SyncError):
            hs.restore_plan("../../bad", str(self.target))

    def test_ssh_options_and_no_credentials_in_argv(self):
        self.assertEqual(hs.ssh_args("ssh://alice@example.test:2222")[-4:], ["-p", "2222", "--", "alice@example.test"])
        for endpoint in ("-oProxyCommand=bad", "host name", "ssh://u:pass@example.test", "ssh://example.test/private"):
            with self.assertRaises(hs.SyncError):
                hs.ssh_args(endpoint)

    def test_ssh_worker_runs_public_source_and_receives_secrets_on_stdin(self):
        seen = {}
        def fake_ssh(argv, **kwargs):
            seen["argv"] = argv
            seen["input"] = kwargs["input"]
            request = json.loads(kwargs["input"])
            request["home"] = str(self.target)
            command = shlex_split(argv[-1])
            return subprocess.run([sys.executable, *command[1:]], input=json.dumps(request).encode(), capture_output=True)
        request = {"operation": "apply", "plan": self.plan(secrets=True)}
        run = subprocess.run
        def dispatch(argv, **kwargs):
            with patch.object(subprocess, "run", run):
                return fake_ssh(argv, **kwargs)
        with patch.object(hs.subprocess, "run", side_effect=dispatch):
            result = hs.call("example-ssh-alias", request)
        self.assertTrue(result["updated"])
        self.assertNotIn("fixture-source-secret", " ".join(seen["argv"]))
        self.assertIn(hs.encode(b"fixture-source-key\n"), seen["input"].decode())

    def test_toml_quoted_keys_arrays_and_datetime_round_trip(self):
        import datetime
        data = {"str": "a\nline\twith \"quote\"", "rows": [{"a.b": [1, True, "test"]}], "quoted.table": {"unicode": "中文"}, "date": datetime.date(2026, 1, 1)}
        self.assertEqual(tomllib.loads(hs.toml_dump(data).decode()), data)


def shlex_split(command):
    import shlex
    return shlex.split(command)


if __name__ == "__main__":
    unittest.main()
