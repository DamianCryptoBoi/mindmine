import json
import sys
import types

import pytest
import requests

from neurons.generator.services.maxcheapai_service import (
    CHECKPOINT_KIND_MAXCHEAPAI,
    MaxCheapAIService,
)
from neurons.generator.task_manager import GenerationTask, TaskStatus


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("response is not JSON")
        return self._payload


def make_task(modality, parameters=None, checkpoint=None):
    return GenerationTask(
        task_id="task-123",
        modality=modality,
        status=TaskStatus.PROCESSING,
        prompt="A red fox running through fresh snow",
        parameters=parameters or {},
        webhook_url="https://validator.example/callback",
        signed_by="validator-hotkey",
        created_at=1_700_000_000.0,
        checkpoint=checkpoint,
    )


def configure_service(monkeypatch):
    monkeypatch.setenv("MAXCHEAPAI_API_KEY", "mcai_test_key")
    monkeypatch.setenv("MAXCHEAPAI_SPEED", "slow")
    monkeypatch.setenv("MAXCHEAPAI_POLL_INTERVAL", "0")
    monkeypatch.setenv("MAXCHEAPAI_RETRY_DELAY", "0")
    monkeypatch.delenv("MAXCHEAPAI_MAX_RETRIES", raising=False)
    return MaxCheapAIService()


def test_defaults_to_priority_speed(monkeypatch):
    monkeypatch.setenv("MAXCHEAPAI_API_KEY", "mcai_test_key")
    monkeypatch.delenv("MAXCHEAPAI_SPEED", raising=False)

    service = MaxCheapAIService()

    assert service.speed == "priority"


def test_image_request_honors_resolution_and_returns_untouched_bytes(monkeypatch):
    service = configure_service(monkeypatch)
    original_media = b"\xff\xd8\xff\xe1provider-c2pa-image-bytes\xff\xd9"
    submitted = []
    checkpoints = []

    def post(url, headers, json, timeout):
        submitted.append((url, headers, json, timeout))
        return FakeResponse(
            payload={
                "requestId": 42,
                "hpCost": 8,
                "hpBalance": {"freeHp": 92, "paidHp": 50, "totalHp": 142},
            }
        )

    def get(url, headers=None, timeout=None):
        if url.endswith("/generations/42"):
            return FakeResponse(
                payload={
                    "id": 42,
                    "modelId": "nano-banana-pro",
                    "prompt": "A red fox running through fresh snow",
                    "aspectRatio": "16:9",
                    "resolution": "4K",
                    "imageCount": 1,
                    "speed": "slow",
                    "autoEnhance": False,
                    "hpCost": 8,
                    "status": "success",
                    "resultImages": [
                        {
                            "url": "https://media.example/image.jpg",
                            "thumbnailUrl": "https://media.example/image-thumb.jpg",
                        }
                    ],
                    "errorMessage": None,
                    "isFavorite": False,
                    "createdAt": "2026-03-10T03:00:00.000Z",
                }
            )
        assert url == "https://media.example/image.jpg"
        return FakeResponse(
            content=original_media,
            headers={"Content-Type": "image/jpeg"},
        )

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get", get
    )

    result = service.process_with_checkpoint(
        make_task("image", {"resolution": "4K", "aspect_ratio": "16:9"}),
        checkpoints.append,
    )

    assert result["data"] == original_media
    assert result["metadata"]["model"] == "nano-banana-pro"
    assert result["metadata"]["provider"] == "maxcheapai"
    assert submitted == [
        (
            "https://maxcheapai.com/api/generate/image",
            {
                "Authorization": "Bearer mcai_test_key",
                "Content-Type": "application/json",
            },
            {
                "modelId": "nano-banana-pro",
                "prompt": "A red fox running through fresh snow",
                "resolution": "4K",
                "speed": "slow",
                "aspectRatio": "16:9",
                "imageCount": 1,
                "autoEnhance": False,
            },
            60.0,
        )
    ]
    assert checkpoints == [
        {
            "kind": CHECKPOINT_KIND_MAXCHEAPAI,
            "request_id": 42,
            "modality": "image",
            "model": "nano-banana-pro",
            "retries_used": 0,
        },
        None,
    ]


