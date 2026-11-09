# 网络代理节点模式

> 语言：**简体中文** | [English](proxy-node.md)

网络代理节点模式把 `castrel-proxy` 变成 Castrel 私有网络代理的用户侧守护进程。它向 Castrel
发起一条出站 WebSocket，在用户内网中执行 connector、shell 命令和 bundle 安装。不需要入站
防火墙规则或公网 IP。

原有 bridge 命令（`pair`、`start`、MCP、文档操作）保持不变；代理节点使用独立命令组：

```bash
castrel-proxy proxy pair ...
castrel-proxy proxy start ...
```

## 1. 环境要求

- Python 3.10 或更高版本
- 能出站访问 Castrel API 的 HTTPS/WSS
- Linux x86_64/arm64 或 macOS arm64/x86_64
- Castrel 部署开启 `PROXY_ENABLED=true`

## 2. 从本地源码安装

### 2.1 先检查是否已有旧的打包二进制

之前可能安装过 Castrel 的独立二进制。`uv pip install -e .` 只会装进**项目自己的 `.venv`**，
不会替换 `~/.local/bin` 或 `/usr/local/bin` 里的二进制。

```bash
which -a castrel-proxy
```

如果第一行是 `~/.local/bin/castrel-proxy` 或 `/usr/local/bin/castrel-proxy`，说明 shell 正在
运行旧版本，它没有 `proxy` 命令。请选择下面任一方式。

### 2.2 方式 A：在项目内运行（开发推荐）

```bash
cd open-castrel-proxy
uv sync --extra dev
uv run castrel-proxy proxy --help
```

`uv run` 总是使用项目环境，不需要激活虚拟环境，也不会受 PATH 影响。

### 2.3 方式 B：可编辑安装到虚拟环境

```bash
cd open-castrel-proxy
uv venv .venv
source .venv/bin/activate

uv pip install -e .
which castrel-proxy
# 必须输出 .../open-castrel-proxy/.venv/bin/castrel-proxy

castrel-proxy proxy --help
```

不激活虚拟环境时：

```bash
./.venv/bin/castrel-proxy proxy --help
```

### 2.4 方式 C：替换全局命令

旧独立二进制不是 `uv tool` 管理的，需要先移走，再安装可编辑命令：

```bash
mv ~/.local/bin/castrel-proxy ~/.local/bin/castrel-proxy.old 2>/dev/null || true
# 如果 /usr/local/bin/castrel-proxy 也存在，也一并移走（可能需要 sudo）。

cd open-castrel-proxy
uv tool install --editable .
hash -r

which castrel-proxy
# 必须输出 /Users/<you>/.local/bin/castrel-proxy

castrel-proxy proxy --help
```

`--editable` 表示之后修改源码会直接生效，无需重新安装。

### 2.5 从 PyPI 安装

```bash
pip install castrel-proxy
```

PyPI 包只包含已发布功能；开发代理节点时请使用本地可编辑安装。

## 3. 配对节点

在 Castrel UI 的 `/proxies` 页面点击「配对节点」，或调用 API：

```bash
curl -s -X POST https://castrel.example.com/api/v1/proxies/pairings \
  -H 'Content-Type: application/json' \
  -d '{"name":"build-box-1"}'
```

然后执行：

```bash
castrel-proxy proxy pair --server https://castrel.example.com --code ABCD-EFGH --name build-box-1
```

命令会：

1. 生成 Ed25519 密钥对（WebSocket challenge-response）与 X25519 密钥对（secret 信封）。
2. 兑换一次性配对码。
3. 把 node id、token、私钥、服务器地址与 bundle 公钥写入
   `~/.castrel/node/keys.json`，权限 `0600`。

token 只返回一次，服务端只保存 SHA-256。

配对成功且节点为 `active` 时，命令会自动在后台启动节点，并打印 PID 与日志路径。只想配对
不启动时加 `--no-start`；需要留在终端前台时加 `--foreground`。

如果部署设置 `PROXY_AUTO_APPROVE_PAIRING=false`，节点先进入 `pending`。管理员在 UI 核对
key fingerprint 并批准后，节点才能连接。

## 4. 运行与管理节点

后台启动（默认）：

```bash
castrel-proxy proxy start
```

前台运行：

```bash
castrel-proxy proxy start --foreground
```

如果服务器地址与配对时不同：

```bash
castrel-proxy proxy start --server https://castrel.example.com
```

进程管理：

```bash
castrel-proxy proxy status     # 运行状态、PID、日志路径、节点 ID/服务器
castrel-proxy proxy restart    # 停止后重新在后台启动
castrel-proxy proxy stop       # 停止后台进程
```

`start` / `restart` 都支持 `--foreground`，需要留在终端时使用。

### 运行时目录

```text
~/.castrel/node/keys.json       node id、token、私钥、服务器地址
~/.castrel/node/outbox.jsonl    有界离线 outbox
~/.castrel/proxy-node.pid       后台进程 PID
~/.castrel/proxy-node.log       后台进程日志
~/.castrel/bundles/             已安装 connector bundle
~/.castrel/bundles/current      当前激活版本
~/.castrel/bundles/runtimes/    托管 CPython 3.12 运行时
```

