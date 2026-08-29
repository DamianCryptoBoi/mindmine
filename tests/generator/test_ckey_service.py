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


def make_task(parameters=None):
    return GenerationTask(
        task_id="task-123",
        modality="image",
        status=TaskStatus.PROCESSING,
        prompt="A red fox running through fresh snow",
        parameters=parameters or {},
        webhook_url="https://validator.example/callback",
        signed_by="validator-hotkey",
        created_at=1_700_000_000.0,
    )


@pytest.mark.parametrize(
    ("resolution", "expected_size"),
    [
        ("1K", "1024x1024"),
        ("2K", "2048x2048"),
        ("4K", "4096x4096"),
        ("unsupported", "1024x1024"),
    ],
)
def test_primary_model_maps_tiers_to_square_and_preserves_downloaded_bytes(
    monkeypatch, resolution, expected_size
):
    from neurons.generator.services.ckey_service import CKeyService

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("CKEY_RETRY_DELAY", "0")
    service = CKeyService()
    original_media = b"\xff\xd8\xff\xe1google-c2pa-image-bytes\xff\xd9"
    submitted = []

    def post(url, headers, json, timeout):
        submitted.append((url, headers, json, timeout))
        return FakeResponse(
            payload={
                "created": 1_787_982_748,
                "data": [{"url": "https://flow-content.google/image/test"}],
                "model": "wowztools/nano-banana-pro",
            }
        )

    def get(url, timeout, allow_redirects):
        assert url == "https://flow-content.google/image/test"
        assert allow_redirects is False
        return FakeResponse(
            content=original_media,
            headers={"Content-Type": "image/jpeg"},
        )

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)

    result = service.process(make_task({"resolution": resolution}))

    assert result["data"] == original_media
    assert result["metadata"] == {
        "model": "wowztools/nano-banana-pro",
        "provider": "ckey",
        "mime_type": "image/jpeg",
    }
    assert submitted == [
        (
            "https://api.xah.io/v1/images/generations",
            {
                "Authorization": "Bearer ckey_test_key",
                "Content-Type": "application/json",
            },
            {
                "model": "wowztools/nano-banana-pro",
                "prompt": "A red fox running through fresh snow",
                "n": 1,
                "size": expected_size,
            },
            1800.0,
        )
    ]


def test_fallback_starts_after_three_retries_and_gets_three_retries(monkeypatch):
    from neurons.generator.services.ckey_service import (
        CKeyService,
        FALLBACK_MODEL,
        PRIMARY_MODEL,
    )

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("CKEY_RETRY_DELAY", "0")
    service = CKeyService()
    submitted_models = []

    def post(url, headers, json, timeout):
        submitted_models.append(json["model"])
        if len(submitted_models) < 8:
            return FakeResponse(status_code=503, payload={"error": "temporary failure"})
        return FakeResponse(
            payload={
                "created": 1_787_982_748,
                "data": [{"url": "https://flow-content.google/image/fallback"}],
                "model": FALLBACK_MODEL,
            }
        )

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr(
        "requests.get",
        lambda url, timeout, allow_redirects: FakeResponse(
            content=b"fallback-google-c2pa-bytes",
            headers={"Content-Type": "image/png"},
        ),
    )

    result = service.process(make_task({"resolution": "4K"}))

    assert submitted_models == [PRIMARY_MODEL] * 4 + [FALLBACK_MODEL] * 4
    assert result["data"] == b"fallback-google-c2pa-bytes"
    assert result["metadata"]["model"] == FALLBACK_MODEL
    assert result["metadata"]["mime_type"] == "image/png"


def test_registry_selects_ckey_for_image_generation(monkeypatch):
    from neurons.generator.services.ckey_service import CKeyService
    from neurons.generator.services.service_registry import ServiceRegistry

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("IMAGE_SERVICE", "ckey")
    monkeypatch.setenv("VIDEO_SERVICE", "none")

    registry = ServiceRegistry()

    assert isinstance(registry.get_service("image"), CKeyService)
    assert registry.get_service("video") is None


@pytest.mark.parametrize(
    "media_url",
    [
        "http://flow-content.google/image/not-https",
        "https://127.0.0.1/internal",
        "https://flow-content.google.evil.example/image/fake",
    ],
)
def test_untrusted_media_urls_are_rejected_without_fetching(monkeypatch, media_url):
    from neurons.generator.services.ckey_service import CKeyService

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("CKEY_RETRY_DELAY", "0")
    service = CKeyService()
    downloads = []

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: FakeResponse(
            payload={"data": [{"url": media_url}]}
        ),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: downloads.append(args[0])
        or FakeResponse(content=b"private data"),
    )

    with pytest.raises(RuntimeError, match="refused untrusted media URL"):
        service.process(make_task())

    assert downloads == []
