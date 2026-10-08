"""
/memory/domain — create and activate decision domains at runtime.

The four domains in kb/*.pl are code that shipped in the image, so they cannot be
edited through an API. This is the route for a domain that has to be created later:
its nodes are tree_registry:domain_node/7 facts rather than a module, which makes
them auditable, reloadable and reversible.

Two pairs of endpoints, because creating a tree and trusting it are
different acts, and so are stopping one and removing one:

    POST /memory/domain                    writes it, inactive
    POST /memory/domain/{id}/activate      makes /decide and /tree consider it
    POST /memory/domain/{id}/deactivate    stops routing, keeps it registered
    DELETE /memory/domain/{id}             removes it; the audit trail stays

A domain is created inactive on purpose. Every structural mistake this route cannot
see — a condition that fires when it should not, an owner that does not exist — shows
up as a wrong decision, and an explicit activation is the moment someone accepts that
responsibility. Every transition is audited (invariant 3).

Validation is deliberately structural only: unique ids, one root, resolvable parents,
no cycles, and conditions that parse. Whether the *logic* is right is not something
this route can check, and pretending otherwise would be worse than leaving it to
activation.
"""
import hashlib
import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import database as db
from src.api.prolog.client import PrologClient, PrologError, get_prolog_client
from src.models.schemas import DomainRequest, DomainResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory/domain", tags=["domains"])

_ROOT_PARENT = "none"


def _is_root(parent: Any) -> bool:
    return parent is None or str(parent) == _ROOT_PARENT


def _diff_hash(domain_id: str, payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps({"domain": domain_id, **payload}, sort_keys=True).encode()
    ).hexdigest()[:16]


def _validate_structure(spec: DomainRequest, known_node_ids: Dict[str, str]) -> None:
    """Reject a spec that could not be walked, before anything is written.

    The cycle check is the one that earns its keep: deepest_in/4 recurses over
    children with no depth bound, so a cycle is a hung request rather than a wrong
    answer. It runs over parent pointers rather than children, because that is the
    only direction a loop can exist in — each node has exactly one parent, so a
    child-walk cannot re-enter a node. Every node is checked, not only those
    reachable from the root, because a self-parented node is otherwise reported as
    an unreachable branch and the caller is sent looking for the wrong fault.
    """
    ids = [node.id for node in spec.nodes]
    duplicates = {node_id for node_id in ids if ids.count(node_id) > 1}
    if duplicates:
        raise HTTPException(
            status_code=400,
            detail=f"Duplicate node ids: {sorted(duplicates)}",
        )

    clashes = {
        node_id: domain_id
        for node_id, domain_id in known_node_ids.items()
        if node_id in set(ids)
    }
    if clashes:
        raise HTTPException(
            status_code=400,
            detail=(
                "Node ids must be unique across every domain; these already belong "
                f"elsewhere: {clashes}. /tree/{{id}} and the override audit resolve a "
                "bare id without being told its domain."
            ),
        )

    roots = [node.id for node in spec.nodes if _is_root(node.parent)]
    if len(roots) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A domain needs exactly one root, got {len(roots)}: {roots}. "
                "root_in/2 cannot pick one, and a tree with two roots is not a tree."
            ),
        )

    parent_of = {
        node.id: (_ROOT_PARENT if _is_root(node.parent) else str(node.parent))
        for node in spec.nodes
    }
    for node in spec.nodes:
        parent = parent_of[node.id]
        if parent != _ROOT_PARENT and parent not in parent_of:
            raise HTTPException(
                status_code=400,
                detail=f"Node '{node.id}' names parent '{parent}', which is not in the domain",
            )

    children: Dict[str, List[str]] = {node_id: [] for node_id in parent_of}
    for node_id, parent in parent_of.items():
        if parent != _ROOT_PARENT:
            children[parent].append(node_id)

    for node in spec.nodes:
        chain: set[str] = set()
        cursor = parent_of[node.id]
        while cursor != _ROOT_PARENT:
            if cursor in chain:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cycle through node '{cursor}'. The walk recurses over children "
                        "with no depth bound, so this would hang rather than fail."
                    ),
                )
            chain.add(cursor)
            cursor = parent_of[cursor]

    seen: set[str] = {roots[0]}
    frontier = list(children[roots[0]])
    while frontier:
        node_id = frontier.pop()
        seen.add(node_id)
        frontier.extend(children[node_id])

    unreachable = sorted(set(parent_of) - seen)
    if unreachable:
        raise HTTPException(
            status_code=400,
            detail=f"Nodes unreachable from root '{roots[0]}': {unreachable}",
        )


async def _known_node_ids(
    prolog: PrologClient, exclude_domain: str
) -> Dict[str, str]:
    """node id -> domain id, for every active domain.

    Read rather than cached: domains and overrides change under the API, and a stale
    map would let a clashing id through to a node that then resolves ambiguously.
    """
    owners: Dict[str, str] = {}
    for domain in await prolog.active_domains():
        if domain["id"] == exclude_domain:
            continue
        for node in await prolog.get_all_nodes(domain["id"]):
            owners[str(node.get("id", ""))] = domain["id"]
    return owners


