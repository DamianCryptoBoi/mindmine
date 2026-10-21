import json
from dataclasses import replace
from email.utils import formatdate

import pytest
import requests

from neurons.generator.task_manager import GenerationTask, TaskStatus


ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ACCOUNT_ID = "22222222-2222-4222-8222-222222222222"
TASK_ID = "33333333-3333-4333-8333-333333333333"
REQUEST_ID = "44444444-4444-4444-8444-444444444444"
IMAGE_TASK_ID = "55555555-5555-4555-8555-555555555555"
IMAGE_REQUEST_ID = "66666666-6666-4666-8666-666666666666"
REFERENCE_URL = "https://images.plenxai.com/reference.png"
MEDIA_URL = "https://cdn.plenxai.com/video.mp4"
MEDIA = b"\x00\x00\x00\x18ftypisom-original-video-bytes"


def response(payload=None, status=200, content=None, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = content if content is not None else json.dumps(payload).encode()
    result.headers.update(headers or {})
    return result


def make_task(parameters=None, checkpoint=None):
    return GenerationTask(
        task_id="miner-task-123",
        modality="video",
        status=TaskStatus.PROCESSING,
        prompt="A cat walking toward the camera",
        parameters=parameters or {},
        webhook_url="https://validator.example/callback",
        signed_by="validator-hotkey",
        created_at=1_700_000_000.0,
        checkpoint=checkpoint,
    )


def configure_service(monkeypatch):
    from neurons.generator.services.plenx_service import PlenxService

    monkeypatch.setenv("PLENX_WRAPPER_PASSWORD", "test-password")
    monkeypatch.setenv("PLENX_WRAPPER_BASE", "https://wrapper.example")
    monkeypatch.setenv("PLENX_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("PLENX_POLL_INTERVAL", "0")
    return PlenxService()


def accepted():
    return {
        "task_id": TASK_ID,
        "account_id": ACCOUNT_ID,
        "request_id": REQUEST_ID,
        "status": "queued",
        "message": "Flow Veo3 T2V task submitted",
    }


def completed():
    return {
        "task_id": TASK_ID,
        "status": "completed",
        "progress": 100,
        "message": "",
        "current_step": "",
        "result_url": MEDIA_URL,
        "duration": None,
        "generation_time": None,
        "is_nsfw": False,
        "account_id": ACCOUNT_ID,
        "request_id": REQUEST_ID,
    }


def submitted_checkpoint(phase=None):
    checkpoint = {
        "kind": "plenx_generation",
        "modality": "video",
        "base_url": "https://wrapper.example",
        "stage": "submitted",
        "task_id": TASK_ID,
        "account_id": ACCOUNT_ID,
        "request_id": REQUEST_ID,
    }
    if phase:
        checkpoint.update(phase=phase, retries_used=0)
        if phase == "image":
            checkpoint.update(task_id=IMAGE_TASK_ID, request_id=IMAGE_REQUEST_ID)
        else:
            checkpoint.update(
                reference_image_url=REFERENCE_URL,
                image_task_id=IMAGE_TASK_ID,
                image_request_id=IMAGE_REQUEST_ID,
            )
        checkpoint["status_url"] = "/api/requests/" + checkpoint["request_id"]
    return checkpoint


def install_http(
    monkeypatch,
    statuses=None,
    submit=None,
    media=MEDIA,
    image_statuses=None,
    account_id=ACCOUNT_ID,
    image_submit=None,
    video_submit=None,
):
    polls = iter(statuses if statuses is not None else [response(completed())])
    image_polls = iter(
        image_statuses
        or [
            response({"status": "completed", "result": {"image_urls": [REFERENCE_URL]}})
        ]
    )
    last_poll = None
    last_image_poll = None
    calls = []

    def post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        if isinstance(submit, Exception):
            raise submit
        if submit is not None:
            return submit
        if url.endswith("/api/v1/queue/submit"):
            if isinstance(image_submit, Exception):
                raise image_submit
            if image_submit is not None:
                return image_submit
            return response(
                {
                    **accepted(),
                    "task_id": IMAGE_TASK_ID,
                    "request_id": IMAGE_REQUEST_ID,
                    "account_id": account_id,
                }
            )
        if isinstance(video_submit, Exception):
            raise video_submit
        if video_submit is not None:
            return video_submit
        return response({**accepted(), "account_id": account_id})

    def get(url, **kwargs):
        nonlocal last_poll, last_image_poll
        calls.append(("GET", url, kwargs))
        if url.endswith("/api/accounts"):
            return response(
                {"accounts": [{"id": ACCOUNT_ID, "name": "First", "has_token": True}]}
            )
        if url.endswith("/api/requests/" + IMAGE_REQUEST_ID):
            result = next(image_polls, last_image_poll)
            last_image_poll = result
            if isinstance(result, Exception):
                raise result
            if result.status_code != 200:
                return result
            data = result.json()
            if "id" in data:
                return result
            return response(
                {
                    "id": IMAGE_REQUEST_ID,
                    "kind": "image",
                    "account_id": account_id,
                    "task_id": IMAGE_TASK_ID,
                    "status": data.get("status"),
                    "response": data,
                    "error": data.get("error"),
                }
            )
        if url.endswith("/api/v1/video/status/" + TASK_ID) or url.endswith(
            "/api/requests/" + REQUEST_ID
        ):
            result = next(polls, last_poll)
            last_poll = result
            if isinstance(result, Exception):
                raise result
            if "/api/requests/" in url and result.status_code == 200:
                data = result.json()
                if "id" in data:
                    return result
                return response(
                    {
                        "id": REQUEST_ID,
                        "kind": "video",
                        "account_id": account_id,
                        "task_id": TASK_ID,
                        "status": data.get("status"),
                        "response": data,
                        "error": data.get("error"),
                    }
                )
            return result
        assert url == MEDIA_URL
        return response(
            content=media, headers={"Content-Type": "video/mp4; charset=binary"}
        )

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "get", get)
    return calls


def test_registry_selects_plenx_only_for_video(monkeypatch):
    from neurons.generator.services.service_registry import ServiceRegistry

    monkeypatch.setenv("PLENX_WRAPPER_PASSWORD", "test-password")
    monkeypatch.setenv("IMAGE_SERVICE", "none")
    monkeypatch.setenv("VIDEO_SERVICE", "plenx")
    registry = ServiceRegistry()
    service = registry.get_service("video")
    assert service is not None
    assert service.name == "PlenxService"
    assert registry.get_service("image") is None
    assert not service.supports_modality("image")


def test_multipart_submission_checkpoints_and_returns_original_bytes(monkeypatch):
    service = configure_service(monkeypatch)
    calls = install_http(monkeypatch)
    checkpoints = []
    result = service.process_with_checkpoint(
        make_task({"resolution": "1080p", "aspect_ratio": "9:16", "duration": 6}),
        checkpoints.append,
    )

    assert calls[0][1] == "https://wrapper.example/api/v1/queue/submit"
    assert calls[0][2]["json"] == {
        "task_type": "image/gem-pix-2",
        "payload": {
            "prompt": "Generate the start frame for this video: A cat walking toward the camera",
            "aspect_ratio": "9:16",
            "num_outputs": 1,
            "output_format": "png",
            "resolution": "1k",
            "resolution_tier": "1k",
        },
    }
    method, url, kwargs = next(
        call
        for call in calls
        if call[0] == "POST" and call[1].endswith("/veo/generate")
    )
    assert (method, url) == ("POST", "https://wrapper.example/api/v1/veo/generate")
    prepared = requests.Request(
        "POST", url, headers=kwargs["headers"], files=kwargs["files"]
    ).prepare()
    assert prepared.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert kwargs["files"] == {
        "prompt": (None, "A cat walking toward the camera"),
        "model": (None, "omni-flash"),
        "aspect_ratio": (None, "9:16"),
        "duration": (None, "6"),
        "resolution": (None, "1080"),
        "reference_images": (None, json.dumps([REFERENCE_URL])),
    }
    assert kwargs["headers"] == {
        "Authorization": "Bearer test-password",
        "X-Account-ID": ACCOUNT_ID,
    }
    assert checkpoints[0] == {
        "kind": "plenx_generation",
        "modality": "video",
        "base_url": service.base_url,
        "stage": "submitting",
        "account_id": ACCOUNT_ID,
        "phase": "image",
        "retries_used": 0,
    }
    assert checkpoints[1] == submitted_checkpoint("image")
    assert checkpoints[2]["stage"] == "ready"
    assert checkpoints[2]["reference_image_url"] == REFERENCE_URL
    assert checkpoints[-2:] == [submitted_checkpoint("video"), None]
    assert "test-password" not in json.dumps(checkpoints)
    assert calls[-1][2].get("headers", {}) == {}
    assert calls[-1][2]["allow_redirects"] is False
    assert result["data"] == MEDIA
    assert result["metadata"]["provider"] == "plenx"
    assert result["metadata"]["model"] == "omni-flash"
    assert result["metadata"]["mime_type"] == "video/mp4"
    assert result["metadata"]["request_id"] == REQUEST_ID
    assert result["metadata"]["reference_image_url"] == REFERENCE_URL


def test_discovers_first_account_when_none_configured(monkeypatch):
    configure_service(monkeypatch)
    monkeypatch.delenv("PLENX_ACCOUNT_ID")
    from neurons.generator.services.plenx_service import PlenxService

    calls = install_http(monkeypatch)
    result = PlenxService().process(make_task())
    assert calls[0][1] == "https://wrapper.example/api/accounts"
    assert calls[1][2]["headers"]["X-Account-ID"] == ACCOUNT_ID
    assert result["metadata"]["account_id"] == ACCOUNT_ID


def test_resume_polls_original_account_without_submitting_again(monkeypatch):
    service = configure_service(monkeypatch)
    service.account_id = OTHER_ACCOUNT_ID
    calls = install_http(monkeypatch)
    result = service.process(make_task(checkpoint=submitted_checkpoint()))
    assert all(method == "GET" for method, _, _ in calls)
    assert calls[0][2]["headers"]["X-Account-ID"] == ACCOUNT_ID
    assert result["metadata"]["resumed"] is True


def test_equivalent_uuid_spelling_does_not_lose_an_accepted_job(monkeypatch):
    configure_service(monkeypatch)
    monkeypatch.setenv("PLENX_ACCOUNT_ID", "ABCDEFAB-1234-4234-8234-ABCDEFABCDEF")
    from neurons.generator.services.plenx_service import PlenxService

    canonical_account = "abcdefab-1234-4234-8234-abcdefabcdef"
    calls = install_http(monkeypatch, account_id=canonical_account)
    checkpoints = []
    result = PlenxService().process_with_checkpoint(make_task(), checkpoints.append)
    assert result["data"] == MEDIA
    assert calls[0][2]["headers"]["X-Account-ID"] == canonical_account
    assert checkpoints[1]["account_id"] == canonical_account
    assert checkpoints[1]["task_id"] == IMAGE_TASK_ID


@pytest.mark.parametrize(
    "submit",
    [
        requests.Timeout("timeout"),
        response({"detail": "upstream unavailable"}, status=504),
        response({"status": "queued"}),
        response(content=b"invalid JSON"),
    ],
)
def test_uncertain_submission_is_never_replayed(monkeypatch, submit):
    service = configure_service(monkeypatch)
    calls = install_http(monkeypatch, submit=submit)
    checkpoints = []
    with pytest.raises(RuntimeError, match="unknown"):
        service.process_with_checkpoint(make_task(), checkpoints.append)
    assert len(calls) == 1
    assert checkpoints[-1]["stage"] == "submitting"
    with pytest.raises(RuntimeError, match="unknown"):
        service.process(make_task(checkpoint=checkpoints[-1]))
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status",
    [
        "QUEUED",
        "pending",
        "processing",
        "running",
        "submitting",
        "in_progress",
        "starting",
    ],
)
def test_active_statuses_continue_polling_same_job(monkeypatch, status):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch,
        statuses=[
            response({"status": status, "result_url": None}),
            response(completed()),
        ],
    )
    assert service.process(make_task())["data"] == MEDIA
    assert len([call for call in calls if call[0] == "POST"]) == 2


