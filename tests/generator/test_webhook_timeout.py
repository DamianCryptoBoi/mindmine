import argparse
from types import SimpleNamespace

from gas.config import add_miner_args
from gas.protocol import webhooks


class ImmediateThread:
    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


def test_default_webhook_timeout_is_thirty_seconds(monkeypatch):
    parser = argparse.ArgumentParser()
    add_miner_args(parser)

    calls = []
    monkeypatch.setattr(webhooks.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(webhooks, "_send_webhook", lambda *args: calls.append(args))

    task = SimpleNamespace(task_id="task-1")
    webhooks.send_success_webhook(task, {"data": b"image"}, None, "127.0.0.1", 8093)
    webhooks.send_failure_webhook(task, None, "127.0.0.1", 8093)

    assert getattr(parser.parse_args([]), "miner.webhook_timeout") == 30.0
    assert [call[-1] for call in calls] == [30.0, 30.0]
