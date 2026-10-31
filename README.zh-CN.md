# harness-sync

同步本机与 SSH 机器上的 Codex、Claude Code 默认配置。无需运行时依赖，远端无需安装本工具；两端需要 Python 3.11+，支持 macOS 和 Linux。

## 安装

```sh
pipx install git+https://github.com/Ghost-LZW/harness-sync.git

# 也可以直接运行源码
git clone https://github.com/Ghost-LZW/harness-sync.git
cd harness-sync
python3 harness_sync.py --help
```

## 同步默认配置

```sh
# 默认只预览，显示变更的配置项，不显示值
harness-sync sync --to devbox --to alice@server.example.com

# 应用变更，自动备份旧文件
harness-sync sync --to devbox --apply

# 同时同步当前机器的凭据
harness-sync sync --to devbox --with-secrets --apply

# 从远端拉到本机
harness-sync sync --from devbox --to local --apply

# 两台远端之间同步，经当前机器中转
harness-sync sync --from devbox --to staging --harness codex --apply

# 指定 SSH 端口
harness-sync sync --to ssh://alice@server.example.com:2222 --apply
```

`local` 表示运行本工具的机器。SSH 别名、密钥、agent 和跳板机沿用系统 SSH 配置；连接使用批处理模式，请先确保可以通过 SSH 登录。`--harness codex` 或 `--harness claude` 可以只同步一个工具。

## 自定义 API key

可以给两个工具共用一个 key，也可以分别设置。从当前机器上的环境变量或私有文件读取；不提供把 key 明文放进命令参数的接口。

```sh
# 共用一个 key
harness-sync sync --to devbox \
  --api-key-file ~/.config/my-gateway/api-key --apply

# 两个工具分别使用不同的 key；请预先设置好这两个环境变量
harness-sync sync --to devbox \
  --codex-api-key-env CODEX_GATEWAY_KEY \
  --claude-api-key-env CLAUDE_GATEWAY_KEY --apply
```

支持 `--api-key-env`、`--api-key-file` 以及带 `codex-`、`claude-` 前缀的独立参数。独立设置优先于共用设置，显式 key 优先于 `--with-secrets` 复制的凭据。设置 key 不需要开启全部凭据同步。

Codex 自定义 key 需要源配置中存在自定义 `model_provider`，且目标 Codex 支持 provider `auth` 命令。key 保存到目标 Codex 配置目录下的 `api-key` 文件，通过 `/bin/cat` 读取。Claude 使用 `settings.json` 的 `env.ANTHROPIC_AUTH_TOKEN`，并清除配置中冲突的 `ANTHROPIC_API_KEY` 和 `apiKeyHelper`。服务地址沿用源配置。配置文件以外的环境变量或包装脚本仍可能覆盖实际行为。

## 同步范围与恢复

同步模型、推理强度、service tier、provider、权限、沙箱、环境默认值等设置；源端没有的旧默认项会从目标中移除。源端的宽松权限和沙箱设置也会同步，请先查看预览。

目标机自己的项目信任记录、hooks、MCP、插件注册和桌面集成会保留，不复制本机专用集成。skills、指令文件、会话、历史和项目级配置不在同步范围内。已有的目标集成会原样保留，本工具不修复它们。Codex 选用的自定义模型目录 JSON 会同步；home 和配置目录路径会替换为目标路径。其他绝对路径需要目标机上原本存在。

默认保留目标凭据。`--with-secrets` 允许同步受支持的内联凭据和 Codex `api-key`、`auth.json`、Claude `.credentials.json`。系统钥匙串、外部 secret helper 和任意认证脚本依赖文件不会同步。无法可靠识别藏在任意字段里的所有秘密，真实配置应始终视为私有文件。

配置目录支持 `CODEX_HOME` 和 `CLAUDE_CONFIG_DIR`。远端 Python 进程必须能看到这些变量，仅在交互 shell 中设置可能不会生效。修改 TOML 后会重新格式化并丢失注释，原文件可以从备份恢复。

```sh
harness-sync backups --target devbox
harness-sync restore --target devbox --backup BACKUP_ID
harness-sync restore --target devbox --backup BACKUP_ID --apply
```

备份位于目标机 `${XDG_STATE_HOME:-~/.local/state}/harness-sync/backups/`，不自动清理。恢复也会创建新的备份。文件权限为 `0600`，备份目录为 `0700`。写入前检查并发修改，各文件使用原子替换，普通写入失败会尝试回滚当前机器；多台目标之间不做全局回滚。进程被强制终止或断电后可以使用备份恢复。

公开仓库仅包含工具源码、文档和模拟测试数据，不存放实际配置。配置与凭据通过 SSH 标准输入传输，不进入远端进程命令参数；备份可能含有凭据，应保留在私有目录。工具不发送遥测。

完整行为及限制见 [English README](README.md)。测试命令：

```sh
python3 -m unittest discover -s tests -v
```

MIT 协议。Lan Zongwei · i@soullan.com
