"""
/decide — evaluate a decision tree, choosing the domain from the inputs.

The request carries no domain. The registry declares an input key set per domain and
this endpoint routes on which of those keys the caller actually sent, so adding a
domain to the KB needs no change here. The domain that answered comes back as
`domain` — a caller that guessed wrong about which tree applied can see it.

Nothing about the walk lives in Python: tree_engine decides, and the response is
the symbolic proof trace it produced. Explanations are not generated here;
/agent/ask and /llm/ask turn the same facts into prose whenever an LLM is available.
"""
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from src.api.prolog.client import (
    AmbiguousDomainError,
    PrologClient,
    PrologError,
    UnknownDomainError,
    get_prolog_client,
)
from src.models.schemas import DecideRequest, DecideResponse, ProofStep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/decide", tags=["decide"])


@router.post("", response_model=DecideResponse)
async def decide(
    request: DecideRequest,
    domain: Optional[str] = None,
    prolog: PrologClient = Depends(get_prolog_client),
):
    """Evaluate the decision tree and return the leaf plus its proof trace.

    `?domain=` overrides detection. It does not bypass validation — naming a domain
    that does not exist or is not active is still a 400 — and it is here for the UI
    and for tests that want one specific tree, not as a way to skip the routing.
    """
    started = time.perf_counter()

    try:
        (
            domain_id,
            leaf,
            action,
            owner,
            proof_trace,
        ) = await prolog.decide(request.inputs, domain_id=domain)
    except UnknownDomainError as error:
        # 422 would be tempting, but the request body is well-formed — it just does
        # not name a domain this KB can answer for. The per-domain key list is the
        # actionable part, so it goes in the detail rather than only in a log line.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_domain",
                "message": str(error),
                "keys_by_domain": error.keys_by_domain,
            },
        ) from error
    except AmbiguousDomainError as error:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "ambiguous_domain",
                "message": str(error),
                "candidates": error.candidates,
            },
        ) from error
    except PrologError as error:
        raise HTTPException(status_code=400, detail=f"No decision matched these inputs: {error}")
    except Exception as error:
        logger.error("Decision failed: %s", error)
        raise HTTPException(status_code=500, detail=f"Decision failed: {error}")

    latency_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "Decision in %.2fms: [%s] %s -> %s", latency_ms, domain_id, leaf, action
    )

    return DecideResponse(
        domain=domain_id,
        leaf_node=leaf,
        action=action,
        owner=owner,
        proof_trace=[ProofStep(**step) for step in proof_trace],
    )