def test_video_request_maps_sn34_tier_enables_audio_and_polls(monkeypatch):
    service = configure_service(monkeypatch)
    original_media = b"\x00\x00\x00\x18ftypisom-provider-c2pa-video"
    submitted = []
    checkpoints = []
    status_calls = 0

    def post(url, headers, json, timeout):
        submitted.append((url, json))
        return FakeResponse(
            payload={
                "requestId": 18,
                "hpCost": 56,
                "hpBalance": {"freeHp": 44, "paidHp": 50, "totalHp": 94},
            }
        )

    def get(url, headers=None, timeout=None):
        nonlocal status_calls
        if url.endswith("/video-generations/18"):
            status_calls += 1
            status = "processing" if status_calls == 1 else "success"
            return FakeResponse(
                payload={
                    "id": 18,
                    "modelId": "veo-3.1",
                    "prompt": "A red fox running through fresh snow",
                    "aspectRatio": "9:16",
                    "resolution": "720p",
                    "duration": 4,
                    "speed": "slow",
                    "generateAudio": True,
                    "hpCost": 56,
                    "status": status,
                    "resultVideoUrl": (
                        "https://media.example/video.mp4"
                        if status == "success"
                        else None
                    ),
                    "thumbnailUrl": None,
                    "errorMessage": None,
                    "isFavorite": False,
                    "createdAt": "2026-03-10T03:00:00.000Z",
                }
            )
        assert url == "https://media.example/video.mp4"
        return FakeResponse(
            content=original_media,
            headers={"Content-Type": "video/mp4"},
        )

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get", get
    )

    result = service.process_with_checkpoint(
        make_task(
            "video",
            {
                "resolution": "480p",
                "duration": 5,
                "aspect_ratio": "9:16",
            },
        ),
        checkpoints.append,
    )

    assert result["data"] == original_media
    assert result["metadata"]["mime_type"] == "video/mp4"
    assert status_calls == 2
    assert submitted == [
        (
            "https://maxcheapai.com/api/generate/video",
            {
                "modelId": "veo-3.1",
                "prompt": "A red fox running through fresh snow",
                "resolution": "720p",
                "duration": 4,
                "speed": "slow",
                "aspectRatio": "9:16",
                "generateAudio": True,
            },
        )
    ]
    assert checkpoints[-1] is None


def test_checkpoint_resume_does_not_submit_duplicate_generation(monkeypatch):
    service = configure_service(monkeypatch)
    post_called = False
    checkpoints = []

    def post(*args, **kwargs):
        nonlocal post_called
        post_called = True
        raise AssertionError("resume must not submit another provider job")

    def get(url, headers=None, timeout=None):
        if url.endswith("/video-generations/99"):
            return FakeResponse(
                payload={
                    "id": 99,
                    "modelId": "veo-3.1",
                    "prompt": "A red fox running through fresh snow",
                    "aspectRatio": "16:9",
                    "resolution": "1080p",
                    "duration": 4,
                    "speed": "slow",
                    "generateAudio": True,
                    "hpCost": 56,
                    "status": "success",
                    "resultVideoUrl": "https://media.example/resumed.mp4",
                    "thumbnailUrl": None,
                    "errorMessage": None,
                    "isFavorite": False,
                    "createdAt": "2026-03-10T03:00:00.000Z",
                }
            )
        assert url == "https://media.example/resumed.mp4"
        return FakeResponse(content=b"resumed-provider-bytes")

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get", get
    )

    result = service.process_with_checkpoint(
        make_task(
            "video",
            checkpoint={
                "kind": CHECKPOINT_KIND_MAXCHEAPAI,
                "request_id": 99,
                "modality": "video",
                "model": "veo-3.1",
            },
        ),
        checkpoints.append,
    )

    assert post_called is False
    assert result["data"] == b"resumed-provider-bytes"
    assert result["metadata"]["resumed"] is True
    assert checkpoints == [None]


