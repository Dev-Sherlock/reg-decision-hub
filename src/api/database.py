"""
PostgreSQL persistence for the symbolic memory.

This layer is the durable system of record. The embedded Prolog KB is a
derived, in-memory view that is rebuilt from these tables on API startup, so
losing the API container never loses knowledge.

Tables
    symbolic_facts  (subject, predicate) -> JSONB value + pgvector embedding
    symbolic_rules  name -> Prolog head/body text
    audit_log       append-only trail for every change to either

Vector handling: embeddings are bound as text literals and cast with
`CAST(:param AS vector)`. That avoids a hard dependency on the pgvector
Python bindings while still getting indexed ANN search.
"""
import os
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/regdecision"
)
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "384"))

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency for database session."""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SYMBOLIC_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS symbolic_facts (
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    value       JSONB,
    embedding   VECTOR({dim}),
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (subject, predicate)
)
"""

_SYMBOLIC_RULES_DDL = """
CREATE TABLE IF NOT EXISTS symbolic_rules (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    head        TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
)
"""

_AUDIT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          SERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    subject     TEXT,
    predicate   TEXT,
    rule_name   TEXT,
    operation   TEXT NOT NULL,
    old_value   JSONB,
    new_value   JSONB,
    owner       TEXT,
    timestamp   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    diff_hash   TEXT
)
"""

# A runtime decision domain: the declared input keys plus one row per node. This is
# the system of record for domains created through POST /memory/domain, so Prolog is
# again derived state that bootstrap_kb_from_db rebuilds on startup. `active` lives
# here rather than in Prolog for the same reason.
#
# Nodes are rows, not a JSON blob, because a node's condition is Prolog source and
# nothing queries across nodes yet — the alternative would be a column whose only
# reader is "give me the whole tree back".
_DOMAINS_DDL = """
CREATE TABLE IF NOT EXISTS decision_domains (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    input_keys  JSONB NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
)
"""

_DOMAIN_NODES_DDL = """
CREATE TABLE IF NOT EXISTS decision_domain_nodes (
    domain_id   TEXT NOT NULL REFERENCES decision_domains(id) ON DELETE CASCADE,
    node_id     TEXT NOT NULL,
    parent      TEXT NOT NULL,
    condition   TEXT NOT NULL,
    action      TEXT NOT NULL,
    owner       TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (domain_id, node_id)
)
"""


async def _migrate_legacy_audit_log(conn) -> None:
    """Fold a pre-symbolic-memory audit_log into the generalized schema.

    Earlier revisions stored node overrides only, in a table with a NOT NULL
    node_id column. Renaming rather than dropping keeps the history; the old
    rows are copied across mapped onto the new shape.
    """
    legacy = await conn.execute(
        text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'audit_log' AND column_name = 'node_id'
        """)
    )
    if legacy.first() is None:
        return

    logger.info("Migrating legacy node_id audit_log to the generalized schema")
    await conn.execute(text("ALTER TABLE audit_log RENAME TO audit_log_legacy"))
    await conn.execute(text(_AUDIT_LOG_DDL))
    await conn.execute(text("""
        INSERT INTO audit_log (
            entity_type, subject, operation, old_value, new_value, owner, timestamp, diff_hash
        )
        SELECT
            'node',
            node_id,
            'UPDATE',
            jsonb_build_object('action', old_action),
            jsonb_build_object('action', new_action),
            owner,
            timestamp,
            diff_hash
        FROM audit_log_legacy
    """))
    await conn.execute(text("ALTER TABLE audit_log_legacy RENAME TO audit_log_legacy_node"))


