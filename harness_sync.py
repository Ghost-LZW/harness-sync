#!/usr/bin/env python3
"""Synchronize Codex and Claude defaults over SSH without a remote install."""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import uuid
from urllib.parse import urlsplit

__version__ = "0.3.0"
MAX_FILE = 16 * 1024 * 1024
MACHINE_CODEX = {
    "projects", "desktop", "marketplaces", "plugins", "mcp_servers",
    "hooks", "notify", "tui",
}
MACHINE_CLAUDE = {
    "hooks", "enabledPlugins", "extraKnownMarketplaces", "skillOverrides",
}
SECRET_KEY = re.compile(
    r"(?:^|_)(?:access_|refresh_|auth_|bearer_)?token$|(?:access|refresh|auth|bearer)Token$|"
    r"secret|password|credential|authorization|(?:^|_)api_?key$|"
    r"^apiKey$|^key$|^http_headers$|^headers$|^dsn$|_DSN$|^apiKeyHelper$", re.I
)
ALLOWED = {
    "codex/config.toml", "codex/models.json", "codex/api-key", "codex/auth.json",
    "claude/settings.json", "claude/.credentials.json",
    "codex/refresh-models.py", "bin/codex-with-models",
    "shell/.zshrc", "shell/.bashrc", "home/.claude-ag-settings.json",
}
CONFIGS = {"codex/config.toml", "claude/settings.json"}
SENSITIVE_FILES = {"codex/api-key", "codex/auth.json", "claude/.credentials.json"}
MODEL_REFRESH_FILES = {"codex/refresh-models.py", "bin/codex-with-models"}
# Written only by the launchers command; sync never reads or copies them.
LAUNCHER_FILES = {"shell/.zshrc", "shell/.bashrc", "home/.claude-ag-settings.json"}
LAUNCHER_MARK = "# agent launchers (harness-sync)"


class SyncError(Exception):
    pass


def encode(data):
    return base64.b64encode(data).decode("ascii")


def decode(data):
    return base64.b64decode(data, validate=True)


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def roots(home=None):
    h = Path(home).expanduser().absolute() if home else Path.home()
    return {
        "home": str(h),
        "codex": str(h / ".codex" if home else Path(os.environ.get("CODEX_HOME", h / ".codex")).expanduser().absolute()),
        "claude": str(h / ".claude" if home else Path(os.environ.get("CLAUDE_CONFIG_DIR", h / ".claude")).expanduser().absolute()),
    }


def file_path(label, location):
    if label not in ALLOWED:
        raise SyncError("unsupported file label")
    harness, name = label.split("/", 1)
    if harness == "bin":
        directory = Path(location["home"]) / ".local/bin"
    elif harness in ("shell", "home"):
        directory = Path(location["home"])
    else:
        directory = Path(location[harness])
    p = directory / name
    if p.is_symlink():
        raise SyncError(f"refusing symlink file: {label}")
    return p


def read_file(p):
    if not p.exists():
        return None
    if not p.is_file() or p.stat().st_size > MAX_FILE:
        raise SyncError("expected a regular configuration file smaller than 16 MiB")
    return p.read_bytes()


def parse_config(label, data):
    try:
        value = tomllib.loads(data.decode()) if label.endswith(".toml") else json.loads(data)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError) as e:
        # Parser exceptions can contain secret values; never echo them.
        raise SyncError(f"invalid configuration: {label}") from e


def toml_dump(data):
    """Serialize the TOML types accepted by tomllib, with quoted keys."""
    def key(k):
        return json.dumps(k, ensure_ascii=False)

    def value(v):
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, (dt.datetime, dt.date, dt.time)):
            return v.isoformat()
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{ " + ", ".join(key(k) + " = " + value(x) for k, x in v.items()) + " }"
        raise SyncError("unsupported TOML value")

    lines = []

    def table(d, path):
        if path:
            lines.extend(["", "[" + ".".join(key(k) for k in path) + "]"])
        for k, v in d.items():
            if not isinstance(v, dict):
                lines.append(key(k) + " = " + value(v))
        for k, v in d.items():
            if isinstance(v, dict):
                table(v, path + [k])

    table(data, [])
    result = "\n".join(lines).lstrip() + "\n"
    if tomllib.loads(result) != data:
        raise SyncError("TOML round-trip validation failed")
    return result.encode()


def portable(data, harness):
    excluded = MACHINE_CODEX if harness == "codex" else MACHINE_CLAUDE
    return {k: copy.deepcopy(v) for k, v in data.items() if k not in excluded}


