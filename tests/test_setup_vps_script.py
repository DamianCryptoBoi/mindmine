import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPO_ROOT / "setup-vps.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _fake_vps(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    home.mkdir()
    bin_dir.mkdir()
    state_dir.mkdir()

    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')

    _write_executable(
        bin_dir / "sudo",
        "#!/bin/bash\n"
        'if [ "${1:-}" = "-v" ]; then exit 0; fi\n'
        'exec "$@"\n',
    )
    _write_executable(
        bin_dir / "apt-get",
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$SETUP_TEST_STATE/apt.log"\n',
    )
    _write_executable(
        bin_dir / "ufw",
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$SETUP_TEST_STATE/ufw.log"\n',
    )
    _write_executable(
        bin_dir / "curl",
        r'''#!/bin/bash
printf '%s\n' "$*" >> "$SETUP_TEST_STATE/installer-urls"
case "$*" in
  *nvm-sh/nvm*)
    cat <<'INSTALL_NVM'
mkdir -p "$NVM_DIR"
touch "$SETUP_TEST_STATE/nvm-installer-ran"
cat > "$NVM_DIR/nvm.sh" <<'LOAD_NVM'
nvm() {
  if [ "${1:-}" = "--version" ]; then
    printf '0.40.6\n'
    return
  fi
  case "${1:-} ${2:-}" in
    "install --lts") touch "$SETUP_TEST_STATE/node-installed" ;;
    "alias default") touch "$SETUP_TEST_STATE/node-defaulted" ;;
    "use default") touch "$SETUP_TEST_STATE/node-selected" ;;
  esac
}
LOAD_NVM
INSTALL_NVM
    ;;
  *astral.sh/uv*)
    cat <<'INSTALL_UV'
mkdir -p "$HOME/.local/bin"
touch "$SETUP_TEST_STATE/uv-installed"
printf '#!/bin/bash\nexit 0\n' > "$HOME/.local/bin/uv"
chmod +x "$HOME/.local/bin/uv"
INSTALL_UV
    ;;
  *) exit 1 ;;
esac
''',
    )
    _write_executable(
        bin_dir / "npm",
        "#!/bin/bash\n"
        'if [ "$*" = "install -g pm2" ]; then\n'
        '  touch "$SETUP_TEST_STATE/pm2-installed"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )
    _write_executable(bin_dir / "node", "#!/bin/bash\necho 'v24.0.0'\n")
    _write_executable(
        bin_dir / "pm2",
        "#!/bin/bash\n"
        'touch "$SETUP_TEST_STATE/pm2-was-run"\n'
        "exit 1\n",
    )
    _write_executable(
        bin_dir / "git",
        r'''#!/bin/bash
if [ "${1:-}" = "clone" ]; then
  destination="$3"
  [ ! -e "$destination" ] || exit 1
  printf '%s\n' "$2" >> "$SETUP_TEST_STATE/repository-url"
  mkdir -p "$destination/.git"
  printf '%s\n' "$2" > "$destination/.git/origin"
  printf '[project]\nname = "gas"\n' > "$destination/pyproject.toml"
  cat > "$destination/install.sh" <<'INSTALL_MINER'
#!/bin/bash
if [ "$*" = "--generator-only" ]; then
  touch "$SETUP_TEST_STATE/generator-runtime-installed"
  exit 0
fi
exit 1
INSTALL_MINER
  chmod +x "$destination/install.sh"
  touch "$SETUP_TEST_STATE/repository-cloned"
  exit 0
fi
if [ "${1:-}" = "-C" ] && [ -d "$2/.git" ]; then
  repository="$2"
  shift 2
  case "$*" in
    "rev-parse --is-inside-work-tree") printf 'true\n' ;;
    "rev-parse --show-toplevel") printf '%s\n' "$repository" ;;
    "rev-parse --verify HEAD") printf '0123456789abcdef\n' ;;
    "remote get-url origin") cat "$repository/.git/origin" ;;
    "ls-files --error-unmatch -- install.sh") exit 0 ;;
    "diff --quiet HEAD -- install.sh")
      if [ -e "$repository/.git/modified-install" ]; then exit 1; fi
      ;;
    *) exit 1 ;;
  esac
  exit 0
fi
exit 1
''',
    )

    env = os.environ.copy()
    env.pop("NVM_DIR", None)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "SETUP_OS_RELEASE_FILE": str(os_release),
            "SETUP_TEST_STATE": str(state_dir),
        }
    )
    return env, home, state_dir


