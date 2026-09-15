import os
import shutil
import stat
import subprocess
from pathlib import Path

from dotenv import dotenv_values


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPO_ROOT / "setup-gen-miner-hotkey.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _fake_project(tmp_path: Path, env_contents: str) -> tuple[Path, dict[str, str], Path]:
    project_dir = tmp_path / "miner"
    bin_dir = tmp_path / "bin"
    project_dir.mkdir()
    bin_dir.mkdir()

    shutil.copy(REPO_ROOT / ".env.gen_miner.hotkey.template", project_dir)
    (project_dir / ".env").write_text(env_contents)
    (project_dir / "gen_miner.config.js").write_text("module.exports = { apps: [] };\n")
    (project_dir / "pyproject.toml").write_text('[project]\nname = "gas"\n')

    command_log = tmp_path / "pm2.log"
    _write_executable(
        bin_dir / "curl",
        "#!/bin/bash\nprintf '203.0.113.42\\n'\n",
    )
    _write_executable(
        bin_dir / "pm2",
        "#!/bin/bash\n"
        'printf "%s\\n" "$GEN_MINER_ENV_FILE" >> "$PM2_TEST_LOG"\n'
        'printf "%s\\n" "$*" >> "$PM2_TEST_LOG"\n'
        'hotkey="${GEN_MINER_ENV_FILE#.env.}"\n'
        'if [[ "$1" == start && "${PM2_TEST_EXISTING:-}" == "$hotkey" ]]; then\n'
        "  exit 1\n"
        "fi\n",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["PM2_TEST_LOG"] = str(command_log)
    return project_dir, env, command_log


def _run_setup(
    project_dir: Path, env: dict[str, str], answers: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SETUP_SCRIPT)],
        cwd=project_dir,
        env=env,
        input=answers,
        text=True,
        capture_output=True,
    )


def _read_env(path: Path) -> tuple[dict[str, str], list[str]]:
    assignments = [
        line for line in path.read_text().splitlines() if line and not line.startswith("#")
    ]
    keys = [line.split("=", 1)[0] for line in assignments]
    return dict(dotenv_values(path)), keys


def test_creates_isolated_hotkey_env_and_starts_exact_pm2_config(tmp_path):
    project_dir, env, command_log = _fake_project(
        tmp_path,
        "OPENAI_API_KEY=openai-test-secret\n"
        "RUNWAYML_API_SECRET='runway # test secret'\n"
        'CKEY_API_KEY="ckey# test secret"\n'
        "STABILITY_API_KEY=\n"
        "MAXCHEAPAI_API_KEY=\n",
    )

    result = _run_setup(project_dir, env, "cold-wallet\nhotkey-7\n2\n2\n")

    assert result.returncode == 0, result.stdout + result.stderr
    generated_path = project_dir / ".env.hotkey-7"
    assert generated_path.exists()
    assert stat.S_IMODE(generated_path.stat().st_mode) == 0o600

    generated, keys = _read_env(generated_path)
    assert len(keys) == len(set(keys)), "generated env contains duplicate assignments"
    assert generated["MINER_PM2_NAME"] == "hotkey-7"
    assert generated["IMAGE_SERVICE"] == "ckey"
    assert generated["VIDEO_SERVICE"] == "runway"
    assert generated["CKEY_API_KEY"] == "ckey# test secret"
    assert generated["RUNWAYML_API_SECRET"] == "runway # test secret"
    assert "OPENAI_API_KEY" not in generated
    assert "MAXCHEAPAI_API_KEY" not in generated
    assert generated["BT_WALLET_NAME"] == "cold-wallet"
    assert generated["BT_WALLET_HOTKEY"] == "hotkey-7"
    assert generated["BT_AXON_EXTERNAL_IP"] == "203.0.113.42"
    assert 8000 <= int(generated["BT_AXON_PORT"]) <= 9000
    assert generated["MINER_STATE_DIR"] == (
        "./miner_generated_content/.gen_miner_state/hotkey-7"
    )
    assert generated["MINER_MAX_CONCURRENT_TASKS"] == "8"
    assert generated["MINER_WORKER_THREADS"] == "8"
    assert generated["MINER_TASK_TIMEOUT"] == "3600"
    assert generated["MINER_SAVE_LOCALLY"] == "false"
    assert generated["MINER_OUTPUT_DIR"] == "./miner_generated_content/"
    assert generated["BT_LOGGING_LEVEL"] == "INFO"
    assert generated["AUTO_UPDATE"] == "false"

    combined_output = result.stdout + result.stderr
    assert "OpenAI" in combined_output
    assert "CKey" in combined_output
    assert "Runway" in combined_output
    assert "Stability AI" not in combined_output
    assert "MaxCheapAI" not in combined_output
    for secret in ("openai-test-secret", "runway # test secret", "ckey# test secret"):
        assert secret not in combined_output

    assert command_log.read_text().splitlines() == [
        ".env.hotkey-7",
        "startOrRestart gen_miner.config.js",
    ]


