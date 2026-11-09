# Proxy Node Mode

> Language: **English** | [简体中文](proxy-node.zh-CN.md)

Proxy node mode turns `castrel-proxy` into the user-side daemon for Castrel private-network
proxies. The daemon opens one outbound WebSocket to Castrel and serves connector execution,
shell commands and bundle installation inside the customer network. No inbound firewall rule
or public IP is required.

The legacy bridge commands (`pair`, `start`, MCP, documents) are unchanged. Proxy node mode
uses the separate command group:

```bash
castrel-proxy proxy pair ...
castrel-proxy proxy start ...
```

## 1. Requirements

- Python 3.10 or newer
- Outbound HTTPS/WSS access to the Castrel API
- Linux x86_64/arm64 or macOS arm64/x86_64
- A Castrel deployment with `PROXY_ENABLED=true`

## 2. Install from source

### 2.1 Check for an older packaged binary first

Castrel may have been installed earlier as a standalone binary. `uv pip install -e .` installs
the package **only into the project `.venv`**; it does not replace a binary in
`~/.local/bin` or `/usr/local/bin`.

```bash
which -a castrel-proxy
```

If the first result is `~/.local/bin/castrel-proxy` or `/usr/local/bin/castrel-proxy`, the
shell is running an older build that has no `proxy` command. Use one of the options below.

### 2.2 Option A: run inside the project (recommended for development)

```bash
cd open-castrel-proxy
uv sync --extra dev
uv run castrel-proxy proxy --help
```

`uv run` always uses the project environment; no activation or PATH change is needed.

### 2.3 Option B: editable install into a virtual environment

```bash
cd open-castrel-proxy
uv venv .venv
source .venv/bin/activate

uv pip install -e .
which castrel-proxy
# must print .../open-castrel-proxy/.venv/bin/castrel-proxy

castrel-proxy proxy --help
```

Without activating the environment:

```bash
./.venv/bin/castrel-proxy proxy --help
```

### 2.4 Option C: replace the global CLI

The old standalone binary is not managed by `uv tool`, so move it first, then install the
editable command:

```bash
mv ~/.local/bin/castrel-proxy ~/.local/bin/castrel-proxy.old 2>/dev/null || true
# If /usr/local/bin/castrel-proxy also exists, move it too (may need sudo).

cd open-castrel-proxy
uv tool install --editable .
hash -r

which castrel-proxy
# must print /Users/<you>/.local/bin/castrel-proxy

castrel-proxy proxy --help
```

`--editable` makes later source edits take effect without reinstalling.

### 2.5 Install from PyPI

```bash
pip install castrel-proxy
```

PyPI packages only contain released features. Use a local editable install while developing
the proxy node.

## 3. Pair the node

Create a pairing code in the Castrel UI (`/proxies` → **Pair node**) or with the API:

```bash
curl -s -X POST https://castrel.example.com/api/v1/proxies/pairings \
  -H 'Content-Type: application/json' \
  -d '{"name":"build-box-1"}'
```

Then run:

```bash
castrel-proxy proxy pair --server https://castrel.example.com --code ABCD-EFGH --name build-box-1
```

The command:

1. Generates an Ed25519 keypair (WebSocket challenge-response) and an X25519 keypair
   (secret envelopes).
2. Redeems the one-time pairing code.
3. Stores the node id, token, private keys, server URL and bundle public key in
   `~/.castrel/node/keys.json` with mode `0600`.

The token is shown to the node only once; the server stores only its SHA-256 hash.

When pairing succeeds and the node is `active`, the command starts the node in the
background automatically and prints the PID/log paths. Use `--no-start` to pair only, or
`--foreground` to keep it attached to the terminal.

If the deployment uses `PROXY_AUTO_APPROVE_PAIRING=false`, the node starts as `pending`.
An administrator verifies the key fingerprint in the UI and approves it before the node can
connect.

## 4. Run and manage the node

Start in the background (default):

```bash
castrel-proxy proxy start
```

Run in the foreground:

```bash
castrel-proxy proxy start --foreground
```

Use an explicit server URL when it differs from the paired one:

```bash
castrel-proxy proxy start --server https://castrel.example.com
```

Manage the process:

```bash
castrel-proxy proxy status     # running/stopped, PID, log path, node id/server
castrel-proxy proxy restart    # stop then start in the background
castrel-proxy proxy stop       # stop the background process
```

`restart` and `start` accept `--foreground` when the process must stay attached to the
terminal.

### Runtime layout

```text
~/.castrel/node/keys.json       node id, token, private keys, server URL
~/.castrel/node/outbox.jsonl    bounded offline outbox
~/.castrel/proxy-node.pid       background process PID
~/.castrel/proxy-node.log       background process log
~/.castrel/bundles/             installed connector bundles
~/.castrel/bundles/current      active bundle version
~/.castrel/bundles/runtimes/    managed CPython 3.12 runtimes
```

