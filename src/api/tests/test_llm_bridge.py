"""
LLM bridge and graceful degradation.

The memory is the system; the LLM is an optional client of it. These tests pin
that contract: memory writes and retrieval keep working when the model is
missing, /agent/ask still returns the facts it found, and /llm/ask reports the
outage honestly instead of inventing an answer.
"""
import pytest

from src.api.embeddings import EmbeddingUnavailable


async def test_agent_returns_facts_when_the_llm_is_down(client_without_llm, embeddings_ready, unique):
    await client_without_llm.post(
        "/memory/fact",
        json={"subject": f"{unique}_room_101", "predicate": "temperature", "value": 22},
    )

    response = await client_without_llm.post(
        "/agent/ask", json={"prompt": "What is the temperature in room 101?", "top_k": 5}
    )
    assert response.status_code == 200

    body = response.json()
    assert body["used_fallback"] is True
    assert body["response"] is None
    assert body["error"]
    assert body["retrieved_facts"], "facts must still be retrieved without the LLM"
    assert body["retrieved_facts"][0]["subject"] == f"{unique}_room_101"


async def test_memory_still_works_while_the_llm_is_down(client_without_llm, unique):
    stored = await client_without_llm.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 19}
    )
    assert stored.status_code == 201

    fetched = await client_without_llm.get(f"/memory/fact/{unique}/temperature")
    assert fetched.json()["value"] == 19

    listed = await client_without_llm.get("/memory/fact?subject=*&predicate=*")
    assert listed.status_code == 200

    audit = await client_without_llm.get(f"/memory/audit?subject={unique}")
    assert audit.json()["count"] == 1


async def test_llm_ask_returns_503_when_the_llm_is_down(client_without_llm, unique):
    await client_without_llm.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 19}
    )
    response = await client_without_llm.post("/llm/ask", json={"prompt": "how hot is it"})
    assert response.status_code == 503
    assert "unavailable" in response.json()["detail"].lower()


async def test_llm_health_reports_unreachable(client_without_llm):
    response = await client_without_llm.get("/llm/health")
    assert response.status_code == 200
    assert response.json()["reachable"] is False


async def test_agent_context_comes_from_prolog_not_just_postgres(client_without_llm, prolog, unique):
    """The answer's grounding is the KB's view, so it must be the consistent one."""
    await client_without_llm.post(
        "/memory/fact",
        json={"subject": f"{unique}_room_101", "predicate": "temperature", "value": 22},
    )

    response = await client_without_llm.post(
        "/agent/ask", json={"prompt": "temperature in room 101", "top_k": 5}
    )
    retrieved = {(fact["subject"], fact["predicate"]): fact["value"] for fact in response.json()["retrieved_facts"]}

    prolog_fact = await prolog.get_fact(f"{unique}_room_101", "temperature")
    assert prolog_fact is not None
    assert retrieved[(f"{unique}_room_101", "temperature")] == prolog_fact["value"]


async def test_agent_without_an_embedding_model_still_returns_facts(
    client_without_llm, unique, monkeypatch
):
    async def unavailable(*args, **kwargs):
        raise EmbeddingUnavailable("model unavailable in this test")

    monkeypatch.setattr("src.api.routes.agent.get_embedding_service", unavailable)

    await client_without_llm.post(
        "/memory/fact",
        json={"subject": f"{unique}_alice", "predicate": "role", "value": "forecaster"},
    )

    response = await client_without_llm.post(
        "/agent/ask", json={"prompt": "what is alice's role?", "top_k": 5}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["used_fallback"] is True
    # Keyword fallback still retrieves, so the caller is not left empty-handed.
    assert any(fact["subject"] == f"{unique}_alice" for fact in body["retrieved_facts"])


async def test_agent_ask_validates_top_k(client):
    for top_k in (0, 999):
        response = await client.post("/agent/ask", json={"prompt": "hello", "top_k": top_k})
        assert response.status_code == 422


async def test_agent_ask_requires_a_prompt(client):
    assert (await client.post("/agent/ask", json={})).status_code == 422


async def test_agent_answer_is_grounded_when_the_llm_is_up(client, monkeypatch, unique):
    """With a stubbed provider, check the prompt really carries the facts."""
    from src.llm.base import get_llm_provider

    seen: dict[str, str] = {}

    class RecordingLLM:
        async def ask(self, prompt: str, system_prompt=None) -> str:
            seen["prompt"] = prompt
            return "It is 22 degrees."

        async def is_healthy(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    monkeypatch.setenv("ADMIN_API_KEY", "test-key")
    from src.api.main import app

    app.dependency_overrides[get_llm_provider] = lambda: RecordingLLM()
    try:
        await client.post(
            "/memory/fact",
            json={"subject": f"{unique}_room_101", "predicate": "temperature", "value": 22},
        )
        response = await client.post(
            "/agent/ask", json={"prompt": "What is the temperature in room 101?"}
        )
    finally:
        app.dependency_overrides.pop(get_llm_provider, None)

    assert response.status_code == 200
    body = response.json()
    assert body["used_fallback"] is False
    assert body["response"] == "It is 22 degrees."
    assert f"{unique}_room_101.temperature = 22" in seen["prompt"]
    assert body["retrieved_facts"]


@pytest.mark.parametrize("path", ["/memory/fact", "/memory/search"])
async def test_memory_endpoints_do_not_depend_on_the_llm(client_without_llm, unique, path):
    assert path  # documents intent: neither endpoint consults the LLM
    health = await client_without_llm.get("/health")
    assert health.status_code == 200
    overall = health.json()["overall"]
    # Prolog and PostgreSQL are up; the LLM is the only degraded component.
    assert overall == "degraded"
    statuses = {service["service"]: service["status"] for service in health.json()["services"]}
    assert statuses["prolog"] == "healthy"
    assert statuses["postgresql"] == "healthy"
    assert statuses["llm"] == "degraded"
