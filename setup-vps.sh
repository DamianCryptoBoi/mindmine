#!/usr/bin/env bash

set -euo pipefail

readonly NVM_INSTALL_RELEASE="v0.40.6"
readonly PROJECT_DIRECTORY="$(pwd -P)"
readonly OS_RELEASE_PATH="${SETUP_OS_RELEASE_FILE:-/etc/os-release}"
readonly COMMAND_BIN_DIRECTORY="${SETUP_COMMAND_BIN_DIR:-/usr/local/bin}"
readonly USER_COMMAND_DIRECTORY="$HOME/.local/bin"

info() {
    printf '[setup] %s\n' "$1"
}

fail() {
    printf '[setup] ERROR: %s\n' "$1" >&2
    exit 1
}

expose_command() {
    local command_name="$1"
    local source_path="$2"
    local target_path="$COMMAND_BIN_DIRECTORY/$command_name"

    [[ -x "$source_path" ]] || fail "$source_path is missing or not executable."
    if [[ "$source_path" == "$target_path" ]]; then
        return
    fi
    if [[ -L "$target_path" ]]; then
        [[ "$(readlink "$target_path")" == "$source_path" ]] || \
            fail "Refusing to replace existing command link: $target_path"
        return
    fi
    if [[ -e "$target_path" ]]; then
        fail "Refusing to replace existing command: $target_path"
    fi
    "${ELEVATE[@]}" mkdir -p "$COMMAND_BIN_DIRECTORY"
    "${ELEVATE[@]}" ln -s "$source_path" "$target_path"
}

if [[ ! -r "$OS_RELEASE_PATH" ]]; then
    fail "Cannot read $OS_RELEASE_PATH. This script supports Ubuntu 22.04 and 24.04 only."
fi

# shellcheck disable=SC1090
source "$OS_RELEASE_PATH"
if [[ "${ID:-}" != "ubuntu" ]] || [[ "${VERSION_ID:-}" != "22.04" && "${VERSION_ID:-}" != "24.04" ]]; then
    fail "Unsupported operating system: ${PRETTY_NAME:-unknown}. Use Ubuntu 22.04 or 24.04."
fi

if [[ ! -f "$PROJECT_DIRECTORY/pyproject.toml" || ! -f "$PROJECT_DIRECTORY/install.sh" || ! -f "$PROJECT_DIRECTORY/.env.gen_miner.template" ]]; then
    fail "Run this script from the miner repository root after cloning it."
fi
case ":$PATH:" in
    *":$COMMAND_BIN_DIRECTORY:"*) ;;
    *) fail "$COMMAND_BIN_DIRECTORY must already be on PATH so uv and btcli remain available after setup exits." ;;
esac

if [[ "$EUID" -eq 0 ]]; then
    ELEVATE=()
else
    command -v sudo >/dev/null 2>&1 || fail "sudo is required when running as a non-root user."
    sudo -v
    ELEVATE=(sudo)
fi

info "Installing Ubuntu packages"
"${ELEVATE[@]}" apt-get update
"${ELEVATE[@]}" env DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
"${ELEVATE[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential \
    ca-certificates \
    curl \
    git \
    libssl-dev \
    python3 \
    python3-dev \
    python3-venv \
    ufw

info "Configuring UFW"
"${ELEVATE[@]}" ufw allow 22/tcp
"${ELEVATE[@]}" ufw allow 8000:9000/tcp
"${ELEVATE[@]}" ufw --force enable

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
    # shellcheck disable=SC1090
    source "$NVM_DIR/nvm.sh"
fi
if ! command -v nvm >/dev/null 2>&1 || [[ "$(nvm --version)" != "${NVM_INSTALL_RELEASE#v}" ]]; then
    info "Installing NVM $NVM_INSTALL_RELEASE"
    curl -fsSL "https://raw.githubusercontent.com/nvm-sh/nvm/$NVM_INSTALL_RELEASE/install.sh" | bash
fi

[[ -s "$NVM_DIR/nvm.sh" ]] || fail "NVM installation did not create $NVM_DIR/nvm.sh."
# shellcheck disable=SC1090
source "$NVM_DIR/nvm.sh"
[[ "$(nvm --version)" == "${NVM_INSTALL_RELEASE#v}" ]] || fail "NVM $NVM_INSTALL_RELEASE installation failed."

info "Installing the current Node.js LTS and PM2"
nvm install --lts
nvm alias default 'lts/*'
nvm use default
npm install -g pm2

export PATH="$USER_COMMAND_DIRECTORY:$PATH"
UV_COMMAND_PATH="$USER_COMMAND_DIRECTORY/uv"
if [[ ! -x "$UV_COMMAND_PATH" ]]; then
    info "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
[[ -x "$UV_COMMAND_PATH" ]] || fail "uv installation failed."

info "Installing btcli as a per-user tool"
"$UV_COMMAND_PATH" tool install --force bittensor-cli==9.22.0
"$UV_COMMAND_PATH" tool update-shell
BTCLI_COMMAND_PATH="$USER_COMMAND_DIRECTORY/btcli"
[[ -x "$BTCLI_COMMAND_PATH" ]] || fail "btcli installation failed."
expose_command uv "$UV_COMMAND_PATH"
expose_command btcli "$BTCLI_COMMAND_PATH"

info "Installing the generator miner runtime"
(
    cd "$PROJECT_DIRECTORY"
    bash ./install.sh --generator-only
)

cat <<EOF

VPS setup complete. The miner has not been configured or started.
uv and btcli are available immediately through $COMMAND_BIN_DIRECTORY.

Manual next steps:
  1. cd $PROJECT_DIRECTORY
  2. Use btcli to create or restore this user's Bittensor wallet.
  3. cp .env.gen_miner.template .env.gen_miner and edit it.
  4. source "$NVM_DIR/nvm.sh"
  5. pm2 start gen_miner.config.js
  6. pm2 save

UFW is enabled with TCP ports 22 and 8000-9000 open.
EOF