@router.post("", response_model=DomainResponse, status_code=201)
async def create_domain(
    spec: DomainRequest,
    prolog: PrologClient = Depends(get_prolog_client),
    session: AsyncSession = Depends(db.get_db),
):
    """Register a new domain. It stays inactive until it is activated."""
    if await prolog.domain_exists(spec.id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Domain '{spec.id}' already exists. Re-registering an active domain "
                "would replace the tree under decisions already in flight; deactivate "
                "it first, or choose another id."
            ),
        )

    _validate_structure(spec, await _known_node_ids(prolog, exclude_domain=spec.id))

    if not await prolog.conditions_readable([n.condition for n in spec.nodes]):
        raise HTTPException(
            status_code=400,
            detail=(
                "One or more conditions are not readable Prolog. A condition the "
                "evaluator cannot match leaves a node that never fires."
            ),
        )

    nodes = [node.model_dump() for node in spec.nodes]

    # Prolog first: a failed assert means nothing was registered, whereas a failure
    # after a successful Prolog write would leave a domain this process can answer
    # for and the next startup cannot restore.
    try:
        await prolog.register_domain(spec.id, spec.label, spec.input_keys, nodes)
    except PrologError as error:
        logger.error("Prolog registration failed for domain %s: %s", spec.id, error)
        raise HTTPException(
            status_code=500, detail=f"Could not register the domain: {error}"
        ) from error

    await db.save_domain(
        session,
        spec.id,
        spec.label,
        list(spec.input_keys),
        nodes,
        active=False,
    )
    await db.log_audit(
        session,
        entity_type="domain",
        subject=spec.id,
        operation="CREATE",
        old_value=None,
        new_value={
            "label": spec.label,
            "input_keys": list(spec.input_keys),
            "nodes": nodes,
            "active": False,
        },
        owner=None,
        diff_hash=_diff_hash(spec.id, {"op": "CREATE", "nodes": nodes}),
        commit=True,
    )

    logger.info("Registered domain %s with %d nodes (inactive)", spec.id, len(nodes))
    return DomainResponse(
        id=spec.id,
        label=spec.label,
        input_keys=list(spec.input_keys),
        node_count=len(nodes),
        active=False,
    )


@router.post("/{domain_id}/activate", response_model=DomainResponse)
async def activate_domain(
    domain_id: str,
    owner: str,
    prolog: PrologClient = Depends(get_prolog_client),
    session: AsyncSession = Depends(db.get_db),
):
    """Make a runtime domain routable by /decide and /tree.

    Takes `owner` as a query parameter and records it, because activation is the
    moment someone takes responsibility for a tree's logic.
    """
    stored = await db.get_domain(session, domain_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"Domain '{domain_id}' not found")

    try:
        await prolog.activate_domain(domain_id)
    except PrologError as error:
        logger.error("Prolog activation failed for domain %s: %s", domain_id, error)
        raise HTTPException(
            status_code=500, detail=f"Could not activate the domain: {error}"
        ) from error

    await db.set_domain_active(session, domain_id, True)
    await db.log_audit(
        session,
        entity_type="domain",
        subject=domain_id,
        operation="ACTIVATE",
        old_value={"active": stored["active"]},
        new_value={"active": True},
        owner=owner,
        diff_hash=_diff_hash(domain_id, {"op": "ACTIVATE", "owner": owner}),
        commit=True,
    )

    nodes = len(await prolog.get_all_nodes(domain_id))
    logger.info("Activated domain %s by %s", domain_id, owner)
    return DomainResponse(
        id=stored["id"],
        label=stored["label"],
        input_keys=stored["input_keys"],
        node_count=nodes,
        active=True,
    )


@router.post("/{domain_id}/deactivate", response_model=DomainResponse)
async def deactivate_domain(
    domain_id: str,
    owner: str,
    prolog: PrologClient = Depends(get_prolog_client),
    session: AsyncSession = Depends(db.get_db),
):
    """Take a runtime domain out of the routable set.

    The domain stays registered and visible in /trees, but /decide and
    /tree stop considering it. Takes `owner` as a query parameter and
    records it, for the same reason activation does: this is the moment
    someone decides a tree should stop answering.

    Prolog is written first, as in activation: the effect is that
    /decide stops routing to the domain immediately, and a failure of
    the database write afterwards is reported rather than swallowed, so
    the operator knows the deactivation did not persist. Deactivating
    an already-inactive domain succeeds and is still audited.
    """
    stored = await db.get_domain(session, domain_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"Domain '{domain_id}' not found")

    try:
        await prolog.deactivate_domain(domain_id)
    except PrologError as error:
        logger.error("Prolog deactivation failed for domain %s: %s", domain_id, error)
        raise HTTPException(
            status_code=500, detail=f"Could not deactivate the domain: {error}"
        ) from error

    await db.set_domain_active(session, domain_id, False)
    await db.log_audit(
        session,
        entity_type="domain",
        subject=domain_id,
        operation="DEACTIVATE",
        old_value={"active": stored["active"]},
        new_value={"active": False},
        owner=owner,
        diff_hash=_diff_hash(domain_id, {"op": "DEACTIVATE", "owner": owner}),
        commit=True,
    )

    nodes = len(await prolog.get_all_nodes(domain_id))
    logger.info("Deactivated domain %s by %s", domain_id, owner)
    return DomainResponse(
        id=stored["id"],
        label=stored["label"],
        input_keys=stored["input_keys"],
        node_count=nodes,
        active=False,
    )


