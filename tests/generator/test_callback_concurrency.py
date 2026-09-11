import asyncio
import importlib.util
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace


class StubRequest:
    headers = {"content-type": "video/mp4", "task-id": "task-1"}
    client = SimpleNamespace(host="127.0.0.1")

    async def body(self):
        return b"video"


def load_challenge_manager(monkeypatch):
    modules = {
        "gas.cache": {},
        "gas.cache.content_manager": {"ContentManager": object},
        "gas.evaluation": {},
        "gas.evaluation.resolution_tiers": {
            "sample_challenge_tier": lambda _modality: "480p"
        },
        "gas.protocol": {},
        "gas.protocol.epistula": {"get_verifier": lambda *_args, **_kwargs: None},
        "gas.protocol.validator_requests": {"query_generative_miner": None},
        "gas.types": {"MediaType": object, "MinerType": object, "Modality": object},
        "gas.verification": {},
        "gas.verification.c2pa_verification": {"verify_c2pa": None},
        "gas.verification.duplicate_detection": {
            "compute_media_hash": None,
            "DEFAULT_HAMMING_THRESHOLD": 8,
        },
    }
    for name, attributes in modules.items():
        module = ModuleType(name)
        if not attributes:
            module.__path__ = []
        for attribute, value in attributes.items():
            setattr(module, attribute, value)
        monkeypatch.setitem(sys.modules, name, module)

    path = (
        Path(__file__).parents[2]
        / "gas"
        / "evaluation"
        / "generative_challenge_manager.py"
    )
    spec = importlib.util.spec_from_file_location("challenge_manager_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_manager(module):
    module.compute_media_hash = lambda *_args, **_kwargs: None
    module.verify_c2pa = lambda _data: SimpleNamespace(
        verified=True,
        is_trusted_issuer=True,
        issuer="test-issuer",
        model_name="test-model",
    )
    manager = object.__new__(module.GenerativeChallengeManager)
    manager.challenge_tasks = {
        "task-1": {
            "uid": 0,
            "modality": SimpleNamespace(value="video"),
            "media_type": object(),
            "prompt_id": "prompt-1",
        }
    }
    manager.challenge_lock = threading.Lock()
    manager.media_processing_lock = threading.Lock()
    manager.metagraph = SimpleNamespace(hotkeys=["miner-hotkey"])
    manager.generator_last_seen = {}
    manager.config = SimpleNamespace(store_failed_media=False)
    manager.content_manager = SimpleNamespace(
        check_duplicate=lambda *_args, **_kwargs: None,
        write_miner_media=lambda **_kwargs: "/tmp/video.mp4",
    )
    return manager


def test_callback_keeps_event_loop_responsive_during_media_validation(monkeypatch):
    module = load_challenge_manager(monkeypatch)

    async def run():
        manager = make_manager(module)

        release_storage = threading.Event()
        storage_started = threading.Event()
        events = []

        def blocking_corruption_check(*_args):
            events.append("storage-started")
            storage_started.set()
            release_storage.wait(timeout=1)
            events.append("storage-finished")
            return False

        manager._check_media_corrupted = blocking_corruption_check

        callback = asyncio.create_task(manager.generative_callback(StubRequest()))
        while not storage_started.is_set():
            await asyncio.sleep(0)

        def mark_event_loop_responsive():
            events.append("event-loop-responsive")
            release_storage.set()

        asyncio.get_running_loop().call_soon(mark_event_loop_responsive)
        response = await callback

        assert response.status_code == 200
        assert events.index("event-loop-responsive") < events.index("storage-finished")

    asyncio.run(run())


def test_duplicate_callback_does_not_store_the_same_task_twice(monkeypatch):
    module = load_challenge_manager(monkeypatch)

    async def run():
        manager = make_manager(module)
        release_storage = threading.Event()
        storage_started = threading.Event()
        storage_calls = 0

        def blocking_corruption_check(*_args):
            nonlocal storage_calls
            storage_calls += 1
            storage_started.set()
            release_storage.wait(timeout=1)
            return False

        manager._check_media_corrupted = blocking_corruption_check

        first = asyncio.create_task(manager.generative_callback(StubRequest()))
        while not storage_started.is_set():
            await asyncio.sleep(0)

        duplicate_response = await manager.generative_callback(StubRequest())
        release_storage.set()
        first_response = await first

        assert duplicate_response.status_code == 200
        assert first_response.status_code == 200
        assert storage_calls == 1

    asyncio.run(run())


def test_cancelled_callback_finishes_bookkeeping_for_claimed_task(monkeypatch):
    module = load_challenge_manager(monkeypatch)

    async def run():
        manager = make_manager(module)
        release_storage = threading.Event()
        storage_started = threading.Event()

        def blocking_corruption_check(*_args):
            storage_started.set()
            release_storage.wait(timeout=1)
            return False

        manager._check_media_corrupted = blocking_corruption_check

        callback = asyncio.create_task(manager.generative_callback(StubRequest()))
        while not storage_started.is_set():
            await asyncio.sleep(0)

        callback.cancel()
        release_storage.set()
        cancelled = False
        try:
            await callback
        except asyncio.CancelledError:
            cancelled = True

        assert cancelled
        assert "miner-hotkey" in manager.generator_last_seen

    asyncio.run(run())


def test_duplicate_admission_is_serialized_across_different_tasks(monkeypatch):
    module = load_challenge_manager(monkeypatch)
    manager = make_manager(module)
    manager._check_media_corrupted = lambda *_args: False
    module.compute_media_hash = lambda *_args, **_kwargs: "same-hash"

    stored = []
    first_checks = threading.Barrier(2)
    thread_state = threading.local()

    def check_duplicate(*_args, **_kwargs):
        if getattr(thread_state, "checked", False):
            return None
        thread_state.checked = True
        duplicate = bool(stored)
        try:
            first_checks.wait(timeout=0.2)
        except threading.BrokenBarrierError:
            pass
        return ("existing-media", 0) if duplicate else None

    def write_miner_media(**_kwargs):
        stored.append("media")
        return f"/tmp/video-{len(stored)}.mp4"

    manager.content_manager.check_duplicate = check_duplicate
    manager.content_manager.write_miner_media = write_miner_media

    def store(task_id):
        task_info = manager.challenge_tasks["task-1"].copy()
        return manager.store_binary_content(
            b"same-video", "video/mp4", 0, task_id, task_info
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(store, ["task-1", "task-2"]))

    assert len(stored) == 1
    assert sum(filepath is not None for filepath, _error in results) == 1