@pytest.mark.parametrize("status", ["failed", "error", "cancelled", "canceled"])
def test_terminal_failure_retries_errors_at_most_three_times(monkeypatch, status):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch,
        statuses=[
            response(
                {
                    "status": status,
                    "success": False,
                    "error_message": "provider rejected prompt",
                }
            )
        ],
    )
    checkpoints = []
    with pytest.raises(RuntimeError, match="provider rejected prompt"):
        service.process_with_checkpoint(make_task(), checkpoints.append)
    assert checkpoints[-1] is None
    video_posts = [
        call
        for call in calls
        if call[0] == "POST" and call[1].endswith("/veo/generate")
    ]
    assert len(video_posts) == (4 if status in {"failed", "error"} else 1)
    assert (
        len(
            [
                call
                for call in calls
                if call[0] == "POST" and call[1].endswith("/queue/submit")
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    "result",
    [
        {"status": "completed", "result_url": None},
        {"status": "unrecognized", "message": "new provider state"},
        {"status": "completed", "success": False, "error": "not generated"},
    ],
)
def test_status_without_a_successful_deliverable_is_rejected(monkeypatch, result):
    service = configure_service(monkeypatch)
    calls = install_http(monkeypatch, statuses=[response(result)])
    with pytest.raises(RuntimeError):
        service.process(make_task())
    assert len(calls) == (10 if result.get("success") is False else 4)


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("temporarily offline"),
        response({"detail": "unavailable"}, status=502),
    ],
)
def test_transient_get_failure_retries_same_task(monkeypatch, failure):
    service = configure_service(monkeypatch)
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    calls = install_http(monkeypatch, statuses=[failure, response(completed())])
    assert service.process(make_task())["data"] == MEDIA
    assert len(sleeps) == 1
    assert len([call for call in calls if call[0] == "POST"]) == 2


