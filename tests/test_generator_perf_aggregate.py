import json
import os
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "generator_perf_aggregate.py"


def _write_fake_bittensor(tmp_path, hotkeys, chain_metrics=None):
    module = tmp_path / "bittensor.py"
    module.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "BLOCKTIME = 12\n"
        "class Subtensor:\n"
        "    def close(self):\n"
        "        if os.environ.get('FAKE_BITTENSOR_CLOSE_ERROR'):\n"
        "            raise RuntimeError(os.environ['FAKE_BITTENSOR_CLOSE_ERROR'])\n"
        "        if os.environ.get('FAKE_BITTENSOR_CLOSE_LOG'):\n"
        "            Path(os.environ['FAKE_BITTENSOR_CLOSE_LOG']).write_text('closed')\n"
        "class Wallet:\n"
        "    def __init__(self, name, hotkey):\n"
        "        self.hotkey = SimpleNamespace(ss58_address=f'address-{hotkey}')\n"
        "class Metagraph:\n"
        "    def __init__(self, netuid, network, lite, sync):\n"
        "        if os.environ.get('FAKE_BITTENSOR_ERROR'):\n"
        "            raise RuntimeError(os.environ['FAKE_BITTENSOR_ERROR'])\n"
        "        if netuid != 34 or network != 'finney' or not lite or not sync:\n"
        "            raise RuntimeError('unexpected metagraph arguments')\n"
        "        metrics = json.loads(os.environ['FAKE_BITTENSOR_METRICS'])\n"
        "        self.hotkeys = [f'address-{name}' for name in metrics]\n"
        "        self.I = [values['incentive'] for values in metrics.values()]\n"
        "        self.E = [values['emission'] for values in metrics.values()]\n"
        "        self.tempo = 360\n"
        "        self.pool = SimpleNamespace(tao_in=50.0, alpha_in=100.0)\n"
        "        self.subtensor = Subtensor()\n"
    )
    return chain_metrics or {
        hotkey: {"incentive": 0.0, "emission": 0.0} for hotkey in hotkeys
    }


def _write_fake_gascli(tmp_path, payloads, chain_metrics=None):
    executable = tmp_path / "gascli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "hotkey = sys.argv[sys.argv.index('--wallet-hotkey') + 1]\n"
        "modality = sys.argv[sys.argv.index('--modality') + 1] if '--modality' in sys.argv else 'all'\n"
        "payloads = json.loads(os.environ['FAKE_GASCLI_PAYLOADS'])\n"
        "if os.environ.get('FAKE_GASCLI_LOG_DIR'):\n"
        "    log_dir = Path(os.environ['FAKE_GASCLI_LOG_DIR'])\n"
        "    log_dir.mkdir(exist_ok=True)\n"
        "    (log_dir / f'{hotkey}-{modality}.json').write_text(json.dumps(sys.argv[1:]))\n"
        "result = payloads.get(f'{hotkey}:{modality}', payloads.get(hotkey))\n"
        "if result is None:\n"
        "    print(f'missing fixture for {hotkey}:{modality}', file=sys.stderr)\n"
        "    raise SystemExit(2)\n"
        "if os.environ.get('FAKE_GASCLI_CONCURRENCY_DIR'):\n"
        "    active_dir = Path(os.environ['FAKE_GASCLI_CONCURRENCY_DIR'])\n"
        "    active_dir.mkdir(exist_ok=True)\n"
        "    active = active_dir / f'{hotkey}-{modality}.active'\n"
        "    sibling = active_dir / f'{hotkey}-{'video' if modality == 'image' else 'image'}.active'\n"
        "    active.write_text('')\n"
        "    try:\n"
        "        deadline = time.monotonic() + 2\n"
        "        while not sibling.exists() and time.monotonic() < deadline:\n"
        "            time.sleep(0.01)\n"
        "        if not sibling.exists():\n"
        "            print('same-hotkey modalities did not overlap', file=sys.stderr)\n"
        "            raise SystemExit(1)\n"
        "        if any(not path.name.startswith(f'{hotkey}-') for path in active_dir.glob('*.active')):\n"
        "            print('different hotkeys overlapped', file=sys.stderr)\n"
        "            raise SystemExit(1)\n"
        "        time.sleep(0.2)\n"
        "    finally:\n"
        "        active.unlink(missing_ok=True)\n"
        "if result.get('__silent_error__'):\n"
        "    raise SystemExit(1)\n"
        "if '__error__' in result:\n"
        "    print(result['__error__'], file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "print(f'\\n  ⛽  generator  5{hotkey}\\n')\n"
        "print(result.get('__raw__', json.dumps(result)))\n"
    )
    executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["FAKE_GASCLI_PAYLOADS"] = json.dumps(payloads)
    hotkeys = {key.split(":", 1)[0] for key in payloads}
    env["FAKE_BITTENSOR_METRICS"] = json.dumps(
        _write_fake_bittensor(tmp_path, hotkeys, chain_metrics)
    )
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(tmp_path), env.get("PYTHONPATH")))
    )
    return env