def test_prepares_fresh_ubuntu_vps_without_starting_or_configuring_miner(tmp_path):
    assert SETUP_SCRIPT.exists(), "setup-vps.sh is missing"
    env, home, state_dir = _fake_vps(tmp_path)

    result = subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    apt_commands = (state_dir / "apt.log").read_text()
    for package in (
        "build-essential",
        "ca-certificates",
        "curl",
        "git",
        "libssl-dev",
        "python3",
        "python3-dev",
        "python3-venv",
        "ufw",
    ):
        assert package in apt_commands
    assert (state_dir / "ufw.log").read_text().splitlines() == [
        "allow 22/tcp",
        "allow 8000:9000/tcp",
        "--force enable",
    ]
    for marker in (
        "nvm-installer-ran",
        "node-installed",
        "node-defaulted",
        "node-selected",
        "pm2-installed",
        "repository-cloned",
        "generator-runtime-installed",
        "uv-installed",
    ):
        assert (state_dir / marker).exists(), marker
    assert (state_dir / "repository-url").read_text().strip() == (
        "https://github.com/DamianCryptoBoi/mindmine.git"
    )
    assert not (home / "getmesomemoney" / ".env.gen_miner").exists()
    assert not (state_dir / "pm2-was-run").exists()
    assert "pm2 start gen_miner.config.js" in result.stdout
    installer_urls = (state_dir / "installer-urls").read_text()
    assert "https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh" in installer_urls
    assert "https://astral.sh/uv/install.sh" in installer_urls


def test_rejects_unsupported_linux_before_changing_the_server(tmp_path):
    env, _, state_dir = _fake_vps(tmp_path)
    Path(env["SETUP_OS_RELEASE_FILE"]).write_text(
        'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12"\n'
    )

    result = subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "Ubuntu 22.04 or 24.04" in result.stderr
    assert not (state_dir / "apt.log").exists()


def test_rerun_reuses_existing_miner_checkout(tmp_path):
    env, home, state_dir = _fake_vps(tmp_path)
    first_run = subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )
    assert first_run.returncode == 0, first_run.stdout + first_run.stderr

    checkout = home / "getmesomemoney"
    sentinel = checkout / ".venv" / "keep-me"
    sentinel.parent.mkdir()
    sentinel.write_text("existing environment")

    second_run = subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert second_run.returncode == 0, second_run.stdout + second_run.stderr
    assert "Reusing existing checkout" in second_run.stdout
    assert (state_dir / "repository-url").read_text().count("\n") == 1
    assert (state_dir / "generator-runtime-installed").exists()
    assert sentinel.read_text() == "existing environment"


def test_rejects_existing_checkout_from_another_repository(tmp_path):
    env, home, state_dir = _fake_vps(tmp_path)
    checkout = home / "getmesomemoney"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "origin").write_text(
        "https://github.com/example/untrusted.git\n"
    )
    (checkout / "pyproject.toml").write_text('[project]\nname = "gas"\n')
    _write_executable(
        checkout / "install.sh",
        "#!/bin/bash\n"
        'touch "$SETUP_TEST_STATE/untrusted-installer-ran"\n',
    )

    result = subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "expected repository" in result.stderr
    assert not (state_dir / "untrusted-installer-ran").exists()


def test_rejects_locally_modified_installer(tmp_path):
    env, home, state_dir = _fake_vps(tmp_path)
    checkout = home / "getmesomemoney"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "origin").write_text(
        "https://github.com/DamianCryptoBoi/mindmine.git\n"
    )
    (checkout / ".git" / "modified-install").touch()
    (checkout / "pyproject.toml").write_text('[project]\nname = "gas"\n')
    _write_executable(
        checkout / "install.sh",
        "#!/bin/bash\n"
        'touch "$SETUP_TEST_STATE/modified-installer-ran"\n',
    )

    result = subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "install.sh has local changes" in result.stderr
    assert not (state_dir / "modified-installer-ran").exists()


def test_updates_an_existing_nvm_when_it_is_not_the_pinned_version(tmp_path):
    env, home, state_dir = _fake_vps(tmp_path)
    nvm_dir = home / ".nvm"
    nvm_dir.mkdir()
    (nvm_dir / "nvm.sh").write_text(
        "nvm() {\n"
        '  if [ "${1:-}" = "--version" ]; then printf "0.39.0\\n"; fi\n'
        "}\n"
    )

    result = subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (state_dir / "nvm-installer-ran").exists()
