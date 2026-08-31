import base64
import math
import os
import time
from typing import Any, Dict

import bittensor as bt
import google.auth
from google import genai
from google.genai import types

from .base_service import BaseGenerationService
from ..task_manager import GenerationTask

MODEL = "gemini-3.1-flash-lite-image"
IMAGE_TIERS = {"1K", "2K", "4K"}
MAX_RETRIES = 3


class VertexAIService(BaseGenerationService):
    """Google image generation through Vertex AI and ADC."""

    def __init__(self, config: Any = None):
        super().__init__(config)
        project = (
            os.getenv("VERTEXAI_PROJECT", "").strip()
            or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        )
        if not project:
            _, project = google.auth.default()
        if not project:
            raise RuntimeError("Vertex AI could not resolve a Google Cloud project")

        self.project = project
        self.location = os.getenv("VERTEXAI_LOCATION", "global").strip() or "global"
        self.retry_delay = float(os.getenv("VERTEXAI_RETRY_DELAY", "10"))
        self.request_timeout = float(os.getenv("VERTEXAI_REQUEST_TIMEOUT", "600"))
        if not math.isfinite(self.retry_delay) or self.retry_delay < 0:
            raise ValueError("VERTEXAI_RETRY_DELAY must be a non-negative number")
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ValueError("VERTEXAI_REQUEST_TIMEOUT must be a positive number")
        self.client = genai.Client(
            vertexai=True,
            project=self.project,
            location=self.location,
            http_options=types.HttpOptions(
                timeout=max(1, int(self.request_timeout * 1000))
            ),
        )

    def is_available(self) -> bool:
        return self.client is not None

    def supports_modality(self, modality: str) -> bool:
        return modality == "image"

    def get_supported_tasks(self) -> Dict[str, list]:
        return {"image": ["image_generation"], "video": []}

    def get_api_key_requirements(self) -> Dict[str, str]:
        return {
            "VERTEXAI_PROJECT": "Google Cloud project; optional when ADC resolves it",
            "VERTEXAI_LOCATION": "Vertex AI location (default: global)",
            "VERTEXAI_REQUEST_TIMEOUT": "Vertex request timeout in seconds (default: 600)",
        }

    def process(self, task: GenerationTask) -> Dict[str, Any]:
        if task.modality != "image":
            raise ValueError(
                f"Vertex AI supports image generation, got {task.modality!r}"
            )

        resolution = str((task.parameters or {}).get("resolution", "1K")).upper()
        if resolution not in IMAGE_TIERS:
            resolution = "1K"
        request = {
            "model": MODEL,
            "contents": [
                types.Content(
                    role="user", parts=[types.Part.from_text(text=task.prompt)]
                )
            ],
            "config": types.GenerateContentConfig(
                temperature=1,
                top_p=0.95,
                max_output_tokens=32768,
                response_modalities=["TEXT", "IMAGE"],
                safety_settings=[
                    types.SafetySetting(
                        category="HARM_CATEGORY_IMAGE_HATE", threshold="OFF"
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT",
                        threshold="OFF",
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_IMAGE_HARASSMENT", threshold="OFF"
                    ),
                    types.SafetySetting(
                        category="HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT",
                        threshold="OFF",
                    ),
                ],
                image_config=types.ImageConfig(
                    aspect_ratio="1:1",
                    image_size=resolution,
                    output_mime_type="image/jpeg",
                ),
                thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
            ),
        }
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = self.client.models.generate_content(**request)
                image_data, mime_type = self._extract_image(response)
                break
            except Exception as exc:
                if attempt == MAX_RETRIES:
                    raise
                bt.logging.warning(
                    f"Vertex AI attempt {attempt + 1} failed: {exc}; retrying"
                )
                time.sleep(self.retry_delay)
        bt.logging.success(
            f"Vertex AI image generated with {MODEL} ({len(image_data)} bytes)"
        )
        return {
            "data": image_data,
            "metadata": {
                "model": MODEL,
                "provider": "vertexai",
                "mime_type": mime_type,
            },
        }

    @staticmethod
    def _extract_image(response: Any) -> tuple[bytes, str]:
        for candidate in response.candidates or []:
            content = candidate.content
            for part in (content.parts if content else []) or []:
                inline_data = part.inline_data
                if inline_data and inline_data.data:
                    data = inline_data.data
                    if isinstance(data, str):
                        data = base64.b64decode(data)
                    return bytes(data), inline_data.mime_type or "image/jpeg"
        raise RuntimeError("Vertex AI response contained no image")