@pytest.mark.parametrize("retry_after", ["7", formatdate(1_700_000_007, usegmt=True)])
def test_rate_limited_poll_honors_retry_after(monkeypatch, retry_after):
    service = configure_service(monkeypatch)
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    monkeypatch.setattr("time.time", lambda: 1_700_000_000)
    install_http(
        monkeypatch,
        statuses=[
            response(
                {"detail": "rate limited"},
                status=429,
                headers={"Retry-After": retry_after},
            ),
            response(completed()),
        ],
    )
    assert service.process(make_task())["data"] == MEDIA
    assert sleeps == [7.0]


def test_poll_timeout_preserves_checkpoint_and_never_resubmits(monkeypatch):
    service = configure_service(monkeypatch)
    service.poll_timeout = 1.0
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + max(seconds, 0.5))
    )
    calls = install_http(
        monkeypatch, statuses=[response({"status": "processing"}) for _ in range(3)]
    )
    checkpoints = []
    with pytest.raises(TimeoutError, match=TASK_ID):
        service.process_with_checkpoint(make_task(), checkpoints.append)
    assert checkpoints[-1] == submitted_checkpoint("video")
    assert len([call for call in calls if call[0] == "POST"]) == 2
    assert all(call[2]["timeout"] <= 1 for call in calls if "/api/requests/" in call[1])


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422])
def test_permanent_poll_errors_are_not_retried(monkeypatch, status):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch,
        statuses=[response({"detail": "test-password rejected"}, status=status)],
    )
    with pytest.raises(RuntimeError) as exc:
        service.process(make_task())
    assert str(status) in str(exc.value)
    assert "test-password" not in str(exc.value)
    assert len(calls) == 4


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.plenxai.com/video.mp4",
        "file:///tmp/video.mp4",
        "https://user:password@cdn.plenxai.com/video.mp4",
    ],
)
def test_untrusted_media_urls_are_rejected_before_download(monkeypatch, url):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch, statuses=[response({**completed(), "result_url": url})]
    )
    with pytest.raises(RuntimeError, match="media URL"):
        service.process(make_task())
    assert len(calls) == 4


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "another_provider"},
        {"modality": "image"},
        {"base_url": "https://another-wrapper.example"},
        {"task_id": "../../other"},
        {"account_id": "bad-id"},
    ],
)
def test_invalid_checkpoint_never_submits_a_new_generation(monkeypatch, changes):
    service = configure_service(monkeypatch)
    calls = install_http(monkeypatch)
    with pytest.raises(ValueError):
        service.process(make_task(checkpoint={**submitted_checkpoint(), **changes}))
    assert calls == []