def _run_script(env, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--wallet-name", "cold", *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _table_rows(output):
    return [
        [cell.strip() for cell in line.split("|")]
        for line in output.splitlines()
        if "|" in line
    ]


def test_separates_image_and_video_and_recomputes_weighted_combined_totals(tmp_path):
    payloads = {
        "hk0:image": {
            "verification": {
                "validator_count": 0,
                "aggregate_pass_rate": None,
                "total_verified": 0,
                "total_failed": 0,
                "total_evaluated": 0,
                "by_validator": [],
            },
            "fool_aggregate": {
                "fool_rate": 0.082,
                "total_samples": 5266,
                "fooled_count": 432,
                "not_fooled_count": 4834,
                "benchmark_run_count": 129,
            },
        },
        "hk0:video": {
            "verification": {
                "validator_count": 0,
                "aggregate_pass_rate": None,
                "total_verified": 0,
                "total_failed": 0,
                "total_evaluated": 0,
                "by_validator": [],
            },
            "fool_aggregate": {
                "fool_rate": 0.0003,
                "total_samples": 3501,
                "fooled_count": 1,
                "not_fooled_count": 3500,
                "benchmark_run_count": 67,
            },
        },
        "hk1:image": {
            "verification": {
                "validator_count": 0,
                "total_verified": 0,
                "total_failed": 0,
                "total_evaluated": 0,
                "aggregate_pass_rate": None,
                "by_validator": [],
            },
            "fool_aggregate": {
                "fool_rate": 0.25,
                "total_samples": 100,
                "fooled_count": 25,
                "not_fooled_count": 75,
                "benchmark_run_count": 2,
            },
        },
        "hk1:video": {
            "verification": {
                "validator_count": 0,
                "total_verified": 0,
                "total_failed": 0,
                "total_evaluated": 0,
                "aggregate_pass_rate": None,
                "by_validator": [],
            },
            "fool_aggregate": {
                "fool_rate": 0.05,
                "total_samples": 400,
                "fooled_count": 20,
                "not_fooled_count": 380,
                "benchmark_run_count": 3,
            },
        },
    }

    chain_metrics = {
        "hk0": {"incentive": 0.001, "emission": 0.361},
        "hk1": {"incentive": 0.002, "emission": 0.722},
    }

    result = _run_script(
        _write_fake_gascli(tmp_path, payloads, chain_metrics), "hk0", "hk1"
    )

    assert result.returncode == 0, result.stderr
    assert _table_rows(result.stdout) == [
        [
            "HOTKEY",
            "IMAGE SAMPLES",
            "IMAGE SUCCESS RATE",
            "VIDEO SAMPLES",
            "VIDEO SUCCESS RATE",
            "COMBINED SAMPLES",
            "COMBINED SUCCESS RATE",
            "INCENTIVE",
            "α/DAY",
            "τ/DAY",
        ],
        [
            "hk0",
            "5266",
            "8.20%",
            "3501",
            "0.03%",
            "8767",
            "4.94%",
            "0.001000",
            "7.2000",
            "3.600000",
        ],
        [
            "hk1",
            "100",
            "25.00%",
            "400",
            "5.00%",
            "500",
            "9.00%",
            "0.002000",
            "14.4000",
            "7.200000",
        ],
        [
            "TOTAL",
            "5366",
            "8.52%",
            "3901",
            "0.54%",
            "9267",
            "5.16%",
            "0.003000",
            "21.6000",
            "10.800000",
        ],
    ]


def test_forwards_shared_filters_and_queries_both_modalities(tmp_path):
    payload = {
        "verification": {"by_validator": []},
        "fool_aggregate": {},
    }
    env = _write_fake_gascli(tmp_path, {"hk0": payload})
    log_dir = tmp_path / "gascli-args"
    env["FAKE_GASCLI_LOG_DIR"] = str(log_dir)

    result = _run_script(
        env,
        "--lookback-days",
        "14",
        "--api-url",
        "https://gas.example",
        "hk0",
    )

    assert result.returncode == 0, result.stderr
    assert {path.stem: json.loads(path.read_text()) for path in log_dir.iterdir()} == {
        f"hk0-{modality}": [
            "g",
            "perf",
            "--wallet-name",
            "cold",
            "--wallet-hotkey",
            "hk0",
            "--lookback-days",
            "14",
            "--modality",
            modality,
            "--api-url",
            "https://gas.example",
            "--json",
        ]
        for modality in ("image", "video")
    }


def test_closes_chain_connection_before_exit(tmp_path):
    payload = {
        "verification": {"by_validator": []},
        "fool_aggregate": {},
    }
    env = _write_fake_gascli(tmp_path, {"hk0": payload})
    close_log = tmp_path / "chain-closed"
    env["FAKE_BITTENSOR_CLOSE_LOG"] = str(close_log)

    result = _run_script(env, "hk0")

    assert result.returncode == 0, result.stderr
    assert close_log.read_text() == "closed"


def test_reports_chain_close_failure_without_losing_table(tmp_path):
    payload = {
        "verification": {"by_validator": []},
        "fool_aggregate": {},
    }
    env = _write_fake_gascli(tmp_path, {"hk0": payload})
    env["FAKE_BITTENSOR_CLOSE_ERROR"] = "close timed out"

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    assert _table_rows(result.stdout)[1] == [
        "hk0",
        "0",
        "—",
        "0",
        "—",
        "0",
        "—",
        "0.000000",
        "0.0000",
        "0.000000",
    ]
    assert result.stderr == (
        "warning: hk0: chain: connection cleanup failed: close timed out\n"
    )


def test_keeps_successful_modality_and_excludes_failed_query_from_totals(tmp_path):
    payloads = {
        "hk0": {
            "verification": {
                "validator_count": 2,
                "aggregate_pass_rate": 0.75,
                "total_verified": 3,
                "total_failed": 1,
                "total_evaluated": 4,
                "by_validator": [],
            },
            "fool_aggregate": {
                "fool_rate": 0.4,
                "total_samples": 10,
                "fooled_count": 4,
                "not_fooled_count": 6,
                "benchmark_run_count": 2,
            },
        },
        "broken:image": {
            "verification": {
                "total_verified": 0,
                "total_failed": 0,
                "total_evaluated": 0,
                "by_validator": [],
            },
            "fool_aggregate": {
                "fool_rate": 0.6,
                "total_samples": 5,
                "fooled_count": 3,
                "not_fooled_count": 2,
                "benchmark_run_count": 1,
            },
        },
        "broken:video": {"__error__": "API error 500: unavailable"},
    }

    result = _run_script(_write_fake_gascli(tmp_path, payloads), "hk0", "broken")

    assert result.returncode == 1
    assert _table_rows(result.stdout) == [
        [
            "HOTKEY",
            "IMAGE SAMPLES",
            "IMAGE SUCCESS RATE",
            "VIDEO SAMPLES",
            "VIDEO SUCCESS RATE",
            "COMBINED SAMPLES",
            "COMBINED SUCCESS RATE",
            "INCENTIVE",
            "α/DAY",
            "τ/DAY",
        ],
        [
            "hk0",
            "10",
            "40.00%",
            "10",
            "40.00%",
            "20",
            "40.00%",
            "0.000000",
            "0.0000",
            "0.000000",
        ],
        [
            "broken",
            "5",
            "60.00%",
            "—",
            "—",
            "5",
            "60.00%",
            "0.000000",
            "0.0000",
            "0.000000",
        ],
        [
            "TOTAL",
            "15",
            "46.67%",
            "10",
            "40.00%",
            "25",
            "44.00%",
            "0.000000",
            "0.0000",
            "0.000000",
        ],
    ]
    assert result.stderr == "warning: broken: video: API error 500: unavailable\n"


def test_reports_malformed_single_hotkey_json(tmp_path):
    env = _write_fake_gascli(tmp_path, {"hk0": {"__raw__": "{broken"}})

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    rows = _table_rows(result.stdout)
    assert rows[1] == [
        "hk0",
        "—",
        "—",
        "—",
        "—",
        "0",
        "—",
        "0.000000",
        "0.0000",
        "0.000000",
    ]
    assert result.stderr == "warning: hk0: image: invalid JSON; video: invalid JSON\n"


def test_reports_when_gascli_is_not_available(tmp_path):
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)

    result = _run_script(env, "hk0")

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "error: gascli not found; activate the project virtualenv\n"


