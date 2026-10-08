"""
LLM provider abstraction.

The LLM is a separate service reached over HTTP — it runs in its own
container because the API container does not (and should not) mount the model
weights. Two providers are available, selected by LLM_PROVIDER:

    http       (default) proxy to LLM_SERVICE_URL, e.g. the llama container
    openrouter call the OpenRouter API directly

The provider is a thin completion interface. Deciding *what* context the model
gets is the API's job — see routes/agent.py — not the provider's.
"""
from abc import ABC, abstractmethod
from typing import List, Optional
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_URL = os.getenv("LLM_SERVICE_URL", "http://llm:8001")

SYSTEM_PROMPT = (
    "You are a reasoning agent with access to a symbolic memory. Use the "
    "provided facts to inform your response. When you learn new facts, state "
    "them clearly.\n\n"
    "Guidelines:\n"
    "- Ground every claim in the facts you were given; say so plainly when the "
    "facts do not answer the question\n"
    "- Do not invent facts. If you believe something new, state it as a "
    "candidate for storage rather than as established knowledge\n"
    "- Keep it concise and factual"
)


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def ask(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Answer a prompt. Raises LLMUnavailable if the backend cannot be reached."""

    @abstractmethod
    async def is_healthy(self) -> bool:
        """Check whether the backend is available."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""


class LLMUnavailable(RuntimeError):
    """The LLM backend could not be reached or returned an error."""


def build_prompt(prompt: str, context: Optional[str] = None) -> str:
    """Assemble the final user message from the memory context and the prompt."""
    if not context:
        return prompt
    return f"Known facts:\n{context}\n\nQuestion: {prompt}"


def format_facts_for_context(facts: List[dict]) -> str:
    """Render retrieved facts as one 'subject.predicate = value' line each."""
    from src.api.embeddings import render_value

    lines = []
    for fact in facts:
        subject = fact.get("subject", "?")
        predicate = fact.get("predicate", "?")
        value = render_value(fact.get("value"))
        similarity = fact.get("similarity")
        suffix = f" (relevance {similarity:.2f})" if isinstance(similarity, (int, float)) else ""
        lines.append(f"- {subject}.{predicate} = {value}{suffix}")
    return "\n".join(lines) or "(no facts matched)"


_llm_provider: Optional[LLMProvider] = None


def get_llm_provider() -> LLMProvider:
    """Get the configured LLM provider (singleton)."""
    global _llm_provider
    if _llm_provider is None:
        choice = os.getenv("LLM_PROVIDER", "http").strip().lower()
        if choice == "openrouter":
            from src.llm.openrouter_provider import OpenRouterProvider

            _llm_provider = OpenRouterProvider()
        elif choice == "http":
            from src.llm.http_provider import HttpLLMProvider

            _llm_provider = HttpLLMProvider()
        else:
            logger.warning(
                "Unknown LLM_PROVIDER '%s'; falling back to the HTTP bridge", choice
            )
            from src.llm.http_provider import HttpLLMProvider

            _llm_provider = HttpLLMProvider()
        logger.info("LLM provider: %s", type(_llm_provider).__name__)
    return _llm_provider


async def close_llm_provider() -> None:
    """Close the LLM provider on shutdown."""
    global _llm_provider
    if _llm_provider is not None:
        await _llm_provider.close()
        _llm_provider = None
