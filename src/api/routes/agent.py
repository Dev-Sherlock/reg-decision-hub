"""
/agent — ask a question with the symbolic memory in context.

This is the endpoint that ties the three services together:

    prompt -> embed -> pgvector top-K -> cross-check against Prolog
           -> LLM service over HTTP -> answer + the facts that grounded it

The retrieved facts are always returned, even when the LLM is down. A host can
therefore fall back to raw fact retrieval rather than losing the answer
entirely — which is what `used_fallback` signals.
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import database as db
from src.api.embeddings import EmbeddingService, EmbeddingUnavailable, get_embedding_service
from src.api.prolog.client import PrologClient, from_prolog, get_prolog_client
from src.llm.base import (
    LLMProvider,
    LLMUnavailable,
    format_facts_for_context,
    get_llm_provider,
)
from src.models.schemas import AgentAskRequest, AgentAskResponse, MemorySearchHit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


async def retrieve_facts(
    prompt: str,
    top_k: int,
    session: AsyncSession,
    embeddings: EmbeddingService,
    prolog: PrologClient,
) -> tuple[list[dict], bool]:
    """Find the facts most relevant to a prompt.

    Searches pgvector by embedding, then reads each hit back from Prolog so the
    context handed to the LLM is the same view the API queries serve. A fact
    present in PostgreSQL but missing from Prolog means the KB fell behind, and
    that is worth logging rather than hiding.

    Returns (hits, semantic).
    """
    try:
        vector = await embeddings.embed_text(prompt)
        hits = await db.search_facts_by_embedding(session, vector, top_k=top_k)
        semantic = True
    except EmbeddingUnavailable as error:
        logger.warning("Agent retrieval falling back to keywords: %s", error)
        hits = await db.search_facts_by_keyword(session, prompt, top_k=top_k)
        semantic = False

    for hit in hits:
        try:
            prolog_fact = await prolog.get_fact(hit["subject"], hit["predicate"])
        except Exception as error:
            logger.warning(
                "Prolog lookup failed for %s/%s: %s", hit["subject"], hit["predicate"], error
            )
            continue
        if prolog_fact is None:
            logger.warning(
                "Fact %s/%s is in PostgreSQL but not in the Prolog KB; "
                "restart the API to resynchronise",
                hit["subject"],
                hit["predicate"],
            )
            continue
        # Prefer the KB's value so the LLM sees the logically consistent one.
        hit["value"] = from_prolog(prolog_fact["value"])

    return hits, semantic


@router.post("/ask", response_model=AgentAskResponse)
async def ask_agent(
    request: AgentAskRequest,
    include_context: bool = Query(True, description="Include facts in the prompt"),
    session: AsyncSession = Depends(db.get_db),
    prolog: PrologClient = Depends(get_prolog_client),
    embeddings: EmbeddingService = Depends(get_embedding_service),
    llm: LLMProvider = Depends(get_llm_provider),
):
    """Answer a prompt using facts retrieved from symbolic memory.

    Never fails on an unavailable LLM: the response comes back with
    response=null, an error message and the retrieved facts so the caller can
    still answer from memory.
    """
    hits, semantic = await retrieve_facts(
        request.prompt, request.top_k, session, embeddings, prolog
    )
    fact_models = [MemorySearchHit(**hit) for hit in hits]
    logger.info(
        "Agent retrieved %d facts (semantic=%s) for prompt of %d chars",
        len(hits),
        semantic,
        len(request.prompt),
    )

    prompt = request.prompt
    if include_context:
        prompt = f"Known facts:\n{format_facts_for_context(hits)}\n\nQuestion: {request.prompt}"

    try:
        response = await llm.ask(prompt)
        return AgentAskResponse(response=response, retrieved_facts=fact_models, used_fallback=False)
    except LLMUnavailable as error:
        logger.warning("LLM unavailable, answering from memory alone: %s", error)
        return AgentAskResponse(
            response=None,
            retrieved_facts=fact_models,
            used_fallback=True,
            error=f"LLM service unavailable: {error}",
        )
    except Exception as error:
        logger.error("Unexpected LLM failure: %s", error)
        return AgentAskResponse(
            response=None,
            retrieved_facts=fact_models,
            used_fallback=True,
            error=f"LLM error: {error}",
        )
