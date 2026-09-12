import os
import time
from typing import Any, Dict
from urllib.parse import urlparse

import bittensor as bt
import requests

from .base_service import BaseGenerationService
from ..task_manager import GenerationTask


API_BASE = "https://api.xah.io/v1"
PRIMARY_MODEL = "wowztools/nano-banana-pro"
FALLBACK_MODEL = "wowztools/Nano-Banana-2"
RESOLUTION_SIZES = {
    "1K": "1024x1024",
    "2K": "2048x2048",
    "4K": "4096x4096",
}
RETRIES_PER_MODEL = 3


class CKeyService(BaseGenerationService):
    """Image generation through CKey's OpenAI-compatible API."""

    def __init__(self, config: Any = None):
        super().__init__(config)
        self.api_key = os.getenv("CKEY_API_KEY", "").strip()
        self.base_url = os.getenv("CKEY_API_BASE_URL", API_BASE).rstrip("/")
        self.request_timeout = float(os.getenv("CKEY_REQUEST_TIMEOUT", "1800"))
        self.download_timeout = float(os.getenv("CKEY_DOWNLOAD_TIMEOUT", "600"))
        self.retry_delay = float(os.getenv("CKEY_RETRY_DELAY", "10"))

    def is_available(self) -> bool:
        return bool(self.api_key)

    def supports_modality(self, modality: str) -> bool:
        return modality == "image"

    def get_supported_tasks(self) -> Dict[str, list]:
        return {"image": ["image_generation"], "video": []}

    def get_api_key_requirements(self) -> Dict[str, str]:
        return {"CKEY_API_KEY": "CKey API key for Google image generation"}

    def process(self, task: GenerationTask) -> Dict[str, Any]:
        if not self.is_available():
            raise RuntimeError("CKey service is unavailable: missing API key")
        if task.modality != "image":
            raise ValueError(f"CKey supports image generation, got {task.modality!r}")

        resolution = str((task.parameters or {}).get("resolution", "1K")).upper()
        size = RESOLUTION_SIZES.get(resolution, RESOLUTION_SIZES["1K"])
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        models = (PRIMARY_MODEL, FALLBACK_MODEL)
        for model_index, model in enumerate(models):
            for attempt in range(RETRIES_PER_MODEL + 1):
                try:
                    return self._generate(task, model, size, headers)
                except (requests.RequestException, RuntimeError):
                    is_last = (
                        model_index == len(models) - 1
                        and attempt == RETRIES_PER_MODEL
                    )
                    if is_last:
                        raise
                    bt.logging.warning(
                        f"CKey {model} attempt {attempt + 1} failed; retrying"
                    )
                    time.sleep(max(0.0, self.retry_delay))

        raise RuntimeError("CKey image generation exhausted all attempts")

    def _generate(
        self,
        task: GenerationTask,
        model: str,
        size: str,
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/images/generations",
            headers=headers,
            json={"model": model, "prompt": task.prompt, "n": 1, "size": size},
            timeout=self.request_timeout,
        )
        self._raise_for_status(response, "generation")
        try:
            media_url = response.json()["data"][0]["url"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "CKey generation response omitted the image URL"
            ) from exc
        if not isinstance(media_url, str) or not media_url.strip():
            raise RuntimeError("CKey generation response omitted the image URL")
        self._validate_media_url(media_url)

        download = requests.get(
            media_url,
            timeout=self.download_timeout,
            allow_redirects=False,
        )
        self._raise_for_status(download, "download")
        if not download.content:
            raise RuntimeError("CKey download returned empty media")
        mime_type = download.headers.get("Content-Type", "image/jpeg").split(";", 1)[0]
        bt.logging.success(
            f"CKey image generated with {model} ({len(download.content)} bytes)"
        )
        return {
            "data": download.content,
            "metadata": {
                "model": model,
                "provider": "ckey",
                "mime_type": mime_type,
            },
        }

    @staticmethod
    def _raise_for_status(response: Any, operation: str) -> None:
        if 200 <= response.status_code < 300:
            return
        raise RuntimeError(
            f"CKey {operation} failed: {response.status_code} - "
            f"{response.text or 'empty response'}"
        )

    @staticmethod
    def _validate_media_url(url: str) -> None:
        try:
            parsed = urlparse(url)
            trusted = (
                parsed.scheme.lower() == "https"
                and parsed.hostname is not None
                and parsed.port in (None, 443)
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            trusted = False
        if not trusted:
            raise RuntimeError("CKey refused untrusted media URL")