def protect_secrets(source, target, include):
    if include:
        return copy.deepcopy(source)
    if isinstance(source, dict):
        target = target if isinstance(target, dict) else {}
        result = {}
        for k, v in source.items():
            if SECRET_KEY.search(k) and k not in {"env_key", "env_key_instructions"}:
                if k in target:
                    result[k] = copy.deepcopy(target[k])
            else:
                result[k] = protect_secrets(v, target.get(k), False)
        # Keep target credentials even when the source uses a different mechanism.
        for k, v in target.items():
            if k not in source and SECRET_KEY.search(k) and k != "env_key":
                result[k] = copy.deepcopy(v)
        return result
    if isinstance(source, list):
        return [protect_secrets(v, None, False) for v in source]
    return copy.deepcopy(source)


def remap(value, source, target):
    pairs = sorted([(source[k], target[k]) for k in ("codex", "claude", "home")], key=lambda x: -len(x[0]))
    if isinstance(value, dict):
        return {k: remap(v, source, target) for k, v in value.items()}
    if isinstance(value, list):
        return [remap(v, source, target) for v in value]
    if isinstance(value, str):
        # Replace in one pass so a target path is never rewritten a second time.
        pattern = "|".join(re.escape(a) + r"(?=/|$|[\s'\" :])" for a, _ in pairs)
        mapping = dict(pairs)
        return re.sub(pattern, lambda m: mapping[m.group()], value)
    return value


def changed_keys(a, b, prefix=""):
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            path = f"{prefix}.{k}" if prefix else k
            if k not in a or k not in b:
                out.append(path)
            else:
                out.extend(changed_keys(a[k], b[k], path))
        return out
    return [prefix] if a != b else []


def inspect(home=None, harness="both", secrets=False, api_key=False, catalog=True, model_refresh=False):
    location = roots(home)
    files = {}
    for label in sorted(ALLOWED - LAUNCHER_FILES):
        if label in MODEL_REFRESH_FILES and not model_refresh:
            continue
        if harness == "claude" and label in MODEL_REFRESH_FILES:
            continue
        if harness != "both" and not label.startswith(harness + "/") and label not in MODEL_REFRESH_FILES:
            continue
        if label in SENSITIVE_FILES and not secrets and not (api_key and label == "codex/api-key"):
            continue
        data = read_file(file_path(label, location))
        if data is not None:
            files[label] = encode(data)
    # A custom catalog is normalized to models.json at the destination.
    if catalog and "codex/config.toml" in files:
        config = parse_config("codex/config.toml", decode(files["codex/config.toml"]))
        if config.get("model_catalog_json"):
            p = Path(config["model_catalog_json"]).expanduser()
            if not p.is_absolute():
                raise SyncError("model_catalog_json must be an absolute path")
            catalog = read_file(p)
            if catalog is None:
                raise SyncError("source model catalog does not exist")
            try:
                json.loads(catalog)
            except ValueError as e:
                raise SyncError("invalid model catalog JSON") from e
            files["codex/models.json"] = encode(catalog)
    result = {"roots": location, "files": files}
    if model_refresh and harness != "claude":
        # Preserve the original CLI entry so package upgrades still take effect.
        binary = Path(location["home"]) / ".local/bin/codex"
        if not binary.is_file() or not os.access(binary, os.X_OK):
            import shutil
            found = shutil.which("codex")
            binary = Path(found) if found else None
        if binary is not None:
            if binary.resolve() == file_path("bin/codex-with-models", location).resolve():
                raise SyncError("Codex binary points to the model-refresh launcher")
            result["codex_binary"] = str(binary.absolute())
        result["python"] = sys.executable
    return result


def check_claude_gateway(new, old, custom_key, allow_gateway_drop):
    """Refuse Claude settings that would silently point the target at the default API."""
    if (new.get("env") or {}).get("ANTHROPIC_BASE_URL"):
        return
    if custom_key:
        # Never send a gateway key to whatever endpoint Claude falls back to.
        raise SyncError("a custom Claude API key requires env.ANTHROPIC_BASE_URL in the source settings")
    if (old.get("env") or {}).get("ANTHROPIC_BASE_URL") and not allow_gateway_drop:
        raise SyncError(
            "source Claude settings have no env.ANTHROPIC_BASE_URL but the target does; "
            "syncing would remove the target's gateway (use --allow-gateway-drop to proceed)"
        )


