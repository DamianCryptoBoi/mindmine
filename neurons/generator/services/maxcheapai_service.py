import os
import time
from typing import Any, Dict, Optional

import bittensor as bt
import requests

from .base_service import BaseGenerationService, CheckpointFn
from ..task_manager import GenerationTask


API_BASE = "https://maxcheapai.com/api"
CHECKPOINT_KIND_MAXCHEAPAI = "maxcheapai_generation"
IMAGE_MODEL = "nano-banana-pro"
VIDEO_MODEL = "veo-3.1"

IMAGE_RESOLUTIONS = {"1K", "2K", "4K"}
IMAGE_ASPECT_RATIOS = {"16:9", "9:16", "1:1", "2:3", "3:2", "3:4", "4:3"}
VIDEO_RESOLUTION_MAP = {
    "480p": "720p",
    "720p": "720p",
    "1080p": "1080p",
    "4k": "4k",
}
VIDEO_ASPECT_RATIOS = {"16:9", "9:16"}
VIDEO_DURATIONS = (4, 6, 8)
PENDING_STATUSES = {"pending", "processing"}


class _ProviderGenerationFailed(RuntimeError):
    pass


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _nearest_duration(value: Any) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = 4
    return min(VIDEO_DURATIONS, key=lambda duration: abs(duration - requested))


