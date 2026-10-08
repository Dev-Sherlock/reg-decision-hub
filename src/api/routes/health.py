"""
/health — dependency health for the whole stack.

Reports each service independently so a degraded LLM is visible without the API
looking dead: the memory endpoints keep working when the LLM is down, and this
endpoint is how you confirm that.
"""
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import database as db
from src.api.embeddings import get_embedding_service
from src.api.prolog.client import PrologClient, get_prolog_client
from src.llm.base import LLMProvider, get_llm_provider
from src.models.schemas import HealthResponse, HealthStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
async def health_check(
    prolog: PrologClient = Depends(get_prolog_client),
    session: AsyncSession = Depends(db.get_db),
    llm_provider: LLMProvider = Depends(get_llm_provider),
):
    """Health check for every backing service.

    overall is 'healthy', 'degraded' or 'unhealthy'. The LLM is treated as
    optional: its absence degrades the stack without breaking it.
    """
    services = []

    started = time.perf_counter()
    try:
        fact_count = await prolog.fact_count()
        services.append(
            HealthStatus(
                service="prolog",
                status="healthy",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={"facts": fact_count, "embedded": True},
            )
        )
    except Exception as error:
        services.append(
            HealthStatus(
                service="prolog",
                status="unhealthy",
                details={"error": str(error)},
            )
        )

    started = time.perf_counter()
    try:
        fact_count = await db.count_facts(session)
        services.append(
            HealthStatus(
                service="postgresql",
                status="healthy",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={"facts": fact_count},
            )
        )
    except Exception as error:
        services.append(
            HealthStatus(
                service="postgresql",
                status="unhealthy",
                details={"error": str(error)},
            )
        )

    started = time.perf_counter()
    try:
        embedding_service = get_embedding_service()
        healthy = await embedding_service.is_healthy()
        services.append(
            HealthStatus(
                service="embeddings",
                status="healthy" if healthy else "degraded",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={
                    "model": embedding_service.model_name,
                    "note": None if healthy else "semantic search falls back to keywords",
                },
            )
        )
    except Exception as error:
        services.append(
            HealthStatus(
                service="embeddings", status="degraded", details={"error": str(error)}
            )
        )

    started = time.perf_counter()
    try:
        # Resolved through Depends like every other route, so a test that injects
        # a down provider sees this endpoint report the LLM as degraded too.
        llm_healthy = await llm_provider.is_healthy()
        services.append(
            HealthStatus(
                service="llm",
                status="healthy" if llm_healthy else "degraded",
                latency_ms=(time.perf_counter() - started) * 1000,
                details={
                    "provider": type(llm_provider).__name__,
                    "note": None if llm_healthy else "/agent/ask will report used_fallback",
                },
            )
        )
    except Exception as error:
        services.append(
            HealthStatus(
                service="llm", status="degraded", details={"error": str(error)}
            )
        )

    statuses = {service.service: service.status for service in services}
    if statuses.get("prolog") == "unhealthy" or statuses.get("postgresql") == "unhealthy":
        overall = "unhealthy"
    elif "degraded" in statuses.values():
        overall = "degraded"
    else:
        overall = "healthy"

    return HealthResponse(overall=overall, services=services, timestamp=datetime.now(timezone.utc))


@router.get("/all")
async def health_all():
    """Cheap liveness probe for Docker healthchecks. Never touches dependencies."""
    return {"status": "ok"}
