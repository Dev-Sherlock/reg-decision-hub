"""
/memory — the symbolic memory API.

Facts and rules are written through to both stores: PostgreSQL is the durable
system of record and the embedded Prolog KB is the derived, queryable view.
PostgreSQL is always written first, so a Prolog failure degrades to a KB that
is rebuilt correctly on the next restart rather than to lost or divergent data.

Every mutation appends to the audit trail in PostgreSQL; Prolog keeps its own
provenance/6 trail in memory.
"""
import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import database as db
from src.api.embeddings import EmbeddingService, EmbeddingUnavailable, get_embedding_service
from src.api.prolog.client import (
    MEMORY_MODULE,
    REGISTRY_MODULE,
    PrologClient,
    PrologError,
    from_prolog,
    get_prolog_client,
)
from src.models.schemas import (
    FactDeleteResponse,
    FactListResponse,
    FactOverrideRequest,
    FactRequest,
    FactResponse,
    MemorySearchHit,
    MemorySearchResponse,
    ProvenanceEntry,
    QueryRequest,
    QueryResponse,
    RuleListResponse,
    RuleRequest,
    RuleResponse,
    RunRuleResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])

WILDCARD = "*"

# Prolog is Turing-complete, so /memory/query is an arbitrary-program endpoint.
# Only these read-only goals are accepted; everything else is rejected before
# the goal reaches the interpreter.
#
# The allowlist is one trust boundary but not one namespace. The example domain
# modules each export node/6, trace/2 and decide/3 because they implement one
# interface, so a goal naming one of those is ambiguous until the caller says which
# domain meant. MEMORY_QUERY_PREDICATES resolve on their own; TREE_QUERY_PREDICATES
# need "domain" on the request.
MEMORY_QUERY_PREDICATES = frozenset(
    {
        "fact",
        "rule",
        "query_facts",
        "get_fact",
        "all_facts",
        "fact_count",
        "get_rule",
        "all_rules",
        "provenance",
        "provenance_entry",
        "provenance_for",
        "run_rule",
        "true",
    }
)

TREE_QUERY_PREDICATES = frozenset(
    {
        "node",
        "can_write",
        "trace",
        "decide",
        "eval_condition",
        "get_all_nodes",
        "get_leaves",
        "is_leaf",
    }
)

ALLOWED_QUERY_PREDICATES = MEMORY_QUERY_PREDICATES | TREE_QUERY_PREDICATES

