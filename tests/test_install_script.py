import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_generator_only_installs_runtime_without_validator_stack(tmp_path):
    shutil.copy(REPO_ROOT / "install.sh", tmp_path / "install.sh")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'gas'\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"

    _write_executable(
        bin_dir / "python3",
        "#!/bin/bash\necho 'Python 3.10.12'\n",
    )
    _write_executable(
        bin_dir / "uv",
        """#!/bin/bash
echo "uv $*" >> "$COMMAND_LOG"
if [ "$1" = "venv" ]; then
  mkdir -p .venv/bin
  printf '#!/bin/bash\nexit 0\n' > .venv/bin/python
  printf '#!/bin/bash\nexit 0\n' > .venv/bin/gascli
  chmod +x .venv/bin/python .venv/bin/gascli
fi
""",
    )
    for name in ("node", "pm2", "sudo"):
        _write_executable(
            bin_dir / name,
            f'#!/bin/bash\necho "{name} $*" >> "$COMMAND_LOG"\n',
        )
    _write_executable(
        bin_dir / "npm",
        """#!/bin/bash
echo "npm $*" >> "$COMMAND_LOG"
if [ "$1" = "--version" ]; then echo '10.0.0'; fi
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["COMMAND_LOG"] = str(command_log)
    result = subprocess.run(
        ["bash", "install.sh", "--generator-only"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = command_log.read_text()
    assert "uv venv" in commands
    assert "uv pip install --python .venv/bin/python bittensor==10.4.0" in commands
    assert "numpy==2.0.1" in commands
    assert "async-substrate-interface==2.2.1" in commands
    assert "uvicorn==0.27.1" in commands
    assert "c2pa-python>=0.29.0" in commands
    assert "python-dotenv==1.2.2" in commands
    assert "uv pip install --python .venv/bin/python --no-deps -e ." in commands
    for unwanted in (
        "uv sync",
        "flash-attn",
        "torch",
        "Janus",
        "CLIP",
        "gasbench",
        "diffusers",
        "transformers",
        "apt-get",
    ):
        assert unwanted not in commands


def test_generator_import_does_not_load_validator_dependencies():
    code = """
import sys

blocked = {"clip", "cv2", "diffusers", "ffmpeg", "flash_attn", "scipy", "torch", "transformers"}

class BlockValidatorImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise AssertionError(f"validator dependency imported: {fullname}")
        return None

sys.meta_path.insert(0, BlockValidatorImports())
import gas.utils
import gas.verification
import gas.protocol
from neurons.generator.miner import GenerativeMiner
from neurons.generator.services.maxcheapai_service import MaxCheapAIService
assert not blocked.intersection(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generator_only_preserves_existing_venv_without_clear_flag(tmp_path):
    shutil.copy(REPO_ROOT / "install.sh", tmp_path / "install.sh")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'gas'\n")

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_executable(venv_bin / "python", "#!/bin/bash\nexit 0\n")
    _write_executable(venv_bin / "gascli", "#!/bin/bash\nexit 0\n")
    sentinel = tmp_path / ".venv" / "keep-me"
    sentinel.write_text("existing environment")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    _write_executable(bin_dir / "python3", "#!/bin/bash\necho 'Python 3.10.12'\n")
    _write_executable(
        bin_dir / "uv",
        """#!/bin/bash
echo "uv $*" >> "$COMMAND_LOG"
if [ "$1" = "venv" ]; then
  rm -rf .venv
  mkdir -p .venv/bin
  printf '#!/bin/bash\nexit 0\n' > .venv/bin/python
  printf '#!/bin/bash\nexit 0\n' > .venv/bin/gascli
  chmod +x .venv/bin/python .venv/bin/gascli
fi
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["COMMAND_LOG"] = str(command_log)
    result = subprocess.run(
        ["bash", "install.sh", "--generator-only", "--no-system-deps"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.exists()
    assert "uv venv" not in command_log.read_text()


def test_generator_only_updates_apt_before_installing_node(tmp_path):
    shutil.copy(REPO_ROOT / "install.sh", tmp_path / "install.sh")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'gas'\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    _write_executable(bin_dir / "python3", "#!/bin/bash\necho 'Python 3.10.12'\n")
    _write_executable(
        bin_dir / "uv",
        """#!/bin/bash
echo "uv $*" >> "$COMMAND_LOG"
if [ "$1" = "venv" ]; then
  mkdir -p .venv/bin
  printf '#!/bin/bash\nexit 0\n' > .venv/bin/python
  printf '#!/bin/bash\nexit 0\n' > .venv/bin/gascli
  chmod +x .venv/bin/python .venv/bin/gascli
fi
""",
    )
    _write_executable(
        bin_dir / "sudo",
        """#!/bin/bash
if [ "$1" = "-n" ]; then exit 0; fi
"$@"
""",
    )
    _write_executable(
        bin_dir / "apt-get",
        """#!/bin/bash
echo "apt-get $*" >> "$COMMAND_LOG"
""",
    )
    _write_executable(
        bin_dir / "npm",
        """#!/bin/bash
echo "npm $*" >> "$COMMAND_LOG"
if [ "$1" = "--version" ]; then echo '10.0.0'; fi
""",
    )
    for name in ("pm2", "sleep"):
        _write_executable(bin_dir / name, "#!/bin/bash\nexit 0\n")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["COMMAND_LOG"] = str(command_log)
    result = subprocess.run(
        ["bash", "install.sh", "--generator-only"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = command_log.read_text()
    assert commands.index("apt-get update") < commands.index(
        "apt-get install -y nodejs npm"
    )
