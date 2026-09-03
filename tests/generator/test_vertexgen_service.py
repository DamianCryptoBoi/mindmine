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
        task_id="task-vertexgen-123",
        modality="image",
        status=TaskStatus.PROCESSING,
        prompt="A red fox running through fresh snow",
        parameters=parameters or {},
        webhook_url="https://validator.example/callback",
        signed_by="validator-hotkey",
        created_at=1_700_000_000.0,
        checkpoint=checkpoint,
    )


def test_async_generation_preserves_provider_bytes_and_checkpoints(monkeypatch):
    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    monkeypatch.setenv("VERTEXGEN_POLL_INTERVAL", "0")
    monkeypatch.setenv("VERTEXGEN_MAX_RETRIES", "0")
    service = VertexGenService()
    media = b"\x89PNG\r\n\x1a\nprovider-c2pa-bytes"
    calls = []

    def post(url, headers, params, json, timeout):
        calls.append(("post", url, headers, params, json, timeout))
        return FakeResponse(
            status_code=202,
            payload={
                "id": "gen_123",
                "status": "running",
                "model": "nano-banana-pro",
                "aspect_ratio": "3:2",
                "resolution": "2K",
            },
        )

    def get(url, headers, timeout):
        calls.append(("get", url, headers, timeout))
        if url.endswith("/content"):
            return FakeResponse(
                content=media,
                headers={"Content-Type": "image/png; charset=binary"},
            )
        return FakeResponse(
            payload={
                "id": "gen_123",
                "status": "succeeded",
                "model": "nano-banana-pro",
                "aspect_ratio": "3:2",
                "resolution": "2K",
                "image_url": "/v1/images/generations/gen_123/content",
            }
        )

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)
    checkpoints = []

    result = service.process_with_checkpoint(
        make_task({"resolution": "2K", "aspect_ratio": "3:2"}),
        checkpoints.append,
    )

    assert result["data"] == media
    assert result["metadata"]["model"] == "nano-banana-pro"
    assert result["metadata"]["provider"] == "vertexgen"
    assert result["metadata"]["mime_type"] == "image/png"
    assert result["metadata"]["job_id"] == "gen_123"
    assert calls == [
        (
            "post",
            "https://vertexgen.example/v1/images/generations",
            {
                "Authorization": "Bearer vertexgen_test_key",
                "Content-Type": "application/json",
                "Idempotency-Key": "task-vertexgen-123-0",
            },
            {"wait": 20},
            {
                "prompt": "A red fox running through fresh snow",
                "model": "nano-banana-pro",
                "resolution": "2K",
                "aspect_ratio": "3:2",
            },
            60.0,
        ),
        (
            "get",
            "https://vertexgen.example/v1/images/generations/gen_123",
            {"Authorization": "Bearer vertexgen_test_key"},
            60.0,
        ),
        (
            "get",
            "https://vertexgen.example/v1/images/generations/gen_123/content",
            {"Authorization": "Bearer vertexgen_test_key"},
            600.0,
        ),
    ]
    assert checkpoints == [
        {
            "kind": "vertexgen_generation",
            "attempt": 0,
            "model": "nano-banana-pro",
            "retry_pending": True,
        },
        {
            "kind": "vertexgen_generation",
            "job_id": "gen_123",
            "attempt": 0,
            "model": "nano-banana-pro",
        },
        None,
    ]


def test_checkpoint_resume_polls_existing_job_without_resubmitting(monkeypatch):
    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    monkeypatch.setenv("VERTEXGEN_POLL_INTERVAL", "0")
    service = VertexGenService()
    calls = []

    def post(*args, **kwargs):
        raise AssertionError("resume must not submit another VertexGen job")

    def get(url, headers, timeout):
        calls.append((url, headers, timeout))
        if url.endswith("/content"):
            return FakeResponse(
                content=b"resumed-provider-c2pa-bytes",
                headers={"Content-Type": "image/png"},
            )
        return FakeResponse(
            payload={
                "id": "gen_resume",
                "status": "succeeded",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "image_url": "/v1/images/generations/gen_resume/content",
            }
        )

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)
    checkpoints = []
    task = make_task(
        checkpoint={
            "kind": "vertexgen_generation",
            "job_id": "gen_resume",
            "attempt": 1,
            "model": "nano-banana-pro",
        }
    )

    result = service.process_with_checkpoint(task, checkpoints.append)

    assert result["data"] == b"resumed-provider-c2pa-bytes"
    assert result["metadata"]["resumed"] is True
    assert calls == [
        (
            "https://vertexgen.example/v1/images/generations/gen_resume",
            {"Authorization": "Bearer vertexgen_test_key"},
            60.0,
        ),
        (
            "https://vertexgen.example/v1/images/generations/gen_resume/content",
            {"Authorization": "Bearer vertexgen_test_key"},
            600.0,
        ),
    ]
    assert checkpoints == [None]


