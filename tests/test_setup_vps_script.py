import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPO_ROOT / "setup-vps.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _fake_vps(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    project_dir = tmp_path / "miner"
    command_bin_dir = tmp_path / "command-bin"
    for directory in (home, bin_dir, state_dir, project_dir, command_bin_dir):
        directory.mkdir()

    (project_dir / "pyproject.toml").write_text('[project]\nname = "gas"\n')
    (project_dir / ".env.gen_miner.template").write_text("BT_WALLET_NAME=default\n")
    _write_executable(
        project_dir / "install.sh",
        "#!/bin/bash\n"
        'if [ "$*" = "--generator-only" ]; then\n'
        "  mkdir -p .venv\n"
        '  touch "$SETUP_TEST_STATE/generator-runtime-installed"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
    )

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
cat > "$HOME/.local/bin/uv" <<'UV'
#!/bin/bash
printf '%s\n' "$*" >> "$SETUP_TEST_STATE/uv-tool.log"
case "$*" in
  "--version") printf 'uv 0.8.0\n' ;;
  "tool install --force bittensor-cli==9.22.0")
    touch "$SETUP_TEST_STATE/btcli-tool-installed"
    printf '#!/bin/bash\necho "btcli 9.22.0"\n' > "$HOME/.local/bin/btcli"
    chmod +x "$HOME/.local/bin/btcli"
    ;;
  "tool update-shell") touch "$SETUP_TEST_STATE/uv-path-persisted" ;;
  *) exit 1 ;;
esac
UV
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
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$SETUP_TEST_STATE/git.log"\n'
        "exit 1\n",
    )

    env = os.environ.copy()
    env.pop("NVM_DIR", None)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}:{command_bin_dir}:/usr/bin:/bin",
            "SETUP_OS_RELEASE_FILE": str(os_release),
            "SETUP_TEST_STATE": str(state_dir),
            "SETUP_COMMAND_BIN_DIR": str(command_bin_dir),
        }
    )
    return env, home, state_dir, project_dir


def _run_setup(env: dict[str, str], project_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        cwd=project_dir,
        env=env,
        text=True,
        capture_output=True,
    )


def test_prepares_current_checkout_without_starting_or_configuring_miner(tmp_path):
    assert SETUP_SCRIPT.exists(), "setup-vps.sh is missing"
    env, home, state_dir, project_dir = _fake_vps(tmp_path)

    result = _run_setup(env, project_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    apt_commands = (state_dir / "apt.log").read_text().splitlines()
    assert apt_commands[:2] == ["update", "upgrade -y"]
    assert apt_commands[2].startswith("install -y ")
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
        assert package in apt_commands[2]
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
        "uv-installed",
        "btcli-tool-installed",
        "uv-path-persisted",
        "generator-runtime-installed",
    ):
        assert (state_dir / marker).exists(), marker
    assert (state_dir / "uv-tool.log").read_text().splitlines() == [
        "tool install --force bittensor-cli==9.22.0",
        "tool update-shell",
    ]
    assert not (project_dir / ".env.gen_miner").exists()
    assert not (state_dir / "pm2-was-run").exists()
    assert not (state_dir / "git.log").exists()
    assert str(project_dir) in result.stdout
    assert "btcli" in result.stdout
    assert "uv and btcli are available immediately" in result.stdout
    assert "pm2 start gen_miner.config.js" in result.stdout
    installer_urls = (state_dir / "installer-urls").read_text()
    assert "https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh" in installer_urls
    assert "https://astral.sh/uv/install.sh" in installer_urls
    assert (home / ".local" / "bin" / "btcli").exists()
    for command in ("uv", "btcli"):
        command_link = tmp_path / "command-bin" / command
        assert command_link.is_symlink()
        assert command_link.resolve() == (home / ".local" / "bin" / command).resolve()
        command_result = subprocess.run(
            [command, "--version"],
            cwd=project_dir,
            env=env,
            text=True,
            capture_output=True,
        )
        assert command_result.returncode == 0, command_result.stderr


def test_rejects_unsupported_linux_before_changing_the_server(tmp_path):
    env, _, state_dir, project_dir = _fake_vps(tmp_path)
    Path(env["SETUP_OS_RELEASE_FILE"]).write_text(
        'ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Debian GNU/Linux 12"\n'
    )

    result = _run_setup(env, project_dir)

    assert result.returncode != 0
    assert "Ubuntu 22.04 or 24.04" in result.stderr
    assert not (state_dir / "apt.log").exists()


def test_requires_running_from_the_miner_repository_root(tmp_path):
    env, _, state_dir, _ = _fake_vps(tmp_path)
    wrong_directory = tmp_path / "wrong-directory"
    wrong_directory.mkdir()

    result = _run_setup(env, wrong_directory)

    assert result.returncode != 0
    assert "repository root" in result.stderr
    assert not (state_dir / "apt.log").exists()


def test_rerun_preserves_existing_virtual_environment(tmp_path):
    env, _, state_dir, project_dir = _fake_vps(tmp_path)
    first_run = _run_setup(env, project_dir)
    assert first_run.returncode == 0, first_run.stdout + first_run.stderr

    sentinel = project_dir / ".venv" / "keep-me"
    sentinel.write_text("existing environment")
    second_run = _run_setup(env, project_dir)

    assert second_run.returncode == 0, second_run.stdout + second_run.stderr
    assert sentinel.read_text() == "existing environment"
    assert not (state_dir / "git.log").exists()


def test_refuses_to_overwrite_an_unrelated_global_command(tmp_path):
    env, _, state_dir, project_dir = _fake_vps(tmp_path)
    existing_uv = Path(env["SETUP_COMMAND_BIN_DIR"]) / "uv"
    _write_executable(existing_uv, "#!/bin/bash\necho 'unrelated uv'\n")

    result = _run_setup(env, project_dir)

    assert result.returncode != 0
    assert "Refusing to replace" in result.stderr
    assert existing_uv.read_text() == "#!/bin/bash\necho 'unrelated uv'\n"
    assert not (state_dir / "generator-runtime-installed").exists()


def test_requires_global_command_directory_on_parent_path(tmp_path):
    env, _, state_dir, project_dir = _fake_vps(tmp_path)
    command_bin_dir = env["SETUP_COMMAND_BIN_DIR"]
    env["PATH"] = ":".join(
        entry for entry in env["PATH"].split(":") if entry != command_bin_dir
    )

    result = _run_setup(env, project_dir)

    assert result.returncode != 0
    assert "must already be on PATH" in result.stderr
    assert not (state_dir / "apt.log").exists()


def test_updates_an_existing_nvm_when_it_is_not_the_pinned_version(tmp_path):
    env, home, state_dir, project_dir = _fake_vps(tmp_path)
    nvm_dir = home / ".nvm"
    nvm_dir.mkdir()
    (nvm_dir / "nvm.sh").write_text(
        "nvm() {\n"
        '  if [ "${1:-}" = "--version" ]; then printf "0.39.0\\n"; fi\n'
        "}\n"
    )

    result = _run_setup(env, project_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (state_dir / "nvm-installer-ran").exists()