def build_plan(source, target, harness="both", secrets=False, api_keys=None, model_refresh=False,
               allow_gateway_drop=False):
    api_keys = api_keys or {}
    writes, changes = {}, {}
    for name, label in (("codex", "codex/config.toml"), ("claude", "claude/settings.json")):
        if harness not in ("both", name):
            continue
        if label not in source["files"]:
            raise SyncError(f"source configuration is missing: {label}")
        src = parse_config(label, decode(source["files"][label]))
        old = parse_config(label, decode(target["files"][label])) if label in target["files"] else {}
        new = remap(portable(src, name), source["roots"], target["roots"])
        if name == "codex" and "model_catalog_json" in new:
            new["model_catalog_json"] = str(Path(target["roots"]["codex"]) / "models.json")
        new = protect_secrets(new, portable(old, name), secrets)
        if name in api_keys:
            if name == "codex":
                provider = new.get("model_provider")
                if not provider or provider not in new.get("model_providers", {}):
                    raise SyncError("custom Codex API key requires a configured custom model_provider")
                settings = new["model_providers"][provider]
                for k in ("env_key", "experimental_bearer_token", "auth"):
                    settings.pop(k, None)
                for k in list(settings.get("http_headers", {})):
                    if k.lower() in ("authorization", "x-api-key"):
                        del settings["http_headers"][k]
                for k in list(settings.get("env_http_headers", {})):
                    if k.lower() in ("authorization", "x-api-key"):
                        del settings["env_http_headers"][k]
                settings["auth"] = {
                    "command": "/bin/cat",
                    "args": [str(Path(target["roots"]["codex"]) / "api-key")],
                    "timeout_ms": 5000, "refresh_interval_ms": 0,
                }
                key_label = "codex/api-key"
                key_data = (api_keys[name] + "\n").encode()
                if encode(key_data) != target["files"].get(key_label):
                    writes[key_label] = key_data
                    changes[key_label] = ["custom API key (value hidden)"]
            else:
                env = new.setdefault("env", {})
                env.pop("ANTHROPIC_API_KEY", None)
                env["ANTHROPIC_AUTH_TOKEN"] = api_keys[name]
                new.pop("apiKeyHelper", None)
        if name == "claude":
            check_claude_gateway(new, old, name in api_keys, allow_gateway_drop)
        excluded = MACHINE_CODEX if name == "codex" else MACHINE_CLAUDE
        new.update({k: v for k, v in old.items() if k in excluded})
        # Hooks remain target-local, including their trust state and feature switch.
        if name == "codex":
            if "hooks" in old.get("features", {}):
                new.setdefault("features", {})["hooks"] = old["features"]["hooks"]
            elif not old.get("hooks"):
                new.get("features", {}).pop("hooks", None)
            if new.get("features") == {}:
                new.pop("features", None)
        keys = changed_keys(old, new)
        if keys:
            data = toml_dump(new) if name == "codex" else (json.dumps(new, indent=2, ensure_ascii=False) + "\n").encode()
            parse_config(label, data)
            writes[label], changes[label] = data, keys
    for label, encoded in source["files"].items():
        if label in MODEL_REFRESH_FILES:
            continue
        if label in CONFIGS or (label in SENSITIVE_FILES and not secrets):
            continue
        if harness != "both" and not label.startswith(harness + "/"):
            continue
        if label.split("/", 1)[0] in api_keys and label in SENSITIVE_FILES:
            continue
        # Only transfer a catalog when the source config actually selects it.
        if label == "codex/models.json":
            config = parse_config("codex/config.toml", decode(source["files"]["codex/config.toml"]))
            if not config.get("model_catalog_json"):
                continue
        if encoded != target["files"].get(label):
            writes[label] = decode(encoded)
            changes[label] = ["content (values hidden)"]
    if model_refresh:
        if harness == "claude":
            raise SyncError("--with-model-refresh requires the Codex harness")
        script_label = "codex/refresh-models.py"
        if script_label not in source["files"]:
            raise SyncError("source refresh-models.py is missing from the Codex configuration directory")
        if not target.get("codex_binary"):
            raise SyncError("cannot find an executable Codex CLI on the target")
        src = parse_config("codex/config.toml", decode(source["files"]["codex/config.toml"]))
        if not src.get("model_catalog_json"):
            raise SyncError("model refresh requires model_catalog_json in the source configuration")
        script = decode(source["files"][script_label])
        try:
            compile(script, "refresh-models.py", "exec")
        except (SyntaxError, ValueError) as e:
            raise SyncError("source refresh-models.py is not valid Python") from e
        location = target["roots"]
        wrapper = (
            '#!/bin/sh\nset -eu\n'
            + "export CODEX_HOME=" + shlex.quote(location["codex"]) + "\n"
            + shlex.quote(target["python"]) + " " + shlex.quote(str(Path(location["codex"]) / "refresh-models.py")) + "\n"
            + "exec " + shlex.quote(target["codex_binary"]) + ' "$@"\n'
        ).encode()
        for label, data in ((script_label, script), ("bin/codex-with-models", wrapper)):
            if encode(data) != target["files"].get(label):
                writes[label] = data
                changes[label] = ["model-refresh script" if label == script_label else "refresh before starting Codex"]
    return {
        "writes": {k: encode(v) for k, v in writes.items()},
        "expected": {k: digest(decode(target["files"][k])) if k in target["files"] else None for k in writes},
        "changes": changes,
    }


