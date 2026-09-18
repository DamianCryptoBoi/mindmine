import argparse
from concurrent.futures import ThreadPoolExecutor

import pytest

from gas.config import add_miner_args
from neurons.generator.task_manager import TaskManager, TaskStatus


def test_pending_tasks_are_claimed_once_across_concurrent_workers():
    manager = TaskManager()
    task_ids = {
        manager.create_task(
            modality="image",
            prompt=f"prompt {index}",
            parameters={},
            webhook_url="https://validator.example/callback",
            signed_by="validator-hotkey",
        )
        for index in range(12)
    }

    with ThreadPoolExecutor(max_workers=12) as executor:
        claimed = list(
            executor.map(lambda _: manager.claim_next_pending_task(), range(12))
        )

    claimed_ids = {task.task_id for task in claimed if task is not None}
    assert claimed_ids == task_ids
    assert all(task.status == TaskStatus.PROCESSING for task in claimed)
    assert all(task.started_at is not None for task in claimed)
    assert manager.claim_next_pending_task() is None


def test_worker_thread_count_is_a_supported_miner_argument():
    parser = argparse.ArgumentParser()
    add_miner_args(parser)

    config = parser.parse_args(["--miner.worker-threads", "7"])

    assert getattr(config, "miner.worker_threads") == 7


def test_worker_thread_count_defaults_from_environment(monkeypatch):
    monkeypatch.setenv("MINER_WORKER_THREADS", "7")
    parser = argparse.ArgumentParser()
    add_miner_args(parser)

    config = parser.parse_args([])

    assert getattr(config, "miner.worker_threads") == 7


def test_failed_claim_persistence_returns_task_to_pending():
    manager = TaskManager()
    task_id = manager.create_task(
        modality="video",
        prompt="a test prompt",
        parameters={},
        webhook_url="https://validator.example/callback",
        signed_by="validator-hotkey",
    )

    def persistence_failure(_task):
        raise OSError("state directory is unavailable")

    with pytest.raises(OSError, match="state directory is unavailable"):
        manager.claim_next_pending_task(persistence_failure)

    task = manager.get_task(task_id)
    assert task.status == TaskStatus.PENDING
    assert task.started_at is None
    assert manager.claim_next_pending_task().task_id == task_id