class MaxCheapAIService(BaseGenerationService):
    """Nano Banana Pro and Veo 3.1 generation through MaxCheapAI's REST API."""

    def __init__(self, config: Any = None):
        super().__init__(config)
        self.api_key = os.getenv("MAXCHEAPAI_API_KEY", "").strip()
        self.base_url = os.getenv("MAXCHEAPAI_API_BASE_URL", API_BASE).rstrip("/")
        self.speed = os.getenv("MAXCHEAPAI_SPEED", "priority").strip().lower()
        if self.speed not in {"slow", "normal", "priority"}:
            raise ValueError("MAXCHEAPAI_SPEED must be one of: slow, normal, priority")

        self.request_timeout = _env_float("MAXCHEAPAI_REQUEST_TIMEOUT", 60.0)
        self.poll_interval = _env_float("MAXCHEAPAI_POLL_INTERVAL", 10.0)
        self.poll_timeout = _env_float("MAXCHEAPAI_POLL_TIMEOUT", 1800.0)
        self.download_timeout = _env_float("MAXCHEAPAI_DOWNLOAD_TIMEOUT", 600.0)
        self.max_retries = min(
            3, max(0, int(_env_float("MAXCHEAPAI_MAX_RETRIES", 3.0)))
        )
        self.retry_delay = _env_float("MAXCHEAPAI_RETRY_DELAY", 10.0)

        if self.api_key:
            bt.logging.info("MaxCheapAI service initialized")
        else:
            bt.logging.warning(
                "MAXCHEAPAI_API_KEY is not set; MaxCheapAI service is unavailable"
            )

    def is_available(self) -> bool:
        return bool(self.api_key)

    def supports_modality(self, modality: str) -> bool:
        return modality in {"image", "video"}

    def get_supported_tasks(self) -> Dict[str, list]:
        return {
            "image": ["image_generation"],
            "video": ["text_to_video"],
        }

    def get_api_key_requirements(self) -> Dict[str, str]:
        return {
            "MAXCHEAPAI_API_KEY": (
                "MaxCheapAI API key for Nano Banana Pro and Veo 3.1 generation"
            )
        }

    def process(self, task: GenerationTask) -> Dict[str, Any]:
        return self.process_with_checkpoint(task)

    def process_with_checkpoint(
        self, task: GenerationTask, on_checkpoint: CheckpointFn = None
    ) -> Dict[str, Any]:
        if not self.is_available():
            raise RuntimeError("MaxCheapAI service is unavailable: missing API key")
        if task.modality not in {"image", "video"}:
            raise ValueError(
                f"MaxCheapAI supports image and video, got {task.modality!r}"
            )

        checkpoint_callback = on_checkpoint or (lambda _: None)
        parameters = task.parameters or {}
        poll_interval = float(
            parameters.get("maxcheapai_poll_interval", self.poll_interval)
        )
        poll_timeout = float(
            parameters.get("maxcheapai_poll_timeout", self.poll_timeout)
        )
        model = IMAGE_MODEL if task.modality == "image" else VIDEO_MODEL
        started_at = time.monotonic()

        checkpoint = task.checkpoint
        resumed = checkpoint is not None
        retries_used = 0
        checkpoint_stage = checkpoint.get("stage") if checkpoint else None
        checkpoint_start_frame = checkpoint.get("start_frame") if checkpoint else None
        if checkpoint is not None:
            self._validate_checkpoint(checkpoint, task.modality)
            try:
                retries_used = int(checkpoint.get("retries_used", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("Invalid MaxCheapAI checkpoint retries_used") from exc
            if not 0 <= retries_used <= self.max_retries:
                raise ValueError("Invalid MaxCheapAI checkpoint retries_used")
            if checkpoint.get("retry_pending") and checkpoint_stage != "start_frame":
                checkpoint = None

        payload = self._build_payload(task)
        if task.modality == "video":
            start_frame = None
            if task.checkpoint is None or checkpoint_stage == "start_frame":
                start_frame = self._generate_start_frame(
                    task,
                    checkpoint,
                    checkpoint_callback,
                    poll_interval,
                    poll_timeout,
                )
                checkpoint = None
                retries_used = 0
            elif checkpoint_stage == "video":
                start_frame = checkpoint_start_frame
            if start_frame is not None:
                payload["startFrame"] = start_frame

        while True:
            if checkpoint is not None:
                request_id = checkpoint["request_id"]
                model = checkpoint.get("model", model)
                checkpoint = None
                bt.logging.info(
                    f"Resuming MaxCheapAI {task.modality} request {request_id}"
                )
            else:
                request_id = self._submit(task.modality, payload)
                next_checkpoint = {
                    "kind": CHECKPOINT_KIND_MAXCHEAPAI,
                    "request_id": request_id,
                    "modality": task.modality,
                    "model": model,
                    "retries_used": retries_used,
                }
                if task.modality == "video":
                    next_checkpoint["stage"] = "video"
                    next_checkpoint["start_frame"] = payload.get("startFrame")
                checkpoint_callback(next_checkpoint)

            try:
                status_result = self._poll(
                    task.modality,
                    request_id,
                    poll_interval=poll_interval,
                    poll_timeout=poll_timeout,
                )
                media_url = self._result_url(task.modality, status_result)
                media, mime_type = self._download(media_url, task.modality)
            except _ProviderGenerationFailed:
                if retries_used >= self.max_retries:
                    checkpoint_callback(None)
                    raise
                retries_used += 1
                retry_checkpoint = {
                    "kind": CHECKPOINT_KIND_MAXCHEAPAI,
                    "modality": task.modality,
                    "model": model,
                    "retry_pending": True,
                    "retries_used": retries_used,
                }
                if task.modality == "video":
                    retry_checkpoint["stage"] = "video"
                    retry_checkpoint["start_frame"] = payload.get("startFrame")
                checkpoint_callback(retry_checkpoint)
                bt.logging.warning(
                    f"Retrying MaxCheapAI {task.modality} generation "
                    f"({retries_used}/{self.max_retries})"
                )
                time.sleep(max(0.0, self.retry_delay))
                continue
            except Exception:
                checkpoint_callback(None)
                raise
            else:
                checkpoint_callback(None)
                break

        elapsed = time.monotonic() - started_at
        bt.logging.success(
            f"MaxCheapAI {task.modality} request {request_id} downloaded "
            f"({len(media)} bytes in {elapsed:.1f}s)"
        )
        metadata = {
            "model": model,
            "provider": "maxcheapai",
            "mime_type": mime_type,
            "request_id": request_id,
            "generation_time": elapsed,
        }
        if resumed:
            metadata["resumed"] = True
        return {"data": media, "metadata": metadata}

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _generate_start_frame(
        self,
        task: GenerationTask,
        checkpoint: Optional[Dict[str, Any]],
        checkpoint_callback: CheckpointFn,
        poll_interval: float,
        poll_timeout: float,
    ) -> Optional[Dict[str, Any]]:
        retries_used = int(checkpoint.get("retries_used", 0)) if checkpoint else 0
        if checkpoint and checkpoint.get("retry_pending"):
            checkpoint = None

        while True:
            if checkpoint is not None:
                request_id = checkpoint["request_id"]
                checkpoint = None
            else:
                request_id = self._submit(
                    "image", self._build_start_frame_payload(task)
                )
                checkpoint_callback(
                    {
                        "kind": CHECKPOINT_KIND_MAXCHEAPAI,
                        "request_id": request_id,
                        "modality": "video",
                        "stage": "start_frame",
                        "model": IMAGE_MODEL,
                        "retries_used": retries_used,
                    }
                )

            try:
                result = self._poll(
                    "image",
                    request_id,
                    poll_interval=poll_interval,
                    poll_timeout=poll_timeout,
                )
                return {
                    "id": result.get("id", request_id),
                    "label": "Nano Banana Pro start frame",
                    "url": self._result_url("image", result),
                    "type": "image",
                }
            except _ProviderGenerationFailed:
                if retries_used >= self.max_retries:
                    bt.logging.warning(
                        "MaxCheapAI start frame generation exhausted retries; "
                        "falling back to text-to-video"
                    )
                    return None
                retries_used += 1
                checkpoint_callback(
                    {
                        "kind": CHECKPOINT_KIND_MAXCHEAPAI,
                        "modality": "video",
                        "stage": "start_frame",
                        "model": IMAGE_MODEL,
                        "retry_pending": True,
                        "retries_used": retries_used,
                    }
                )
                bt.logging.warning(
                    "Retrying MaxCheapAI start frame generation "
                    f"({retries_used}/{self.max_retries})"
                )
                time.sleep(max(0.0, self.retry_delay))
            except Exception:
                checkpoint_callback(None)
                raise

    def _build_start_frame_payload(self, task: GenerationTask) -> Dict[str, Any]:
        parameters = task.parameters or {}
        aspect_ratio = parameters.get(
            "aspect_ratio", parameters.get("aspectRatio", "16:9")
        )
        if aspect_ratio not in VIDEO_ASPECT_RATIOS:
            aspect_ratio = "16:9"
        return {
            "modelId": IMAGE_MODEL,
            "prompt": task.prompt,
            "resolution": "1K",
            "speed": "priority",
            "aspectRatio": aspect_ratio,
            "imageCount": 1,
            "autoEnhance": False,
        }

    def _build_payload(self, task: GenerationTask) -> Dict[str, Any]:
        parameters = task.parameters or {}
        if task.modality == "image":
            resolution = str(parameters.get("resolution", "1K")).upper()
            if resolution not in IMAGE_RESOLUTIONS:
                resolution = "1K"
            aspect_ratio = parameters.get(
                "aspect_ratio", parameters.get("aspectRatio", "1:1")
            )
            if aspect_ratio not in IMAGE_ASPECT_RATIOS:
                aspect_ratio = "1:1"
            return {
                "modelId": IMAGE_MODEL,
                "prompt": task.prompt,
                "resolution": resolution,
                "speed": self.speed,
                "aspectRatio": aspect_ratio,
                "imageCount": 1,
                "autoEnhance": False,
            }

        raw_resolution = str(parameters.get("resolution", "720p")).lower()
        resolution = VIDEO_RESOLUTION_MAP.get(raw_resolution, "720p")
        aspect_ratio = parameters.get(
            "aspect_ratio", parameters.get("aspectRatio", "16:9")
        )
        if aspect_ratio not in VIDEO_ASPECT_RATIOS:
            aspect_ratio = "16:9"
        return {
            "modelId": VIDEO_MODEL,
            "prompt": task.prompt,
            "resolution": resolution,
            "duration": _nearest_duration(parameters.get("duration", 4)),
            "speed": self.speed,
            "aspectRatio": aspect_ratio,
            "generateAudio": True,
        }

    def _submit(self, modality: str, payload: Dict[str, Any]) -> Any:
        path = "/generate/image" if modality == "image" else "/generate/video"
        response = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=payload,
            timeout=self.request_timeout,
        )
        result = self._json_response(response, f"{modality} submission")
        request_id = result.get("requestId")
        if request_id is None:
            raise RuntimeError(
                f"MaxCheapAI {modality} submission response omitted requestId"
            )
        bt.logging.info(f"Submitted MaxCheapAI {modality} request {request_id}")
        return request_id

    def _poll(
        self,
        modality: str,
        request_id: Any,
        poll_interval: float,
        poll_timeout: float,
    ) -> Dict[str, Any]:
        path = "generations" if modality == "image" else "video-generations"
        deadline: Optional[float] = None
        if poll_timeout > 0:
            deadline = time.monotonic() + poll_timeout
        timeout_error = (
            f"MaxCheapAI {modality} request {request_id} exceeded "
            f"{poll_timeout:g}s polling timeout"
        )

        while True:
            response = self._get_with_retries(
                f"{self.base_url}/{path}/{request_id}",
                operation=f"{modality} status",
                headers=self._headers(),
                timeout=self.request_timeout,
                deadline=deadline,
                deadline_error=timeout_error,
            )
            self._raise_if_deadline_passed(deadline, timeout_error)
            result = self._json_response(response, f"{modality} status")
            status = str(result.get("status", "")).lower()

            if status == "success":
                return result
            if status == "failed":
                reason = result.get("errorMessage") or "provider reported failure"
                raise _ProviderGenerationFailed(
                    f"MaxCheapAI {modality} request {request_id} failed: {reason}"
                )
            if status not in PENDING_STATUSES:
                raise RuntimeError(
                    f"MaxCheapAI {modality} request {request_id} returned "
                    f"unknown status {status!r}"
                )
            sleep_for = max(0.0, poll_interval)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(timeout_error)
                sleep_for = min(sleep_for, remaining)
            time.sleep(sleep_for)

    def _result_url(self, modality: str, result: Dict[str, Any]) -> str:
        if modality == "video":
            url = result.get("resultVideoUrl")
        else:
            images = result.get("resultImages")
            url = (
                images[0].get("url") if images and isinstance(images[0], dict) else None
            )
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError(
                f"MaxCheapAI {modality} success response omitted the media URL"
            )
        return url

    def _download(self, url: str, modality: str) -> tuple[bytes, str]:
        response = self._get_with_retries(
            url,
            operation=f"{modality} download",
            timeout=self.download_timeout,
        )
        media = response.content
        if not media:
            raise RuntimeError(f"MaxCheapAI {modality} download returned empty media")
        default_mime = "image/jpeg" if modality == "image" else "video/mp4"
        mime_type = response.headers.get("Content-Type", default_mime).split(";", 1)[0]
        return media, mime_type

    def _get_with_retries(
        self,
        url: str,
        operation: str,
        timeout: float,
        headers: Optional[Dict[str, str]] = None,
        deadline: Optional[float] = None,
        deadline_error: Optional[str] = None,
    ) -> Any:
        for attempt in range(self.max_retries + 1):
            request_timeout = timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(deadline_error or "MaxCheapAI deadline exceeded")
                request_timeout = min(timeout, remaining)
            try:
                response = requests.get(url, headers=headers, timeout=request_timeout)
                self._raise_for_status(response, operation)
                return response
            except (requests.RequestException, RuntimeError):
                if attempt >= self.max_retries:
                    raise
                bt.logging.warning(
                    f"Retrying MaxCheapAI {operation} request "
                    f"({attempt + 1}/{self.max_retries})"
                )
                sleep_for = max(0.0, self.retry_delay)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            deadline_error or "MaxCheapAI deadline exceeded"
                        )
                    sleep_for = min(sleep_for, remaining)
                time.sleep(sleep_for)

        raise RuntimeError(f"MaxCheapAI {operation} exhausted retries")

    @staticmethod
    def _raise_if_deadline_passed(
        deadline: Optional[float], error_message: str
    ) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(error_message)

    def _json_response(self, response: Any, operation: str) -> Dict[str, Any]:
        self._raise_for_status(response, operation)
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError(f"MaxCheapAI {operation} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError(
                f"MaxCheapAI {operation} returned {type(result).__name__}, expected object"
            )
        return result

    @staticmethod
    def _raise_for_status(response: Any, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        detail = response.text or "empty response"
        raise RuntimeError(
            f"MaxCheapAI {operation} failed: {response.status_code} - {detail}"
        )

    @staticmethod
    def _validate_checkpoint(checkpoint: Dict[str, Any], modality: str) -> None:
        if checkpoint.get("kind") != CHECKPOINT_KIND_MAXCHEAPAI:
            raise ValueError(
                f"Unsupported MaxCheapAI checkpoint kind {checkpoint.get('kind')!r}"
            )
        if not checkpoint.get("retry_pending") and checkpoint.get("request_id") is None:
            raise ValueError("MaxCheapAI checkpoint omitted request_id")
        if checkpoint.get("modality") != modality:
            raise ValueError(
                "MaxCheapAI checkpoint modality does not match the restored task"
            )