def load_key(env, filename):
    if env:
        value = os.environ.get(env)
        if value is None:
            raise SyncError("API key environment variable is not set")
    elif filename:
        p = Path(filename).expanduser()
        data = read_file(p)
        if data is None:
            raise SyncError("API key file does not exist")
        value = data.decode()
    else:
        return None
    value = value.strip()
    if not value or re.search(r"[\x00-\x20\x7f]", value):
        raise SyncError("API key must be a single nonempty token")
    return value


def load_api_keys(args):
    load = load_key
    shared = load(args.api_key_env, args.api_key_file)
    result = {}
    for harness in ("codex", "claude"):
        specific = load(getattr(args, harness + "_api_key_env"), getattr(args, harness + "_api_key_file"))
        if specific is not None or shared is not None:
            if args.harness in ("both", harness):
                result[harness] = specific if specific is not None else shared
    return result


def state_dir(home=None):
    # Explicit home is useful for isolated testing; normal operation honors XDG.
    h = Path(roots(home)["home"])
    return (h / ".local/state" if home else Path(os.environ.get("XDG_STATE_HOME", h / ".local/state"))) / "harness-sync"


def private_dir(p):
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if p.is_symlink():
        raise SyncError("refusing symlink state directory")
    p.chmod(0o700)


def atomic_write(p, data, mode=0o600):
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".harness-sync-", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, p)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextlib.contextmanager
def state_lock(home):
    state = state_dir(home)
    private_dir(state)
    lock = state / "lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield state


def apply_plan(plan, home=None):
    location = roots(home)
    with state_lock(home) as state:
        before, modes = {}, {}
        for label in plan["writes"]:
            p = file_path(label, location)
            before[label] = read_file(p)
            modes[label] = p.stat().st_mode & 0o777 if p.exists() else 0o600
            if digest(before[label]) != plan["expected"].get(label):
                raise SyncError(f"configuration changed during sync; retry: {label}")
            data = plan["writes"][label]
            if data is not None:
                raw = decode(data)
                if len(raw) > MAX_FILE:
                    raise SyncError("configuration exceeds size limit")
                if label in CONFIGS:
                    parse_config(label, raw)
        if not before:
            return {"backup": None, "updated": []}
        backup_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        backup = state / "backups" / backup_id
        private_dir(backup.parent)
        private_dir(backup)
        manifest = {"version": 1, "roots": location, "files": {}}
        for label, data in before.items():
            manifest["files"][label] = {"exists": data is not None, "mode": modes[label], "sha256": digest(data)}
            if data is not None:
                atomic_write(backup / label, data)
        atomic_write(backup / "manifest.json", json.dumps(manifest).encode())
        done = []
        try:
            for label, encoded in plan["writes"].items():
                p = file_path(label, location)
                if encoded is None:
                    p.unlink(missing_ok=True)
                else:
                    if label == "bin/codex-with-models":
                        mode = 0o700
                    elif label.startswith("shell/") and before[label] is not None:
                        mode = modes[label]  # rc files keep their permissions
                    else:
                        mode = 0o600
                    atomic_write(p, decode(encoded), mode)
                done.append(label)
        except BaseException:
            for label in reversed(done):
                p = file_path(label, location)
                if before[label] is None:
                    p.unlink(missing_ok=True)
                else:
                    atomic_write(p, before[label], modes[label])
            raise
        return {"backup": backup_id, "updated": done}


