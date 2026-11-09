# Castrel Proxy

[![CI](https://github.com/castrel-ai/castrel-proxy/workflows/CI/badge.svg)](https://github.com/castrel-ai/castrel-proxy/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python Version](https://img.shields.io/pypi/pyversions/castrel-proxy)](https://pypi.org/project/castrel-proxy/)

A lightweight remote command execution bridge client that connects to a server via WebSocket to receive and execute commands, with MCP (Model Context Protocol) integration.

## ✨ Features

- ✅ **Secure Pairing**: Pair with server using verification codes
- ✅ **Persistent Configuration**: Configuration saved in `~/.castrel/config.yaml`
- ✅ **Unique Identifier**: Generate stable client ID based on machine characteristics
- ✅ **WebSocket Connection**: Real-time bidirectional communication
- ✅ **Command Execution**: Execute shell commands with whitelist security
- ✅ **Document Operations**: Read, write, and edit files remotely
- ✅ **Auto Reconnect**: Automatically reconnect when connection is lost
- ✅ **Timeout Control**: Command execution timeout protection
- ✅ **MCP Integration**: Connect to local MCP services and sync tools information

## 📦 Installation

### Via pip

```bash
pip install castrel-proxy
```

### From source

```bash
git clone https://github.com/castrel-ai/castrel-proxy.git
cd castrel-proxy
pip install -e .
```

If `which -a castrel-proxy` lists an older binary in `~/.local/bin` or `/usr/local/bin`, the
shell is not running the editable install. Use `uv run castrel-proxy`, `.venv/bin/castrel-proxy`,
or replace the old binary; see [docs/proxy-node.md](docs/proxy-node.md#21-check-for-an-older-packaged-binary-first).

## 🔒 Proxy node mode

`castrel-proxy` also runs as the user-side daemon for Castrel private-network proxies: it
opens one outbound WebSocket and executes connectors and shell commands inside the customer
network. See [docs/proxy-node.md](docs/proxy-node.md).

```bash
castrel-proxy proxy pair --server <server_url> --code <pairing_code>
castrel-proxy proxy start          # background by default
castrel-proxy proxy status
castrel-proxy proxy restart
castrel-proxy proxy stop
```

## 🚀 Quick Start

### 1. Pair with Server

```bash
castrel-proxy pair <verification_code> <server_url>
```

Example:
```bash
castrel-proxy pair eyJ0cyI6MTczNTA4ODQwMCwid2lkIjoiZGVmYXVsdCIsInJhbmQiOiIxMjM0NTYifQ https://server.example.com
```

### 2. Start Bridge Service

```bash
# Run in background (default)
castrel-proxy start

# Run in foreground
castrel-proxy start --foreground

# Press Ctrl+C to stop (foreground mode only)
```

### 3. Stop Bridge Service

```bash
# Stop background daemon
castrel-proxy stop
```

### 4. Check Status

```bash
castrel-proxy status
```

### 5. View Logs

```bash
# View last 50 lines (default)
castrel-proxy logs

# View last 100 lines
castrel-proxy logs -n 100

# Follow logs in real-time
castrel-proxy logs -f
```

## 🛠️ Managing Skills

**Create a skill scaffold:**
```bash
castrel-bridge-cli skill init my-skill -d "my custom skill"
```

**List local skills:**
```bash
castrel-bridge-cli skill list -v
```

**Validate a skill:**
```bash
castrel-bridge-cli skill validate my-skill --strict
```

**Package / Import a skill:**
```bash
castrel-bridge-cli skill package my-skill -o /tmp/my-skill.skill
castrel-bridge-cli skill import /tmp/my-skill.skill
```

**Sync skills to server:**
```bash
castrel-bridge-cli skill sync
```

### Skill Sync Troubleshooting

- `401/403`: Re-run `castrel-bridge-cli pair` and verify the code and workspace match.
- `skill_content_pull` fails with `content_hash` mismatch: Server content has changed — retry sync or check if server is caching an old hash.
- Import fails with "directory name does not match frontmatter name": Rename the directory to exactly match the `name` field in `SKILL.md`.
- Import fails with "forbidden file type": Remove `.pem/.key/.dll/.so` and other restricted files before importing.
- Skills not visible on server after sync: Confirm `castrel-bridge-cli skill list -v` shows local skills, then run `castrel-bridge-cli mcp-sync` or `skill sync`.

## 📖 Documentation

- [Installation Guide](docs/installation.md)
- [Configuration Guide](docs/configuration.md)
- [Daemon Mode Guide](docs/daemon-mode.md)
- [MCP Integration](docs/mcp-integration.md)
- [API Reference](docs/api-reference.md)
- [Protocol Specification](docs/protocol.md)
- [Migration Guide](MIGRATION_GUIDE.md)

## 🔧 Configuration

### Bridge Configuration (`~/.castrel/config.yaml`)

Pairing information is saved automatically:

```yaml
server_url: "https://server.example.com"
verification_code: "ABC123"
client_id: "a1b2c3d4e5f6"
workspace_id: "default"
paired_at: "2025-12-22T10:30:00Z"
```

### MCP Configuration (`~/.castrel/mcp.json`)

Configure MCP services (optional):

```json
{
  "mcpServers": {
    "filesystem": {
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
      "env": {}
    }
  }
}
```

See `examples/mcp.json.example` for more examples.

### Command Whitelist (`~/.castrel/whitelist.conf`)

Configure allowed commands for security:

```
# Add commands one per line
ls
cat
git
python
# etc.
```

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────┐
│           Bridge Client (Local)                 │
│                                                 │
│  ┌──────────────────────────────────────────┐  │
│  │         CLI Commands                     │  │
│  └──────────────────────────────────────────┘  │
│                    │                            │
│      ┌─────────────┼─────────────┐              │
│      │             │             │              │
│  ┌───▼────┐  ┌────▼─────┐  ┌───▼─────┐        │
│  │ Core   │  │   MCP    │  │ Network │        │
│  │ Config │  │  Manager │  │ Client  │        │
│  └────────┘  └──────────┘  └──────────┘        │
│                    │             │              │
│              ┌─────▼─────┐       │              │
│              │   MCP     │       │              │
│              │  Servers  │       │              │
│              └───────────┘  ┌────▼─────┐       │
│                             │ Command  │       │
│                             │ Executor │       │
│                             └──────────┘       │
└─────────────────────────────────────────────────┘
                              │
                              │ WebSocket
                              ▼
┌─────────────────────────────────────────────────┐
│            Bridge Server (Remote)               │
│  /api/v1/bridge/ws?client_id=xxx&code=yyy       │
└─────────────────────────────────────────────────┘
```

## 🛠️ Development

### Setup Development Environment

```bash
# Clone repository
git clone https://github.com/castrel-ai/castrel-proxy.git
cd castrel-proxy

# Install dependencies and sync environment
uv sync
```

### Run Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=castrel_proxy

# Run specific test file
pytest tests/test_core.py
```

### Code Quality

```bash
# Format code
black src/

# Lint code
flake8 src/

# Type checking
mypy src/
```

### Build Binary (Docker)

```bash
# Build with host architecture (docker-only)
scripts/package_binary.sh --mode local

# Build Linux x86_64 binary with manylinux2014 (glibc 2.17 baseline)
scripts/package_binary.sh --mode manylinux-x86_64

# Build with explicit CPython version used in manylinux container
scripts/package_binary.sh --mode manylinux-x86_64 --python-version 3.11.9

# Build Linux arm64 binary with manylinux2014 (glibc 2.17 baseline)
scripts/package_binary.sh --mode manylinux-arm64

# Disable cache (for clean build)
scripts/package_binary.sh --mode manylinux-x86_64 --no-cache

# Clean cache directory before build
scripts/package_binary.sh --mode manylinux-x86_64 --clean-cache
```

> Note: Docker cannot produce native macOS binaries. Use GitHub Actions macOS runners to build `castrel-proxy-macos-arm64` and `castrel-proxy-macos-x86_64`.

By default, build cache is enabled and stored under `.cache/package_binary/` (pip + PyInstaller cache) to speed up subsequent builds.
The first Linux build may take longer because it can bootstrap OpenSSL and shared CPython in the container.

## 🤝 Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🔒 Security

For security concerns, please see [SECURITY.md](SECURITY.md) or contact security@example.com.

## 📮 Contact

- Issues: [GitHub Issues](https://github.com/castrel-ai/castrel-proxy/issues)
- Discussions: [GitHub Discussions](https://github.com/castrel-ai/castrel-proxy/discussions)

## 🙏 Acknowledgments

- Built with [Typer](https://typer.tiangolo.com/) for CLI
- Uses [aiohttp](https://docs.aiohttp.org/) for async WebSocket communication
- Integrates with [MCP](https://modelcontextprotocol.io/) for tool protocols