# Belt-and-braces: refuse goals mentioning a mutation or I/O predicate even if
# the head functor would pass the allowlist (e.g. inside a conjunction).
FORBIDDEN_QUERY_TOKENS = re.compile(
    r"\b(assert|asserta|assertz|retract|retractall|abolish|consult|load_files|"
    r"open|close|shell|system|halt|write|read_term|atom_length)\b|:[-\\?]"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _diff_hash(subject: str, predicate: str, old_value: Any, new_value: Any) -> str:
    payload = json.dumps(
        {"subject": subject, "predicate": predicate, "old": old_value, "new": new_value},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def _embed_fact(
    embeddings: EmbeddingService,
    subject: str,
    predicate: str,
    value: Any,
    reindex: bool = True,
) -> Tuple[Optional[List[float]], bool]:
    """Embed a fact, tolerating an unavailable model.

    Returns (vector, embedded). A fact is always storable without a vector —
    it just will not appear in semantic search results.
    """
    if not reindex:
        return None, False
    try:
        return await embeddings.embed_fact(subject, predicate, value), True
    except EmbeddingUnavailable as error:
        logger.warning("Storing %s.%s without an embedding: %s", subject, predicate, error)
        return None, False


async def _write_fact(
    session: AsyncSession,
    prolog: PrologClient,
    embeddings: EmbeddingService,
    subject: str,
    predicate: str,
    value: Any,
    owner: Optional[str] = None,
    reindex: bool = True,
) -> Dict[str, Any]:
    """Write one fact to PostgreSQL, then mirror it into Prolog, then audit."""
    vector, embedded = await _embed_fact(embeddings, subject, predicate, value, reindex)

    outcome = await db.upsert_fact(session, subject, predicate, value, vector)
    old_value = outcome["old_value"]

    try:
        await prolog.assert_fact(subject, predicate, value)
    except PrologError as error:
        # PostgreSQL is the source of truth, so this is recoverable: the next
        # API start reloads the KB from the database.
        logger.error("Prolog assert failed for %s.%s: %s", subject, predicate, error)

    await db.log_audit(
        session,
        entity_type="fact",
        subject=subject,
        predicate=predicate,
        operation=outcome["operation"],
        old_value=old_value,
        new_value=value,
        owner=owner,
        diff_hash=_diff_hash(subject, predicate, old_value, value),
    )

    stored = await db.get_fact(session, subject, predicate)
    return {"fact": stored, "operation": outcome["operation"], "embedded": embedded}


async def _delete_fact(
    session: AsyncSession,
    prolog: PrologClient,
    subject: str,
    predicate: str,
    owner: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    removed = await db.delete_fact(session, subject, predicate)
    if removed is None:
        return None

    try:
        await prolog.retract_fact(subject, predicate)
    except PrologError as error:
        logger.error("Prolog retract failed for %s.%s: %s", subject, predicate, error)

    await db.log_audit(
        session,
        entity_type="fact",
        subject=subject,
        predicate=predicate,
        operation="DELETE",
        old_value=removed["value"],
        new_value=None,
        owner=owner,
        diff_hash=_diff_hash(subject, predicate, removed["value"], None),
    )
    return removed


def _normalise_wildcard(value: Optional[str]) -> Optional[str]:
    """Treat '*' and an absent parameter as 'match anything'."""
    if value is None or value == WILDCARD or value == "":
        return None
    return value


def _require_admin(provided: Optional[str]) -> None:
    configured = os.getenv("ADMIN_API_KEY", "").strip()
    if not configured:
        raise HTTPException(
            status_code=403,
            detail=(
                "Arbitrary Prolog queries are disabled: set ADMIN_API_KEY in the "
                "environment and send the same value in the X-Admin-Key header."
            ),
        )
    if not provided or provided != configured:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key")


def _validate_query(query: str) -> Tuple[str, str]:
    """Reject anything that is not a read-only goal over an allowed predicate.

    Returns the cleaned goal and its head functor. The functor is returned rather
    than re-derived because it decides which module the goal is qualified with,
    and getting that from the same parse keeps the allowlist and the resolution
    from drifting apart.
    """
    stripped = query.strip().rstrip(".").strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="Query must not be empty")
    if ".." in stripped:
        raise HTTPException(status_code=400, detail="End-of-clause markers are not allowed")

    if FORBIDDEN_QUERY_TOKENS.search(stripped):
        raise HTTPException(
            status_code=400,
            detail="Query may only contain read-only goals",
        )

    head = re.match(r"^([a-z][A-Za-z0-9_]*)", stripped)
    if not head:
        raise HTTPException(status_code=400, detail="Could not read a predicate from the query")
    functor = head.group(1)
    if functor not in ALLOWED_QUERY_PREDICATES:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Predicate '{functor}' is not allowed. Permitted predicates: "
                + ", ".join(sorted(ALLOWED_QUERY_PREDICATES))
            ),
        )
    return stripped, functor


