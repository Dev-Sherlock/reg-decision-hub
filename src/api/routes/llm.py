"""
/llm — direct bridge to the LLM service.

This is the plumbing endpoint: it retrieves context from memory and forwards
the prompt to whatever LLM_PROVIDER points at. /agent/ask is the richer
convenience wrapper (it also cross-checks Prolog and reports fallback state);
use /llm/ask when you want the raw bridge behaviour or when you already know
which facts matter and want to pass them in explicitly.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import database as db
from src.api.embeddings import EmbeddingService, get_embedding_service
from src.llm.base import LLMProvider, LLMUnavailable, format_facts_for_context, get_llm_provider
from src.models.schemas import (
    LLMAskRequest,
    LLMAskResponse,
    LLMHealthResponse,
    MemorySearchHit,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["llm"])

DEFAULT_TOP_K = 5


@router.post("/ask", response_model=LLMAskResponse)
async def ask_llm(
    request: LLMAskRequest,
    top_k: int = DEFAULT_TOP_K,
    session: AsyncSession = Depends(db.get_db),
    embeddings: EmbeddingService = Depends(get_embedding_service),
    llm: LLMProvider = Depends(get_llm_provider),
):
    """Ask the LLM with facts from symbolic memory as context.

    Supply `context_facts` to control exactly which facts are used; omit it and
    the top-k semantically closest facts are retrieved automatically.

    Returns 503 when the LLM is unreachable — memory endpoints are unaffected.
    """
    if request.context_facts:
        context = format_facts_for_context(
            [fact.model_dump() for fact in request.context_facts]
        )
        hits: list[dict] = [fact.model_dump() for fact in request.context_facts]
    else:
        try:
            vector = await embeddings.embed_text(request.prompt)
            hits = await db.search_facts_by_embedding(session, vector, top_k=top_k)
        except Exception as error:
            # Retrieval is best-effort here: the model can still answer
            # without memory, which is worse than lexical but better than
            # refusing to call it at all.
            logger.warning("Could not retrieve context for /llm/ask: %s", error)
            hits = []
        context = format_facts_for_context(hits)

    prompt = f"Known facts:\n{context}\n\nQuestion: {request.prompt}"
    try:
        response = await llm.ask(prompt)
    except LLMUnavailable as error:
        # The provider's own wording varies by cause, so prefix it: a client can
        # tell "the model is down" apart from "the request was malformed"
        # without string-matching the reason.
        raise HTTPException(
            status_code=503, detail=f"LLM unavailable: {error}"
        ) from error

    return LLMAskResponse(
        response=response,
        context_facts=[MemorySearchHit(**hit) for hit in hits],
    )


@router.get("/health", response_model=LLMHealthResponse)
async def llm_health(llm: LLMProvider = Depends(get_llm_provider)):
    """Proxy the LLM service's own health check.

    Reports the provider in use rather than pretending there is only one.
    """
    reachable = await llm.is_healthy()
    return LLMHealthResponse(
        reachable=reachable,
        status="healthy" if reachable else "unhealthy",
        reason=None if reachable else "LLM provider did not respond to a health check",
    )
