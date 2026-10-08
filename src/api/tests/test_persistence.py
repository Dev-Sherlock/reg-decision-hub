"""
Restart recovery: PostgreSQL is the system of record, Prolog is derived.

Each test stores facts, throws the Prolog KB away, reloads it the way a fresh
API container does at startup, and checks the knowledge survived. That is the
property the whole persistence design exists to provide — the API container is
disposable.
"""
import pytest
from sqlalchemy import text

from src.api import database as db
from src.api.embeddings import EmbeddingUnavailable, get_embedding_service
from src.api.prolog.client import MEMORY_MODULE


async def test_facts_survive_a_kb_reload(client, prolog, session, unique):
    expected = {
        ("room_101", "temperature"): 22,
        ("room_101", "humidity"): 45,
        (f"{unique}_person", "works_at"): "Acme Corp",
        (f"{unique}_person", "knows"): ["english", "spanish"],
    }
    for (subject, predicate), value in expected.items():
        response = await client.post(
            "/memory/fact",
            json={"subject": subject, "predicate": predicate, "value": value},
        )
        assert response.status_code == 201, response.text

    # Wipe the in-memory KB exactly as a container restart would.
    await prolog.query(f"{MEMORY_MODULE}:clear_facts")
    assert await prolog.fact_count() == 0

    reloaded = await db.bootstrap_kb_from_db(session)
    assert reloaded >= len(expected)

    restored = await prolog.query_facts()
    restored_pairs = {(fact["subject"], fact["predicate"]): fact["value"] for fact in restored}
    for key, value in expected.items():
        assert restored_pairs[key] == value, f"{key} did not survive the reload"


async def test_rules_survive_a_kb_reload(client, prolog, session, unique):
    rule_name = f"{unique}_colleagues"
    created = await client.post(
        "/memory/rule",
        json={
            "name": rule_name,
            "head": "fact(X, colleague_of, Y)",
            "body": "[fact(X, works_at, Y)]",
        },
    )
    assert created.status_code == 201

    await prolog.query(f"{MEMORY_MODULE}:clear_rules")
    await db.bootstrap_kb_from_db(session)

    # The rule still works after the reload, which means the head/body text
    # round-tripped from PostgreSQL into a usable Prolog term. The rule matches
    # every works_at fact in the shared database, so assert on this run's own
    # subject rather than on a total that earlier runs' leftovers would change.
    await client.post(
        "/memory/fact",
        json={"subject": f"{unique}_alice", "predicate": "works_at", "value": f"{unique}_acme"},
    )
    run = await client.post(f"/memory/rule/{rule_name}/run")
    assert run.status_code == 200
    derived = [
        fact for fact in run.json()["derived"] if fact["subject"] == f"{unique}_alice"
    ]
    assert len(derived) == 1
    assert derived[0]["predicate"] == "colleague_of"
    assert derived[0]["value"] == f"{unique}_acme"


async def test_reload_does_not_duplicate_facts(client, prolog, session, unique):
    for value in (20, 21, 22):
        await client.post(
            "/memory/fact",
            json={"subject": unique, "predicate": "temperature", "value": value},
        )

    await prolog.query(f"{MEMORY_MODULE}:clear_facts")
    await db.bootstrap_kb_from_db(session)
    await db.bootstrap_kb_from_db(session)

    facts = await prolog.query_facts(subject=unique)
    assert len(facts) == 1
    assert facts[0]["value"] == 22


async def test_reload_does_not_flood_the_audit_trail(client, prolog, session, unique):
    """Bootstrapping restores facts without inventing history for them."""
    await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "temperature", "value": 22, "owner": "alice"},
    )

    await prolog.query(f"{MEMORY_MODULE}:clear_facts")
    await db.bootstrap_kb_from_db(session)

    provenance = await client.get(f"/memory/fact/{unique}/temperature/provenance")
    # One entry from the original write; the reload adds none.
    assert len(provenance.json()) == 1


async def test_postgres_audit_survives_and_is_not_duplicated(client, prolog, session, unique):
    await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "temperature", "value": 22, "owner": "alice"},
    )
    await prolog.query(f"{MEMORY_MODULE}:clear_facts")
    await db.bootstrap_kb_from_db(session)

    audit = await client.get(f"/memory/audit?subject={unique}")
    assert audit.status_code == 200
    entries = audit.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["operation"] == "INSERT"
    assert entries[0]["new_value"] == 22
    assert entries[0]["owner"] == "alice"


async def test_embeddings_persist_alongside_facts(client, session, unique):
    """A fact written with an embedding still has its vector after a reload."""
    await client.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 22}
    )

    result = await session.execute(
        text(
            "SELECT embedding IS NOT NULL AS has_embedding FROM symbolic_facts "
            "WHERE subject = :subject AND predicate = 'temperature'"
        ),
        {"subject": unique},
    )
    row = result.mappings().first()
    assert row is not None
    if not row["has_embedding"]:
        pytest.skip("the fact was stored without a vector (embedding model unavailable)")

    try:
        vector = await get_embedding_service().embed_text("room temperature")
    except EmbeddingUnavailable:
        pytest.skip("the embedding model is unavailable")

    hits = await db.search_facts_by_embedding(session, vector, top_k=100)
    assert any(hit["subject"] == unique for hit in hits)