async def _goal_module(functor: str, domain: Optional[str], prolog: PrologClient) -> str:
    """The module a validated goal is qualified with.

    The KB is loaded with imports([]) so the four example domains keep four
    independent namespaces, which means `user` holds none of these predicates and
    an unqualified goal fails with existence_error rather than answering. So the
    goal is qualified explicitly here.
    """
    if functor not in TREE_QUERY_PREDICATES:
        return MEMORY_MODULE

    domains = {entry["id"]: entry for entry in await prolog.active_domains()}
    if not domain:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{functor}' is exported by every decision domain, so the goal is "
                "ambiguous without one. Send \"domain\" to name it. Registered domains: "
                + ", ".join(sorted(domains))
            ),
        )
    entry = domains.get(domain)
    if entry is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown domain '{domain}'. Registered domains: "
                + ", ".join(sorted(domains))
            ),
        )
    if not entry["module"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Domain '{domain}' is a runtime domain: its nodes live in "
                f"{REGISTRY_MODULE} rather than in a module of their own, so "
                f"'{functor}' has nothing to run against. Use /tree/{domain}'s node "
                "ids with the routes that resolve a domain, not /memory/query."
            ),
        )
    return str(entry["module"])


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@router.post("/fact", response_model=FactResponse, status_code=201)
async def store_fact(
    request: FactRequest,
    session: AsyncSession = Depends(db.get_db),
    prolog: PrologClient = Depends(get_prolog_client),
    embeddings: EmbeddingService = Depends(get_embedding_service),
):
    """Store a fact. Asserting an existing (subject, predicate) replaces its value."""
    result = await _write_fact(
        session,
        prolog,
        embeddings,
        request.subject,
        request.predicate,
        request.value,
        owner=request.owner,
        reindex=request.reindex,
    )
    return result["fact"]