## 5. 节点提供的消息

WebSocket 握手（`auth.challenge` → `auth.proof` → `session.ready`）后，节点处理：

| 消息 | 作用 |
|---|---|
| `connector.describe` | 返回 connector 工具目录。 |
| `connector.validate` | 执行 connector 配置校验。 |
| `connector.invoke` | 解密单次调用信封并执行一个工具。 |
| `node.exec` | 按节点策略与白名单执行一条 shell 命令。 |
| `bundle.install` | 下载、校验、安装并激活签名 bundle。 |
| `bundle.status` | 回报安装状态。 |
| `policy.update` | 更新 node 策略（allowlist、限制、exec 模式）。 |
| `token.rotate` | 保存轮换后的 token 并回 ack。 |
| `heartbeat` | 刷新 presence；缺失心跳会被服务端断开。 |

connector 调用在 bundle venv 的隔离子进程中运行。凭据只出现在 stdin 的加密信封里，不会
进入 argv 或环境变量。

## 6. Bundle 与托管运行时

Castrel 平台发布签名 connector bundle 与托管 CPython 3.12 运行时。节点在 `session.ready`
收到所需 bundle 后：

1. 每平台按 digest 下载一次运行时。
2. 校验运行时签名与每个文件哈希。
3. 支持续传下载 bundle 并校验 digest。
4. 校验 bundle 签名、`manifest.json` 与每个文件哈希。
5. 拒绝 symlink、绝对路径、`..` 与超大归档。
6. 构建离线 venv、烟测 import、原子激活，并保留上一版用于回滚。

bundle 失败时节点上报 `bundle.status = failed`，并保留上一版 active bundle。

## 7. 命令策略

`node.exec` 受 Castrel 下发的节点策略控制：

```json
{
  "exec": {
    "enabled": true,
    "mode": "allowlist",
    "allowlist": ["ls", "cat", "df", "systemctl"],
    "max_timeout_seconds": 120,
    "max_output_bytes": 262144
  }
}
```

`mode` 可选 `allowlist`（默认）、`allow_all`、`deny_all`。`allowlist` 模式下如果策略里的
`allowlist` 为空，节点使用本地 `~/.castrel/whitelist.conf`；非空时使用策略中的列表。输出
超限会截断并带标记。

connector 调用受 `allowed_connectors`、`allowed_tools`、`denied_tools`、
`max_timeout_seconds`、`max_result_bytes`、`max_concurrency` 控制。

## 8. 常见故障

| 现象 | 排查 |
|---|---|
| `No such command 'proxy'` | `which -a castrel-proxy` 先找到了旧的打包二进制。激活 `.venv`、使用 `.venv/bin/castrel-proxy`，或 `uv run castrel-proxy`。 |
| `Cannot load node keys` | 先执行 `castrel-proxy proxy pair`；检查 `~/.castrel/node/keys.json` 权限。 |
| WebSocket 关闭 `4001` | token 无效、节点非 `active` 或 Ed25519 签名失败；重新配对。 |
| WebSocket 关闭 `4002` | 节点已撤销；重新配对。 |
| WebSocket 关闭 `4003` | 协议版本不支持；升级客户端。 |
| WebSocket 关闭 `4009` | 新连接取代旧连接；重连时正常。 |
| `Bundle for connector ... is not installed` | 平台尚未下发 `bundle.install`；检查节点详情页与 bundle 状态。 |
| `signature_invalid` / `digest_mismatch` | bundle/运行时被篡改或签名公钥变化。 |
| `Command is not allowed by the node whitelist` | 把命令加入策略 allowlist 或切换模式。 |
| 服务端报 `proxy_node_offline` | 节点 WebSocket/心跳中断；检查网络与日志。 |

## 9. 安全说明

- 私钥永不离开节点。
- 服务端只保存 token 的 SHA-256。
- 单次调用凭据使用 X25519 + HKDF-SHA256 + AES-256-GCM 封装，60 秒过期。
- OAuth refresh token 永不下发；只下发短期 access token。
- 服务端审计不保存原始命令与参数，只保存 HMAC 摘要。
- `run_proxy_command` 默认需要审批。

## 10. 开发

```bash
cd open-castrel-proxy
uv sync --extra dev
uv run pytest tests
uv run flake8 --max-line-length=100 src/castrel_proxy
```

相关模块：

```text
src/castrel_proxy/pairing/keys.py        密钥与 token 存储
src/castrel_proxy/pairing/challenge.py   Ed25519 challenge 签名
src/castrel_proxy/security/envelope.py   信封解密
src/castrel_proxy/security/policy.py     节点策略复核
src/castrel_proxy/bundles/               下载、校验、安装、运行时
src/castrel_proxy/execution/             connector 子进程与资源限制
src/castrel_proxy/transport/ws.py        代理节点 WebSocket 运行时
src/castrel_proxy/cli/proxy_commands.py  `castrel-proxy proxy` 命令
```