def test_provider_failure_clears_checkpoint_and_reports_reason(monkeypatch):
    service = configure_service(monkeypatch)
    checkpoints = []

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post",
        lambda *args, **kwargs: FakeResponse(payload={"requestId": 51}),
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "id": 51,
                "modelId": "nano-banana-pro",
                "prompt": "A red fox running through fresh snow",
                "aspectRatio": "1:1",
                "resolution": "1K",
                "imageCount": 1,
                "speed": "slow",
                "autoEnhance": False,
                "hpCost": 5,
                "status": "failed",
                "resultImages": [],
                "errorMessage": "provider safety rejection",
                "isFavorite": False,
                "createdAt": "2026-03-10T03:00:00.000Z",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="provider safety rejection"):
        service.process_with_checkpoint(make_task("image"), checkpoints.append)

    assert checkpoints[0]["request_id"] == 51
    assert checkpoints[-1] is None


def test_provider_failure_is_retried_three_times_before_success(monkeypatch):
    service = configure_service(monkeypatch)
    request_ids = iter([61, 62, 63, 64])
    submitted = []
    checkpoints = []

    def post(*args, **kwargs):
        request_id = next(request_ids)
        submitted.append(request_id)
        return FakeResponse(
            payload={
                "requestId": request_id,
                "hpCost": 5,
                "hpBalance": {"freeHp": 95, "paidHp": 50, "totalHp": 145},
            }
        )

    def get(url, headers=None, timeout=None):
        if url == "https://media.example/retry-image.jpg":
            return FakeResponse(
                content=b"provider-media-after-three-retries",
                headers={"Content-Type": "image/jpeg"},
            )

        request_id = int(url.rsplit("/", 1)[-1])
        succeeded = request_id == 64
        return FakeResponse(
            payload={
                "id": request_id,
                "modelId": "nano-banana-pro",
                "prompt": "A red fox running through fresh snow",
                "aspectRatio": "1:1",
                "resolution": "1K",
                "imageCount": 1,
                "speed": "slow",
                "autoEnhance": False,
                "hpCost": 5,
                "status": "success" if succeeded else "failed",
                "resultImages": (
                    [{"url": "https://media.example/retry-image.jpg"}]
                    if succeeded
                    else []
                ),
                "errorMessage": None if succeeded else "provider generation failed",
                "isFavorite": False,
                "createdAt": "2026-03-10T03:00:00.000Z",
            }
        )

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get", get
    )

    result = service.process_with_checkpoint(make_task("image"), checkpoints.append)

    assert result["data"] == b"provider-media-after-three-retries"
    assert result["metadata"]["request_id"] == 64
    assert submitted == [61, 62, 63, 64]
    assert [
        (
            checkpoint.get("request_id"),
            checkpoint.get("retry_pending", False),
            checkpoint.get("retries_used"),
        )
        if checkpoint
        else None
        for checkpoint in checkpoints
    ] == [
        (61, False, 0),
        (None, True, 1),
        (62, False, 1),
        (None, True, 2),
        (63, False, 2),
        (None, True, 3),
        (64, False, 3),
        None,
    ]


def test_polling_network_errors_retry_same_provider_request(monkeypatch):
    service = configure_service(monkeypatch)
    submissions = 0
    status_attempts = 0

    def post(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        return FakeResponse(payload={"requestId": 71})

    def get(url, headers=None, timeout=None):
        nonlocal status_attempts
        if url.endswith("/generations/71"):
            status_attempts += 1
            if status_attempts <= 3:
                raise requests.ConnectionError("temporary provider network failure")
            return FakeResponse(
                payload={
                    "id": 71,
                    "modelId": "nano-banana-pro",
                    "prompt": "A red fox running through fresh snow",
                    "aspectRatio": "1:1",
                    "resolution": "1K",
                    "imageCount": 1,
                    "speed": "slow",
                    "autoEnhance": False,
                    "hpCost": 5,
                    "status": "success",
                    "resultImages": [{"url": "https://media.example/network.jpg"}],
                    "errorMessage": None,
                    "isFavorite": False,
                    "createdAt": "2026-03-10T03:00:00.000Z",
                }
            )
        assert url == "https://media.example/network.jpg"
        return FakeResponse(content=b"same-request-result")

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get", get
    )

    result = service.process_with_checkpoint(make_task("image"))

    assert result["data"] == b"same-request-result"
    assert submissions == 1
    assert status_attempts == 4