def test_video_only_service_rejects_images_before_http(monkeypatch):
    service = configure_service(monkeypatch)
    calls = install_http(monkeypatch)
    with pytest.raises(ValueError, match="video"):
        service.process(replace(make_task(), modality="image"))
    assert calls == []


def test_empty_download_is_not_a_success(monkeypatch):
    service = configure_service(monkeypatch)
    install_http(monkeypatch, media=b"")
    with pytest.raises(RuntimeError, match="empty"):
        service.process(make_task())


def test_image_failure_retries_before_submitting_reference_video(monkeypatch):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch,
        image_statuses=[
            response({"status": "error", "error": "temporary image failure"}),
            response({"status": "completed", "result": {"result_url": REFERENCE_URL}}),
        ],
    )
    checkpoints = []
    result = service.process_with_checkpoint(make_task(), checkpoints.append)
    posts = [call for call in calls if call[0] == "POST"]
    assert [call[1].rsplit("/", 1)[-1] for call in posts] == [
        "submit",
        "submit",
        "generate",
    ]
    assert posts[-1][2]["files"]["reference_images"] == (
        None,
        json.dumps([REFERENCE_URL]),
    )
    assert any(
        cp and cp.get("phase") == "image" and cp.get("retries_used") == 1
        for cp in checkpoints
    )
    assert result["data"] == MEDIA


