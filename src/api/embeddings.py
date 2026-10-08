"""
Semantic embeddings for fact storage and retrieval.

Facts are embedded with sentence-transformers and indexed in pgvector, so
/agent/ask and /memory/search can find relevant facts by meaning rather than by
exact subject/predicate match.

The model is loaded lazily. Loading ~80MB of weights and warming the model is
wasteful for an API that is only ever asked to store facts, and it means a
failed model download (offline host, no HF cache) does not take the whole API
down — see AGENTS.md "Running without the embedding model".
"""
import asyncio
import logging
import os
import time
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv(
    "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)
DEFAULT_DIM = int(os.getenv("EMBEDDING_DIM", "384"))

# How long to wait before retrying a failed model load.
LOAD_RETRY_COOLDOWN = float(os.getenv("EMBEDDING_LOAD_RETRY_SECONDS", "60"))


class EmbeddingUnavailable(RuntimeError):
    """Raised when the embedding model cannot be loaded or run."""


def fact_to_text(subject: str, predicate: str, value: Any) -> str:
    """Render a fact as natural language for embedding.

    The wording matters: the same phrasing is used when indexing a fact and
    when embedding a user's question, so that "What is the temperature in room
    101?" lands near "room_101 temperature is 22".

    Composite values are flattened rather than dumped as JSON — an embedding of
    the literal text '["english", "spanish"]' carries no useful signal.
    """
    return f"{subject} {predicate}: {render_value(value)}"


def render_value(value: Any) -> str:
    """Flatten a JSON value into searchable prose."""
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(render_value(item) for item in value) or "none"
    if isinstance(value, dict):
        return " ".join(
            f"{key} {render_value(item)}" for key, item in sorted(value.items())
        ) or "none"
    return str(value)


class EmbeddingService:
    """Lazily-loaded wrapper around a sentence-transformers model.

    All methods are async and run the (blocking) model in a thread pool so the
    event loop stays responsive.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model: Any = None
        self._lock = asyncio.Lock()
        self._retry_after: float = 0.0

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    async def load(self) -> None:
        """Load the model. Safe to call repeatedly; only the first call works.

        A failed load is remembered for LOAD_RETRY_COOLDOWN seconds. Without
        that, a host with no network and no HF cache would retry a doomed
        download on every single /memory/search and /agent/ask call.
        """
        if self._model is not None:
            return
        if time.monotonic() < self._retry_after:
            raise EmbeddingUnavailable(
                f"embedding model '{self.model_name}' failed to load recently; "
                f"retrying in {LOAD_RETRY_COOLDOWN:.0f}s"
            )
        async with self._lock:
            if self._model is not None:
                return
            loop = asyncio.get_running_loop()
            try:
                model = await loop.run_in_executor(None, self._load_model)
            except Exception as error:
                self._retry_after = time.monotonic() + LOAD_RETRY_COOLDOWN
                raise EmbeddingUnavailable(
                    f"could not load embedding model '{self.model_name}': {error}"
                ) from error
            self._model = model
            self._retry_after = 0.0
            self.dim = model.get_sentence_embedding_dimension() or self.dim
            logger.info("Embedding model '%s' loaded (%d dims)", self.model_name, self.dim)

    def _load_model(self) -> Any:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(self.model_name)

    async def embed_text(self, text: str) -> List[float]:
        """Embed a single string. Returns a plain list of floats."""
        return (await self.embed_texts([text]))[0]

    async def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed a batch of strings."""
        if not texts:
            return []
        await self.load()
        loop = asyncio.get_running_loop()
        try:
            vectors = await loop.run_in_executor(
                None, lambda: self._model.encode(  # type: ignore[union-attr]
                    list(texts),
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )
            )
        except Exception as error:
            raise EmbeddingUnavailable(f"embedding failed: {error}") from error
        return [vector.tolist() for vector in vectors]

    async def embed_fact(self, subject: str, predicate: str, value: Any) -> List[float]:
        """Embed a fact, using the same phrasing as fact_to_text()."""
        return await self.embed_text(fact_to_text(subject, predicate, value))

    async def is_healthy(self) -> bool:
        """True when the model is loaded (or can be) and returns usable vectors."""
        try:
            await self.load()
            vector = await self.embed_text("health check")
            return len(vector) == self.dim
        except EmbeddingUnavailable:
            return False


_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Process-wide singleton, so the model is loaded at most once."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


async def close_embedding_service() -> None:
    """Release the model on shutdown."""
    global _embedding_service
    if _embedding_service is not None:
        _embedding_service = None
