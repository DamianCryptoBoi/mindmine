import os
import time
from typing import Any, Callable, Dict, Optional

import bittensor as bt
import requests

from .base_service import BaseGenerationService, CheckpointFn
from ..task_manager import GenerationTask


API_BASE = "http://107.178.109.250:8686/v1"
MODEL = "nano-banana-pro"
CHECKPOINT_KIND_VERTEXGEN = "vertexgen_generation"
IMAGE_RESOLUTIONS = {"1K", "2K", "4K"}
IMAGE_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "2:3", "3:2", "3:4", "4:3"}
PENDING_STATUSES = {"queued", "pending", "running", "processing"}
SUCCESS_STATUSES = {"success", "succeeded", "completed"}


class _ProviderGenerationFailed(RuntimeError):
    pass


class VertexGenService(BaseGenerationService):
    """C2PA-preserving Nano Banana Pro image generation through VertexGen."""

    def __init__(self, config: Any = None):
        super().__init__(config)
        self.api_key = os.getenv("VERTEXGEN_API_KEY", "").strip()
        self.base_url = os.getenv("VERTEXGEN_API_BASE_URL", API_BASE).rstrip("/")
        self.wait_seconds = min(
            30, max(0, int(os.getenv("VERTEXGEN_WAIT_SECONDS", "20")))
        )
        self.request_timeout = float(os.getenv("VERTEXGEN_REQUEST_TIMEOUT", "60"))
        self.poll_interval = float(os.getenv("VERTEXGEN_POLL_INTERVAL", "2"))
        self.poll_timeout = float(os.getenv("VERTEXGEN_POLL_TIMEOUT", "600"))
        self.download_timeout = float(os.getenv("VERTEXGEN_DOWNLOAD_TIMEOUT", "600"))
        self.max_retries = max(0, int(os.getenv("VERTEXGEN_MAX_RETRIES", "3")))
        self.retry_delay = float(os.getenv("VERTEXGEN_RETRY_DELAY", "10"))

    def is_available(self) -> bool:
        return bool(self.api_key)

    def supports_modality(self, modality: str) -> bool:
        return modality == "image"

    def get_supported_tasks(self) -> Dict[str, list]:
        return {"image": ["image_generation"], "video": []}

    def get_api_key_requirements(self) -> Dict[str, str]:
        return {"VERTEXGEN_API_KEY": "VertexGen API key for image generation"}

    def process(self, task: GenerationTask) -> Dict[str, Any]:
        return self.process_with_checkpoint(task)

    def process_with_checkpoint(
        self, task: GenerationTask, on_checkpoint: CheckpointFn = None
    ) -> Dict[str, Any]:
        if not self.is_available():
            raise RuntimeError("VertexGen service is unavailable: missing API key")
        if task.modality != "image":
            raise ValueError(
                f"VertexGen supports image generation, got {task.modality!r}"
            )

        checkpoint_callback = on_checkpoint or (lambda _: None)
        started_at = time.monotonic()
        payload = self._build_payload(task)
        resumed = task.checkpoint is not None
        result = None
        if resumed:
            checkpoint = task.checkpoint or {}
            if checkpoint.get("kind") != CHECKPOINT_KIND_VERTEXGEN:
                raise ValueError("Invalid VertexGen checkpoint kind")
            try:
                attempt = int(checkpoint.get("attempt", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid VertexGen checkpoint attempt") from exc
            if not 0 <= attempt <= self.max_retries:
                raise ValueError("Invalid VertexGen checkpoint attempt")
            if not checkpoint.get("retry_pending"):
                job_id = checkpoint.get("job_id")
                if not isinstance(job_id, str) or not job_id:
                    raise ValueError("Invalid VertexGen checkpoint job id")
                result = {"id": job_id, "status": "running"}
                bt.logging.info(f"Resuming VertexGen image job {job_id}")
        else:
            attempt = 0
            checkpoint_callback(
                {
                    "kind": CHECKPOINT_KIND_VERTEXGEN,
                    "attempt": attempt,
                    "model": MODEL,
                    "retry_pending": True,
                }
            )

        while True:
            try:
                if result is None:
                    result = self._submit(task.task_id, attempt, payload)
                    job_id = result.get("id")
                    if not isinstance(job_id, str) or not job_id:
                        raise RuntimeError(
                            "VertexGen generation response omitted the job id"
                        )
                    checkpoint_callback(
                        {
                            "kind": CHECKPOINT_KIND_VERTEXGEN,
                            "job_id": job_id,
                            "attempt": attempt,
                            "model": MODEL,
                        }
                    )

                status = str(result.get("status", "")).lower()
                if status in PENDING_STATUSES:
                    self._poll(job_id)
                elif status == "failed":
                    reason = self._failure_reason(result)
                    raise _ProviderGenerationFailed(
                        f"VertexGen job {job_id} failed: {reason}"
                    )
                elif status not in SUCCESS_STATUSES:
                    raise RuntimeError(
                        f"VertexGen job {job_id} returned unknown status {status!r}"
                    )
                media, mime_type = self._download(job_id)
            except _ProviderGenerationFailed:
                if attempt >= self.max_retries:
                    checkpoint_callback(None)
                    raise
                attempt += 1
                checkpoint_callback(
                    {
                        "kind": CHECKPOINT_KIND_VERTEXGEN,
                        "attempt": attempt,
                        "model": MODEL,
                        "retry_pending": True,
                    }
                )
                bt.logging.warning(
                    f"Retrying VertexGen image generation "
                    f"({attempt}/{self.max_retries})"
                )
                time.sleep(max(0.0, self.retry_delay))
                result = None
                continue
            else:
                checkpoint_callback(None)
                break

        elapsed = time.monotonic() - started_at
        bt.logging.success(
            f"VertexGen image job {job_id} downloaded "
            f"({len(media)} bytes in {elapsed:.1f}s)"
        )
        metadata = {
            "model": MODEL,
            "provider": "vertexgen",
            "mime_type": mime_type,
            "job_id": job_id,
            "generation_time": elapsed,
        }
        if resumed:
            metadata["resumed"] = True
        return {
            "data": media,
            "metadata": metadata,
        }

    def _build_payload(self, task: GenerationTask) -> Dict[str, str]:
        parameters = task.parameters or {}
        resolution = str(parameters.get("resolution", "1K")).upper()
        if resolution not in IMAGE_RESOLUTIONS:
            resolution = "1K"
        aspect_ratio = parameters.get(
            "aspect_ratio", parameters.get("aspectRatio", "1:1")
        )
        if aspect_ratio not in IMAGE_ASPECT_RATIOS:
            aspect_ratio = "1:1"
        return {
            "prompt": task.prompt,
            "model": MODEL,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
        }

    def _submit(
        self, task_id: str, attempt: int, payload: Dict[str, str]
    ) -> Dict[str, Any]:
        response = self._request_with_retries(
            lambda timeout: requests.post(
                f"{self.base_url}/images/generations",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": f"{task_id}-{attempt}",
                },
                params={"wait": self.wait_seconds},
                json=payload,
                timeout=timeout,
            ),
            "generation submission",
            self.request_timeout,
        )
        return self._json_response(response, "generation submission")

    def _poll(self, job_id: str) -> Dict[str, Any]:
        deadline = (
            time.monotonic() + self.poll_timeout if self.poll_timeout > 0 else None
        )
        timeout_error = (
            f"VertexGen job {job_id} exceeded {self.poll_timeout:g}s polling timeout"
        )
        while True:
            response = self._request_with_retries(
                lambda timeout: requests.get(
                    f"{self.base_url}/images/generations/{job_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=timeout,
                ),
                "generation status",
                self.request_timeout,
                deadline,
                timeout_error,
            )
            result = self._json_response(response, "generation status")
            status = str(result.get("status", "")).lower()
            if status in SUCCESS_STATUSES:
                return result
            if status == "failed":
                reason = self._failure_reason(result)
                raise _ProviderGenerationFailed(
                    f"VertexGen job {job_id} failed: {reason}"
                )
            if status not in PENDING_STATUSES:
                raise RuntimeError(
                    f"VertexGen job {job_id} returned unknown status {status!r}"
                )
            sleep_for = max(0.0, self.poll_interval)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(timeout_error)
                sleep_for = min(sleep_for, remaining)
            time.sleep(sleep_for)

    def _download(self, job_id: str) -> tuple[bytes, str]:
        response = self._request_with_retries(
            lambda timeout: requests.get(
                f"{self.base_url}/images/generations/{job_id}/content",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=timeout,
            ),
            "image download",
            self.download_timeout,
        )
        self._raise_for_status(response, "image download")
        if not response.content:
            raise RuntimeError("VertexGen image download returned empty media")
        mime_type = response.headers.get("Content-Type", "image/png").split(";", 1)[0]
        return response.content, mime_type

    def _request_with_retries(
        self,
        request: Callable[[float], Any],
        operation: str,
        timeout: float,
        deadline: Optional[float] = None,
        deadline_error: Optional[str] = None,
    ) -> Any:
        for retry in range(self.max_retries + 1):
            request_timeout = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(deadline_error or "VertexGen deadline exceeded")
                request_timeout = min(request_timeout, remaining)
            try:
                response = request(request_timeout)
            except requests.RequestException:
                if retry >= self.max_retries:
                    raise
            else:
                is_transient = response.status_code == 429 or (
                    500 <= response.status_code < 600
                    and self._error_details(response).get("code")
                    != "provider_unavailable"
                )
                if not is_transient or retry >= self.max_retries:
                    return response

            bt.logging.warning(
                f"Retrying transient VertexGen {operation} "
                f"({retry + 1}/{self.max_retries})"
            )
            sleep_for = max(0.0, self.retry_delay)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(deadline_error or "VertexGen deadline exceeded")
                sleep_for = min(sleep_for, remaining)
            time.sleep(sleep_for)

        raise RuntimeError(f"VertexGen {operation} exhausted retries")

    @classmethod
    def _json_response(cls, response: Any, operation: str) -> Dict[str, Any]:
        if not 200 <= response.status_code < 300:
            error = cls._error_details(response)
            if error.get("code") == "provider_unavailable":
                reason = error.get("message") or "provider unavailable"
                raise _ProviderGenerationFailed(
                    f"VertexGen {operation} failed: {reason}"
                )
        cls._raise_for_status(response, operation)
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(f"VertexGen {operation} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"VertexGen {operation} returned invalid JSON")
        return result

    @staticmethod
    def _error_details(response: Any) -> Dict[str, Any]:
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        error = payload.get("error")
        return error if isinstance(error, dict) else {}

    @staticmethod
    def _failure_reason(result: Dict[str, Any]) -> str:
        error = result.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        elif isinstance(error, str) and error.strip():
            return error
        return "provider reported failure"

    @staticmethod
    def _raise_for_status(response: Any, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        raise RuntimeError(
            f"VertexGen {operation} failed: {response.status_code} - "
            f"{response.text or 'empty response'}"
        )