def test_image_failure_stops_after_three_retries_without_video_submission(monkeypatch):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch,
        image_statuses=[
            response(
                {"status": "failed", "error_message": "image generation unavailable"}
            )
        ],
    )
    checkpoints = []
    with pytest.raises(RuntimeError, match="image generation unavailable"):
        service.process_with_checkpoint(make_task(), checkpoints.append)
    posts = [call for call in calls if call[0] == "POST"]
    assert len(posts) == 4
    assert all(call[1].endswith("/queue/submit") for call in posts)
    assert checkpoints[-1] is None


def test_video_retries_reuse_image_and_checkpoint_retry_budget(monkeypatch):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch,
        statuses=[
            response({"status": "error", "error": "try again"}),
            response(completed()),
        ],
    )
    checkpoints = []
    result = service.process_with_checkpoint(make_task(), checkpoints.append)
    posts = [call for call in calls if call[0] == "POST"]
    assert len(posts) == 3
    assert posts[0][1].endswith("/queue/submit")
    assert (
        posts[1][2]["files"]["reference_images"]
        == posts[2][2]["files"]["reference_images"]
        == (None, json.dumps([REFERENCE_URL]))
    )
    assert any(
        cp
        and cp.get("phase") == "video"
        and cp.get("stage") == "ready"
        and cp.get("retries_used") == 1
        for cp in checkpoints
    )
    assert result["data"] == MEDIA