def test_retry_pending_checkpoint_resumes_only_remaining_attempt(monkeypatch):
    service = configure_service(monkeypatch)
    submitted = []
    checkpoints = []

    def post(*args, **kwargs):
        submitted.append(kwargs["json"])
        return FakeResponse(payload={"requestId": 75})

    def get(url, headers=None, timeout=None):
        if url.endswith("/generations/75"):
            return FakeResponse(
                payload={
                    "id": 75,
                    "modelId": "nano-banana-pro",
                    "status": "success",
                    "resultImages": [{"url": "https://media.example/restarted.jpg"}],
                    "errorMessage": None,
                }
            )
        assert url == "https://media.example/restarted.jpg"
        return FakeResponse(content=b"result-after-restart")

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get", get
    )

    result = service.process_with_checkpoint(
        make_task(
            "image",
            checkpoint={
                "kind": CHECKPOINT_KIND_MAXCHEAPAI,
                "modality": "image",
                "model": "nano-banana-pro",
                "retry_pending": True,
                "retries_used": 2,
            },
        ),
        checkpoints.append,
    )

    assert result["data"] == b"result-after-restart"
    assert len(submitted) == 1
    assert checkpoints[0]["request_id"] == 75
    assert checkpoints[0]["retries_used"] == 2


def test_restored_final_attempt_failure_does_not_reset_retry_budget(monkeypatch):
    service = configure_service(monkeypatch)
    checkpoints = []
    submitted = []

    def post(*args, **kwargs):
        submitted.append(kwargs["json"])
        return FakeResponse(payload={"requestId": 77 + len(submitted)})

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post",
        post,
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "id": 76,
                "modelId": "nano-banana-pro",
                "status": "failed",
                "resultImages": [],
                "errorMessage": "final attempt failed",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="final attempt failed"):
        service.process_with_checkpoint(
            make_task(
                "image",
                checkpoint={
                    "kind": CHECKPOINT_KIND_MAXCHEAPAI,
                    "request_id": 76,
                    "modality": "image",
                    "model": "nano-banana-pro",
                    "retries_used": 3,
                },
            ),
            checkpoints.append,
        )

    assert checkpoints == [None]
    assert submitted == []


def test_http_error_is_not_mistaken_for_a_submitted_job(monkeypatch):
    service = configure_service(monkeypatch)
    checkpoints = []
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post",
        lambda *args, **kwargs: FakeResponse(
            status_code=429,
            payload={"error": "Too many requests"},
        ),
    )

    with pytest.raises(RuntimeError, match="429.*Too many requests"):
        service.process_with_checkpoint(make_task("video"), checkpoints.append)

    assert checkpoints == []


def test_success_without_media_url_fails_and_clears_checkpoint(monkeypatch):
    service = configure_service(monkeypatch)
    checkpoints = []
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "id": 77,
                "modelId": "nano-banana-pro",
                "status": "success",
                "resultImages": [],
                "errorMessage": None,
            }
        ),
    )

    task = make_task(
        "image",
        checkpoint={
            "kind": CHECKPOINT_KIND_MAXCHEAPAI,
            "request_id": 77,
            "modality": "image",
            "model": "nano-banana-pro",
        },
    )
    with pytest.raises(RuntimeError, match="omitted the media URL"):
        service.process_with_checkpoint(task, checkpoints.append)

    assert checkpoints == [None]