## 5. What the node serves

After the WebSocket handshake (`auth.challenge` → `auth.proof` → `session.ready`), the node
handles:

| Message | Purpose |
|---|---|
| `connector.describe` | Return the connector tool catalog. |
| `connector.validate` | Run the connector's config validation. |
| `connector.invoke` | Decrypt the per-call secret envelope and run one connector tool. |
| `node.exec` | Run one shell command under the node policy and whitelist. |
| `bundle.install` | Download, verify, install and activate a signed connector bundle. |
| `bundle.status` | Report install state back to Castrel. |
| `policy.update` | Replace the node policy (allowlists, limits, exec mode). |
| `token.rotate` | Store a rotated node token and acknowledge it. |
| `heartbeat` | Keep presence fresh; the server closes on missed heartbeats. |

Connector calls run in an isolated child process from the bundle virtual environment.
Secrets arrive only inside the encrypted envelope on stdin; they never appear in argv or the
environment.

## 6. Bundles and managed runtime

The Castrel platform publishes signed connector bundles and a managed CPython 3.12 runtime.
On `session.ready` the node receives the bundles it needs, then:

1. Downloads the runtime once per platform (content-addressed by digest).
2. Verifies the runtime signature and every file hash.
3. Downloads the bundle with resume support and verifies its digest.
4. Verifies the bundle signature, `manifest.json` and every file hash.
5. Rejects symlinks, absolute paths, `..` and oversized archives.
6. Builds an offline venv, smoke-imports the connector, activates atomically and keeps the
   previous version for rollback.

If a bundle fails, the node reports `bundle.status = failed` with the error and keeps the
previous active version.

## 7. Command policy

`node.exec` is controlled by the node policy pushed from Castrel:

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

`mode` is `allowlist` (default), `allow_all` or `deny_all`. In `allowlist` mode, an empty
policy list falls back to the node-local whitelist at `~/.castrel/whitelist.conf`; a
non-empty list replaces it. Output is truncated with a marker when it exceeds the limit.

Connector calls are controlled by `allowed_connectors`, `allowed_tools`, `denied_tools`,
`max_timeout_seconds`, `max_result_bytes` and `max_concurrency`.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `No such command 'proxy'` | `which -a castrel-proxy` shows an older packaged binary first. Activate `.venv`, use `.venv/bin/castrel-proxy`, or `uv run castrel-proxy`. |
| `Cannot load node keys` | Run `castrel-proxy proxy pair` first; check `~/.castrel/node/keys.json` permissions. |
| WebSocket closes `4001` | Token invalid, node not `active`, or the Ed25519 proof failed. Re-pair. |
| WebSocket closes `4002` | Node revoked. Pair again. |
| WebSocket closes `4003` | Protocol version unsupported. Upgrade the client. |
| WebSocket closes `4009` | A newer connection superseded this one. Normal during reconnect. |
| `Bundle for connector ... is not installed` | The platform has not sent `bundle.install`; check the node detail page and bundle status. |
| `signature_invalid` / `digest_mismatch` | Bundle or runtime was tampered with, or the signing public key changed. |
| `Command is not allowed by the node whitelist` | Add the command to the policy allowlist or switch mode. |
| `proxy_node_offline` on the server | The node lost its WebSocket or heartbeat; check network and logs. |

## 9. Security notes

- Private keys never leave the node.
- The server stores only the token hash.
- Per-call credentials are sealed with X25519 + HKDF-SHA256 + AES-256-GCM and expire in 60
  seconds.
- OAuth refresh tokens are never sent to the node; only short-lived access tokens are.
- Commands and connector parameters are not written to server audit logs; only an HMAC
  digest is stored.
- `run_proxy_command` requires approval in Castrel by default.

## 10. Development

```bash
cd open-castrel-proxy
uv sync --extra dev
uv run pytest tests
uv run flake8 --max-line-length=100 src/castrel_proxy
```

Relevant modules:

```text
src/castrel_proxy/pairing/keys.py        key and token storage
src/castrel_proxy/pairing/challenge.py   Ed25519 challenge signing
src/castrel_proxy/security/envelope.py   secret envelope opening
src/castrel_proxy/security/policy.py     node policy re-checks
src/castrel_proxy/bundles/               download, verify, install, runtime
src/castrel_proxy/execution/             connector child processes and limits
src/castrel_proxy/transport/ws.py        proxy node WebSocket runtime
src/castrel_proxy/cli/proxy_commands.py  `castrel-proxy proxy` commands
```
