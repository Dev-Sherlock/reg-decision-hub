"""
/memory/node — governed overrides for the decision-tree domain.

The generalisation of the old PUT /node/{id}: an override is now a governed
change to a knowledge-base entry, checked against the KB's own permission rule
(can_write/2 — only the owning agent may change its nodes), written to both
stores and recorded in the audit trail.

Fact overrides live alongside these at POST /memory/fact/{subject}/{predicate}/override.
Both write to the same generalized audit_log table, distinguished by entity_type.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import database as db
from src.api.prolog.client import PrologClient, PrologError, get_prolog_client
from src.models.schemas import OverrideRequest, OverrideResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory/node", tags=["overrides"])


def _diff_hash(node_id: str, old_action: str, new_action: str, owner: str) -> str:
    payload = json.dumps(
        {"node": node_id, "old": old_action, "new": new_action, "owner": owner},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@router.post("/{node_id}/override", response_model=OverrideResponse)
async def override_node(
    node_id: str,
    request: OverrideRequest,
    prolog: PrologClient = Depends(get_prolog_client),
    session: AsyncSession = Depends(db.get_db),
):
    """Change a node's action, if the requesting agent owns the node.

    Permission is decided by the KB, not by the API, so a different domain can
    implement its own rule by shipping a different can_write/2.
    """
    node = await prolog.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")

    old_action = node.get("action")
    if not await prolog.can_write(request.owner, node_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Agent '{request.owner}' is not authorised to modify node '{node_id}' "
                f"(owned by '{node.get('owner')}')"
            ),
        )

    try:
        updated = await prolog.set_node_action(node_id, request.action, request.owner)
    except PrologError as error:
        logger.error("Prolog update failed for %s: %s", node_id, error)
        raise HTTPException(status_code=500, detail=f"Failed to update node: {error}") from error
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update node in Prolog")

    timestamp = datetime.now(timezone.utc)
    diff_hash = _diff_hash(node_id, str(old_action), request.action, request.owner)

    await db.log_audit(
        session,
        entity_type="node",
        subject=node_id,
        operation="UPDATE",
        old_value={"action": old_action},
        new_value={"action": request.action},
        owner=request.owner,
        diff_hash=diff_hash,
        commit=True,
    )

    return OverrideResponse(
        success=True,
        node_id=node_id,
        old_action=str(old_action),
        new_action=request.action,
        owner=request.owner,
        timestamp=timestamp,
        diff_hash=diff_hash,
    )


@router.get("/{node_id}/audit")
async def get_node_audit(
    node_id: str,
    limit: int = 100,
    session: AsyncSession = Depends(db.get_db),
):
    """Override history for one node, newest first."""
    entries = await db.get_audit(session, subject=node_id, entity_type="node", limit=limit)
    return {
        "node_id": node_id,
        "entries": [
            {
                "id": entry["id"],
                "old_action": (entry["old_value"] or {}).get("action"),
                "new_action": (entry["new_value"] or {}).get("action"),
                "owner": entry["owner"],
                "timestamp": entry["timestamp"],
                "diff_hash": entry["diff_hash"],
            }
            for entry in entries
        ],
        "count": len(entries),
    }