def test_polling_timeout_fails_without_submitting_a_second_job(monkeypatch):
    service = configure_service(monkeypatch)
    checkpoints = []
    monotonic_values = iter([0.0, 0.0, 1.0])

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post",
        lambda *args, **kwargs: FakeResponse(payload={"requestId": 88}),
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "id": 88,
                "modelId": "veo-3.1",
                "status": "processing",
                "resultVideoUrl": None,
                "errorMessage": None,
            }
        ),
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.time.monotonic",
        lambda: next(monotonic_values),
    )

    with pytest.raises(TimeoutError, match="exceeded 0.5s polling timeout"):
        service.process_with_checkpoint(
            make_task("video", {"maxcheapai_poll_timeout": 0.5}),
            checkpoints.append,
        )

    assert checkpoints[0]["request_id"] == 88
    assert checkpoints[-1] is None


def test_default_generation_deadline_is_1800_seconds(monkeypatch):
    service = configure_service(monkeypatch)
    submitted = []
    monotonic_values = iter([0.0, 0.0, 1801.0, 5401.0])

    def post(*args, **kwargs):
        submitted.append(kwargs["json"])
        return FakeResponse(payload={"requestId": 89})

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "id": 89,
                "modelId": "veo-3.1",
                "status": "processing",
                "resultVideoUrl": None,
                "errorMessage": None,
            }
        ),
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.time.monotonic",
        lambda: next(monotonic_values),
    )

    with pytest.raises(TimeoutError, match="exceeded 1800s polling timeout"):
        service.process_with_checkpoint(make_task("video"))

    assert len(submitted) == 1


def test_failed_status_after_deadline_does_not_submit_retry(monkeypatch):
    service = configure_service(monkeypatch)
    submissions = 0
    monotonic_values = iter([0.0, 0.0, 0.0, 1.0])

    def post(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        return FakeResponse(payload={"requestId": 90 + submissions})

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "id": 91,
                "modelId": "veo-3.1",
                "status": "failed",
                "resultVideoUrl": None,
                "errorMessage": "late provider failure",
            }
        ),
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.time.monotonic",
        lambda: next(monotonic_values, 1.0),
    )

    with pytest.raises(TimeoutError, match="exceeded 0.5s polling timeout"):
        service.process_with_checkpoint(
            make_task("video", {"maxcheapai_poll_timeout": 0.5})
        )

    assert submissions == 1


def test_retry_configuration_cannot_exceed_three_resubmissions(monkeypatch):
    monkeypatch.setenv("MAXCHEAPAI_API_KEY", "mcai_test_key")
    monkeypatch.setenv("MAXCHEAPAI_POLL_INTERVAL", "0")
    monkeypatch.setenv("MAXCHEAPAI_RETRY_DELAY", "0")
    monkeypatch.setenv("MAXCHEAPAI_MAX_RETRIES", "99")
    service = MaxCheapAIService()
    submissions = 0

    def post(*args, **kwargs):
        nonlocal submissions
        submissions += 1
        return FakeResponse(payload={"requestId": submissions})

    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.post", post
    )
    monkeypatch.setattr(
        "neurons.generator.services.maxcheapai_service.requests.get",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "id": submissions,
                "modelId": "nano-banana-pro",
                "status": "failed",
                "resultImages": [],
                "errorMessage": "provider generation failed",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="provider generation failed"):
        service.process_with_checkpoint(make_task("image"))

    assert submissions == 4


def test_registry_reuses_one_maxcheapai_service_for_both_modalities(monkeypatch):
    # The local test environment omits the optional Stability service's C2PA
    # runtime. Registry construction does not exercise that module.
    monkeypatch.setitem(sys.modules, "c2pa", types.ModuleType("c2pa"))
    from neurons.generator.services.service_registry import ServiceRegistry

    monkeypatch.setenv("MAXCHEAPAI_API_KEY", "mcai_test_key")
    monkeypatch.setenv("IMAGE_SERVICE", "maxcheapai")
    monkeypatch.setenv("VIDEO_SERVICE", "maxcheapai")

    registry = ServiceRegistry()

    assert isinstance(registry.get_service("image"), MaxCheapAIService)
    assert registry.get_service("video") is registry.get_service("image")
