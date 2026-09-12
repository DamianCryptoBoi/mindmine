import base64
import json

import pytest

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


def make_task(parameters=None, checkpoint=None):
    return GenerationTask(
        task_id="task-gpti2-123",
        modality="image",
        status=TaskStatus.PROCESSING,
        prompt="A red fox running through fresh snow",
        parameters=parameters or {},
        webhook_url="https://validator.example/callback",
        signed_by="validator-hotkey",
        created_at=1_700_000_000.0,
        checkpoint=checkpoint,
    )


def configure_service(monkeypatch, model=None):
    from neurons.generator.services.gpti2_service import GPTi2Service

    monkeypatch.setenv("GPTI2_API_KEY", "sk-gpti2-test")
    monkeypatch.setenv("GPTI2_POLL_INTERVAL", "0")
    if model is None:
        monkeypatch.delenv("GPTI2_MODEL", raising=False)
    else:
        monkeypatch.setenv("GPTI2_MODEL", model)
    return GPTi2Service()


def test_sync_generation_maps_sn34_request_and_decodes_original_bytes(monkeypatch):
    service = configure_service(monkeypatch)
    media = b"\x89PNG\r\n\x1a\nopenai-c2pa-image"
    calls = []

    def post(url, headers, json, timeout):
        calls.append((url, headers, json, timeout))
        return FakeResponse(
            payload={
                "created": 1_789_000_000,
                "data": [{"b64_json": base64.b64encode(media).decode()}],
                "usage": {"input_tokens": 12, "output_tokens": 34},
                "size": "2400x1600",
                "quality": "medium",
            }
        )

    monkeypatch.setattr("requests.post", post)

    result = service.process(
        make_task(
            {
                "resolution": "2K",
                "aspect_ratio": "3:2",
                "quality": "medium",
            }
        )
    )

    assert result["data"] == media
    assert result["metadata"] == {
        "model": "gpt-image-2.5-flare",
        "provider": "gpti2",
        "mime_type": "image/png",
        "size": "2400x1600",
        "quality": "medium",
    }
    assert calls == [
        (
            "https://gpti2.store/v1/images/generations",
            {
                "Authorization": "Bearer sk-gpti2-test",
                "Content-Type": "application/json",
                "Idempotency-Key": "task-gpti2-123",
            },
            {
                "model": "gpt-image-2.5-flare",
                "prompt": "A red fox running through fresh snow",
                "size": "2400x1600",
                "quality": "medium",
                "background": "opaque",
                "n": 1,
            },
            1800.0,
        )
    ]


def test_model_env_overrides_default_in_request_and_metadata(monkeypatch):
    service = configure_service(monkeypatch, model="gpt-image-custom")
    submitted = []

    monkeypatch.setattr(
        "requests.post",
        lambda url, headers, json, timeout: submitted.append(json)
        or FakeResponse(
            payload={
                "data": [{"b64_json": base64.b64encode(b"png").decode()}],
                "size": "1024x1024",
                "quality": "low",
            }
        ),
    )

    result = service.process(make_task())

    assert submitted[0]["model"] == "gpt-image-custom"
    assert result["metadata"]["model"] == "gpt-image-custom"


@pytest.mark.parametrize(
    ("resolution", "aspect_ratio", "expected_size"),
    [
        ("1K", "1:1", "1024x1024"),
        ("2K", "1:1", "1536x1536"),
        ("4K", "1:1", "2048x2048"),
        ("1K", "16:9", "1280x720"),
        ("2K", "16:9", "2560x1440"),
        ("4K", "16:9", "3840x2160"),
        ("1K", "9:16", "720x1280"),
        ("2K", "9:16", "1440x2560"),
        ("4K", "9:16", "2160x3840"),
        ("1K", "4:3", "1024x768"),
        ("2K", "4:3", "2048x1536"),
        ("4K", "4:3", "3200x2400"),
        ("1K", "3:4", "768x1024"),
        ("2K", "3:4", "1536x2048"),
        ("4K", "3:4", "2400x3200"),
        ("1K", "3:2", "1536x1024"),
        ("2K", "3:2", "2400x1600"),
        ("4K", "3:2", "3360x2240"),
        ("1K", "2:3", "1024x1536"),
        ("2K", "2:3", "1600x2400"),
        ("4K", "2:3", "2240x3360"),
        ("1K", "21:9", "1280x544"),
        ("2K", "21:9", "2560x1088"),
        ("4K", "21:9", "3840x1632"),
    ],
)
def test_maps_each_sn34_resolution_and_aspect_ratio(
    monkeypatch, resolution, aspect_ratio, expected_size
):
    service = configure_service(monkeypatch)
    submitted = []

    def post(url, headers, json, timeout):
        submitted.append(json)
        return FakeResponse(
            payload={
                "data": [{"b64_json": base64.b64encode(b"png").decode()}],
                "size": expected_size,
                "quality": "low",
            }
        )

    monkeypatch.setattr("requests.post", post)

    service.process(make_task({"resolution": resolution, "aspect_ratio": aspect_ratio}))

    assert submitted[0]["size"] == expected_size


