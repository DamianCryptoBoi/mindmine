#!/usr/bin/env bash

set -euo pipefail

readonly NVM_INSTALL_RELEASE="v0.40.6"
readonly REPOSITORY_URL="https://github.com/DamianCryptoBoi/mindmine.git"
readonly INSTALL_DIRECTORY="$HOME/getmesomemoney"
readonly OS_RELEASE_PATH="${SETUP_OS_RELEASE_FILE:-/etc/os-release}"

info() {
    printf '[setup] %s\n' "$1"
}

fail() {
    printf '[setup] ERROR: %s\n' "$1" >&2
    exit 1
}

validate_checkout() {
    local checkout_root git_root origin_url

    checkout_root=$(cd "$INSTALL_DIRECTORY" && pwd -P)
    git_root=$(git -C "$INSTALL_DIRECTORY" rev-parse --show-toplevel 2>/dev/null) || \
        fail "$INSTALL_DIRECTORY is not a valid Git checkout."
    git_root=$(cd "$git_root" && pwd -P)
    [[ "$git_root" == "$checkout_root" ]] || \
        fail "$INSTALL_DIRECTORY is nested inside another Git checkout."
    git -C "$INSTALL_DIRECTORY" rev-parse --verify HEAD >/dev/null 2>&1 || \
        fail "$INSTALL_DIRECTORY has no valid checked-out revision."

    origin_url=$(git -C "$INSTALL_DIRECTORY" remote get-url origin 2>/dev/null) || \
        fail "$INSTALL_DIRECTORY has no origin remote."
    [[ "$origin_url" == "$REPOSITORY_URL" ]] || \
        fail "$INSTALL_DIRECTORY is not the expected repository ($REPOSITORY_URL)."

    git -C "$INSTALL_DIRECTORY" ls-files --error-unmatch -- install.sh >/dev/null 2>&1 || \
        fail "install.sh is not tracked by the miner repository."
    git -C "$INSTALL_DIRECTORY" diff --quiet HEAD -- install.sh || \
        fail "install.sh has local changes. Restore it before rerunning setup."
}

if [[ ! -r "$OS_RELEASE_PATH" ]]; then
    fail "Cannot read $OS_RELEASE_PATH. This script supports Ubuntu 22.04 and 24.04 only."
fi

# shellcheck disable=SC1090
source "$OS_RELEASE_PATH"
if [[ "${ID:-}" != "ubuntu" ]] || [[ "${VERSION_ID:-}" != "22.04" && "${VERSION_ID:-}" != "24.04" ]]; then
    fail "Unsupported operating system: ${PRETTY_NAME:-unknown}. Use Ubuntu 22.04 or 24.04."
fi

if [[ "$EUID" -eq 0 ]]; then
    ELEVATE=()
else
    command -v sudo >/dev/null 2>&1 || fail "sudo is required when running as a non-root user."
    sudo -v
    ELEVATE=(sudo)
fi

info "Installing Ubuntu packages"
"${ELEVATE[@]}" apt-get update
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

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    info "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
command -v uv >/dev/null 2>&1 || fail "uv installation failed."

if [[ ! -e "$INSTALL_DIRECTORY" ]]; then
    info "Cloning miner into $INSTALL_DIRECTORY"
    git clone "$REPOSITORY_URL" "$INSTALL_DIRECTORY"
elif [[ ! -d "$INSTALL_DIRECTORY" ]] || ! git -C "$INSTALL_DIRECTORY" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    fail "$INSTALL_DIRECTORY exists but is not a Git checkout. Move it aside and rerun this script."
else
    info "Reusing existing checkout at $INSTALL_DIRECTORY"
fi

[[ -f "$INSTALL_DIRECTORY/pyproject.toml" && -f "$INSTALL_DIRECTORY/install.sh" ]] || \
    fail "$INSTALL_DIRECTORY is not a valid miner checkout."
validate_checkout

info "Installing the generator miner runtime"
(
    cd "$INSTALL_DIRECTORY"
    bash ./install.sh --generator-only
)

cat <<EOF

VPS setup complete. The miner has not been configured or started.

Manual next steps:
  1. cd $INSTALL_DIRECTORY
  2. cp .env.gen_miner.template .env.gen_miner
  3. Edit .env.gen_miner and restore/copy the Bittensor wallet for this user.
  4. source "$NVM_DIR/nvm.sh"
  5. pm2 start gen_miner.config.js
  6. pm2 save

UFW is enabled with TCP ports 22 and 8000-9000 open.
EOF