def test_checkpoint_before_submit_recovers_with_same_idempotency_key(monkeypatch):
    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    service = VertexGenService()
    first_checkpoints = []

    def interrupted_post(*args, **kwargs):
        raise KeyboardInterrupt("simulated process exit during POST")

    monkeypatch.setattr("requests.post", interrupted_post)

    with pytest.raises(KeyboardInterrupt):
        service.process_with_checkpoint(make_task(), first_checkpoints.append)

    assert first_checkpoints == [
        {
            "kind": "vertexgen_generation",
            "attempt": 0,
            "model": "nano-banana-pro",
            "retry_pending": True,
        }
    ]

    submitted_keys = []

    def resumed_post(url, headers, params, json, timeout):
        submitted_keys.append(headers["Idempotency-Key"])
        return FakeResponse(
            payload={
                "id": "gen_recovered",
                "status": "succeeded",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "image_url": "/v1/images/generations/gen_recovered/content",
            }
        )

    monkeypatch.setattr("requests.post", resumed_post)
    monkeypatch.setattr(
        "requests.get",
        lambda url, headers, timeout: FakeResponse(
            content=b"recovered-provider-bytes",
            headers={"Content-Type": "image/png"},
        ),
    )
    resume_checkpoints = []
    resumed_task = make_task(checkpoint=first_checkpoints[-1])

    result = service.process_with_checkpoint(resumed_task, resume_checkpoints.append)

    assert result["data"] == b"recovered-provider-bytes"
    assert result["metadata"]["resumed"] is True
    assert submitted_keys == ["task-vertexgen-123-0"]
    assert resume_checkpoints == [
        {
            "kind": "vertexgen_generation",
            "job_id": "gen_recovered",
            "attempt": 0,
            "model": "nano-banana-pro",
        },
        None,
    ]


def test_provider_failure_retries_with_fresh_idempotency_key(monkeypatch):
    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    monkeypatch.setenv("VERTEXGEN_POLL_INTERVAL", "0")
    monkeypatch.setenv("VERTEXGEN_RETRY_DELAY", "0")
    monkeypatch.setenv("VERTEXGEN_MAX_RETRIES", "1")
    service = VertexGenService()
    idempotency_keys = []

    def post(url, headers, params, json, timeout):
        idempotency_keys.append(headers["Idempotency-Key"])
        if len(idempotency_keys) == 1:
            return FakeResponse(
                status_code=502,
                payload={
                    "error": {
                        "code": "provider_unavailable",
                        "message": "Image generation failed unexpectedly",
                        "job_id": "gen_failed",
                    }
                },
            )
        return FakeResponse(
            status_code=202,
            payload={
                "id": "gen_retry",
                "status": "running",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
            },
        )

    def get(url, headers, timeout):
        if url.endswith("/content"):
            return FakeResponse(
                content=b"provider-media-after-retry",
                headers={"Content-Type": "image/png"},
            )
        return FakeResponse(
            payload={
                "id": "gen_retry",
                "status": "succeeded",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "image_url": "/v1/images/generations/gen_retry/content",
            }
        )

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)
    checkpoints = []

    result = service.process_with_checkpoint(make_task(), checkpoints.append)

    assert result["data"] == b"provider-media-after-retry"
    assert result["metadata"]["job_id"] == "gen_retry"
    assert idempotency_keys == ["task-vertexgen-123-0", "task-vertexgen-123-1"]
    assert checkpoints == [
        {
            "kind": "vertexgen_generation",
            "attempt": 0,
            "model": "nano-banana-pro",
            "retry_pending": True,
        },
        {
            "kind": "vertexgen_generation",
            "attempt": 1,
            "model": "nano-banana-pro",
            "retry_pending": True,
        },
        {
            "kind": "vertexgen_generation",
            "job_id": "gen_retry",
            "attempt": 1,
            "model": "nano-banana-pro",
        },
        None,
    ]