async def init_db() -> None:
    """Create the extension, tables and indexes, and migrate legacy schemas."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text(_SYMBOLIC_FACTS_DDL.format(dim=EMBEDDING_DIM)))
        await conn.execute(text(_SYMBOLIC_RULES_DDL))
        await _migrate_legacy_audit_log(conn)
        await conn.execute(text(_AUDIT_LOG_DDL))
        await conn.execute(text(_DOMAINS_DDL))
        await conn.execute(text(_DOMAIN_NODES_DDL))

        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_symbolic_facts_subject "
            "ON symbolic_facts(subject)",
            "CREATE INDEX IF NOT EXISTS idx_symbolic_facts_predicate "
            "ON symbolic_facts(predicate)",
            "CREATE INDEX IF NOT EXISTS idx_symbolic_facts_updated "
            "ON symbolic_facts(updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_symbolic_facts_embedding "
            "ON symbolic_facts USING hnsw (embedding vector_cosine_ops)",
            "CREATE INDEX IF NOT EXISTS idx_symbolic_rules_name ON symbolic_rules(name)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_subject ON audit_log(subject)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_rule_name ON audit_log(rule_name)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_audit_log_entity_type ON audit_log(entity_type)",
            "CREATE INDEX IF NOT EXISTS idx_decision_domain_nodes_domain "
            "ON decision_domain_nodes(domain_id)",
        ):
            await conn.execute(text(statement))
    logger.info("Database schema ready (vector dim %d)", EMBEDDING_DIM)


async def close_db() -> None:
    await engine.dispose()


# ---------------------------------------------------------------------------
# JSONB value helpers
#
# PostgreSQL JSONB cannot hold a bare scalar, so values are stored wrapped as
# {"v": <value>} and unwrapped on read. That keeps 22, "abc", true, null and
# [1,2] all representable and distinct.
#
# A JSON null value is stored as SQL NULL rather than {"v": null}, and reads
# back as None — which is the same thing to a JSON client, so nothing is lost.
# It also lets audit_log distinguish "no previous value" from "was null".
# ---------------------------------------------------------------------------

_VALUE_WRAPPER = "v"


def _wrap(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps({_VALUE_WRAPPER: value})


def _unwrap(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw.get(_VALUE_WRAPPER) if isinstance(raw, dict) else raw
    return raw


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


async def upsert_fact(
    db: AsyncSession,
    subject: str,
    predicate: str,
    value: Any,
    embedding: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Insert or replace one fact. Returns the previous value, if any.

    A fact is keyed by (subject, predicate) — asserting an existing pair
    replaces the value and bumps `version`, matching the Prolog KB's upsert
    semantics in symbolic_memory:assert_fact/3.

    Pass `embedding=None` to leave any existing vector untouched; that is what
    callers want when the embedding model is unavailable and the stored text
    has not changed.
    """
    existing = await get_fact(db, subject, predicate)
    embedding_literal = _vector_literal(embedding)

    if existing is None:
        await db.execute(
            text("""
                INSERT INTO symbolic_facts (subject, predicate, value, embedding)
                VALUES (:subject, :predicate, CAST(:value AS jsonb), CAST(:embedding AS vector))
            """),
            {
                "subject": subject,
                "predicate": predicate,
                "value": _wrap(value),
                "embedding": embedding_literal,
            },
        )
        old_value = None
        operation = "INSERT"
    else:
        old_value = existing["value"]
        if embedding_literal is None:
            await db.execute(
                text("""
                    UPDATE symbolic_facts
                    SET value = CAST(:value AS jsonb), updated_at = NOW(),
                        version = version + 1
                    WHERE subject = :subject AND predicate = :predicate
                """),
                {
                    "subject": subject,
                    "predicate": predicate,
                    "value": _wrap(value),
                },
            )
        else:
            await db.execute(
                text("""
                    UPDATE symbolic_facts
                    SET value = CAST(:value AS jsonb),
                        embedding = CAST(:embedding AS vector),
                        updated_at = NOW(),
                        version = version + 1
                    WHERE subject = :subject AND predicate = :predicate
                """),
                {
                    "subject": subject,
                    "predicate": predicate,
                    "value": _wrap(value),
                    "embedding": embedding_literal,
                },
            )
        operation = "UPDATE"

    await db.commit()
    return {"operation": operation, "old_value": old_value}


