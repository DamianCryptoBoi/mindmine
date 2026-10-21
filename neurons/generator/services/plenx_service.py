import json
import math
import os
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict
from urllib.parse import urlparse
from uuid import UUID

import requests

from ..task_manager import GenerationTask
from .base_service import BaseGenerationService, CheckpointFn


API_BASE = "http://127.0.0.1:8000"
MODEL = "omni-flash"
CHECKPOINT_KIND_PLENX = "plenx_generation"
MAX_GENERATION_RETRIES = 3
PENDING_STATUSES = {
    "waiting",
    "queued",
    "pending",
    "processing",
    "running",
    "submitting",
    "in_progress",
    "starting",
}
SUCCESS_STATUSES = {"completed", "succeeded", "success"}
FAILURE_STATUSES = {"failed", "error", "cancelled", "canceled"}


class PlenxService(BaseGenerationService):
    """Generate a Plenx reference image, then a video using that image."""

    def __init__(self, config: Any = None):
        super().__init__(config)
        self.password = os.getenv("PLENX_WRAPPER_PASSWORD", "").strip()
        self.base_url = os.getenv("PLENX_WRAPPER_BASE", API_BASE).strip().rstrip("/")
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "PLENX_WRAPPER_BASE must be an HTTP(S) URL without credentials, query, or fragment"
            )
        self.account_id = os.getenv("PLENX_ACCOUNT_ID", "").strip()
        if self.account_id:
            self.account_id = self._uuid(self.account_id, "PLENX_ACCOUNT_ID")
        self.request_timeout = float(os.getenv("PLENX_REQUEST_TIMEOUT", "60"))
        self.poll_interval = float(os.getenv("PLENX_POLL_INTERVAL", "5"))
        self.poll_timeout = float(os.getenv("PLENX_POLL_TIMEOUT", "1800"))
        self.download_timeout = float(os.getenv("PLENX_DOWNLOAD_TIMEOUT", "600"))
        for name in (
            "request_timeout",
            "poll_timeout",
            "download_timeout",
            "poll_interval",
        ):
            value = getattr(self, name)
            if (
                not math.isfinite(value)
                or value < 0
                or (value == 0 and name != "poll_interval")
            ):
                raise ValueError(
                    f"PLENX_{name.upper()} must be finite and {'nonnegative' if name == 'poll_interval' else 'positive'}"
                )

    def is_available(self) -> bool:
        return bool(self.password)

    def supports_modality(self, modality: str) -> bool:
        return modality == "video"

    def get_supported_tasks(self) -> Dict[str, list]:
        return {"image": [], "video": ["text_to_video"]}

    def get_api_key_requirements(self) -> Dict[str, str]:
        return {"PLENX_WRAPPER_PASSWORD": "Plenx wrapper password for video generation"}

    def process(self, task: GenerationTask) -> Dict[str, Any]:
        return self.process_with_checkpoint(task)

    def process_with_checkpoint(
        self, task: GenerationTask, on_checkpoint: CheckpointFn = None
    ) -> Dict[str, Any]:
        if not self.is_available():
            raise RuntimeError("Plenx service is unavailable: missing wrapper password")
        if task.modality != "video":
            raise ValueError(f"Plenx supports video generation, got {task.modality!r}")

        save = on_checkpoint or (lambda _: None)
        resumed = task.checkpoint is not None
        payload = self._build_payload(task)
        if resumed:
            checkpoint = dict(task.checkpoint)
            self._validate_checkpoint(checkpoint)
        else:
            checkpoint = {
                "kind": CHECKPOINT_KIND_PLENX,
                "modality": "video",
                "base_url": self.base_url,
                "stage": "ready",
                "phase": "image",
                "account_id": self._select_account(),
                "retries_used": 0,
            }

        if checkpoint.get("phase") == "image":
            image_payload = {
                "task_type": "image/gem-pix-2",
                "payload": {
                    "prompt": f"Generate the start frame for this video: {task.prompt}",
                    "aspect_ratio": payload["aspect_ratio"],
                    "num_outputs": 1,
                    "output_format": "png",
                    "resolution": "1k",
                    "resolution_tier": "1k",
                },
            }
            if "workplace_id" in payload:
                image_payload["workplace_id"] = payload["workplace_id"]
            image_result, image_checkpoint = self._run_generation(
                checkpoint, image_payload, save
            )
            reference_url = self._media_url("image", image_result)
            checkpoint = {
                "kind": CHECKPOINT_KIND_PLENX,
                "modality": "video",
                "base_url": self.base_url,
                "stage": "ready",
                "phase": "video",
                "account_id": image_checkpoint["account_id"],
                "retries_used": 0,
                "reference_image_url": reference_url,
                "image_task_id": image_checkpoint.get("task_id"),
                "image_request_id": image_checkpoint["request_id"],
            }
            save(checkpoint.copy())

        if checkpoint.get("reference_image_url"):
            payload["reference_images"] = json.dumps(
                [checkpoint["reference_image_url"]]
            )
        result, checkpoint = self._run_generation(checkpoint, payload, save)
        media_url = self._media_url("video", result)
        download = requests.get(
            media_url, headers={}, timeout=self.download_timeout, allow_redirects=False
        )
        self._raise_for_status(download, "video download")
        if not download.content:
            raise RuntimeError("Plenx video download returned empty media")
        save(None)
        return {
            "data": download.content,
            "metadata": {
                "provider": "plenx",
                "model": MODEL,
                "mime_type": download.headers.get("Content-Type", "video/mp4").split(
                    ";", 1
                )[0],
                "task_id": checkpoint["task_id"],
                "account_id": checkpoint["account_id"],
                "request_id": checkpoint["request_id"],
                "resumed": resumed,
                **{
                    key: checkpoint[key]
                    for key in (
                        "reference_image_url",
                        "image_task_id",
                        "image_request_id",
                    )
                    if key in checkpoint
                },
            },
        }

    def _run_generation(self, checkpoint, payload, save):
        phase = checkpoint.get("phase", "video")  # Older checkpoints are T2V jobs.
        while True:
            if checkpoint["stage"] == "submitting":
                raise RuntimeError(
                    "Plenx submission outcome is unknown; inspect wrapper history before submitting again"
                )
            if checkpoint["stage"] == "ready":
                checkpoint = self._submit(checkpoint, payload, save)
            result = self._poll(checkpoint, save)
            status = str(result.get("status", "")).strip().lower()
            if status not in FAILURE_STATUSES and result.get("success") is not False:
                return result, checkpoint
            retries = checkpoint.get("retries_used", 0)
            if status in {"cancelled", "canceled"} or retries >= MAX_GENERATION_RETRIES:
                save(None)
                reason = (
                    result.get("error_message")
                    or result.get("error")
                    or result.get("message")
                    or status
                )
                raise RuntimeError(
                    f"Plenx {phase} task {checkpoint.get('task_id') or checkpoint['request_id']} failed: {self._redact(str(reason))}"
                )
            checkpoint = {**checkpoint, "stage": "ready", "retries_used": retries + 1}
            for key in ("task_id", "request_id", "status_url"):
                checkpoint.pop(key, None)
            save(checkpoint.copy())
            time.sleep(self.poll_interval)

    def _submit(self, checkpoint, payload, save):
        checkpoint = {**checkpoint, "stage": "submitting"}
        # Persist before every POST: an uncertain acceptance must never be replayed.
        save(checkpoint.copy())
        phase = checkpoint.get("phase", "video")
        response = None
        try:
            options = {
                "headers": self._headers(checkpoint["account_id"]),
                "timeout": self.request_timeout,
                "allow_redirects": False,
            }
            if phase == "image":
                response = requests.post(
                    f"{self.base_url}/api/v1/queue/submit", json=payload, **options
                )
            else:
                response = requests.post(
                    f"{self.base_url}/api/v1/veo/generate",
                    files={key: (None, str(value)) for key, value in payload.items()},
                    **options,
                )
            result = self._json_response(response, f"{phase} submission")
            if (
                self._uuid(result.get("account_id"), "submission account_id")
                != checkpoint["account_id"]
            ):
                raise ValueError("Plenx submission returned a different account_id")
            request_id = self._uuid(result.get("request_id"), "submission request_id")
            task_id = result.get("task_id")
            if task_id is not None:
                task_id = self._uuid(task_id, "submission task_id")
            checkpoint.update(
                stage="submitted",
                task_id=task_id,
                request_id=request_id,
                status_url=f"/api/requests/{request_id}",
            )
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            if (
                response is not None
                and 400 <= response.status_code < 500
                and response.status_code != 429
            ):
                save(None)
                raise
            raise RuntimeError(
                "Plenx submission outcome is unknown; inspect wrapper history before submitting again"
            ) from exc
        save(checkpoint.copy())
        return checkpoint

    def _media_url(self, phase, result):
        if phase == "image":
            image_result = result.get("result") or {}
            urls = image_result.get("image_urls")
            url = (
                next(
                    (
                        value
                        for value in urls
                        if isinstance(value, str) and value.strip()
                    ),
                    None,
                )
                if isinstance(urls, list)
                else None
            )
            url = url or image_result.get("result_url")
        else:
            url = result.get("result_url")
        self._validate_media_url(url)
        return url

    def _select_account(self) -> str:
        if self.account_id:
            return self.account_id
        response = requests.get(
            f"{self.base_url}/api/accounts",
            headers=self._headers(),
            timeout=self.request_timeout,
            allow_redirects=False,
        )
        accounts = self._json_response(response, "account lookup").get("accounts")
        if (
            not isinstance(accounts, list)
            or not accounts
            or not isinstance(accounts[0], dict)
            or not accounts[0].get("has_token")
        ):
            raise RuntimeError("Plenx requires a configured server account")
        return self._uuid(accounts[0].get("id"), "server account_id")

    @staticmethod
    def _build_payload(task: GenerationTask) -> Dict[str, Any]:
        if (
            not isinstance(task.prompt, str)
            or not task.prompt.strip()
            or len(task.prompt) > 20_000
        ):
            raise ValueError("Plenx prompt must contain 1–20,000 characters")
        parameters = task.parameters or {}
        duration = int(parameters.get("duration", 4))
        if duration <= 0:
            raise ValueError("Plenx video duration must be positive")
        aspect_ratio = parameters.get(
            "aspect_ratio", parameters.get("aspectRatio", "16:9")
        )
        if aspect_ratio not in {"16:9", "9:16"}:
            aspect_ratio = "16:9"
        resolution = str(parameters.get("resolution", "720")).lower().removesuffix("p")
        if resolution not in {"720", "1080", "4k"}:
            resolution = "720"
        payload = {
            "prompt": task.prompt,
            "model": MODEL,
            "aspect_ratio": aspect_ratio,
            "duration": duration,
            "resolution": resolution,
        }
        for key in ("server_id", "workplace_id"):
            if parameters.get(key):
                payload[key] = PlenxService._uuid(parameters[key], key)
        return payload

    def _poll(self, checkpoint, save) -> Dict[str, Any]:
        deadline = time.monotonic() + self.poll_timeout
        failures = 0
        phase = checkpoint.get("phase", "video")
        job_id = checkpoint.get("task_id") or checkpoint["request_id"]
        status_url = checkpoint.get("status_url")
        if not status_url:
            route = "queue" if phase == "image" else "video"
            status_url = f"/api/v1/{route}/status/{checkpoint['task_id']}"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Plenx {phase} task {job_id} exceeded {self.poll_timeout:g}s polling timeout; task is still pending"
                )
            response = None
            try:
                response = requests.get(
                    f"{self.base_url}{status_url}",
                    headers=self._headers(checkpoint["account_id"]),
                    timeout=min(self.request_timeout, remaining),
                    allow_redirects=False,
                )
            except requests.RequestException:
                pass
            if (
                response is None
                or response.status_code == 429
                or 500 <= response.status_code < 600
            ):
                failures += 1
                delay = min(
                    30.0, max(1.0, self.poll_interval) * 2 ** min(failures - 1, 5)
                )
                if response is not None:
                    delay = self._retry_after(
                        response.headers.get("Retry-After"), delay
                    )
            else:
                failures = 0
                result = self._json_response(response, f"{phase} status")
                if checkpoint.get("status_url"):
                    record = result
                    if record.get("task_id") and record["task_id"] != checkpoint.get(
                        "task_id"
                    ):
                        checkpoint["task_id"] = self._uuid(
                            record["task_id"], "status task_id"
                        )
                        save(checkpoint.copy())
                    result = dict(record.get("response") or {})
                    result["status"] = record.get("status")
                    if record.get("error"):
                        result["error"] = record["error"]
                status = str(result.get("status", "")).strip().lower()
                if status == "submission_unknown":
                    raise RuntimeError(
                        f"Plenx task {job_id} submission outcome is unknown; inspect wrapper history"
                    )
                if status in SUCCESS_STATUSES | FAILURE_STATUSES:
                    return result
                if status not in PENDING_STATUSES:
                    raise RuntimeError(
                        f"Plenx task {job_id} returned unknown status: {self._redact(json.dumps(result))}"
                    )
                delay = self.poll_interval
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))

    def _validate_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        if (
            checkpoint.get("kind") != CHECKPOINT_KIND_PLENX
            or checkpoint.get("modality") != "video"
            or checkpoint.get("base_url") != self.base_url
            or checkpoint.get("stage") not in {"ready", "submitting", "submitted"}
            or checkpoint.get("phase", "video") not in {"image", "video"}
        ):
            raise ValueError(
                "Invalid Plenx checkpoint kind, modality, base URL, or stage"
            )
        self._uuid(checkpoint.get("account_id"), "checkpoint account_id")
        retries = checkpoint.get("retries_used", 0)
        if type(retries) is not int or not 0 <= retries <= MAX_GENERATION_RETRIES:
            raise ValueError("Invalid Plenx checkpoint retry count")
        if checkpoint.get("phase") == "video":
            self._validate_media_url(checkpoint.get("reference_image_url"))
        if checkpoint["stage"] == "submitted":
            self._uuid(checkpoint.get("request_id"), "checkpoint request_id")
            if checkpoint.get("status_url"):
                if (
                    checkpoint["status_url"]
                    != f"/api/requests/{checkpoint['request_id']}"
                ):
                    raise ValueError("Invalid Plenx checkpoint status URL")
                if checkpoint.get("task_id") is not None:
                    self._uuid(checkpoint["task_id"], "checkpoint task_id")
            else:
                self._uuid(checkpoint.get("task_id"), "checkpoint task_id")

    def _headers(self, account_id: str = "") -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self.password}"}
        if account_id:
            headers["X-Account-ID"] = account_id
        return headers

    def _json_response(self, response: Any, operation: str) -> Dict[str, Any]:
        self._raise_for_status(response, operation)
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Plenx {operation} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"Plenx {operation} returned invalid JSON")
        return result

    def _raise_for_status(self, response: Any, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        upstream = response.headers.get("X-Plenx-Status")
        origin = f" (upstream Plenx HTTP {upstream})" if upstream else ""
        raise RuntimeError(
            f"Plenx {operation} failed: HTTP {response.status_code}{origin} - {self._redact(response.text)[:1000]}"
        )

    def _redact(self, value: str) -> str:
        return value.replace(self.password, "[redacted]") if self.password else value

    @staticmethod
    def _uuid(value: Any, label: str) -> str:
        try:
            return str(UUID(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Invalid Plenx {label}") from exc

    @staticmethod
    def _validate_media_url(url: Any) -> None:
        try:
            parsed = urlparse(url) if isinstance(url, str) else None
            valid = (
                parsed is not None
                and parsed.scheme == "https"
                and parsed.hostname
                and parsed.port in {None, 443}
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError(
                "Plenx completed response omitted a valid HTTPS media URL"
            )

    @staticmethod
    def _retry_after(value: str, fallback: float) -> float:
        if value:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    seconds = parsedate_to_datetime(value).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    return fallback
            if math.isfinite(seconds):
                return max(0.0, seconds)
        return fallback