def test_high_quality_uses_checkpointed_job_and_downloads_untouched_bytes(monkeypatch):
    service = configure_service(monkeypatch)
    media = b"\xff\xd8\xff\xe1openai-c2pa-image\xff\xd9"
    calls = []
    checkpoints = []

    def post(url, headers, json, timeout):
        calls.append(("post", url, headers, json, timeout))
        return FakeResponse(
            status_code=202,
            payload={
                "id": "job_abc123",
                "status": "queued",
                "price_vnd": 3180,
            },
        )

    def get(url, headers, timeout):
        calls.append(("get", url, headers, timeout))
        if url.endswith("/job_abc123"):
            return FakeResponse(
                payload={
                    "id": "job_abc123",
                    "status": "succeeded",
                    "data": [{"url": "https://gpti2.store/files/job_abc123.jpg"}],
                    "size": "3840x2160",
                    "quality": "high",
                }
            )
        return FakeResponse(content=media, headers={"Content-Type": "image/jpeg"})

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)

    result = service.process_with_checkpoint(
        make_task({"resolution": "4K", "aspectRatio": "16:9", "quality": "high"}),
        checkpoints.append,
    )

    assert result["data"] == media
    assert result["metadata"]["job_id"] == "job_abc123"
    assert result["metadata"]["mime_type"] == "image/jpeg"
    assert calls[0][1] == "https://gpti2.store/v1/images/jobs"
    assert calls[0][2]["Idempotency-Key"] == "task-gpti2-123"
    assert calls[0][3]["size"] == "3840x2160"
    assert checkpoints == [
        {
            "kind": "gpti2_generation",
            "job_id": "job_abc123",
            "size": "3840x2160",
            "quality": "high",
        },
        None,
    ]


def test_high_quality_resume_polls_existing_job_without_resubmitting(monkeypatch):
    service = configure_service(monkeypatch)

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resume must not submit another GPTi2 job")
        ),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda url, headers, timeout: (
            FakeResponse(content=b"resumed-c2pa", headers={"Content-Type": "image/png"})
            if url.endswith(".png")
            else FakeResponse(
                payload={
                    "id": "job_resume",
                    "status": "succeeded",
                    "data": [{"url": "https://gpti2.store/files/job_resume.png"}],
                    "size": "2048x2048",
                    "quality": "high",
                }
            )
        ),
    )

    result = service.process_with_checkpoint(
        make_task(
            checkpoint={
                "kind": "gpti2_generation",
                "job_id": "job_resume",
                "size": "2048x2048",
                "quality": "high",
            }
        )
    )

    assert result["data"] == b"resumed-c2pa"
    assert result["metadata"]["resumed"] is True


def test_defaults_invalid_sn34_parameters_to_low_square_1k(monkeypatch):
    service = configure_service(monkeypatch)
    submitted = []

    monkeypatch.setattr(
        "requests.post",
        lambda url, headers, json, timeout: submitted.append(json)
        or FakeResponse(
            payload={
                "data": [{"b64_json": base64.b64encode(b"png").decode()}],
                "size": "1024x1024",
                "quality": "low",
            }
        ),
    )

    service.process(
        make_task({"resolution": "8K", "aspect_ratio": "10:7", "quality": "ultra"})
    )

    assert submitted[0]["size"] == "1024x1024"
    assert submitted[0]["quality"] == "low"


def test_registry_selects_gpti2_for_image_generation(monkeypatch):
    from neurons.generator.services.gpti2_service import GPTi2Service
    from neurons.generator.services.service_registry import ServiceRegistry

    monkeypatch.setenv("GPTI2_API_KEY", "sk-gpti2-test")
    monkeypatch.setenv("IMAGE_SERVICE", "gpti2")
    monkeypatch.setenv("VIDEO_SERVICE", "none")

    registry = ServiceRegistry()

    assert isinstance(registry.get_service("image"), GPTi2Service)
    assert registry.get_service("video") is None


def test_rejects_malformed_base64_response(monkeypatch):
    service = configure_service(monkeypatch)
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: FakeResponse(payload={"data": [{"b64_json": "%%%"}]}),
    )

    with pytest.raises(RuntimeError, match="invalid image data"):
        service.process(make_task())
