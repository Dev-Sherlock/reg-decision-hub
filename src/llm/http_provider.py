"""
HTTP bridge to the LLM service.

The LLM runs in its own container (llama-cpp-python by default) and is reached
over HTTP at LLM_SERVICE_URL. Nothing is loaded in-process here — that was the
bug this replaces: the API container never mounts ./models, so an in-process
provider could only ever fail.
"""
import logging
import os
from typing import Optional

import httpx

from src.llm.base import (
    SYSTEM_PROMPT,
    LLMProvider,
    LLMUnavailable,
    build_prompt,
)

logger = logging.getLogger(__name__)


class HttpLLMProvider(LLMProvider):
    """Proxies prompts to the LLM service's /ask endpoint."""

    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_SERVICE_URL", "http://llm:8001").rstrip("/")
        self.timeout = float(os.getenv("LLM_SERVICE_TIMEOUT", "120"))
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client

    async def ask(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        client = await self._get_client()
        try:
            response = await client.post(
                "/ask",
                json={"prompt": build_prompt(prompt), "system": system_prompt or SYSTEM_PROMPT},
            )
        except httpx.HTTPError as error:
            raise LLMUnavailable(f"LLM service at {self.base_url} unreachable: {error}") from error

        if response.status_code == 503:
            raise LLMUnavailable("LLM service has no model loaded")
        if response.status_code >= 400:
            raise LLMUnavailable(
                f"LLM service returned {response.status_code}: {response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as error:
            raise LLMUnavailable("LLM service returned a non-JSON response") from error

        text = data.get("response")
        if not isinstance(text, str):
            raise LLMUnavailable("LLM service response had no 'response' field")
        return text.strip()

    async def is_healthy(self) -> bool:
        client = await self._get_client()
        try:
            response = await client.get("/health", timeout=10.0)
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        try:
            return response.json().get("status") == "healthy"
        except ValueError:
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