def restore_plan(backup_id, home=None):
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", backup_id):
        raise SyncError("invalid backup ID")
    backup = state_dir(home) / "backups" / backup_id
    if backup.is_symlink():
        raise SyncError("refusing symlink backup")
    try:
        manifest = json.loads((backup / "manifest.json").read_bytes())
    except (OSError, ValueError) as e:
        raise SyncError("backup manifest unavailable or invalid") from e
    location = roots(home)
    if manifest.get("version") != 1 or manifest.get("roots") != location:
        raise SyncError("backup belongs to different configuration directories")
    writes, expected, changes = {}, {}, {}
    for label, meta in manifest["files"].items():
        current = read_file(file_path(label, location))
        p = backup / label
        if p.is_symlink():
            raise SyncError("refusing symlink backup file")
        data = read_file(p) if meta["exists"] else None
        if digest(data) != meta["sha256"]:
            raise SyncError("backup integrity check failed")
        if current != data:
            writes[label] = encode(data) if data is not None else None
            expected[label] = digest(current)
            changes[label] = ["restore previous content" if data is not None else "remove previously absent file"]
    return {"writes": writes, "expected": expected, "changes": changes}


def reachable(url, timeout=5):
    # Require an HTTP reply: some networks accept the TCP connection and then never answer.
    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url.rstrip("/") + "/", timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # any status code means the gateway answered
    except (OSError, ValueError):
        return False


def shell_rc(home=None):
    # The login shell decides which rc file interactive sessions (and the hub) read.
    return "shell/.zshrc" if Path(os.environ.get("SHELL", "")).name == "zsh" else "shell/.bashrc"


def inspect_launchers(home=None, gateways=(), provider="astergate"):
    location = roots(home)
    rc = shell_rc(home)
    files = {}
    for label in (rc, "codex/config.toml"):
        data = read_file(file_path(label, location))
        if data is not None:
            files[label] = encode(data)
    settings = Path(location["home"]) / ".claude-ag-settings.json"
    present = {"home/.claude-ag-settings.json": settings.exists() or settings.is_symlink()}
    for label in ("codex/api-key", "codex/models.json"):
        p = Path(location["codex"]) / label.split("/", 1)[1]
        present[label] = p.exists() and p.stat().st_size > 0
    # The first gateway this machine can connect to wins (e.g. IPv4 before IPv6).
    gateway = next((g for g in gateways if reachable(g)), None)
    return {"roots": location, "rc": rc, "files": files, "present": present, "gateway": gateway}


def launcher_aliases(provider, model):
    return {
        "claude-ag": "claude --dangerously-skip-permissions --settings ~/.claude-ag-settings.json",
        # Codex 0.166+ rejects --profile when config.toml still has [profiles.*]; -c overrides work on every version.
        "codex-ag": f'codex -c model_provider={provider} -c model={model} -c model_catalog_json="$HOME/.codex/models.json"',
    }


CLAUDE_LAUNCHER_MODELS = {
    "main": "claude-opus-5-5",                   # default, opus, fable
    "sonnet": "dsv41-flash-uncensored[1m]",      # sonnet and subagents
    "haiku": "glm-5.3[1m]",                      # haiku and background tasks
}


def claude_launcher_settings(gateway, key, models=None):
    m = {**CLAUDE_LAUNCHER_MODELS, **(models or {})}
    return {
        "env": {
            "ANTHROPIC_BASE_URL": gateway,
            "ANTHROPIC_AUTH_TOKEN": key,
            "ANTHROPIC_MODEL": m["main"],
            "ANTHROPIC_DEFAULT_OPUS_MODEL": m["main"],
            "ANTHROPIC_DEFAULT_FABLE_MODEL": m["main"],
            "ANTHROPIC_DEFAULT_SONNET_MODEL": m["sonnet"],
            "CLAUDE_CODE_SUBAGENT_MODEL": m["sonnet"],
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": m["haiku"],
            "ANTHROPIC_SMALL_FAST_MODEL": m["haiku"],
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        },
        "permissions": {"defaultMode": "bypassPermissions"},
    }


