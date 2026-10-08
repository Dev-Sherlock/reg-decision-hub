"""
OpenRouter provider — a hosted alternative to the local LLM container.

Selected with LLM_PROVIDER=openrouter. Useful when no GGUF is available or
when a larger model is wanted without running it locally.
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

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-2-7b-chat:free")


class OpenRouterProvider(LLMProvider):
    """LLM provider using the OpenRouter API."""

    def __init__(self) -> None:
        self.api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        self.model = DEFAULT_MODEL
        self.timeout = float(os.getenv("LLM_SERVICE_TIMEOUT", "120"))
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self.api_key:
                raise LLMUnavailable("OPENROUTER_API_KEY is not set")
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def ask(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        client = await self._get_client()  # raises LLMUnavailable when unconfigured

        try:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
                        {"role": "user", "content": build_prompt(prompt)},
                    ],
                    "max_tokens": 400,
                    "temperature": 0.3,
                },
            )
        except httpx.HTTPError as error:
            raise LLMUnavailable(f"OpenRouter unreachable: {error}") from error

        if response.status_code >= 400:
            raise LLMUnavailable(
                f"OpenRouter returned {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()["choices"][0]["message"]["content"].strip()
        except (ValueError, KeyError, IndexError) as error:
            raise LLMUnavailable("Unexpected OpenRouter response shape") from error

    async def is_healthy(self) -> bool:
        if not self.api_key:
            return False
        try:
            client = await self._get_client()
            response = await client.get("/models", timeout=10.0)
        except (httpx.HTTPError, LLMUnavailable):
            return False
        return response.status_code == 200

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
