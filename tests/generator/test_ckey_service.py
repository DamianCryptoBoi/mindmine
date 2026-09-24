import json
import subprocess
import sys
from types import SimpleNamespace

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


def test_service_registry_import_does_not_load_validator_c2pa_verifier():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from neurons.generator.services.service_registry import ServiceRegistry; "
            "assert 'gas.verification.c2pa_verification' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.fixture
def valid_c2pa(monkeypatch):
    monkeypatch.setattr(
        "gas.verification.c2pa_verification.verify_c2pa",
        lambda _media: SimpleNamespace(verified=True, is_trusted_issuer=True),
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
    monkeypatch, valid_c2pa, resolution, expected_size
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


def test_fallback_starts_after_three_retries_and_gets_three_retries(
    monkeypatch, valid_c2pa
):
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


def test_media_url_from_any_host_is_downloaded(monkeypatch, valid_c2pa):
    from neurons.generator.services.ckey_service import CKeyService

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("CKEY_RETRY_DELAY", "0")
    service = CKeyService()
    downloads = []
    media_url = "https://cdn.ckey.example/image/generated"

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: FakeResponse(
            payload={"data": [{"url": media_url}]}
        ),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: downloads.append(args[0])
        or FakeResponse(
            content=b"generated-image",
            headers={"Content-Type": "image/png"},
        ),
    )

    result = service.process(make_task())

    assert downloads == [media_url]
    assert result["data"] == b"generated-image"


def test_non_https_media_url_is_rejected_without_fetching(monkeypatch):
    from neurons.generator.services.ckey_service import CKeyService

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("CKEY_RETRY_DELAY", "0")
    service = CKeyService()
    downloads = []

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: FakeResponse(
            payload={"data": [{"url": "http://cdn.ckey.example/image/generated"}]}
        ),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: downloads.append(args[0]),
    )

    with pytest.raises(RuntimeError, match="refused untrusted media URL"):
        service.process(make_task())

    assert downloads == []


@pytest.mark.parametrize(
    "first_verification",
    [
        SimpleNamespace(verified=False, is_trusted_issuer=False, error="No C2PA manifest"),
        SimpleNamespace(verified=True, is_trusted_issuer=False, error="Untrusted issuer"),
    ],
)
def test_retries_generation_when_downloaded_image_fails_c2pa(
    monkeypatch, first_verification
):
    from neurons.generator.services.ckey_service import CKeyService

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("CKEY_RETRY_DELAY", "0")
    service = CKeyService()
    submissions = []
    checked_media = []

    def post(url, headers, json, timeout):
        submissions.append(json["model"])
        return FakeResponse(payload={"data": [{"url": "https://example.com/image"}]})

    def get(url, timeout, allow_redirects):
        return FakeResponse(content=f"image-{len(submissions)}".encode())

    def verify(media):
        checked_media.append(media)
        if len(checked_media) == 1:
            return first_verification
        return SimpleNamespace(verified=True, is_trusted_issuer=True)

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr("requests.get", get)
    monkeypatch.setattr(
        "gas.verification.c2pa_verification.verify_c2pa", verify
    )

    result = service.process(make_task())

    assert result["data"] == b"image-2"
    assert submissions == ["wowztools/nano-banana-pro"] * 2
    assert checked_media == [b"image-1", b"image-2"]


def test_c2pa_failures_exhaust_each_models_existing_retry_budget(monkeypatch):
    from neurons.generator.services.ckey_service import (
        CKeyService,
        FALLBACK_MODEL,
        PRIMARY_MODEL,
    )

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("CKEY_RETRY_DELAY", "0")
    service = CKeyService()
    submissions = []

    def post(url, headers, json, timeout):
        submissions.append(json["model"])
        return FakeResponse(payload={"data": [{"url": "https://example.com/image"}]})

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: FakeResponse(content=b"invalid-c2pa"),
    )
    monkeypatch.setattr(
        "gas.verification.c2pa_verification.verify_c2pa",
        lambda _media: SimpleNamespace(
            verified=False, is_trusted_issuer=False, error="No C2PA manifest"
        ),
    )

    with pytest.raises(RuntimeError, match="C2PA.*No C2PA manifest"):
        service.process(make_task())

    assert submissions == [PRIMARY_MODEL] * 4 + [FALLBACK_MODEL] * 4


def test_real_c2pa_verifier_rejects_unsigned_download(monkeypatch):
    from neurons.generator.services import ckey_service

    monkeypatch.setenv("CKEY_API_KEY", "ckey_test_key")
    monkeypatch.setenv("CKEY_RETRY_DELAY", "0")
    monkeypatch.setattr(ckey_service, "RETRIES_PER_MODEL", 0)
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: FakeResponse(
            payload={"data": [{"url": "https://example.com/image"}]}
        ),
    )
    monkeypatch.setattr(
        "requests.get", lambda *args, **kwargs: FakeResponse(content=b"unsigned-image")
    )

    with pytest.raises(RuntimeError, match="C2PA"):
        ckey_service.CKeyService().process(make_task())