def build_launcher_plan(target, claude_key=None, codex_key=None, catalog=None, provider="astergate", model="gpt-6.1-sol", claude_models=None):
    gateway = target.get("gateway")
    if not gateway:
        raise SyncError("no gateway is reachable from this machine")
    gateway = gateway.rstrip("/")
    location = target["roots"]
    writes, changes = {}, {}
    if not target["present"]["home/.claude-ag-settings.json"]:
        if claude_key is None:
            raise SyncError("Claude gateway API key is required")
        settings = claude_launcher_settings(gateway, claude_key, claude_models)
        writes["home/.claude-ag-settings.json"] = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode()
        changes["home/.claude-ag-settings.json"] = ["new launcher settings (values hidden)"]
    for label, data, what in (("codex/api-key", codex_key, "Codex API key"), ("codex/models.json", catalog, "Codex model catalog")):
        if not target["present"][label]:
            if data is None:
                raise SyncError(f"{what} is required")
            writes[label] = data
            changes[label] = ["content (values hidden)"]
    old = decode(target["files"]["codex/config.toml"]) if "codex/config.toml" in target["files"] else b""
    config = parse_config("codex/config.toml", old) if old else {}
    if provider not in config.get("model_providers", {}):
        # Append instead of re-serializing so comments and key order survive.
        block = toml_dump({"model_providers": {provider: {
            "name": provider, "base_url": gateway + "/v1", "wire_api": "responses",
            "auth": {"command": "/bin/cat", "args": [str(Path(location["codex"]) / "api-key")],
                     "timeout_ms": 5000, "refresh_interval_ms": 0},
        }}})
        new = old + (b"" if not old or old.endswith(b"\n") else b"\n") + b"\n" + block
        parse_config("codex/config.toml", new)
        writes["codex/config.toml"] = new
        changes["codex/config.toml"] = [f"model_providers.{provider}"]
    rc = target["rc"]
    text = decode(target["files"][rc]).decode() if rc in target["files"] else ""
    lines = []
    for name, command in launcher_aliases(provider, model).items():
        # An existing alias of the same name is the user's choice; never override it.
        if not re.search(rf"^\s*alias\s+{re.escape(name)}=", text, re.M):
            lines.append(f"alias {name}={shlex.quote(command)}")
            changes.setdefault(rc, []).append(f"alias {name}")
    if lines:
        prefix = "" if not text or text.endswith("\n") else "\n"
        mark = "" if LAUNCHER_MARK in text else "\n" + LAUNCHER_MARK + "\n"
        writes[rc] = (text + prefix + mark + "\n".join(lines) + "\n").encode()
    return {
        "writes": {k: encode(v) for k, v in writes.items()},
        "expected": {k: digest(decode(target["files"][k])) if k in target["files"] else None for k in writes},
        "changes": changes,
    }


VERIFY = {
    "claude-ag": "claude-ag -p 'reply pong'",
    "codex-ag": "codex-ag exec --skip-git-repo-check 'reply pong'",
}