def test_reports_launch_failure_without_aborting_table(tmp_path):
    payload = {
        "verification": {"by_validator": []},
        "fool_aggregate": {},
    }
    env = _write_fake_gascli(tmp_path, {"hk0": payload})
    executable = tmp_path / "gascli"
    executable.write_text("#!/missing/python\n")
    executable.chmod(0o755)

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    assert result.stderr.startswith("warning: hk0: image: unable to start gascli:")
    assert "; video: unable to start gascli:" in result.stderr


def test_reports_failed_hotkey_when_gascli_returns_no_error_text(tmp_path):
    env = _write_fake_gascli(tmp_path, {"hk0": {"__silent_error__": True}})

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    assert result.stderr == (
        "warning: hk0: image: gascli exited with status 1; "
        "video: gascli exited with status 1\n"
    )


def test_reports_valid_json_with_invalid_result_shape(tmp_path):
    env = _write_fake_gascli(
        tmp_path,
        {"hk0": {"verification": [], "fool_aggregate": {}}},
    )

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    assert result.stderr == (
        "warning: hk0: image: invalid JSON result; video: invalid JSON result\n"
    )


def test_rejects_invalid_fool_counts_without_losing_other_modality(tmp_path):
    env = _write_fake_gascli(
        tmp_path,
        {
            "hk0:image": {
                "verification": {},
                "fool_aggregate": {
                    "fooled_count": None,
                    "total_samples": 4,
                },
            },
            "hk0:video": {
                "verification": {},
                "fool_aggregate": {
                    "fooled_count": 2,
                    "total_samples": 4,
                },
            },
        },
    )

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    assert result.stderr == "warning: hk0: image: invalid JSON result\n"
    assert _table_rows(result.stdout) == [
        [
            "HOTKEY",
            "IMAGE SAMPLES",
            "IMAGE SUCCESS RATE",
            "VIDEO SAMPLES",
            "VIDEO SUCCESS RATE",
            "COMBINED SAMPLES",
            "COMBINED SUCCESS RATE",
            "INCENTIVE",
            "α/DAY",
            "τ/DAY",
        ],
        [
            "hk0",
            "—",
            "—",
            "4",
            "50.00%",
            "4",
            "50.00%",
            "0.000000",
            "0.0000",
            "0.000000",
        ],
        [
            "TOTAL",
            "0",
            "—",
            "4",
            "50.00%",
            "4",
            "50.00%",
            "0.000000",
            "0.0000",
            "0.000000",
        ],
    ]


