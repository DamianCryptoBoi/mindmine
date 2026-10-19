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
PENDING_STATUSES = {
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
    """Video generation through the operator's local or hosted Plenx wrapper."""

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
        checkpoint = task.checkpoint
        resumed = checkpoint is not None
        if checkpoint is not None:
            self._validate_checkpoint(checkpoint)
            if checkpoint.get("stage") == "submitting":
                raise RuntimeError(
                    "Plenx submission outcome is unknown; inspect wrapper history before submitting again"
                )
        else:
            payload = self._build_payload(task)
            account_id = self._select_account()
            checkpoint = {
                "kind": CHECKPOINT_KIND_PLENX,
                "modality": "video",
                "base_url": self.base_url,
                "stage": "submitting",
                "account_id": account_id,
            }
            # No idempotency support: a restart during POST must not replay it.
            save(checkpoint.copy())
            response = None
            try:
                response = requests.post(
                    f"{self.base_url}/api/v1/veo/generate",
                    headers=self._headers(account_id),
                    files={key: (None, str(value)) for key, value in payload.items()},
                    timeout=self.request_timeout,
                    allow_redirects=False,
                )
                result = self._json_response(response, "submission")
                task_id = self._uuid(result.get("task_id"), "submission task_id")
                if (
                    self._uuid(result.get("account_id"), "submission account_id")
                    != account_id
                ):
                    raise ValueError("Plenx submission returned a different account_id")
                request_id = self._uuid(
                    result.get("request_id"), "submission request_id"
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
            checkpoint.update(stage="submitted", task_id=task_id, request_id=request_id)
            save(checkpoint.copy())

        result = self._poll(checkpoint["task_id"], checkpoint["account_id"])
        status = str(result.get("status", "")).strip().lower()
        if status in FAILURE_STATUSES or result.get("success") is False:
            save(None)
            reason = (
                result.get("error_message")
                or result.get("error")
                or result.get("message")
                or status
            )
            raise RuntimeError(
                f"Plenx video task {checkpoint['task_id']} failed: {self._redact(str(reason))}"
            )
        media_url = result.get("result_url")
        self._validate_media_url(media_url)
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
            },
        }

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
        return {
            "prompt": task.prompt,
            "model": MODEL,
            "aspect_ratio": aspect_ratio,
            "duration": duration,
            "resolution": resolution,
        }

    def _poll(self, task_id: str, account_id: str) -> Dict[str, Any]:
        deadline = time.monotonic() + self.poll_timeout
        failures = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Plenx video task {task_id} exceeded {self.poll_timeout:g}s polling timeout; task is still pending"
                )
            response = None
            try:
                response = requests.get(
                    f"{self.base_url}/api/v1/video/status/{task_id}",
                    headers=self._headers(account_id),
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
                result = self._json_response(response, "video status")
                status = str(result.get("status", "")).strip().lower()
                if status in SUCCESS_STATUSES | FAILURE_STATUSES:
                    return result
                if status not in PENDING_STATUSES:
                    raise RuntimeError(
                        f"Plenx task {task_id} returned unknown status: {self._redact(json.dumps(result))}"
                    )
                delay = self.poll_interval
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))

    def _validate_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        if (
            checkpoint.get("kind") != CHECKPOINT_KIND_PLENX
            or checkpoint.get("modality") != "video"
            or checkpoint.get("base_url") != self.base_url
            or checkpoint.get("stage") not in {"submitting", "submitted"}
        ):
            raise ValueError(
                "Invalid Plenx checkpoint kind, modality, base URL, or stage"
            )
        self._uuid(checkpoint.get("account_id"), "checkpoint account_id")
        if checkpoint["stage"] == "submitted":
            self._uuid(checkpoint.get("task_id"), "checkpoint task_id")
            self._uuid(checkpoint.get("request_id"), "checkpoint request_id")

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