@pytest.mark.parametrize("failure_source", ["submission", "poll"])
def test_string_provider_error_still_retries_generation(monkeypatch, failure_source):
    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    monkeypatch.setenv("VERTEXGEN_POLL_INTERVAL", "0")
    monkeypatch.setenv("VERTEXGEN_RETRY_DELAY", "0")
    monkeypatch.setenv("VERTEXGEN_MAX_RETRIES", "1")
    service = VertexGenService()
    idempotency_keys = []

    def post(url, headers, params, json, timeout):
        idempotency_keys.append(headers["Idempotency-Key"])
        if len(idempotency_keys) == 1:
            return FakeResponse(
                payload={
                    "id": "gen_failed",
                    "status": "failed" if failure_source == "submission" else "running",
                    "model": "nano-banana-pro",
                    "aspect_ratio": "1:1",
                    "resolution": "1K",
                    "error": "provider safety rejection",
                }
            )
        return FakeResponse(
            payload={
                "id": "gen_retry",
                "status": "succeeded",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "image_url": "/v1/images/generations/gen_retry/content",
            }
        )

    def get(url, headers, timeout):
        if url.endswith("/content"):
            return FakeResponse(
                content=b"provider-media-after-string-error",
                headers={"Content-Type": "image/png"},
            )
        return FakeResponse(
            payload={
                "id": "gen_failed",
                "status": "failed",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "error": "provider safety rejection",
            }
        )

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)

    result = service.process_with_checkpoint(make_task())

    assert result["data"] == b"provider-media-after-string-error"
    assert idempotency_keys == ["task-vertexgen-123-0", "task-vertexgen-123-1"]


@pytest.mark.parametrize("first_failure", ["timeout", "generic_503"])
def test_ambiguous_submission_failure_reuses_idempotency_key(
    monkeypatch, first_failure
):
    import requests

    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    monkeypatch.setenv("VERTEXGEN_RETRY_DELAY", "0")
    monkeypatch.setenv("VERTEXGEN_MAX_RETRIES", "1")
    service = VertexGenService()
    idempotency_keys = []

    def post(url, headers, params, json, timeout):
        idempotency_keys.append(headers["Idempotency-Key"])
        if len(idempotency_keys) == 1:
            if first_failure == "timeout":
                raise requests.Timeout("submission result is unknown")
            return FakeResponse(
                status_code=503,
                payload={
                    "error": {
                        "code": "gateway_timeout",
                        "message": "upstream response is unknown",
                    }
                },
            )
        return FakeResponse(
            payload={
                "id": "gen_same_attempt",
                "status": "succeeded",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "image_url": ("/v1/images/generations/gen_same_attempt/content"),
            }
        )

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr(
        "requests.get",
        lambda url, headers, timeout: FakeResponse(
            content=b"same-attempt-provider-bytes",
            headers={"Content-Type": "image/png"},
        ),
    )

    result = service.process_with_checkpoint(make_task())

    assert result["data"] == b"same-attempt-provider-bytes"
    assert idempotency_keys == ["task-vertexgen-123-0", "task-vertexgen-123-0"]