def test_keeps_performance_results_when_chain_lookup_fails(tmp_path):
    payload = {
        "verification": {"by_validator": []},
        "fool_aggregate": {"total_samples": 10, "fooled_count": 4},
    }
    env = _write_fake_gascli(tmp_path, {"hk0": payload})
    env["FAKE_BITTENSOR_ERROR"] = "chain offline"

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    assert _table_rows(result.stdout) == [
        [
            "HOTKEY",
            "IMAGE SAMPLES",
            "IMAGE SUCCESS RATE",
            "VIDEO SAMPLES",
            "VIDEO SUCCESS RATE",
            "COMBINED SAMPLES",
            "COMBINED SUCCESS RATE",
            "INCENTIVE",
            "α/DAY",
            "τ/DAY",
        ],
        [
            "hk0",
            "10",
            "40.00%",
            "10",
            "40.00%",
            "20",
            "40.00%",
            "—",
            "—",
            "—",
        ],
        [
            "TOTAL",
            "10",
            "40.00%",
            "10",
            "40.00%",
            "20",
            "40.00%",
            "—",
            "—",
            "—",
        ],
    ]
    assert result.stderr == "warning: hk0: chain: chain offline\n"


def test_queries_modalities_together_but_waits_before_next_hotkey(tmp_path):
    payload = {
        "verification": {"by_validator": []},
        "fool_aggregate": {},
    }
    env = _write_fake_gascli(tmp_path, {"hk0": payload, "hk1": payload})
    env["FAKE_GASCLI_CONCURRENCY_DIR"] = str(tmp_path / "active")

    result = _run_script(env, "hk0", "hk1")

    assert result.returncode == 0, result.stderr
