"""
Semantic retrieval over the pgvector index.

The point of embedding facts is that a question phrased differently from the
stored key still finds them — "how hot is room 101" must reach
room_101.temperature even though neither the subject nor the predicate appears
in the question. Tests that need a vector skip when the model is unavailable;
the keyword-fallback behaviour is covered separately and always runs.
"""
import pytest

from src.api.embeddings import EmbeddingUnavailable


async def test_search_finds_a_fact_by_meaning(client, embeddings_ready, unique):
    facts = [
        {"subject": f"{unique}_room_101", "predicate": "temperature", "value": 22},
        {"subject": f"{unique}_room_102", "predicate": "temperature", "value": 19},
        {"subject": f"{unique}_alice", "predicate": "works_at", "value": "Acme Corp"},
        {"subject": f"{unique}_alice", "predicate": "role", "value": "forecaster"},
    ]
    for fact in facts:
        response = await client.post("/memory/fact", json=fact)
        assert response.status_code == 201, response.text

    result = await client.get("/memory/search?query=how hot is room 101&top_k=2")
    assert result.status_code == 200
    body = result.json()
    assert body["semantic"] is True
    assert body["count"] > 0

    top = body["hits"][0]
    assert top["subject"] == f"{unique}_room_101"
    assert top["predicate"] == "temperature"
    assert 0.0 <= top["similarity"] <= 1.0


async def test_search_returns_the_closest_match_first(client, embeddings_ready, unique):
    for subject, temperature in (
        (f"{unique}_kitchen", 34),
        (f"{unique}_cellar", 6),
        (f"{unique}_attic", 41),
    ):
        await client.post(
            "/memory/fact",
            json={"subject": subject, "predicate": "temperature", "value": temperature},
        )

    result = await client.get("/memory/search?query=temperature of the kitchen&top_k=3")
    hits = result.json()["hits"]
    assert hits[0]["subject"] == f"{unique}_kitchen"

    similarities = [hit["similarity"] for hit in hits]
    assert similarities == sorted(similarities, reverse=True)


async def test_search_honours_top_k(client, embeddings_ready, unique):
    for index in range(6):
        await client.post(
            "/memory/fact",
            json={"subject": f"{unique}_sensor_{index}", "predicate": "reading", "value": index},
        )

    result = await client.get("/memory/search?query=sensor reading&top_k=2")
    assert result.json()["count"] <= 2


async def test_search_matches_structured_values(client, embeddings_ready, unique):
    await client.post(
        "/memory/fact",
        json={
            "subject": f"{unique}_alice",
            "predicate": "languages",
            "value": ["english", "spanish"],
        },
    )

    result = await client.get("/memory/search?query=languages alice speaks&top_k=3")
    hits = result.json()["hits"]
    assert any(hit["subject"] == f"{unique}_alice" for hit in hits)


async def test_update_reindexes_the_vector(client, embeddings_ready, unique):
    """A changed value must not keep pointing at the old meaning."""
    subject = f"{unique}_room"
    await client.post("/memory/fact", json={"subject": subject, "predicate": "status", "value": "vacant"})

    before = await client.get("/memory/search?query=is the room occupied&top_k=3")
    assert before.json()["semantic"] is True

    await client.post("/memory/fact", json={"subject": subject, "predicate": "status", "value": "occupied"})

    after = await client.get("/memory/search?query=is the room occupied&top_k=3")
    assert any(hit["subject"] == subject for hit in after.json()["hits"])


async def test_deleted_facts_leave_the_index(client, embeddings_ready, unique):
    subject = f"{unique}_temp_room"
    await client.post(
        "/memory/fact", json={"subject": subject, "predicate": "temperature", "value": 22}
    )
    await client.delete(f"/memory/fact/{subject}/temperature")

    result = await client.get("/memory/search?query=temperature&top_k=50")
    assert all(hit["subject"] != subject for hit in result.json()["hits"])


async def test_search_falls_back_to_keywords_without_the_model(client, unique, monkeypatch):
    """The API stays useful when sentence-transformers cannot be loaded."""
    from src.api import database as db_module

    async def unavailable(*args, **kwargs):
        raise EmbeddingUnavailable("model unavailable in this test")

    monkeypatch.setattr(db_module, "search_facts_by_embedding", unavailable)
    monkeypatch.setattr("src.api.routes.memory.get_embedding_service", unavailable)

    await client.post(
        "/memory/fact",
        json={"subject": f"{unique}_room_101", "predicate": "temperature", "value": 22},
    )

    result = await client.get("/memory/search?query=room_101&top_k=5")
    assert result.status_code == 200
    body = result.json()
    assert body["semantic"] is False
    assert any(hit["subject"] == f"{unique}_room_101" for hit in body["hits"])


async def test_embedding_dimension_mismatch_is_reported(client, session, unique):
    """A wrong-width vector is a configuration error, not a silent bad search."""
    from src.api import database as db_module

    with pytest.raises(ValueError, match="dimensions"):
        await db_module._vector_literal([0.1] * 7)