def test_transient_status_failure_retries_existing_job(monkeypatch):
    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    monkeypatch.setenv("VERTEXGEN_POLL_INTERVAL", "0")
    monkeypatch.setenv("VERTEXGEN_RETRY_DELAY", "0")
    monkeypatch.setenv("VERTEXGEN_MAX_RETRIES", "1")
    service = VertexGenService()
    status_urls = []

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: FakeResponse(
            status_code=202,
            payload={
                "id": "gen_status_retry",
                "status": "running",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
            },
        ),
    )

    def get(url, headers, timeout):
        if url.endswith("/content"):
            return FakeResponse(
                content=b"status-retry-provider-bytes",
                headers={"Content-Type": "image/png"},
            )
        status_urls.append(url)
        if len(status_urls) == 1:
            return FakeResponse(
                status_code=503,
                payload={
                    "error": {
                        "code": "gateway_timeout",
                        "message": "status temporarily unavailable",
                    }
                },
            )
        return FakeResponse(
            payload={
                "id": "gen_status_retry",
                "status": "succeeded",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "image_url": ("/v1/images/generations/gen_status_retry/content"),
            }
        )

    monkeypatch.setattr("requests.get", get)

    result = service.process_with_checkpoint(make_task())

    assert result["data"] == b"status-retry-provider-bytes"
    assert status_urls == [
        "https://vertexgen.example/v1/images/generations/gen_status_retry",
        "https://vertexgen.example/v1/images/generations/gen_status_retry",
    ]


def test_download_timeout_retries_existing_job(monkeypatch):
    import requests

    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    monkeypatch.setenv("VERTEXGEN_RETRY_DELAY", "0")
    monkeypatch.setenv("VERTEXGEN_MAX_RETRIES", "1")
    service = VertexGenService()
    download_urls = []

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: FakeResponse(
            payload={
                "id": "gen_download_retry",
                "status": "succeeded",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "image_url": ("/v1/images/generations/gen_download_retry/content"),
            }
        ),
    )

    def get(url, headers, timeout):
        download_urls.append(url)
        if len(download_urls) == 1:
            raise requests.Timeout("content response is unknown")
        return FakeResponse(
            content=b"download-retry-provider-bytes",
            headers={"Content-Type": "image/png"},
        )

    monkeypatch.setattr("requests.get", get)

    result = service.process_with_checkpoint(make_task())

    assert result["data"] == b"download-retry-provider-bytes"
    assert download_urls == [
        "https://vertexgen.example/v1/images/generations/gen_download_retry/content",
        "https://vertexgen.example/v1/images/generations/gen_download_retry/content",
    ]


def test_polling_stops_at_configured_deadline(monkeypatch):
    from neurons.generator.services import vertexgen_service
    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("VERTEXGEN_API_BASE_URL", "https://vertexgen.example/v1")
    monkeypatch.setenv("VERTEXGEN_POLL_INTERVAL", "10")
    monkeypatch.setenv("VERTEXGEN_POLL_TIMEOUT", "0.5")
    service = VertexGenService()
    ticks = iter([0.0, 0.0, 1.0])
    status_calls = 0

    def get(url, headers, timeout):
        nonlocal status_calls
        status_calls += 1
        if status_calls > 1:
            raise AssertionError("polling continued after the deadline")
        return FakeResponse(
            payload={
                "id": "gen_stuck",
                "status": "running",
                "model": "nano-banana-pro",
                "aspect_ratio": "1:1",
                "resolution": "1K",
            }
        )

    monkeypatch.setattr("requests.get", get)
    monkeypatch.setattr(vertexgen_service.time, "monotonic", lambda: next(ticks))

    with pytest.raises(
        TimeoutError,
        match="VertexGen job gen_stuck exceeded 0.5s polling timeout",
    ):
        service._poll("gen_stuck")

    assert status_calls == 1


def test_registry_selects_vertexgen_for_image_generation(monkeypatch):
    from neurons.generator.services.service_registry import ServiceRegistry
    from neurons.generator.services.vertexgen_service import VertexGenService

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("IMAGE_SERVICE", "vertexgen")
    monkeypatch.setenv("VIDEO_SERVICE", "none")

    registry = ServiceRegistry()

    assert isinstance(registry.get_service("image"), VertexGenService)
    assert registry.get_service("video") is None


def test_registry_rejects_vertexgen_for_video_generation(monkeypatch):
    from neurons.generator.services.service_registry import ServiceRegistry

    monkeypatch.setenv("VERTEXGEN_API_KEY", "vertexgen_test_key")
    monkeypatch.setenv("IMAGE_SERVICE", "none")
    monkeypatch.setenv("VIDEO_SERVICE", "vertexgen")

    registry = ServiceRegistry()

    assert registry.get_service("video") is None
