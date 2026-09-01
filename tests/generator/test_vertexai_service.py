from types import SimpleNamespace

import pytest

from neurons.generator.task_manager import GenerationTask, TaskStatus


def make_task(resolution="1K"):
    return GenerationTask(
        task_id="task-vertex-123",
        modality="image",
        status=TaskStatus.PROCESSING,
        prompt="A red fox running through fresh snow",
        parameters={"resolution": resolution},
        webhook_url="https://validator.example/callback",
        signed_by="validator-hotkey",
        created_at=1_700_000_000.0,
    )


class FakeModels:
    def __init__(self, image_bytes):
        self.image_bytes = image_bytes
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text=None,
                                inline_data=SimpleNamespace(
                                    data=self.image_bytes,
                                    mime_type="image/jpeg",
                                ),
                            )
                        ]
                    )
                )
            ]
        )


class FlakyModels(FakeModels):
    def __init__(self, image_bytes, failures):
        super().__init__(image_bytes)
        self.failures = failures

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise RuntimeError("temporary Vertex failure")
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text=None,
                                inline_data=SimpleNamespace(
                                    data=self.image_bytes,
                                    mime_type="image/jpeg",
                                ),
                            )
                        ]
                    )
                )
            ]
        )


@pytest.mark.parametrize("resolution", ["1K", "2K", "4K"])
def test_vertex_maps_sn34_tier_and_returns_untouched_jpeg(monkeypatch, resolution):
    from neurons.generator.services import vertexai_service

    original_media = b"\xff\xd8\xff\xe1vertex-google-c2pa-bytes\xff\xd9"
    fake_models = FakeModels(original_media)
    client_args = []

    def make_client(**kwargs):
        client_args.append(kwargs)
        return SimpleNamespace(models=fake_models)

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "vertex-test-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    monkeypatch.delenv("VERTEXAI_PROJECT", raising=False)
    monkeypatch.delenv("VERTEXAI_LOCATION", raising=False)
    monkeypatch.setattr(vertexai_service.genai, "Client", make_client)

    service = vertexai_service.VertexAIService()
    result = service.process(make_task(resolution))

    assert len(client_args) == 1
    assert client_args[0]["vertexai"] is True
    assert client_args[0]["project"] == "vertex-test-project"
    assert client_args[0]["location"] == "global"
    assert client_args[0]["http_options"].timeout == 600_000
    assert len(fake_models.calls) == 1
    call = fake_models.calls[0]
    assert call["model"] == "gemini-3-pro-image"
    assert call["contents"][0].parts[0].text == ("A red fox running through fresh snow")
    assert call["config"].image_config.image_size == resolution
    assert call["config"].image_config.aspect_ratio == "1:1"
    assert call["config"].image_config.output_mime_type == "image/jpeg"
    assert call["config"].thinking_config is None
    assert result == {
        "data": original_media,
        "metadata": {
            "model": "gemini-3-pro-image",
            "provider": "vertexai",
            "mime_type": "image/jpeg",
        },
    }


def test_vertex_retries_three_times_after_the_initial_attempt(monkeypatch):
    from neurons.generator.services import vertexai_service

    fake_models = FlakyModels(b"google-c2pa-image", failures=3)
    monkeypatch.setenv("VERTEXAI_PROJECT", "vertex-test-project")
    monkeypatch.setenv("VERTEXAI_RETRY_DELAY", "0")
    monkeypatch.setattr(
        vertexai_service.genai,
        "Client",
        lambda **kwargs: SimpleNamespace(models=fake_models),
    )

    result = vertexai_service.VertexAIService().process(make_task())

    assert len(fake_models.calls) == 4
    assert result["data"] == b"google-c2pa-image"


def test_vertex_raises_after_exactly_four_failed_attempts(monkeypatch):
    from neurons.generator.services import vertexai_service

    fake_models = FlakyModels(b"unused", failures=100)
    monkeypatch.setenv("VERTEXAI_PROJECT", "vertex-test-project")
    monkeypatch.setenv("VERTEXAI_RETRY_DELAY", "0")
    monkeypatch.setattr(
        vertexai_service.genai,
        "Client",
        lambda **kwargs: SimpleNamespace(models=fake_models),
    )

    with pytest.raises(RuntimeError, match="temporary Vertex failure"):
        vertexai_service.VertexAIService().process(make_task())

    assert len(fake_models.calls) == 4


def test_registry_selects_vertexai_for_image_generation(monkeypatch):
    from neurons.generator.services import vertexai_service
    from neurons.generator.services.service_registry import ServiceRegistry

    monkeypatch.setenv("VERTEXAI_PROJECT", "vertex-test-project")
    monkeypatch.setenv("IMAGE_SERVICE", "vertexai")
    monkeypatch.setenv("VIDEO_SERVICE", "none")
    monkeypatch.setattr(
        vertexai_service.genai,
        "Client",
        lambda **kwargs: SimpleNamespace(models=FakeModels(b"unused")),
    )

    registry = ServiceRegistry()

    assert isinstance(registry.get_service("image"), vertexai_service.VertexAIService)
    assert registry.get_service("video") is None


def test_vertex_project_precedence_and_adc_fallback(monkeypatch):
    from neurons.generator.services import vertexai_service

    client_args = []
    monkeypatch.setattr(
        vertexai_service.genai,
        "Client",
        lambda **kwargs: client_args.append(kwargs)
        or SimpleNamespace(models=FakeModels(b"unused")),
    )
    monkeypatch.setattr(
        vertexai_service.google.auth,
        "default",
        lambda: (object(), "adc-project"),
    )

    monkeypatch.setenv("VERTEXAI_PROJECT", "explicit-project")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "generic-project")
    vertexai_service.VertexAIService()

    monkeypatch.delenv("VERTEXAI_PROJECT")
    vertexai_service.VertexAIService()

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT")
    vertexai_service.VertexAIService()

    assert [args["project"] for args in client_args] == [
        "explicit-project",
        "generic-project",
        "adc-project",
    ]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VERTEXAI_RETRY_DELAY", "-1"),
        ("VERTEXAI_RETRY_DELAY", "nan"),
        ("VERTEXAI_REQUEST_TIMEOUT", "0"),
        ("VERTEXAI_REQUEST_TIMEOUT", "inf"),
    ],
)
def test_vertex_rejects_invalid_timing_configuration(monkeypatch, name, value):
    from neurons.generator.services import vertexai_service

    monkeypatch.setenv("VERTEXAI_PROJECT", "vertex-test-project")
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        vertexai_service.genai,
        "Client",
        lambda **kwargs: pytest.fail("client should not be created"),
    )

    with pytest.raises(ValueError, match=name):
        vertexai_service.VertexAIService()
