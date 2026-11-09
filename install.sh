#!/bin/sh
#
# Online install script for Castrel Proxy
# Usage: curl -fsSL https://castrel.ai/castrel-proxy/install.sh | bash
#
# Default install: user-level (~/.local/bin, no sudo)
# Custom dir:      curl -fsSL https://castrel.ai/castrel-proxy/install.sh | bash -s -- --install-dir ~/bin
#
# Supports: Ubuntu 20+, Debian 10+, CentOS 7+, macOS
# Compatible with: bash 3.x+, dash, ash, busybox sh, zsh
#

set -e

# Hardcoded version (tag includes "v" prefix)
VERSION="v0.1.14"

BINARY_NAME="castrel-proxy"
INSTALL_DIR=""
INSTALL_DIR_FROM_CLI="0"

# Remote base URL
BASE_URL="https://castrel.ai/castrel-proxy"

# Temporary directory for downloads
TMP_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'castrel-proxy-install')

# ── Platform-to-package lookup (POSIX compatible) ───────────────────────────

get_package_url() {
  case "$1" in
    macos-arm64)  echo "${BASE_URL}/packages/castrel-proxy-macos-arm64" ;;
    macos-x86_64) echo "${BASE_URL}/packages/castrel-proxy-macos-x86_64" ;;
    linux-x86_64) echo "${BASE_URL}/packages/castrel-proxy-linux-x86_64" ;;
    linux-arm64)  echo "${BASE_URL}/packages/castrel-proxy-linux-arm64" ;;
    *)            echo "" ;;
  esac
}

get_checksum_url() {
  case "$1" in
    macos-arm64)  echo "${BASE_URL}/packages/castrel-proxy-macos-arm64.sha256" ;;
    macos-x86_64) echo "${BASE_URL}/packages/castrel-proxy-macos-x86_64.sha256" ;;
    linux-x86_64) echo "${BASE_URL}/packages/castrel-proxy-linux-x86_64.sha256" ;;
    linux-arm64)  echo "${BASE_URL}/packages/castrel-proxy-linux-arm64.sha256" ;;
    *)            echo "" ;;
  esac
}

# ── Colored output helpers (use printf for portability) ──────────────────────

if [ -t 1 ]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[1;33m'
  NC='\033[0m'
else
  RED='' GREEN='' YELLOW='' NC=''
fi

die() {
  printf "${RED}Error: %s${NC}\n" "$1" >&2
  exit 1
}

log() {
  printf "${GREEN}%s${NC}\n" "$1"
}

warn() {
  printf "${YELLOW}%s${NC}\n" "$1"
}

print_usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --install-dir <path>   Install binary into the specified directory
  -h, --help             Show this help message

Environment variables:
  CASTREL_INSTALL_DIR    Custom installation directory
EOF
}

