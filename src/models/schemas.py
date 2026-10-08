"""Request/response models for the API.

Two families live here:

* the symbolic memory — facts, rules, queries and semantic search
* the LLM bridge and agent endpoints, which take a prompt and return an answer
  together with the facts that were retrieved as context

The decision-tree models are kept so /decide, /tree and the overrides endpoint
continue to work as a domain example of the same system.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Decision trees (one per domain)
# ---------------------------------------------------------------------------


class NodeBase(BaseModel):
    id: str
    parent: str
    condition: str
    action: str
    owner: str
    version: int
    # Which domain's tree this node belongs to. Defaults to energy because that is
    # the tree the system shipped with; every node really does carry one, and /tree
    # sets it explicitly.
    domain: str = "energy"


class NodeResponse(NodeBase):
    is_leaf: bool = False
    children: List["NodeResponse"] = []


class DomainSummary(BaseModel):
    """One domain as /trees and /tree report it."""

    id: str = Field(..., description="Registry id, e.g. energy or air_traffic")
    label: str
    root: str = Field(..., description="The domain's root node id")
    node_count: int
    input_keys: List[str] = Field(
        default_factory=list,
        description="Keys this domain declares; /decide routes on them",
    )
    active: bool = True
    source: str = Field(
        "kb",
        description="`kb` for a module in the image, `runtime` for one created via POST /memory/domain",
    )


class DecideRequest(BaseModel):
    """Arbitrary decision inputs.

    Keys are whatever the registered decision domains' conditions reference. The
    energy example uses demand/temperature/humidity, air traffic uses
    region/ice_on_wing_pct, and so on. No domain is named here: /decide routes on
    the keys themselves, and reports which domain it chose in `domain`.
    """

    inputs: Dict[str, Any] = Field(
        default_factory=dict,
        description="Feature name to value map, evaluated against the node conditions",
    )


class ProofStep(BaseModel):
    node_id: str
    condition: str
    action: str
    owner: str


class DecideResponse(BaseModel):
    # Which tree answered. The KB holds several, and a proof trace alone does not
    # say which one's rules were applied.
    domain: str
    leaf_node: str
    action: str
    owner: str
    proof_trace: List[ProofStep]


class OverrideRequest(BaseModel):
    action: str
    owner: str


class OverrideResponse(BaseModel):
    success: bool
    node_id: str
    old_action: str
    new_action: str
    owner: str
    timestamp: datetime
    diff_hash: str


class NodeUpdateRequest(BaseModel):
    action: str
    owner: str


class TreeResponse(BaseModel):
    """The merged forest.

    Node ids are globally unique across domains (a registry invariant), so a flat
    list renders correctly as four separate trees without a per-domain grouping —
    which is why `root` is still a bare id.
    """

    nodes: List[NodeBase]
    root: str = "root"
    domains: List[DomainSummary] = []


class DomainNodeSpec(BaseModel):
    """One node of a runtime domain, as POST /memory/domain accepts it.

    `condition` is Prolog source, not a display string: it is asserted into the KB
    and evaluated, so it is validated against the grammar eval_condition/2 accepts
    rather than being stored as an opaque atom. The value `true` is the root's.
    """

    id: str = Field(..., min_length=1, description="Globally unique node id, e.g. agri_root")
    parent: Optional[str] = Field(
        None,
        description="Parent node id, or `none`/null for the single root",
    )
    condition: str = Field("true", description="Prolog condition source")
    action: str = Field(..., min_length=1)
    owner: str = Field(..., min_length=1)
    version: int = Field(1, ge=1)


class DomainRequest(BaseModel):
    """A whole domain, created inactive.

    Creating it does not make it routable. POST /memory/domain/{id}/activate does,
    and both are audited, so a half-reviewed tree is never consulted for a decision.
    """

    id: str = Field(..., min_length=1, description="Registry id; must not clash with a kb domain")
    label: str = Field(..., min_length=1)
    input_keys: List[str] = Field(
        ...,
        min_length=1,
        description="Keys /decide routes on. Must not overlap another active domain's keys, "
        "or the two domains would tie on the same inputs.",
    )
    nodes: List[DomainNodeSpec] = Field(..., min_length=1)


class DomainResponse(BaseModel):
    id: str
    label: str
    input_keys: List[str]
    node_count: int
    active: bool
    source: str = "runtime"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


class FactRequest(BaseModel):
    subject: str = Field(..., min_length=1, description="Atom naming the thing, e.g. room_101")
    predicate: str = Field(..., min_length=1, description="Atom naming the property")
    value: Any = Field(..., description="Any JSON-serialisable value")
    owner: Optional[str] = Field(None, description="Recorded in the audit trail")
    reindex: bool = Field(
        True,
        description="Recompute the embedding. Set false to keep the stored vector.",
    )


class FactResponse(BaseModel):
    subject: str
    predicate: str
    value: Any
    version: int = 1
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FactListResponse(BaseModel):
    facts: List[FactResponse]
    count: int


class FactOverrideRequest(BaseModel):
    """A governed change to an existing fact: value plus who is asking."""

    value: Any
    owner: str = Field(..., min_length=1)


class FactDeleteResponse(BaseModel):
    deleted: bool
    subject: str
    predicate: str
    value: Any = None
    version: Optional[int] = None


class ProvenanceEntry(BaseModel):
    timestamp: str
    change_type: str
    old_value: Any = None
    new_value: Any = None


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class RuleRequest(BaseModel):
    name: str = Field(..., min_length=1)
    head: str = Field(
        ...,
        description="Prolog goal template, conventionally fact(Subject, Predicate, Value)",
        examples=["fact(X, colleague_of, Y)"],
    )
    body: str = Field(
        ...,
        description="Prolog list of goal templates evaluated against the KB",
        examples=["[fact(X, works_at, Y)]"],
    )


class RuleResponse(BaseModel):
    name: str
    head: str
    body: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RuleListResponse(BaseModel):
    rules: List[RuleResponse]
    count: int


class RunRuleResponse(BaseModel):
    rule: str
    derived: List[FactResponse]
    count: int


# ---------------------------------------------------------------------------
# Arbitrary Prolog queries (admin only)
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
        description="Read-only Prolog goal, e.g. fact(X, Y, Z) or fact_count(N)",
    )
    limit: int = Field(100, ge=1, le=1000)
    domain: Optional[str] = Field(
        None,
        description=(
            "Decision domain the goal runs against. Required for the tree predicates "
            "(node, trace, decide, eval_condition, get_all_nodes, get_leaves, is_leaf), "
            "which every domain module exports, so the goal is otherwise ambiguous. "
            "Ignored for memory predicates, which have one home."
        ),
    )


class QueryResponse(BaseModel):
    query: str
    bindings: List[Dict[str, Any]]
    count: int


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


class MemorySearchHit(BaseModel):
    subject: str
    predicate: str
    value: Any
    similarity: Optional[float] = None
    version: int = 1
    updated_at: Optional[datetime] = None


class MemorySearchResponse(BaseModel):
    query: str
    hits: List[MemorySearchHit]
    count: int
    semantic: bool = Field(
        True,
        description="False when the embedding model was unavailable and results "
        "fell back to keyword matching",
    )


# ---------------------------------------------------------------------------
# LLM bridge
# ---------------------------------------------------------------------------


class LLMAskRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    context_facts: Optional[List[FactResponse]] = None


class LLMAskResponse(BaseModel):
    response: Optional[str] = None
    context_facts: List[MemorySearchHit] = []
    error: Optional[str] = None


class LLMHealthResponse(BaseModel):
    reachable: bool
    status: Optional[str] = None
    model: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AgentAskRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    top_k: int = Field(5, ge=1, le=50)


class AgentAskResponse(BaseModel):
    response: Optional[str] = None
    retrieved_facts: List[MemorySearchHit] = []
    used_fallback: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthStatus(BaseModel):
    service: str
    status: str
    latency_ms: Optional[float] = None
    details: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    overall: str
    services: List[HealthStatus]
    timestamp: datetime
