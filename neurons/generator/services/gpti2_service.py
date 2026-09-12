import base64
import binascii
import os
import time
from typing import Any, Dict, Optional

import requests

from ..task_manager import GenerationTask
from .base_service import BaseGenerationService, CheckpointFn

API_BASE = "https://gpti2.store/v1"
DEFAULT_MODEL = "gpt-image-2.5-flare"
CHECKPOINT_KIND_GPTI2 = "gpti2_generation"
PENDING_STATUSES = {"queued", "running"}
SIZE_MAP = {
    "1:1": {"1K": "1024x1024", "2K": "1536x1536", "4K": "2048x2048"},
    "16:9": {"1K": "1280x720", "2K": "2560x1440", "4K": "3840x2160"},
    "9:16": {"1K": "720x1280", "2K": "1440x2560", "4K": "2160x3840"},
    "4:3": {"1K": "1024x768", "2K": "2048x1536", "4K": "3200x2400"},
    "3:4": {"1K": "768x1024", "2K": "1536x2048", "4K": "2400x3200"},
    "3:2": {"1K": "1536x1024", "2K": "2400x1600", "4K": "3360x2240"},
    "2:3": {"1K": "1024x1536", "2K": "1600x2400", "4K": "2240x3360"},
    "21:9": {"1K": "1280x544", "2K": "2560x1088", "4K": "3840x1632"},
}


