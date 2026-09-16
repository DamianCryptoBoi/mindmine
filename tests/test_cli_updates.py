from types import SimpleNamespace

from click.testing import CliRunner

import gas.cli as gas_cli


def test_discriminator_push_forwards_latest_upload_options(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gas_cli.subprocess, "run", run)
    result = CliRunner().invoke(
        gas_cli.cli,
        [
            "discriminator",
            "push",
            "--image-model",
            "model.zip",
            "--upload-endpoint",
            "https://upload.example/upload",
            "--skip-chain",
            "--max-retries",
            "5",
        ],
    )

    assert result.exit_code == 0
    assert captured["command"][-5:] == [
        "--max-retries",
        "5",
        "--upload-endpoint",
        "https://upload.example/upload",
        "--skip-chain",
    ]


def test_discriminator_push_propagates_child_exit_code(monkeypatch):
    monkeypatch.setattr(
        gas_cli.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=7),
    )

    result = CliRunner().invoke(
        gas_cli.cli,
        ["discriminator", "push", "--image-model", "model.zip"],
    )

    assert result.exit_code == 7