@router.delete("/{domain_id}", response_model=DomainResponse)
async def delete_domain(
    domain_id: str,
    owner: str,
    prolog: PrologClient = Depends(get_prolog_client),
    session: AsyncSession = Depends(db.get_db),
):
    """Remove a runtime domain. Its audit trail remains.

    Only a domain registered through POST /memory/domain can be deleted:
    the four kb domains are code that shipped in the image, and code is
    not something a request may remove. The nodes are read before the
    registry drops them so the audit row carries the whole spec, which
    is what makes the deletion reconstructable from the trail alone.

    An active domain may be deleted directly — the operation is audited
    and takes effect immediately — but the intended flow is to deactivate
    first, then delete, so a tree leaves service before it leaves the
    registry.
    """
    stored = await db.get_domain(session, domain_id)
    if stored is None:
        if await prolog.domain_exists(domain_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Domain '{domain_id}' ships in the image as Prolog "
                    "code. Only a domain registered through "
                    "POST /memory/domain can be deleted."
                ),
            )
        raise HTTPException(status_code=404, detail=f"Domain '{domain_id}' not found")

    # Read the nodes before the registry drops them: the audit row has
    # to carry the whole tree for the deletion to be reconstructable.
    nodes = [
        {
            "id": str(node.get("id", "")),
            "parent": str(node.get("parent", "")),
            "condition": str(node.get("condition", "")),
            "action": str(node.get("action", "")),
            "owner": str(node.get("owner", "")),
            "version": int(node.get("version") or 1),
        }
        for node in await prolog.get_all_nodes(domain_id)
    ]

    try:
        await prolog.unregister_domain(domain_id)
    except PrologError as error:
        logger.error(
            "Prolog unregistration failed for domain %s: %s", domain_id, error
        )
        raise HTTPException(
            status_code=500, detail=f"Could not delete the domain: {error}"
        ) from error

    if not await db.delete_domain(session, domain_id):
        # The registry dropped the domain but the database did not, so
        # the next startup would restore it from the database. That has
        # to be loud rather than reported as a success.
        raise HTTPException(
            status_code=500,
            detail=f"Could not delete domain '{domain_id}' from the database",
        )

    await db.log_audit(
        session,
        entity_type="domain",
        subject=domain_id,
        operation="DELETE",
        old_value={
            "label": stored["label"],
            "input_keys": stored["input_keys"],
            "active": stored["active"],
            "nodes": nodes,
        },
        new_value=None,
        owner=owner,
        diff_hash=_diff_hash(domain_id, {"op": "DELETE", "owner": owner}),
        commit=True,
    )

    logger.info("Deleted domain %s by %s", domain_id, owner)
    return DomainResponse(
        id=stored["id"],
        label=stored["label"],
        input_keys=stored["input_keys"],
        node_count=len(nodes),
        active=stored["active"],
    )


@router.get("/{domain_id}")
async def get_domain_detail(
    domain_id: str,
    prolog: PrologClient = Depends(get_prolog_client),
):
    """One domain's registry entry."""
    entries = {d["id"]: d for d in await prolog.registry()}
    entry = entries.get(domain_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Domain '{domain_id}' not found")
    nodes = await prolog.get_all_nodes(domain_id)
    return {
        "id": entry["id"],
        "label": entry["label"],
        "input_keys": entry["input_keys"],
        "node_count": len(nodes),
        "active": entry["active"],
        "source": entry["source"],
        "nodes": [
            {
                "id": str(node.get("id", "")),
                "parent": str(node.get("parent", "")),
                "condition": str(node.get("condition", "")),
                "action": str(node.get("action", "")),
                "owner": str(node.get("owner", "")),
                "version": int(node.get("version") or 1),
            }
            for node in nodes
        ],
    }


@router.get("/{domain_id}/audit")
async def get_domain_audit(
    domain_id: str,
    limit: int = 100,
    session: AsyncSession = Depends(db.get_db),
):
    """Registration and activation history for one domain, newest first."""
    entries = await db.get_audit(
        session, subject=domain_id, entity_type="domain", limit=limit
    )
    return {
        "domain_id": domain_id,
        "entries": [
            {
                "id": entry["id"],
                "operation": entry["operation"],
                "old_value": entry["old_value"],
                "new_value": entry["new_value"],
                "owner": entry["owner"],
                "timestamp": entry["timestamp"],
                "diff_hash": entry["diff_hash"],
            }
            for entry in entries
        ],
        "count": len(entries),
    }