import json
import os
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "generator_perf_aggregate.py"


def _write_fake_gascli(tmp_path, payloads):
    executable = tmp_path / "gascli"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "hotkey = sys.argv[sys.argv.index('--wallet-hotkey') + 1]\n"
        "payloads = json.loads(os.environ['FAKE_GASCLI_PAYLOADS'])\n"
        "if os.environ.get('FAKE_GASCLI_LOG'):\n"
        "    open(os.environ['FAKE_GASCLI_LOG'], 'w').write(json.dumps(sys.argv[1:]))\n"
        "result = payloads[hotkey]\n"
        "if os.environ.get('FAKE_GASCLI_BARRIER'):\n"
        "    barrier = Path(os.environ['FAKE_GASCLI_BARRIER'])\n"
        "    barrier.mkdir(exist_ok=True)\n"
        "    (barrier / hotkey).touch()\n"
        "    deadline = time.monotonic() + 2\n"
        "    expected = int(os.environ['FAKE_GASCLI_BARRIER_COUNT'])\n"
        "    while len(list(barrier.iterdir())) < expected:\n"
        "        if time.monotonic() >= deadline:\n"
        "            print('queries did not overlap', file=sys.stderr)\n"
        "            raise SystemExit(1)\n"
        "        time.sleep(0.01)\n"
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


def test_combines_hotkey_json_into_table_and_recomputes_totals(tmp_path):
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
        "hk1": {
            "verification": {
                "validator_count": 3,
                "aggregate_pass_rate": 0.5,
                "total_verified": 5,
                "total_failed": 5,
                "total_evaluated": 10,
                "by_validator": [],
            },
            "fool_aggregate": {
                "fool_rate": 0.3,
                "total_samples": 30,
                "fooled_count": 9,
                "not_fooled_count": 21,
                "benchmark_run_count": 4,
            },
        },
    }

    result = _run_script(_write_fake_gascli(tmp_path, payloads), "hk0", "hk1")

    assert result.returncode == 0, result.stderr
    assert _table_rows(result.stdout) == [
        ["HOTKEY", "VALIDATORS", "PASS RATE", "VERIFIED", "FAILED", "EVALUATED", "FOOL RATE", "SAMPLES", "RUNS", "STATUS"],
        ["hk0", "2", "75.0%", "3", "1", "4", "40.0%", "10", "2", "OK"],
        ["hk1", "3", "50.0%", "5", "5", "10", "30.0%", "30", "4", "OK"],
        ["TOTAL", "—", "57.1%", "8", "6", "14", "32.5%", "40", "6", "OK"],
    ]


def test_forwards_generator_performance_filters(tmp_path):
    payload = {
        "verification": {"by_validator": []},
        "fool_aggregate": {},
    }
    env = _write_fake_gascli(tmp_path, {"hk0": payload})
    log = tmp_path / "gascli-args.json"
    env["FAKE_GASCLI_LOG"] = str(log)

    result = _run_script(
        env,
        "--modality",
        "image",
        "--lookback-days",
        "14",
        "--api-url",
        "https://gas.example",
        "hk0",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(log.read_text()) == [
        "g",
        "perf",
        "--wallet-name",
        "cold",
        "--wallet-hotkey",
        "hk0",
        "--lookback-days",
        "14",
        "--modality",
        "image",
        "--api-url",
        "https://gas.example",
        "--json",
    ]


def test_keeps_good_rows_and_excludes_failed_hotkeys_from_totals(tmp_path):
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
        "broken": {"__error__": "API error 500: unavailable"},
    }

    result = _run_script(_write_fake_gascli(tmp_path, payloads), "hk0", "broken")

    assert result.returncode == 1
    assert _table_rows(result.stdout) == [
        ["HOTKEY", "VALIDATORS", "PASS RATE", "VERIFIED", "FAILED", "EVALUATED", "FOOL RATE", "SAMPLES", "RUNS", "STATUS"],
        ["hk0", "2", "75.0%", "3", "1", "4", "40.0%", "10", "2", "OK"],
        ["broken", "—", "—", "—", "—", "—", "—", "—", "—", "ERROR: API error 500: unavailable"],
        ["TOTAL", "—", "75.0%", "3", "1", "4", "40.0%", "10", "2", "PARTIAL"],
    ]


def test_reports_malformed_single_hotkey_json(tmp_path):
    env = _write_fake_gascli(tmp_path, {"hk0": {"__raw__": "{broken"}})

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    rows = _table_rows(result.stdout)
    assert rows[1] == ["hk0", "—", "—", "—", "—", "—", "—", "—", "—", "ERROR: invalid JSON"]
    assert rows[2][-1] == "FAILED"


def test_reports_when_gascli_is_not_available(tmp_path):
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)

    result = _run_script(env, "hk0")

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "error: gascli not found; activate the project virtualenv\n"


def test_reports_failed_hotkey_when_gascli_returns_no_error_text(tmp_path):
    env = _write_fake_gascli(tmp_path, {"hk0": {"__silent_error__": True}})

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    assert _table_rows(result.stdout)[1][-1] == "ERROR: gascli exited with status 1"


def test_reports_valid_json_with_invalid_result_shape(tmp_path):
    env = _write_fake_gascli(
        tmp_path,
        {"hk0": {"verification": [], "fool_aggregate": {}}},
    )

    result = _run_script(env, "hk0")

    assert result.returncode == 1
    assert _table_rows(result.stdout)[1][-1] == "ERROR: invalid JSON result"


def test_queries_hotkeys_in_parallel_and_preserves_input_order(tmp_path):
    payload = {
        "verification": {"by_validator": []},
        "fool_aggregate": {},
    }
    env = _write_fake_gascli(tmp_path, {"hk0": payload, "hk1": payload})
    env["FAKE_GASCLI_BARRIER"] = str(tmp_path / "barrier")
    env["FAKE_GASCLI_BARRIER_COUNT"] = "2"

    result = _run_script(env, "hk0", "hk1")

    assert result.returncode == 0, result.stderr
    rows = _table_rows(result.stdout)
    assert [rows[1][0], rows[2][0]] == ["hk0", "hk1"]
