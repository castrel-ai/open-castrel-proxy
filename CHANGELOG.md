# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`proxy --help` lists parameters**: the group help prints a "Command options" table
  covering every subcommand's arguments and flags, generated from the click command tree in
  `src/castrel_proxy/cli/help.py`, so new options appear without editing the help by hand
- **Proxy node lifecycle commands**: the `castrel-proxy proxy` group now covers the same
  operations as the legacy bridge commands
  - `proxy unpair` stops the node, then deletes `node/keys.json`, `node/policy.json` and
    `node/outbox.jsonl`; the log file is kept
  - `proxy logs` reads `~/.castrel/proxy-node.log` with `--lines/-n` and `--follow/-f`,
    tailing backwards instead of loading the whole file
  - `proxy config` prints paths, node id, server and the key file mode; the token is
    reduced to an 8-character SHA-256 prefix and the private keys are never printed
  - `proxy uninstall` removes the local node, legacy bridge state, bundles, session
    directories, logs and the binary, with `--keep-logs` and `--keep-binary` escape hatches
- `src/castrel_proxy/cli/local_state.py` centralises the `~/.castrel` paths, so the legacy
  and proxy commands clean the same files
- **Background daemon mode**: Bridge can now run as a background process (Unix/macOS only)
  - Automatic PID file management at `~/.castrel/castrel-proxy.pid`
  - Log output redirected to `~/.castrel/castrel-proxy.log`
  - Graceful shutdown handling (SIGTERM, SIGINT)
  - Process status checking and management
- **Enhanced `stop` command**: Now properly stops background daemon processes
- **Enhanced `status` command**: Shows actual running status with PID information
- **Enhanced `logs` command**: 
  - View last N lines of logs with `--lines`
  - Follow logs in real-time with `--follow`
  - Reads from daemon log file

### Changed
- The root `uninstall` command now also stops the proxy node daemon and removes the node
  identity (`node/keys.json`, `policy.json`, `outbox.jsonl`) and `~/.castrel/bundles/`.
  Previously the binary was deleted while the node token and private keys stayed on disk.
- **Breaking**: `castrel-proxy start` now defaults to background mode instead of foreground mode
  - Use `--foreground` or `-f` flag to run in foreground mode
  - Background mode only supported on Unix/macOS (not Windows)
  - Example: `castrel-proxy start --foreground` to run in foreground
- Logging in daemon mode redirects to file instead of stdout/stderr

### Fixed
- Enhanced MCP configuration validation to exit immediately on invalid configuration
- Added strict validation for required `transport` field in MCP server configuration
- Improved error messages for MCP configuration errors