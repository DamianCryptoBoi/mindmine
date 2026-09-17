#!/usr/bin/env python3
"""Aggregate ``gascli g perf --json`` results for multiple hotkeys."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import shutil
import subprocess
import sys


HEADERS = (
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
)
MODALITIES = ("image", "video")


def _percent(value):
    return "—" if value is None else f"{float(value) * 100:.2f}%"


def _decimal(value, places):
    return "—" if value is None else f"{float(value):.{places}f}"


def _extract_json(output):
    start = output.find("{")
    if start < 0:
        raise ValueError("no JSON object in gascli output")
    return json.loads(output[start:])


def _valid_result(data):
    if not isinstance(data, dict) or not all(
        data.get(key) is None or isinstance(data[key], dict)
        for key in ("verification", "fool_aggregate")
    ):
        return False
    fool = data.get("fool_aggregate") or {}
    fooled = fool.get("fooled_count", 0)
    samples = fool.get("total_samples", 0)
    valid_counts = all(
        type(value) in (int, float) and math.isfinite(value) and value >= 0
        for value in (fooled, samples)
    )
    return valid_counts and fooled <= samples


def _format_table(rows):
    values = [HEADERS, *rows]
    widths = [
        max(len(str(row[index])) for row in values) for index in range(len(HEADERS))
    ]

    def render(row):
        return " | ".join(str(value).ljust(width) for value, width in zip(row, widths))

    return "\n".join(
        (
            render(HEADERS),
            "-+-".join("-" * width for width in widths),
            *(render(row) for row in rows),
        )
    )


def _counts(data):
    fool = (data or {}).get("fool_aggregate") or {}
    return fool.get("fooled_count", 0), fool.get("total_samples", 0)


def _sum_counts(results):
    fooled = samples = 0
    for data in results:
        result_fooled, result_samples = _counts(data)
        fooled += result_fooled
        samples += result_samples
    return fooled, samples


def _result_row(hotkey, results, chain_metrics=None):
    image_fooled, image_samples = _counts(results.get("image"))
    video_fooled, video_samples = _counts(results.get("video"))
    combined_fooled = image_fooled + video_fooled
    combined_samples = image_samples + video_samples
    incentive, alpha_day, tao_day = chain_metrics or (None, None, None)
    return (
        hotkey,
        image_samples if "image" in results else "—",
        _percent(image_fooled / image_samples if image_samples else None)
        if "image" in results
        else "—",
        video_samples if "video" in results else "—",
        _percent(video_fooled / video_samples if video_samples else None)
        if "video" in results
        else "—",
        combined_samples,
        _percent(combined_fooled / combined_samples if combined_samples else None),
        _decimal(incentive, 6),
        _decimal(alpha_day, 4),
        _decimal(tao_day, 6),
    )


def _total_row(results, chain_metrics):
    image_fooled, image_samples = _sum_counts(results["image"])
    video_fooled, video_samples = _sum_counts(results["video"])
    combined_fooled = image_fooled + video_fooled
    combined_samples = image_samples + video_samples
    metric_totals = tuple(
        sum(metrics[index] for metrics in chain_metrics.values())
        if chain_metrics
        else None
        for index in range(3)
    )
    return (
        "TOTAL",
        image_samples,
        _percent(image_fooled / image_samples if image_samples else None),
        video_samples,
        _percent(video_fooled / video_samples if video_samples else None),
        combined_samples,
        _percent(combined_fooled / combined_samples if combined_samples else None),
        _decimal(metric_totals[0], 6),
        _decimal(metric_totals[1], 4),
        _decimal(metric_totals[2], 6),
    )


def _chain_metrics(args):
    metagraph = None
    setup_error = None
    close_error = None
    try:
        import certifi

        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        import bittensor as bt

        metagraph = bt.Metagraph(
            netuid=args.netuid,
            network=args.chain_endpoint,
            lite=True,
            sync=True,
        )
        alpha_price = float(metagraph.pool.tao_in) / float(metagraph.pool.alpha_in)
        blocks_per_day = 86_400 / float(bt.BLOCKTIME)
        hotkey_uids = {address: uid for uid, address in enumerate(metagraph.hotkeys)}
    except Exception as error:
        setup_error = error
    finally:
        if metagraph is not None and metagraph.subtensor is not None:
            try:
                metagraph.subtensor.close()
            except Exception as error:
                close_error = error

    if setup_error is not None:
        message = str(setup_error)
        if close_error is not None:
            message += f"; connection cleanup failed: {close_error}"
        return {}, {hotkey: message for hotkey in args.hotkeys}

    metrics = {}
    errors = {}
    for hotkey in args.hotkeys:
        try:
            address = bt.Wallet(
                name=args.wallet_name,
                hotkey=hotkey,
            ).hotkey.ss58_address
            uid = hotkey_uids.get(address)
            if uid is None:
                raise ValueError(f"not registered on subnet {args.netuid}")
            incentive = float(metagraph.I[uid])
            alpha_day = (
                float(metagraph.E[uid]) / (float(metagraph.tempo) + 1) * blocks_per_day
            )
            metrics[hotkey] = (incentive, alpha_day, alpha_day * alpha_price)
        except Exception as error:
            errors[hotkey] = str(error)
    if close_error is not None:
        message = f"connection cleanup failed: {close_error}"
        for hotkey in args.hotkeys:
            errors[hotkey] = "; ".join(filter(None, (errors.get(hotkey), message)))
    return metrics, errors


def _query_hotkey(gascli, args, hotkey, modality):
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
        "--modality",
        modality,
    ]
    if args.api_url:
        command.extend(("--api-url", args.api_url))
    command.append("--json")
    try:
        completed = subprocess.run(command, capture_output=True, text=True)
    except OSError as error:
        return hotkey, modality, None, f"unable to start gascli: {error}"
    if completed.returncode:
        error_lines = (completed.stderr or completed.stdout).strip().splitlines()
        message = (
            error_lines[-1]
            if error_lines
            else f"gascli exited with status {completed.returncode}"
        )
        return hotkey, modality, None, message
    try:
        data = _extract_json(completed.stdout)
    except ValueError:
        return hotkey, modality, None, "invalid JSON"
    if not _valid_result(data):
        return hotkey, modality, None, "invalid JSON result"
    return hotkey, modality, data, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wallet-name", required=True, help="Bittensor wallet name")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--api-url")
    parser.add_argument("--netuid", type=int, default=34)
    parser.add_argument("--chain-endpoint", default="finney")
    parser.add_argument("hotkeys", nargs="+", help="Wallet hotkey names")
    args = parser.parse_args()

    gascli = shutil.which("gascli")
    if gascli is None:
        print(
            "error: gascli not found; activate the project virtualenv", file=sys.stderr
        )
        return 2
    results = {modality: [] for modality in MODALITIES}
    by_hotkey = {hotkey: {} for hotkey in args.hotkeys}
    errors = {hotkey: {} for hotkey in args.hotkeys}
    failures = 0
    for hotkey in args.hotkeys:
        with ThreadPoolExecutor(max_workers=len(MODALITIES)) as executor:
            queries = [
                executor.submit(_query_hotkey, gascli, args, hotkey, modality)
                for modality in MODALITIES
            ]
        for query in queries:
            _, modality, data, error = query.result()
            if error:
                errors[hotkey][modality] = error
                failures += 1
            else:
                results[modality].append(data)
                by_hotkey[hotkey][modality] = data

    chain_metrics, chain_errors = _chain_metrics(args)
    failures += len(chain_errors)
    for hotkey in args.hotkeys:
        messages = [
            f"{modality}: {message}" for modality, message in errors[hotkey].items()
        ]
        if hotkey in chain_errors:
            messages.append(f"chain: {chain_errors[hotkey]}")
        if messages:
            print(f"warning: {hotkey}: {'; '.join(messages)}", file=sys.stderr)
    rows = [
        _result_row(
            hotkey,
            by_hotkey[hotkey],
            chain_metrics.get(hotkey),
        )
        for hotkey in args.hotkeys
    ]
    rows.append(_total_row(results, chain_metrics))
    print(_format_table(rows))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
