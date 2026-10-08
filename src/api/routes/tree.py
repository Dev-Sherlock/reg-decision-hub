"""
/tree and /trees — read-only views of the registered decision domains.

/tree returns the merged forest. Node ids are globally unique across domains — a
registry invariant that /decide's node overrides and the audit lookup both rely on —
so a flat list is enough to render four separate trees and a bare id resolves
without being told which domain it belongs to.

/trees returns the registry itself: which domains exist, what inputs each one
declares, and whether it is active. That is what the UI uses to offer a picker and
to seed input rows, since the key set lives in the KB and not in the frontend.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from src.api.prolog.client import PrologClient, PrologError, get_prolog_client
from src.models.schemas import DomainSummary, NodeBase, TreeResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tree"])


def _node(raw: dict, domain: str, fallback_id: str = "") -> NodeBase:
    return NodeBase(
        id=str(raw.get("id", fallback_id)),
        parent=str(raw.get("parent", "")),
        condition=str(raw.get("condition", "")),
        action=str(raw.get("action", "")),
        owner=str(raw.get("owner", "")),
        version=int(raw.get("version") or 1),
        domain=domain,
    )


@router.get("/trees", response_model=list[DomainSummary])
async def get_trees(prolog: PrologClient = Depends(get_prolog_client)):
    """Every registered domain, including any that are not active yet."""
    try:
        domains = await prolog.registry()
    except PrologError as error:
        raise HTTPException(
            status_code=500, detail=f"Failed to read the domain registry: {error}"
        ) from error

    summaries: list[DomainSummary] = []
    for domain in domains:
        try:
            nodes = await prolog.get_all_nodes(domain["id"])
        except Exception as error:
            # One unreadable domain must not hide the rest. A runtime domain whose
            # nodes failed to load is exactly the case worth reporting per-domain
            # rather than collapsing the whole registry into one 500.
            logger.error("Failed to read nodes for domain %s: %s", domain["id"], error)
            continue
        summaries.append(
            DomainSummary(
                id=domain["id"],
                label=domain["label"],
                root=_root_of(nodes),
                node_count=len(nodes),
                input_keys=list(domain["input_keys"]),
                active=bool(domain.get("active", True)),
                source=domain.get("source", "kb"),
            )
        )
    return summaries


@router.get("/tree", response_model=TreeResponse)
async def get_tree(prolog: PrologClient = Depends(get_prolog_client)):
    """Every node of every active domain, for UI rendering."""
    try:
        active = await prolog.active_domains()
    except PrologError as error:
        raise HTTPException(
            status_code=500, detail=f"Failed to read the domain registry: {error}"
        ) from error

    nodes: list[NodeBase] = []
    summaries: list[DomainSummary] = []
    for domain in active:
        raw_nodes = await prolog.get_all_nodes(domain["id"])
        nodes.extend(_node(raw, domain["id"]) for raw in raw_nodes)
        summaries.append(
            DomainSummary(
                id=domain["id"],
                label=domain["label"],
                root=_root_of(raw_nodes),
                node_count=len(raw_nodes),
                input_keys=list(domain["input_keys"]),
                active=True,
                source=domain.get("source", "kb"),
            )
        )

    return TreeResponse(
        nodes=nodes,
        # Kept bare for compatibility: the merged forest is not one tree, so this is
        # the energy root and nothing else. Use /tree/{id} or /trees to reach others.
        root="root",
        domains=summaries,
    )


@router.get("/tree/{node_id}", response_model=NodeBase)
async def get_node(node_id: str, prolog: PrologClient = Depends(get_prolog_client)):
    """One node by id, searched across every active domain."""
    node = await prolog.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return _node(node, str(node.get("domain", "energy")), node_id)


def _root_of(nodes: list[dict]) -> str:
    """The single node whose parent is `none`.

    Falls back to the empty string rather than guessing when the tree is malformed,
    so a broken domain shows up as an obviously missing root instead of silently
    borrowing another domain's.
    """
    for node in nodes:
        if str(node.get("parent", "")) == "none":
            return str(node.get("id", ""))
    return ""