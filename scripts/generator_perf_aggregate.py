#!/usr/bin/env python3
"""Aggregate ``gascli g perf --json`` results for multiple hotkeys."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import subprocess
import sys


HEADERS = (
    "HOTKEY",
    "VALIDATORS",
    "PASS RATE",
    "VERIFIED",
    "FAILED",
    "EVALUATED",
    "FOOL RATE",
    "SAMPLES",
    "RUNS",
    "STATUS",
)


def _percent(value):
    return "—" if value is None else f"{float(value) * 100:.1f}%"


def _extract_json(output):
    start = output.find("{")
    if start < 0:
        raise ValueError("no JSON object in gascli output")
    return json.loads(output[start:])


def _valid_result(data):
    return isinstance(data, dict) and all(
        data.get(key) is None or isinstance(data[key], dict)
        for key in ("verification", "fool_aggregate")
    )


def _format_table(rows):
    values = [HEADERS, *rows]
    widths = [max(len(str(row[index])) for row in values) for index in range(len(HEADERS))]

    def render(row):
        return " | ".join(str(value).ljust(width) for value, width in zip(row, widths))

    return "\n".join((render(HEADERS), "-+-".join("-" * width for width in widths), *(render(row) for row in rows)))


def _result_row(hotkey, data):
    verification = data.get("verification") or {}
    fool = data.get("fool_aggregate") or {}
    return (
        hotkey,
        verification.get("validator_count", 0),
        _percent(verification.get("aggregate_pass_rate")),
        verification.get("total_verified", 0),
        verification.get("total_failed", 0),
        verification.get("total_evaluated", 0),
        _percent(fool.get("fool_rate")),
        fool.get("total_samples", 0),
        fool.get("benchmark_run_count", 0),
        "OK",
    )


def _error_row(hotkey, message):
    return (hotkey, "—", "—", "—", "—", "—", "—", "—", "—", f"ERROR: {message}")


def _total_row(results, status="OK"):
    verified = sum((data.get("verification") or {}).get("total_verified", 0) for data in results)
    failed = sum((data.get("verification") or {}).get("total_failed", 0) for data in results)
    evaluated = sum((data.get("verification") or {}).get("total_evaluated", 0) for data in results)
    fooled = sum((data.get("fool_aggregate") or {}).get("fooled_count", 0) for data in results)
    samples = sum((data.get("fool_aggregate") or {}).get("total_samples", 0) for data in results)
    runs = sum((data.get("fool_aggregate") or {}).get("benchmark_run_count", 0) for data in results)
    return (
        "TOTAL",
        "—",
        _percent(verified / evaluated if evaluated else None),
        verified,
        failed,
        evaluated,
        _percent(fooled / samples if samples else None),
        samples,
        runs,
        status,
    )


def _query_hotkey(gascli, args, hotkey):
    command = [
        gascli,
        "g",
        "perf",
        "--wallet-name",
        args.wallet_name,
        "--wallet-hotkey",
        hotkey,
        "--lookback-days",
        str(args.lookback_days),
    ]
    if args.modality:
        command.extend(("--modality", args.modality))
    if args.api_url:
        command.extend(("--api-url", args.api_url))
    command.append("--json")
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        error_lines = (completed.stderr or completed.stdout).strip().splitlines()
        message = error_lines[-1] if error_lines else f"gascli exited with status {completed.returncode}"
        return hotkey, None, message
    try:
        data = _extract_json(completed.stdout)
    except ValueError:
        return hotkey, None, "invalid JSON"
    if not _valid_result(data):
        return hotkey, None, "invalid JSON result"
    return hotkey, data, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet-name", required=True, help="Bittensor wallet name")
    parser.add_argument("--modality", choices=("image", "video", "audio"))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--api-url")
    parser.add_argument("hotkeys", nargs="+", help="Wallet hotkey names")
    args = parser.parse_args()

    gascli = shutil.which("gascli")
    if gascli is None:
        print("error: gascli not found; activate the project virtualenv", file=sys.stderr)
        return 2
    rows = []
    results = []
    failures = 0
    with ThreadPoolExecutor(max_workers=min(8, len(args.hotkeys))) as executor:
        outcomes = executor.map(lambda hotkey: _query_hotkey(gascli, args, hotkey), args.hotkeys)
        for hotkey, data, error in outcomes:
            if error:
                rows.append(_error_row(hotkey, error))
                failures += 1
            else:
                results.append(data)
                rows.append(_result_row(hotkey, data))

    status = "OK" if not failures else "PARTIAL" if results else "FAILED"
    rows.append(_total_row(results, status))
    print(_format_table(rows))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