def test_creates_multiple_hotkeys_with_the_same_chosen_settings(tmp_path):
    project_dir, env, command_log = _fake_project(
        tmp_path,
        "OPENAI_API_KEY=openai-test-secret\n"
        "RUNWAYML_API_SECRET=runway-test-secret\n",
    )

    result = _run_setup(
        project_dir,
        env,
        "cold-wallet\nhotkey-7 hotkey-8\n1\n2\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    first, _ = _read_env(project_dir / ".env.hotkey-7")
    second, _ = _read_env(project_dir / ".env.hotkey-8")

    for generated in (first, second):
        assert generated["BT_WALLET_NAME"] == "cold-wallet"
        assert generated["IMAGE_SERVICE"] == "openai"
        assert generated["VIDEO_SERVICE"] == "runway"
        assert generated["OPENAI_API_KEY"] == "openai-test-secret"
        assert generated["RUNWAYML_API_SECRET"] == "runway-test-secret"

    assert first["MINER_PM2_NAME"] == first["BT_WALLET_HOTKEY"] == "hotkey-7"
    assert second["MINER_PM2_NAME"] == second["BT_WALLET_HOTKEY"] == "hotkey-8"
    assert first["MINER_STATE_DIR"].endswith("/hotkey-7")
    assert second["MINER_STATE_DIR"].endswith("/hotkey-8")
    assert first["BT_AXON_PORT"] != second["BT_AXON_PORT"]

    assert command_log.read_text().splitlines() == [
        ".env.hotkey-7",
        "startOrRestart gen_miner.config.js",
        ".env.hotkey-8",
        "startOrRestart gen_miner.config.js",
    ]


def test_overwrites_an_existing_hotkey_env_but_preserves_its_port(tmp_path):
    project_dir, env, command_log = _fake_project(
        tmp_path, "MAXCHEAPAI_API_KEY=maxcheap-test-secret\n"
    )
    existing = project_dir / ".env.hotkey-7"
    existing.write_text(
        "BT_AXON_PORT=8123\n"
        "BT_WALLET_NAME=old-wallet\n"
        "OBSOLETE_SETTING=remove-me\n"
    )
    env["PM2_TEST_EXISTING"] = "hotkey-7"

    result = _run_setup(project_dir, env, "cold-wallet\nhotkey-7\n1\n1\n")

    assert result.returncode == 0, result.stdout + result.stderr
    generated, _ = _read_env(existing)
    assert generated["BT_AXON_PORT"] == "8123"
    assert generated["BT_WALLET_NAME"] == "cold-wallet"
    assert generated["BT_WALLET_HOTKEY"] == "hotkey-7"
    assert generated["IMAGE_SERVICE"] == "maxcheapai"
    assert generated["VIDEO_SERVICE"] == "maxcheapai"
    assert generated["MAXCHEAPAI_API_KEY"] == "maxcheap-test-secret"
    assert "OBSOLETE_SETTING" not in generated
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600
    assert command_log.read_text().splitlines() == [
        ".env.hotkey-7",
        "startOrRestart gen_miner.config.js",
    ]


def test_overwrite_replaces_an_invalid_existing_port(tmp_path):
    project_dir, env, _ = _fake_project(
        tmp_path, "MAXCHEAPAI_API_KEY=maxcheap-test-secret\n"
    )
    existing = project_dir / ".env.hotkey-7"
    existing.write_text("BT_AXON_PORT=not-a-port\n")

    result = _run_setup(project_dir, env, "cold-wallet\nhotkey-7\n1\n1\n")

    assert result.returncode == 0, result.stdout + result.stderr
    generated, _ = _read_env(existing)
    assert generated["BT_AXON_PORT"] != "not-a-port"
    assert 8000 <= int(generated["BT_AXON_PORT"]) <= 9000


def test_overwrite_replaces_a_dangling_symlink_without_following_it(tmp_path):
    project_dir, env, command_log = _fake_project(
        tmp_path, "MAXCHEAPAI_API_KEY=maxcheap-test-secret\n"
    )
    occupied = project_dir / ".env.hotkey-8"
    symlink_target = project_dir / "missing-env"
    occupied.symlink_to(symlink_target)

    result = _run_setup(
        project_dir,
        env,
        "cold-wallet\nhotkey-8\n1\n1\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    generated, _ = _read_env(occupied)
    assert not occupied.is_symlink()
    assert not symlink_target.exists()
    assert 8000 <= int(generated["BT_AXON_PORT"]) <= 9000
    assert command_log.read_text().splitlines() == [
        ".env.hotkey-8",
        "startOrRestart gen_miner.config.js",
    ]


def test_requires_at_least_one_keyed_generation_service(tmp_path):
    project_dir, env, command_log = _fake_project(
        tmp_path,
        'OPENAI_API_KEY="   " # intentionally not configured\n'
        'RUNWAYML_API_KEY="" # intentionally not configured\n'
        "MAXCHEAPAI_API_KEY=\n",
    )

    result = _run_setup(project_dir, env, "cold-wallet\nhotkey-7\n")

    assert result.returncode != 0
    assert "No image or video service API keys" in result.stderr
    assert not (project_dir / ".env.hotkey-7").exists()
    assert not command_log.exists()