def test_resume_image_job_does_not_regenerate_image(monkeypatch):
    service = configure_service(monkeypatch)
    service.account_id = OTHER_ACCOUNT_ID
    calls = install_http(monkeypatch)
    result = service.process(make_task(checkpoint=submitted_checkpoint("image")))
    assert calls[0][1].endswith("/api/requests/" + IMAGE_REQUEST_ID)
    assert calls[0][2]["headers"]["X-Account-ID"] == ACCOUNT_ID
    posts = [call for call in calls if call[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][1].endswith("/veo/generate")
    assert result["metadata"]["resumed"] is True


def test_resume_ready_video_preserves_image_and_used_retries(monkeypatch):
    service = configure_service(monkeypatch)
    checkpoint = {**submitted_checkpoint("video"), "stage": "ready", "retries_used": 3}
    for key in ["task_id", "request_id", "status_url"]:
        checkpoint.pop(key)
    calls = install_http(
        monkeypatch, statuses=[response({"status": "failed", "error": "still failing"})]
    )
    with pytest.raises(RuntimeError, match="still failing"):
        service.process(make_task(checkpoint=checkpoint))
    posts = [call for call in calls if call[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2]["files"]["reference_images"] == (
        None,
        json.dumps([REFERENCE_URL]),
    )


def test_ambiguous_video_submission_never_restarts_either_stage(monkeypatch):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch, video_submit=requests.Timeout("acceptance unknown")
    )
    checkpoints = []
    with pytest.raises(RuntimeError, match="unknown"):
        service.process_with_checkpoint(make_task(), checkpoints.append)
    assert checkpoints[-1]["phase"] == "video"
    assert checkpoints[-1]["stage"] == "submitting"
    assert checkpoints[-1]["reference_image_url"] == REFERENCE_URL
    call_count = len(calls)
    with pytest.raises(RuntimeError, match="unknown"):
        service.process(make_task(checkpoint=checkpoints[-1]))
    assert len(calls) == call_count


@pytest.mark.parametrize("phase", ["image", "video"])
def test_queued_request_with_null_task_id_is_polled_without_resubmission(
    monkeypatch, phase
):
    service = configure_service(monkeypatch)
    request_id = IMAGE_REQUEST_ID if phase == "image" else REQUEST_ID
    task_id = IMAGE_TASK_ID if phase == "image" else TASK_ID
    upstream = (
        {"status": "completed", "result": {"image_urls": [REFERENCE_URL]}}
        if phase == "image"
        else completed()
    )
    queued = response(
        {
            **accepted(),
            "task_id": None,
            "request_id": request_id,
            "status": "waiting",
            "status_url": "/api/requests/" + request_id,
        },
        status=202,
    )
    polls = [
        response(
            {
                "id": request_id,
                "account_id": ACCOUNT_ID,
                "kind": phase,
                "task_id": None,
                "status": "waiting",
                "response": None,
            }
        ),
        response(
            {
                "id": request_id,
                "account_id": ACCOUNT_ID,
                "kind": phase,
                "task_id": task_id,
                "status": "completed",
                "response": upstream,
            }
        ),
    ]
    options = (
        {"image_submit": queued, "image_statuses": polls}
        if phase == "image"
        else {"video_submit": queued, "statuses": polls}
    )
    calls = install_http(monkeypatch, **options)
    checkpoints = []
    result = service.process_with_checkpoint(make_task(), checkpoints.append)
    assert len([call for call in calls if call[0] == "POST"]) == 2
    assert any(
        cp
        and cp.get("phase") == phase
        and cp.get("stage") == "submitted"
        and cp.get("task_id") is None
        for cp in checkpoints
    )
    assert result["data"] == MEDIA


def test_submission_unknown_job_does_not_start_generation_retry(monkeypatch):
    service = configure_service(monkeypatch)
    calls = install_http(
        monkeypatch,
        statuses=[
            response(
                {"status": "submission_unknown", "error": "cannot confirm acceptance"}
            )
        ],
    )
    with pytest.raises(RuntimeError, match="unknown"):
        service.process(make_task())
    assert len([call for call in calls if call[0] == "POST"]) == 2