async def get_fact(db: AsyncSession, subject: str, predicate: str) -> Optional[Dict[str, Any]]:
    """Read one fact, or None. Never returns the embedding vector."""
    result = await db.execute(
        text("""
            SELECT subject, predicate, value, version, created_at, updated_at
            FROM symbolic_facts
            WHERE subject = :subject AND predicate = :predicate
        """),
        {"subject": subject, "predicate": predicate},
    )
    row = result.mappings().first()
    if row is None:
        return None
    return {
        "subject": row["subject"],
        "predicate": row["predicate"],
        "value": _unwrap(row["value"]),
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def list_facts(
    db: AsyncSession,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """List facts, optionally filtered. None means "any"; use '*' for the same."""
    conditions: List[str] = []
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if subject:
        conditions.append("subject = :subject")
        params["subject"] = subject
    if predicate:
        conditions.append("predicate = :predicate")
        params["predicate"] = predicate
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    # Without a filter, order by key so pagination is stable.
    order = "ORDER BY subject, predicate" if not conditions else "ORDER BY updated_at DESC"

    result = await db.execute(
        text(f"""
            SELECT subject, predicate, value, version, created_at, updated_at
            FROM symbolic_facts
            {where}
            {order}
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    return [
        {
            "subject": row["subject"],
            "predicate": row["predicate"],
            "value": _unwrap(row["value"]),
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in result.mappings().all()
    ]


async def fetch_all_facts(db: AsyncSession) -> List[Dict[str, Any]]:
    """Every fact, for bootstrapping the Prolog KB on startup."""
    result = await db.execute(
        text("SELECT subject, predicate, value FROM symbolic_facts")
    )
    return [
        {"subject": row["subject"], "predicate": row["predicate"], "value": _unwrap(row["value"])}
        for row in result.mappings().all()
    ]


async def delete_fact(db: AsyncSession, subject: str, predicate: str) -> Optional[Dict[str, Any]]:
    """Delete a fact. Returns the removed row, or None if it did not exist."""
    result = await db.execute(
        text("""
            DELETE FROM symbolic_facts
            WHERE subject = :subject AND predicate = :predicate
            RETURNING subject, predicate, value
        """),
        {"subject": subject, "predicate": predicate},
    )
    row = result.mappings().first()
    await db.commit()
    if row is None:
        return None
    return {"subject": row["subject"], "predicate": row["predicate"], "value": _unwrap(row["value"])}


async def count_facts(db: AsyncSession) -> int:
    result = await db.execute(text("SELECT COUNT(*) AS n FROM symbolic_facts"))
    return int(result.mappings().first()["n"])


def _vector_literal(embedding: Optional[List[float]]) -> Optional[str]:
    """Format a float list as a pgvector text literal."""
    if embedding is None:
        return None
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(
            f"embedding has {len(embedding)} dimensions, expected {EMBEDDING_DIM} "
            "(set EMBEDDING_DIM to match your embedding model)"
        )
    return "[" + ",".join(repr(float(component)) for component in embedding) + "]"


async def search_facts_by_embedding(
    db: AsyncSession,
    embedding: List[float],
    top_k: int = 5,
    subject: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Top-k facts by cosine similarity to `embedding`.

    Rows with a NULL embedding are invisible to this query by construction, so
    a fact stored while the embedding model was unavailable simply does not
    show up in semantic results until it is re-embedded.
    """
    literal = _vector_literal(embedding)
    params: Dict[str, Any] = {"embedding": literal, "top_k": top_k}
    subject_clause = ""
    if subject:
        subject_clause = "AND subject = :subject"
        params["subject"] = subject

    result = await db.execute(
        text(f"""
            SELECT subject, predicate, value, version, updated_at,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM symbolic_facts
            WHERE embedding IS NOT NULL {subject_clause}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
        """),
        params,
    )
    return [
        {
            "subject": row["subject"],
            "predicate": row["predicate"],
            "value": _unwrap(row["value"]),
            "version": row["version"],
            "updated_at": row["updated_at"],
            "similarity": float(row["similarity"]),
        }
        for row in result.mappings().all()
    ]


async def search_facts_by_keyword(
    db: AsyncSession,
    query: str,
    top_k: int = 5,
    subject: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fallback retrieval when the embedding model is unavailable.

    Ranks by where the match landed rather than by vector distance, so the
    `similarity` field is a coarse 0..1 score. Results are still useful, just
    lexical instead of semantic.
    """
    pattern = f"%{query}%"
    subject_clause = ""
    params: Dict[str, Any] = {"pattern": pattern, "top_k": top_k}
    if subject:
        subject_clause = "AND subject = :subject"
        params["subject"] = subject

    result = await db.execute(
        text(f"""
            SELECT subject, predicate, value, version, updated_at,
                   GREATEST(
                       CASE WHEN subject ILIKE :pattern THEN 0.6 ELSE 0 END,
                       CASE WHEN predicate ILIKE :pattern THEN 0.8 ELSE 0 END,
                       CASE WHEN value::text ILIKE :pattern THEN 0.5 ELSE 0 END
                   ) AS similarity
            FROM symbolic_facts
            WHERE (
                subject ILIKE :pattern
                OR predicate ILIKE :pattern
                OR value::text ILIKE :pattern
            ) {subject_clause}
            ORDER BY similarity DESC, updated_at DESC
            LIMIT :top_k
        """),
        params,
    )
    return [
        {
            "subject": row["subject"],
            "predicate": row["predicate"],
            "value": _unwrap(row["value"]),
            "version": row["version"],
            "updated_at": row["updated_at"],
            "similarity": float(row["similarity"]),
        }
        for row in result.mappings().all()
        if row["similarity"] > 0
    ]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


async def upsert_rule(db: AsyncSession, name: str, head: str, body: str) -> str:
    """Insert or replace a rule definition. Returns 'INSERT' or 'UPDATE'."""
    existing = await get_rule(db, name)
    if existing is None:
        await db.execute(
            text("""
                INSERT INTO symbolic_rules (name, head, body)
                VALUES (:name, :head, :body)
            """),
            {"name": name, "head": head, "body": body},
        )
        operation = "INSERT"
    else:
        await db.execute(
            text("""
                UPDATE symbolic_rules
                SET head = :head, body = :body, updated_at = NOW()
                WHERE name = :name
            """),
            {"name": name, "head": head, "body": body},
        )
        operation = "UPDATE"
    await db.commit()
    return operation


async def get_rule(db: AsyncSession, name: str) -> Optional[Dict[str, Any]]:
    result = await db.execute(
        text("SELECT name, head, body, created_at, updated_at FROM symbolic_rules WHERE name = :name"),
        {"name": name},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def list_rules(db: AsyncSession) -> List[Dict[str, Any]]:
    result = await db.execute(
        text("SELECT name, head, body, created_at, updated_at FROM symbolic_rules ORDER BY name")
    )
    return [dict(row) for row in result.mappings().all()]


async def fetch_all_rules(db: AsyncSession) -> List[Dict[str, Any]]:
    """Every rule, for bootstrapping the Prolog KB on startup."""
    return await list_rules(db)


async def delete_rule(db: AsyncSession, name: str) -> Optional[Dict[str, Any]]:
    result = await db.execute(
        text("DELETE FROM symbolic_rules WHERE name = :name RETURNING name, head, body"),
        {"name": name},
    )
    row = result.mappings().first()
    await db.commit()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Runtime decision domains
# ---------------------------------------------------------------------------


async def get_domain(db: AsyncSession, domain_id: str) -> Optional[Dict[str, Any]]:
    result = await db.execute(
        text("SELECT id, label, input_keys, active FROM decision_domains WHERE id = :id"),
        {"id": domain_id},
    )
    row = result.mappings().first()
    if not row:
        return None
    return {
        "id": row["id"],
        "label": row["label"],
        "input_keys": list(row["input_keys"] or []),
        "active": bool(row["active"]),
    }


async def save_domain(
    db: AsyncSession,
    domain_id: str,
    label: str,
    input_keys: List[str],
    nodes: List[Dict[str, Any]],
    active: bool,
) -> str:
    """Insert or replace a runtime domain and its nodes. Returns INSERT or UPDATE.

    Replacement rather than merge: a re-registered domain must not keep nodes the
    new spec dropped, or the tree would have branches the caller never wrote.
    """
    existing = await get_domain(db, domain_id)
    if existing is None:
        await db.execute(
            text("""
                INSERT INTO decision_domains (id, label, input_keys, active)
                VALUES (:id, :label, CAST(:input_keys AS JSONB), :active)
            """),
            {
                "id": domain_id,
                "label": label,
                "input_keys": json.dumps(list(input_keys)),
                "active": active,
            },
        )
        operation = "INSERT"
    else:
        await db.execute(
            text("""
                UPDATE decision_domains
                   SET label = :label,
                       input_keys = CAST(:input_keys AS JSONB),
                       active = :active,
                       updated_at = NOW()
                 WHERE id = :id
            """),
            {
                "id": domain_id,
                "label": label,
                "input_keys": json.dumps(list(input_keys)),
                "active": active,
            },
        )
        operation = "UPDATE"

    await db.execute(
        text("DELETE FROM decision_domain_nodes WHERE domain_id = :id"),
        {"id": domain_id},
    )
    for node in nodes:
        await db.execute(
            text("""
                INSERT INTO decision_domain_nodes
                    (domain_id, node_id, parent, condition, action, owner, version)
                VALUES (:domain_id, :node_id, :parent, :condition, :action, :owner, :version)
            """),
            {
                "domain_id": domain_id,
                "node_id": node["id"],
                "parent": node.get("parent") or "none",
                "condition": node.get("condition") or "true",
                "action": node["action"],
                "owner": node["owner"],
                "version": int(node.get("version") or 1),
            },
        )

    await db.commit()
    return operation


async def set_domain_active(db: AsyncSession, domain_id: str, active: bool) -> bool:
    """Activate or deactivate a runtime domain. False if it does not exist."""
    result = await db.execute(
        text("""
            UPDATE decision_domains SET active = :active, updated_at = NOW()
             WHERE id = :id
            RETURNING id
        """),
        {"id": domain_id, "active": active},
    )
    found = result.mappings().first() is not None
    await db.commit()
    return found


async def delete_domain(db: AsyncSession, domain_id: str) -> bool:
    """Remove a runtime domain and its nodes. False if it does not exist.

    Nodes first, then the domain row: the two deletes are one logical
    removal, and the foreign key means the domain row cannot go while
    any of its nodes remain. The audit rows are untouched — they are
    append-only, so a deleted domain's history outlives it, which is
    what makes the deletion itself reconstructable.
    """
    await db.execute(
        text("DELETE FROM decision_domain_nodes WHERE domain_id = :id"),
        {"id": domain_id},
    )
    result = await db.execute(
        text("DELETE FROM decision_domains WHERE id = :id RETURNING id"),
        {"id": domain_id},
    )
    found = result.mappings().first() is not None
    await db.commit()
    return found


async def fetch_all_domains(db: AsyncSession) -> List[Dict[str, Any]]:
    """Every runtime domain with its nodes, for bootstrapping the Prolog registry.

    Ordered by id and its nodes by node_id so a reload is deterministic.
    """
    domains_result = await db.execute(
        text("SELECT id, label, input_keys, active FROM decision_domains ORDER BY id")
    )
    nodes_result = await db.execute(
        text("""
            SELECT domain_id, node_id, parent, condition, action, owner, version
              FROM decision_domain_nodes
             ORDER BY domain_id, node_id
        """)
    )

    nodes_by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for row in nodes_result.mappings().all():
        nodes_by_domain.setdefault(row["domain_id"], []).append(
            {
                "id": row["node_id"],
                "parent": row["parent"],
                "condition": row["condition"],
                "action": row["action"],
                "owner": row["owner"],
                "version": int(row["version"] or 1),
            }
        )

    return [
        {
            "id": row["id"],
            "label": row["label"],
            "input_keys": list(row["input_keys"] or []),
            "active": bool(row["active"]),
            "nodes": nodes_by_domain.get(row["id"], []),
        }
        for row in domains_result.mappings().all()
    ]


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


async def log_audit(
    db: AsyncSession,
    entity_type: str,
    operation: str,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    rule_name: Optional[str] = None,
    old_value: Any = None,
    new_value: Any = None,
    owner: Optional[str] = None,
    diff_hash: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Append one audit entry. Does not commit when commit=False, so it can
    join a caller's transaction."""
    await db.execute(
        text("""
            INSERT INTO audit_log (
                entity_type, subject, predicate, rule_name,
                operation, old_value, new_value, owner, diff_hash
            ) VALUES (
                :entity_type, :subject, :predicate, :rule_name,
                :operation, CAST(:old_value AS jsonb), CAST(:new_value AS jsonb),
                :owner, :diff_hash
            )
        """),
        {
            "entity_type": entity_type,
            "subject": subject,
            "predicate": predicate,
            "rule_name": rule_name,
            "operation": operation,
            "old_value": _wrap(old_value),
            "new_value": _wrap(new_value),
            "owner": owner,
            "diff_hash": diff_hash,
        },
    )
    if commit:
        await db.commit()


async def get_audit(
    db: AsyncSession,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    rule_name: Optional[str] = None,
    entity_type: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Audit history, newest first."""
    conditions: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    for column, value in (
        ("subject", subject),
        ("predicate", predicate),
        ("rule_name", rule_name),
        ("entity_type", entity_type),
    ):
        if value:
            conditions.append(f"{column} = :{column}")
            params[column] = value
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    result = await db.execute(
        text(f"""
            SELECT id, entity_type, subject, predicate, rule_name, operation,
                   old_value, new_value, owner, timestamp, diff_hash
            FROM audit_log
            {where}
            ORDER BY timestamp DESC, id DESC
            LIMIT :limit
        """),
        params,
    )
    return [
        {
            "id": row["id"],
            "entity_type": row["entity_type"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "rule_name": row["rule_name"],
            "operation": row["operation"],
            "old_value": _unwrap(row["old_value"]),
            "new_value": _unwrap(row["new_value"]),
            "owner": row["owner"],
            "timestamp": row["timestamp"],
            "diff_hash": row["diff_hash"],
        }
        for row in result.mappings().all()
    ]


# ---------------------------------------------------------------------------
# Startup bootstrap
# ---------------------------------------------------------------------------


async def bootstrap_kb_from_db(db: Optional[AsyncSession] = None) -> int:
    """Reload the Prolog KB from PostgreSQL. Returns the fact count.

    Pass a session to read through someone else's transaction — the API passes
    none and gets its own, while tests pass the transactional session so a
    reload sees uncommitted fixtures.

    Runtime decision domains are restored here too, and only the active ones are
    activated in Prolog: the database records that a domain exists and whether it
    has been activated, while tree_registry holds the nodes and the activation
    flag. Same order as every other write — PostgreSQL first, Prolog second.

    The Prolog import is function-local to avoid a circular import: the
    Prolog client depends on this module for persistence, and this function
    needs the client.
    """
    from src.api.prolog.client import get_prolog_client

    if db is not None:
        facts = await fetch_all_facts(db)
        rules = await fetch_all_rules(db)
        domains = await fetch_all_domains(db)
    else:
        async with async_session_maker() as session:
            facts = await fetch_all_facts(session)
            rules = await fetch_all_rules(session)
            domains = await fetch_all_domains(session)

    client = await get_prolog_client()
    loaded = await client.load_from_db(facts, rules)

    restored = 0
    for domain in domains:
        try:
            await client.register_domain(
                domain["id"],
                domain["label"],
                list(domain["input_keys"]),
                domain["nodes"],
            )
            if domain["active"]:
                await client.activate_domain(domain["id"])
            restored += 1
        except Exception as error:
            # One malformed domain must not cost us the facts and rules, and must
            # not stop the API from starting: /health and /trees report the gap.
            logger.error("Could not restore domain %s into Prolog: %s", domain["id"], error)

    logger.info(
        "Bootstrapped Prolog KB: %d facts, %d rules, %d runtime domains",
        loaded,
        len(rules),
        restored,
    )
    return loaded