@router.get("/fact", response_model=FactListResponse)
async def list_facts(
    subject: Optional[str] = Query(None, description="Subject, or * for any"),
    predicate: Optional[str] = Query(None, description="Predicate, or * for any"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(db.get_db),
):
    """Query stored facts. Omit or wildcard either parameter to filter loosely."""
    rows = await db.list_facts(
        session,
        subject=_normalise_wildcard(subject),
        predicate=_normalise_wildcard(predicate),
        limit=limit,
        offset=offset,
    )
    return FactListResponse(facts=[FactResponse(**row) for row in rows], count=len(rows))


@router.get("/fact/{subject}/{predicate}", response_model=FactResponse)
async def get_fact(
    subject: str,
    predicate: str,
    session: AsyncSession = Depends(db.get_db),
):
    """One fact by exact subject and predicate."""
    row = await db.get_fact(session, subject, predicate)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No fact {subject}/{predicate}")
    return FactResponse(**row)


@router.delete("/fact/{subject}/{predicate}", response_model=FactDeleteResponse)
async def delete_fact(
    subject: str,
    predicate: str,
    owner: Optional[str] = Query(None),
    session: AsyncSession = Depends(db.get_db),
    prolog: PrologClient = Depends(get_prolog_client),
):
    """Retract a fact from both stores."""
    removed = await _delete_fact(session, prolog, subject, predicate, owner=owner)
    if removed is None:
        raise HTTPException(status_code=404, detail=f"No fact {subject}/{predicate}")
    return FactDeleteResponse(
        deleted=True, subject=subject, predicate=predicate, value=removed["value"]
    )


@router.get("/fact/{subject}/{predicate}/provenance", response_model=List[ProvenanceEntry])
async def get_provenance(
    subject: str,
    predicate: str,
    prolog: PrologClient = Depends(get_prolog_client),
):
    """The in-memory Prolog audit trail for one fact, oldest first."""
    return [ProvenanceEntry(**entry) for entry in await prolog.get_provenance(subject, predicate)]


@router.post("/fact/{subject}/{predicate}/override", response_model=FactResponse)
async def override_fact(
    subject: str,
    predicate: str,
    request: FactOverrideRequest,
    session: AsyncSession = Depends(db.get_db),
    prolog: PrologClient = Depends(get_prolog_client),
    embeddings: EmbeddingService = Depends(get_embedding_service),
):
    """Governed change to an existing fact.

    Unlike POST /memory/fact this requires the fact to already exist and
    records who changed it, so every edit is attributable.
    """
    existing = await db.get_fact(session, subject, predicate)
    if existing is None:
        raise HTTPException(
            status_code=404, detail=f"No fact {subject}/{predicate} to override"
        )

    result = await _write_fact(
        session,
        prolog,
        embeddings,
        subject,
        predicate,
        request.value,
        owner=request.owner,
    )
    logger.info("Fact %s.%s overridden by %s", subject, predicate, request.owner)
    return result["fact"]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@router.post("/rule", response_model=RuleResponse, status_code=201)
async def store_rule(
    request: RuleRequest,
    session: AsyncSession = Depends(db.get_db),
    prolog: PrologClient = Depends(get_prolog_client),
):
    """Define an inference rule. Redefining a name replaces the definition."""
    operation = await db.upsert_rule(session, request.name, request.head, request.body)

    try:
        await prolog.add_rule(request.name, request.head, request.body)
    except PrologError as error:
        logger.error("Prolog rule definition failed for %s: %s", request.name, error)
        raise HTTPException(
            status_code=400, detail=f"Rule body is not valid Prolog: {error}"
        ) from error

    await db.log_audit(
        session,
        entity_type="rule",
        rule_name=request.name,
        operation=operation,
        old_value=None if operation == "INSERT" else {"head": request.head},
        new_value={"head": request.head, "body": request.body},
        diff_hash=_diff_hash(request.name, "rule", None, request.body),
    )
    row = await db.get_rule(session, request.name)
    return RuleResponse(**row)


@router.get("/rule", response_model=RuleListResponse)
async def list_rules(session: AsyncSession = Depends(db.get_db)):
    rows = await db.list_rules(session)
    return RuleListResponse(rules=[RuleResponse(**row) for row in rows], count=len(rows))


@router.get("/rule/{name}", response_model=RuleResponse)
async def get_rule(name: str, session: AsyncSession = Depends(db.get_db)):
    """A rule definition. Read from PostgreSQL so the Prolog source text round-trips exactly."""
    row = await db.get_rule(session, name)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No rule named '{name}'")
    return RuleResponse(**row)


@router.delete("/rule/{name}")
async def delete_rule(
    name: str,
    session: AsyncSession = Depends(db.get_db),
    prolog: PrologClient = Depends(get_prolog_client),
):
    removed = await db.delete_rule(session, name)
    if removed is None:
        raise HTTPException(status_code=404, detail=f"No rule named '{name}'")
    try:
        await prolog.retract_rule(name)
    except PrologError as error:
        logger.error("Prolog rule removal failed for %s: %s", name, error)
    await db.log_audit(
        session,
        entity_type="rule",
        rule_name=name,
        operation="DELETE",
        old_value={"head": removed["head"], "body": removed["body"]},
    )
    return {"deleted": True, "rule": name}


@router.post("/rule/{name}/run", response_model=RunRuleResponse)
async def run_rule(
    name: str,
    store: bool = Query(False, description="Persist the derived facts"),
    owner: Optional[str] = Query(None),
    session: AsyncSession = Depends(db.get_db),
    prolog: PrologClient = Depends(get_prolog_client),
    embeddings: EmbeddingService = Depends(get_embedding_service),
):
    """Run a rule over the current KB.

    By default this is a dry run: the derived facts are returned but not
    stored, so a rule can be tested before it is trusted. Pass store=true to
    persist each derived fact through the normal write path.
    """
    if (await db.get_rule(session, name)) is None:
        raise HTTPException(status_code=404, detail=f"No rule named '{name}'")

    derived = await prolog.run_rule(name)
    if store:
        for fact in derived:
            await _write_fact(
                session,
                prolog,
                embeddings,
                fact["subject"],
                fact["predicate"],
                fact["value"],
                owner=owner or f"rule:{name}",
            )

    return RunRuleResponse(
        rule=name,
        derived=[FactResponse(**fact) for fact in derived],
        count=len(derived),
    )


# ---------------------------------------------------------------------------
# Arbitrary queries (admin only)
# ---------------------------------------------------------------------------


@router.post("/query", response_model=QueryResponse)
async def run_query(
    request: QueryRequest,
    x_admin_key: Optional[str] = Header(None, alias="X-Admin-Key"),
    prolog: PrologClient = Depends(get_prolog_client),
):
    """Run a read-only Prolog goal and return its bindings. Admin only."""
    _require_admin(x_admin_key)
    goal, functor = _validate_query(request.query)
    module_name = await _goal_module(functor, request.domain, prolog)

    try:
        results = await prolog.query(f"{module_name}:{goal}")
    except PrologError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    bindings = [
        {str(key): _stringify(value) for key, value in result.items()}
        for result in results[: request.limit]
    ]
    return QueryResponse(
        query=f"{module_name}:{goal}", bindings=bindings, count=len(bindings)
    )


def _stringify(value: Any) -> Any:
    """Make a binding JSON-safe; unbound variables keep their printed name."""
    converted = from_prolog(value)
    if isinstance(converted, (str, int, float, bool, type(None))):
        return converted
    return str(converted)


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


@router.get("/search", response_model=MemorySearchResponse)
async def search_memory(
    query: str = Query(..., min_length=1),
    top_k: int = Query(5, ge=1, le=50),
    session: AsyncSession = Depends(db.get_db),
    embeddings: EmbeddingService = Depends(get_embedding_service),
):
    """Find facts related to a natural-language query.

    Embeds the query and does a pgvector cosine search. If the embedding model
    is unavailable the search falls back to keyword matching and the response
    is flagged semantic=false, so callers can tell the two apart.
    """
    try:
        vector = await embeddings.embed_text(query)
        hits = await db.search_facts_by_embedding(session, vector, top_k=top_k)
        semantic = True
    except EmbeddingUnavailable as error:
        logger.warning("Semantic search unavailable (%s); falling back to keywords", error)
        hits = await db.search_facts_by_keyword(session, query, top_k=top_k)
        semantic = False

    return MemorySearchResponse(
        query=query,
        hits=[MemorySearchHit(**hit) for hit in hits],
        count=len(hits),
        semantic=semantic,
    )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@router.get("/audit")
async def get_audit(
    subject: Optional[str] = Query(None),
    predicate: Optional[str] = Query(None),
    rule_name: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None, description="fact | rule | node"),
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(db.get_db),
):
    """The durable audit trail, newest first."""
    rows = await db.get_audit(
        session,
        subject=_normalise_wildcard(subject),
        predicate=_normalise_wildcard(predicate),
        rule_name=_normalise_wildcard(rule_name),
        entity_type=_normalise_wildcard(entity_type),
        limit=limit,
    )
    return {"entries": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@router.post("/verify")
async def verify_fact(
    request: FactRequest,
    session: AsyncSession = Depends(db.get_db),
    prolog: PrologClient = Depends(get_prolog_client),
):
    """Check a candidate fact against the rule set.

    Reports whether the fact is already stored, and which rules derive it.
    Useful for reviewing facts an LLM proposed before committing them.
    """
    stored = await db.get_fact(session, request.subject, request.predicate)
    candidate = {
        "subject": request.subject,
        "predicate": request.predicate,
        "value": request.value,
    }

    deriving_rules: List[str] = []
    for rule in await db.list_rules(session):
        try:
            derived = await prolog.run_rule(rule["name"])
        except PrologError as error:
            logger.warning("Rule %s failed while verifying: %s", rule["name"], error)
            continue
        if candidate in derived:
            deriving_rules.append(rule["name"])

    return {
        "fact": candidate,
        "stored": stored is not None,
        "stored_value": stored["value"] if stored else None,
        "derivable_from_rules": deriving_rules,
        "consistent": (stored is None or stored["value"] == request.value) and bool(
            deriving_rules or stored is not None
        ),
    }