class GPTi2Service(BaseGenerationService):
    """GPT Image generation through GPTi2's OpenAI-compatible API."""

    def __init__(self, config: Any = None):
        super().__init__(config)
        self.api_key = os.getenv("GPTI2_API_KEY", "").strip()
        self.model = os.getenv("GPTI2_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.base_url = os.getenv("GPTI2_API_BASE_URL", API_BASE).rstrip("/")
        self.request_timeout = float(os.getenv("GPTI2_REQUEST_TIMEOUT", "1800"))
        self.poll_interval = float(os.getenv("GPTI2_POLL_INTERVAL", "5"))
        self.poll_timeout = float(os.getenv("GPTI2_POLL_TIMEOUT", "1800"))
        self.download_timeout = float(os.getenv("GPTI2_DOWNLOAD_TIMEOUT", "600"))

    def is_available(self) -> bool:
        return bool(self.api_key)

    def supports_modality(self, modality: str) -> bool:
        return modality == "image"

    def get_supported_tasks(self) -> Dict[str, list]:
        return {"image": ["image_generation"], "video": []}

    def get_api_key_requirements(self) -> Dict[str, str]:
        return {"GPTI2_API_KEY": "GPTi2 API key for GPT Image generation"}

    def process(self, task: GenerationTask) -> Dict[str, Any]:
        return self.process_with_checkpoint(task)

    def process_with_checkpoint(
        self, task: GenerationTask, on_checkpoint: CheckpointFn = None
    ) -> Dict[str, Any]:
        if not self.is_available():
            raise RuntimeError("GPTi2 service is unavailable: missing API key")
        if task.modality != "image":
            raise ValueError(f"GPTi2 supports image generation, got {task.modality!r}")

        checkpoint_callback = on_checkpoint or (lambda _: None)
        checkpoint = task.checkpoint
        if checkpoint:
            return self._resume_job(checkpoint, checkpoint_callback)

        payload = self._build_payload(task)
        if payload["quality"] == "high":
            return self._generate_job(task, payload, checkpoint_callback)
        return self._generate_sync(task, payload)

    def _build_payload(self, task: GenerationTask) -> Dict[str, Any]:
        parameters = task.parameters or {}
        resolution = str(parameters.get("resolution", "1K")).upper()
        if resolution not in {"1K", "2K", "4K"}:
            resolution = "1K"
        aspect_ratio = parameters.get(
            "aspect_ratio", parameters.get("aspectRatio", "1:1")
        )
        if aspect_ratio not in SIZE_MAP:
            aspect_ratio = "1:1"
        quality = str(parameters.get("quality", "low")).lower()
        if quality not in {"low", "medium", "high"}:
            quality = "low"
        background = str(parameters.get("background", "opaque")).lower()
        if background not in {"transparent", "opaque", "auto"}:
            background = "opaque"
        return {
            "model": self.model,
            "prompt": task.prompt,
            "size": SIZE_MAP[aspect_ratio][resolution],
            "quality": quality,
            "background": background,
            "n": 1,
        }

    def _generate_sync(
        self, task: GenerationTask, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/images/generations",
            headers=self._headers(task.task_id),
            json=payload,
            timeout=self.request_timeout,
        )
        result = self._json_response(response, "generation")
        try:
            encoded = result["data"][0]["b64_json"]
            media = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
            raise RuntimeError("GPTi2 generation returned invalid image data") from exc
        if not media:
            raise RuntimeError("GPTi2 generation returned empty image data")
        return self._result(
            media,
            result.get("size", payload["size"]),
            result.get("quality", payload["quality"]),
            "image/png",
        )

    def _generate_job(
        self,
        task: GenerationTask,
        payload: Dict[str, Any],
        checkpoint_callback,
    ) -> Dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/images/jobs",
            headers=self._headers(task.task_id),
            json=payload,
            timeout=self.request_timeout,
        )
        result = self._json_response(response, "job submission")
        job_id = result.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("GPTi2 job submission omitted the job id")
        checkpoint_callback(
            {
                "kind": CHECKPOINT_KIND_GPTI2,
                "job_id": job_id,
                "size": payload["size"],
                "quality": payload["quality"],
            }
        )
        return self._finish_job(
            job_id,
            payload["size"],
            payload["quality"],
            checkpoint_callback,
            initial=result,
        )

    def _resume_job(
        self, checkpoint: Dict[str, Any], checkpoint_callback
    ) -> Dict[str, Any]:
        if checkpoint.get("kind") != CHECKPOINT_KIND_GPTI2:
            raise ValueError("Invalid GPTi2 checkpoint kind")
        job_id = checkpoint.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("Invalid GPTi2 checkpoint job id")
        result = self._finish_job(
            job_id,
            str(checkpoint.get("size", "1024x1024")),
            str(checkpoint.get("quality", "high")),
            checkpoint_callback,
        )
        result["metadata"]["resumed"] = True
        return result

    def _finish_job(
        self,
        job_id: str,
        size: str,
        quality: str,
        checkpoint_callback,
        initial: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = initial or {}
        deadline = time.monotonic() + self.poll_timeout
        while str(result.get("status", "queued")).lower() in PENDING_STATUSES:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"GPTi2 job {job_id} exceeded "
                    f"{self.poll_timeout:g}s polling timeout"
                )
            response = requests.get(
                f"{self.base_url}/images/jobs/{job_id}",
                headers=self._auth_headers(),
                timeout=self.request_timeout,
            )
            result = self._json_response(response, "job status")
            if str(result.get("status", "")).lower() in PENDING_STATUSES:
                time.sleep(max(0.0, self.poll_interval))

        status = str(result.get("status", "")).lower()
        if status != "succeeded":
            error = result.get("error", "unknown provider error")
            checkpoint_callback(None)
            raise RuntimeError(f"GPTi2 job {job_id} failed: {error}")
        try:
            media_url = result["data"][0]["url"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("GPTi2 job response omitted the image URL") from exc
        if not isinstance(media_url, str) or not media_url.startswith("https://"):
            raise RuntimeError("GPTi2 job response returned an invalid image URL")
        download = requests.get(media_url, headers={}, timeout=self.download_timeout)
        self._raise_for_status(download, "image download")
        if not download.content:
            raise RuntimeError("GPTi2 image download returned empty media")
        checkpoint_callback(None)
        mime_type = download.headers.get("Content-Type", "image/png").split(";", 1)[0]
        return self._result(
            download.content,
            result.get("size", size),
            result.get("quality", quality),
            mime_type,
            job_id,
        )

    def _headers(self, idempotency_key: str) -> Dict[str, str]:
        return {
            **self._auth_headers(),
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _result(
        self,
        media: bytes,
        size: str,
        quality: str,
        mime_type: str,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        metadata = {
            "model": self.model,
            "provider": "gpti2",
            "mime_type": mime_type,
            "size": size,
            "quality": quality,
        }
        if job_id:
            metadata["job_id"] = job_id
        return {"data": media, "metadata": metadata}

    @classmethod
    def _json_response(cls, response: Any, operation: str) -> Dict[str, Any]:
        cls._raise_for_status(response, operation)
        try:
            result = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"GPTi2 {operation} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError(f"GPTi2 {operation} returned invalid JSON")
        return result

    @staticmethod
    def _raise_for_status(response: Any, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        raise RuntimeError(
            f"GPTi2 {operation} failed: {response.status_code} - "
            f"{response.text or 'empty response'}"
        )