expand_home_path() {
  _path="$1"
  case "$_path" in
    "~"/*) printf "%s\n" "$HOME/${_path#\~/}" ;;
    "~")   printf "%s\n" "$HOME" ;;
    *)      printf "%s\n" "$_path" ;;
  esac
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --install-dir)
        [ "$#" -lt 2 ] && die "Missing value for --install-dir"
        INSTALL_DIR="$2"
        INSTALL_DIR_FROM_CLI="1"
        shift
        ;;
      -h|--help)
        print_usage
        exit 0
        ;;
      *)
        die "Unknown option: $1"
        ;;
    esac
    shift
  done
}

prompt_install_dir() {
  _default_dir="$HOME/.local/bin"
  printf "Install directory (default: %s): " "$_default_dir" > /dev/tty
  read _input_dir < /dev/tty || true
  if [ -n "$_input_dir" ]; then
    INSTALL_DIR="$_input_dir"
  else
    INSTALL_DIR="$_default_dir"
    log "Using default install directory: ${INSTALL_DIR}"
  fi
}

can_prompt_install_dir() {
  [ -n "${CI:-}" ] && return 1
  [ -r /dev/tty ] && [ -w /dev/tty ]
}

resolve_install_preferences() {
  if [ -z "$INSTALL_DIR" ] && [ -n "${CASTREL_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$CASTREL_INSTALL_DIR"
  fi

  if [ -z "$INSTALL_DIR" ]; then
    if can_prompt_install_dir; then
      prompt_install_dir
    else
      INSTALL_DIR="$HOME/.local/bin"
    fi
  fi

  INSTALL_DIR=$(expand_home_path "$INSTALL_DIR")
}

ensure_install_dir_exists() {
  if [ -d "$INSTALL_DIR" ]; then
    return
  fi

  if mkdir -p "$INSTALL_DIR" 2>/dev/null; then
    return
  fi

  warn "Need elevated permission to create ${INSTALL_DIR}"
  if command -v sudo >/dev/null 2>&1; then
    sudo mkdir -p "$INSTALL_DIR" || die "Cannot create ${INSTALL_DIR}"
  else
    die "Directory ${INSTALL_DIR} does not exist and sudo is not available. Use --install-dir with a writable path."
  fi
}

copy_binary_to_target() {
  if [ -w "$INSTALL_DIR" ]; then
    cp "$pkg_path" "$target_path" || die "Failed to copy binary to ${target_path}"
    return
  fi

  warn "Need elevated permission to write ${target_path}"
  if command -v sudo >/dev/null 2>&1; then
    sudo cp "$pkg_path" "$target_path" || die "Failed to copy binary to ${target_path}"
  else
    die "Cannot write to ${INSTALL_DIR} and sudo is not available."
  fi
}

ensure_executable() {
  if chmod +x "$target_path" 2>/dev/null; then
    return
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo chmod +x "$target_path" || die "Cannot set executable permission for ${target_path}."
  else
    die "Cannot set executable permission for ${target_path} and sudo is not available."
  fi
}

path_contains_dir() {
  _dir="$1"
  _old_ifs="$IFS"
  IFS=':'
  for _entry in $PATH; do
    if [ "$_entry" = "$_dir" ]; then
      IFS="$_old_ifs"
      return 0
    fi
  done
  IFS="$_old_ifs"
  return 1
}

suggest_path_update() {
  _dir="$1"
  _always_hint="${2:-0}"
  if path_contains_dir "$_dir"; then
    if [ "$_always_hint" = "1" ]; then
      log "${_dir} is already in your PATH."
    fi
    return
  fi

  warn "${_dir} is not in your PATH."
  _shell_name=$(basename "${SHELL:-}")
  case "$_shell_name" in
    zsh)  _rc_file="$HOME/.zshrc" ;;
    bash) _rc_file="$HOME/.bashrc" ;;
    *)    _rc_file="$HOME/.profile" ;;
  esac

  echo "Add this line to ${_rc_file}:"
  echo "  export PATH=\"${_dir}:\$PATH\""
  echo "Then reload your shell, e.g.:"
  echo "  . ${_rc_file}"
}

# ── SHA256 checksum (try multiple tools) ─────────────────────────────────────

compute_sha256() {
  _file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$_file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$_file" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$_file" | awk '{print $NF}'
  else
    die "No SHA256 tool found (sha256sum, shasum, or openssl required)."
  fi
}

# ── Detect OS and architecture ───────────────────────────────────────────────

detect_platform() {
  _os=""
  _arch=""

  case "$(uname -s)" in
    Darwin) _os="macos" ;;
    Linux)  _os="linux" ;;
    *)      die "Unsupported OS: $(uname -s)" ;;
  esac

  case "$(uname -m)" in
    x86_64|amd64)   _arch="x86_64" ;;
    aarch64|arm64)   _arch="arm64" ;;
    *)               die "Unsupported architecture: $(uname -m)" ;;
  esac

  echo "${_os}-${_arch}"
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
  parse_args "$@"
  resolve_install_preferences

  log "Castrel Proxy installer"
  echo ""

  # Hardcoded version
  tag="${VERSION}"
  log "Version: $tag"
  log "Install directory: ${INSTALL_DIR}"

  platform=$(detect_platform)
  log "Detected platform: $platform"

  # Resolve remote package URL
  pkg_url=$(get_package_url "$platform")
  sha_url=$(get_checksum_url "$platform")

  [ -z "$pkg_url" ] && die "No package configured for platform: $platform"

  pkg_name="castrel-proxy-${platform}"
  pkg_path="${TMP_DIR}/${pkg_name}"
  sha_path="${TMP_DIR}/${pkg_name}.sha256"

  log "Downloading ${pkg_name}..."

  # Download package file
  if ! curl -fsSL -o "$pkg_path" "$pkg_url"; then
    die "Failed to download package from: $pkg_url"
  fi

  # Download checksum file
  if ! curl -fsSL -o "$sha_path" "$sha_url"; then
    die "Failed to download checksum from: $sha_url"
  fi

  log "Download complete. Verifying checksum..."

  # Verify checksum
  expected_hash=$(awk '{print $1}' "$sha_path")
  actual_hash=$(compute_sha256 "$pkg_path")

  if [ "$expected_hash" != "$actual_hash" ]; then
    die "SHA256 verification failed. Expected: $expected_hash, got: $actual_hash"
  fi
  log "SHA256 checksum verified."

  # Install to target directory
  target_path="${INSTALL_DIR}/${BINARY_NAME}"
  ensure_install_dir_exists
  copy_binary_to_target
  ensure_executable

  # Clean up temporary directory
  rm -rf "$TMP_DIR"

  log "Installed successfully to $target_path"
  suggest_path_update "$INSTALL_DIR" "$INSTALL_DIR_FROM_CLI"
  echo ""
  echo "Run: castrel-proxy --help"
  echo ""
}

main "$@"