def verify_launchers(home=None):
    # Interactive shell: PATH and aliases often live only in the rc file.
    shell = os.environ.get("SHELL") or "/bin/sh"
    result = {}
    for name, command in VERIFY.items():
        try:
            out = subprocess.run([shell, "-ic", command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, cwd=roots(home)["home"], timeout=240).stdout
            # Report only pass/fail; launcher output may include configuration.
            result[name] = bool(re.search(rb"^\s*pong\s*$", out, re.M | re.I))
        except subprocess.TimeoutExpired:
            result[name] = False
    return result


def worker(request):
    operation, home = request["operation"], request.get("home")
    if operation == "inspect-launchers":
        return inspect_launchers(home, request.get("gateways", []), request.get("provider", "astergate"))
    if operation == "verify-launchers":
        return verify_launchers(home)
    if operation == "inspect":
        return inspect(home, request.get("harness", "both"), request.get("secrets", False), request.get("api_key", False), request.get("catalog", True), request.get("model_refresh", False))
    if operation == "apply":
        return apply_plan(request["plan"], home)
    if operation == "restore":
        return restore_plan(request["backup"], home)
    if operation == "backups":
        p = state_dir(home) / "backups"
        return sorted([x.name for x in p.iterdir() if x.is_dir()], reverse=True) if p.exists() else []
    raise SyncError("unknown worker operation")


def ssh_args(endpoint):
    args = ["ssh", "-oBatchMode=yes", "-oConnectTimeout=10"]
    if endpoint.startswith("ssh://"):
        u = urlsplit(endpoint)
        if not u.hostname or u.password or u.path not in ("", "/") or u.query or u.fragment:
            raise SyncError("use ssh://[user@]host[:port] without a path or password")
        host = (u.username + "@" if u.username else "") + u.hostname
        if u.port:
            args.extend(["-p", str(u.port)])
    else:
        host = endpoint
    if not host or host.startswith("-") or re.search(r"[\s\x00-\x1f]", host):
        raise SyncError("invalid SSH destination")
    args.extend(["--", host])
    return args


def call(endpoint, request, local_home=None, timeout=90):
    if endpoint == "local":
        return worker({**request, "home": local_home})
    # Only public source code is passed in argv; all configuration travels on stdin.
    script = Path(__file__).read_text()
    command = "python3 -c " + shlex.quote(script) + " --worker"
    try:
        result = subprocess.run(ssh_args(endpoint) + [command], input=json.dumps(request).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise SyncError("SSH operation timed out") from e
    try:
        reply = json.loads(result.stdout)
    except (ValueError, UnicodeError) as e:
        # Never echo remote stdout/stderr: shell startup scripts may print secrets.
        raise SyncError(f"SSH worker failed (exit {result.returncode}); check SSH access and Python 3.11+") from e
    if not reply.get("ok"):
        raise SyncError(reply.get("error", "remote worker failed"))
    return reply["result"]


def show_plan(endpoint, plan):
    # Key names only: never print settings, tokens, endpoints, or content diffs.
    print(f"{endpoint}:")
    if not plan["changes"]:
        print("  already in sync")
    for label, keys in sorted(plan["changes"].items()):
        print(f"  {label}: " + ", ".join(keys))


def launcher_inputs(args, needed):
    """Read only the local secrets and catalog that some target is missing."""
    location = roots(args.local_home)
    home = Path(location["home"])
    claude_key = codex_key = catalog = None
    if "home/.claude-ag-settings.json" in needed:
        claude_key = load_key(args.claude_api_key_env, args.claude_api_key_file)
        if claude_key is None:
            data = read_file(home / ".claude-ag-settings.json")
            if data is None:
                raise SyncError("no Claude gateway key: pass --claude-api-key-env or --claude-api-key-file")
            value = parse_config("claude/settings.json", data).get("env", {}).get("ANTHROPIC_AUTH_TOKEN")
            if not isinstance(value, str) or not value.strip():
                raise SyncError("local ~/.claude-ag-settings.json has no ANTHROPIC_AUTH_TOKEN")
            claude_key = value.strip()
    if "codex/api-key" in needed:
        codex_key = load_key(args.codex_api_key_env, args.codex_api_key_file or str(Path(location["codex"]) / "api-key"))
        codex_key = (codex_key + "\n").encode()
    if "codex/models.json" in needed:
        path = args.codex_catalog
        if not path:
            data = read_file(Path(location["codex"]) / "config.toml")
            path = parse_config("codex/config.toml", data).get("model_catalog_json") if data else None
        if not path:
            raise SyncError("no model catalog: pass --codex-catalog")
        catalog = read_file(Path(path).expanduser())
        if catalog is None:
            raise SyncError("model catalog does not exist")
        try:
            json.loads(catalog)
        except ValueError as e:
            raise SyncError("invalid model catalog JSON") from e
    return claude_key, codex_key, catalog


def launchers(args):
    targets = list(dict.fromkeys(args.targets))
    request = {"operation": "inspect-launchers", "gateways": args.gateway, "provider": args.codex_provider}
    found = [(t, call(t, request, args.local_home)) for t in targets]
    needed = {label for _, state in found for label, ok in state["present"].items() if not ok}
    claude_key, codex_key, catalog = launcher_inputs(args, needed)
    plans = []
    # Preflight every target before the first write, as sync does.
    for target, state in found:
        plan = build_launcher_plan(state, claude_key, codex_key, catalog, args.codex_provider, args.codex_model)
        print(f"{target}: gateway {state['gateway'].rstrip('/')}, aliases in ~/{state['rc'].split('/', 1)[1]}")
        show_plan(target, plan)
        plans.append((target, plan))
    if not args.apply:
        print("Preview only. Add --apply to write changes.")
        return 0
    for target, plan in plans:
        result = call(target, {"operation": "apply", "plan": plan}, args.local_home)
        print(f"{target}: updated {len(result['updated'])} file(s); backup: {result['backup'] or 'none needed'}")
    failed = False
    if args.verify:
        for target, _ in plans:
            result = call(target, {"operation": "verify-launchers"}, args.local_home, timeout=600)
            print(f"{target}: " + ", ".join(f"{k} {'pong' if ok else 'FAILED'}" for k, ok in result.items()))
            failed = failed or not all(result.values())
    return 1 if failed else 0


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--local-home", help="override local home (isolated configs/testing)")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="preview or apply source defaults to one or more targets")
    sync.add_argument("--from", dest="source", default="local", help="local, SSH alias, user@host, or ssh://user@host:port")
    sync.add_argument("--to", dest="targets", action="append", required=True)
    sync.add_argument("--harness", choices=["both", "codex", "claude"], default="both")
    sync.add_argument("--with-secrets", action="store_true", help="also transfer inline credentials and supported auth files")
    sync.add_argument("--with-model-refresh", action="store_true", help="copy trusted refresh-models.py and install a codex-with-models launcher")
    for name, prefix in (("shared", ""), ("Codex", "codex-"), ("Claude", "claude-")):
        group = sync.add_mutually_exclusive_group()
        group.add_argument("--" + prefix + "api-key-env", metavar="VAR", help=f"read a custom {name} API key from a local environment variable")
        group.add_argument("--" + prefix + "api-key-file", metavar="PATH", help=f"read a custom {name} API key from a local private file")
    sync.add_argument("--allow-gateway-drop", action="store_true", help="allow a source without env.ANTHROPIC_BASE_URL to remove the target's Claude gateway")
    sync.add_argument("--apply", action="store_true", help="write changes with an automatic backup")
    launch = sub.add_parser("launchers", help="add claude-ag / codex-ag shell aliases wired to a gateway")
    launch.add_argument("--to", dest="targets", action="append", required=True)
    launch.add_argument("--gateway", action="append", required=True, metavar="URL",
                        help="gateway base URL; repeat in preference order, each target uses the first it can reach")
    launch.add_argument("--codex-provider", default="astergate", help="Codex model_providers entry name (default: astergate)")
    launch.add_argument("--codex-model", default="gpt-6.1-sol")
    launch.add_argument("--codex-catalog", metavar="PATH", help="model catalog for targets without one (default: local model_catalog_json)")
    for name, default in (("claude", "env.ANTHROPIC_AUTH_TOKEN of ~/.claude-ag-settings.json"), ("codex", "~/.codex/api-key")):
        group = launch.add_mutually_exclusive_group()
        group.add_argument(f"--{name}-api-key-env", metavar="VAR", help=f"gateway key for {name} (default: {default})")
        group.add_argument(f"--{name}-api-key-file", metavar="PATH")
    launch.add_argument("--apply", action="store_true", help="write changes with an automatic backup")
    launch.add_argument("--verify", action="store_true", help="run each launcher once in an interactive shell and expect pong")
    backups = sub.add_parser("backups", help="list backup IDs on a machine")
    backups.add_argument("--target", default="local")
    restore = sub.add_parser("restore", help="preview or restore a backup (creates an undo backup)")
    restore.add_argument("--target", default="local")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            if args.with_model_refresh and args.harness == "claude":
                raise SyncError("--with-model-refresh requires the Codex harness")
            if args.source in args.targets:
                raise SyncError("source and target must differ")
            targets = list(dict.fromkeys(args.targets))
            request = {"operation": "inspect", "harness": args.harness, "secrets": args.with_secrets, "model_refresh": args.with_model_refresh}
            source = call(args.source, request, args.local_home)
            api_keys = load_api_keys(args)
            if "codex" in api_keys and not args.with_secrets:
                # Read only the target key to compare it; never collect other auth files.
                request = {**request, "api_key": True}
            plans = []
            # Preflight every target before the first write. Each target is transactional.
            for target in targets:
                destination = call(target, {**request, "catalog": False}, args.local_home)
                plan = build_plan(
                    source, destination, args.harness, args.with_secrets, api_keys, args.with_model_refresh,
                    args.allow_gateway_drop,
                )
                show_plan(target, plan)
                plans.append((target, plan))
            if args.apply:
                for target, plan in plans:
                    result = call(target, {"operation": "apply", "plan": plan}, args.local_home)
                    print(f"{target}: updated {len(result['updated'])} file(s); backup: {result['backup'] or 'none needed'}")
            else:
                print("Preview only. Add --apply to write changes.")
            if not args.with_secrets:
                if api_keys:
                    print("Explicit API key overrides selected; other supported credentials stay on each target.")
                else:
                    print("Credentials stay on each target. Use --with-secrets to transfer supported credentials.")
        elif args.command == "launchers":
            return launchers(args)
        elif args.command == "backups":
            for backup in call(args.target, {"operation": "backups"}, args.local_home):
                print(backup)
        else:
            plan = call(args.target, {"operation": "restore", "backup": args.backup}, args.local_home)
            show_plan(args.target, plan)
            if args.apply:
                result = call(args.target, {"operation": "apply", "plan": plan}, args.local_home)
                print(f"Restore complete; undo backup: {result['backup'] or 'none needed'}")
            else:
                print("Preview only. Add --apply to restore.")
        return 0
    except (SyncError, OSError, ValueError) as e:
        # Only deliberately constructed messages are safe to print.
        print("Error: " + (str(e) if isinstance(e, SyncError) else type(e).__name__), file=sys.stderr)
        return 1


def main():
    if sys.argv[1:] == ["--worker"]:
        try:
            result = worker(json.load(sys.stdin))
            print(json.dumps({"ok": True, "result": result}))
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e) if isinstance(e, SyncError) else type(e).__name__}))
            sys.exit(1)
    else:
        sys.exit(cli())


if __name__ == "__main__":
    main()
