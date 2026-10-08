"""
Governance: who changed what, and what an unprivileged caller may do.

Covers the audit trail (every mutation is attributable and reconstructable), the
admin gate on arbitrary Prolog queries, and the KB-owned permission rule behind
decision-node overrides.
"""
import pytest

from src.api import database as db


async def test_fact_write_is_audited(client, unique):
    await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "temperature", "value": 22, "owner": "alice"},
    )

    audit = await client.get(f"/memory/audit?subject={unique}")
    assert audit.status_code == 200
    entries = audit.json()["entries"]
    assert len(entries) == 1

    entry = entries[0]
    assert entry["entity_type"] == "fact"
    assert entry["operation"] == "INSERT"
    assert entry["subject"] == unique
    assert entry["predicate"] == "temperature"
    assert entry["new_value"] == 22
    assert entry["old_value"] is None
    assert entry["owner"] == "alice"
    assert entry["timestamp"] is not None
    assert entry["diff_hash"]


async def test_update_and_delete_are_audited_with_old_values(client, unique):
    await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "temperature", "value": 22, "owner": "alice"},
    )
    await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "temperature", "value": 25, "owner": "bob"},
    )
    await client.delete(f"/memory/fact/{unique}/temperature?owner=carol")

    audit = await client.get(f"/memory/audit?subject={unique}&limit=10")
    entries = audit.json()["entries"]
    operations = [entry["operation"] for entry in entries]

    # Newest first.
    assert operations == ["DELETE", "UPDATE", "INSERT"]
    assert entries[0]["new_value"] is None
    assert entries[0]["old_value"] == 25
    assert entries[0]["owner"] == "carol"
    assert entries[1]["old_value"] == 22
    assert entries[1]["new_value"] == 25


async def test_override_records_the_actor(client, unique):
    await client.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 22}
    )
    await client.post(
        f"/memory/fact/{unique}/temperature/override",
        json={"value": 30, "owner": "dana"},
    )

    audit = await client.get(f"/memory/audit?subject={unique}&entity_type=fact")
    entries = audit.json()["entries"]
    assert entries[0]["owner"] == "dana"
    assert entries[0]["operation"] == "UPDATE"


async def test_rule_changes_are_audited(client, unique):
    rule = {"name": f"{unique}_r", "head": "fact(X, y, Z)", "body": "[fact(X, a, Z)]"}
    await client.post("/memory/rule", json=rule)
    await client.delete(f"/memory/rule/{rule['name']}")

    audit = await client.get(f"/memory/audit?rule_name={rule['name']}")
    entries = audit.json()["entries"]
    assert [entry["operation"] for entry in entries] == ["DELETE", "INSERT"]
    assert all(entry["entity_type"] == "rule" for entry in entries)


async def test_audit_is_filterable(session, client, unique):
    await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "temperature", "value": 22, "owner": "alice"},
    )
    await client.post(
        "/memory/fact",
        json={"subject": f"{unique}_other", "predicate": "humidity", "value": 40, "owner": "bob"},
    )

    filtered = await db.get_audit(session, subject=f"{unique}_other")
    assert len(filtered) == 1
    assert filtered[0]["owner"] == "bob"

    by_entity = await db.get_audit(session, entity_type="rule")
    # Not "== []": the audit table is persistent, so rule rows from earlier
    # runs and from the evaluations are legitimately present. What matters is
    # that the filter selects rules and nothing else.
    assert all(entry["entity_type"] == "rule" for entry in by_entity)
    assert not {unique, f"{unique}_other"} & {entry["subject"] for entry in by_entity}


# ---------------------------------------------------------------------------
# Arbitrary Prolog queries
# ---------------------------------------------------------------------------


async def test_query_requires_admin_key(client, monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    response = await client.post("/memory/query", json={"query": "fact(X, Y, Z)"})
    assert response.status_code == 403
    assert "ADMIN_API_KEY" in response.json()["detail"]


async def test_query_rejects_a_wrong_admin_key(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    response = await client.post(
        "/memory/query",
        json={"query": "fact(X, Y, Z)"},
        headers={"X-Admin-Key": "wrong"},
    )
    assert response.status_code == 403


async def test_query_returns_bindings(client, monkeypatch, unique):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    await client.post(
        "/memory/fact",
        json={"subject": f"{unique}_alice", "predicate": "works_at", "value": "Acme Corp"},
    )

    response = await client.post(
        "/memory/query",
        json={"query": f"fact('{unique}_alice', P, V)"},
        headers={"X-Admin-Key": "correct-horse"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["bindings"][0]["P"] == "works_at"
    assert body["bindings"][0]["V"] == "Acme Corp"


async def test_query_rejects_mutating_goals(client, monkeypatch):
    """Prolog is Turing-complete, so the endpoint must not be a write hole."""
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    for goal in (
        "assertz(fact(evil, pwned, true))",
        "retractall(fact(_, _, _))",
        "shell('rm -rf /')",
        "consult('/etc/passwd')",
    ):
        response = await client.post(
            "/memory/query", json={"query": goal}, headers={"X-Admin-Key": "correct-horse"}
        )
        assert response.status_code in (400, 403), f"{goal} was not rejected"


async def test_query_rejects_predicates_outside_the_allowlist(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    response = await client.post(
        "/memory/query",
        json={"query": "listing_files('/')"},
        headers={"X-Admin-Key": "correct-horse"},
    )
    assert response.status_code == 403
    assert "not allowed" in response.json()["detail"]


async def test_a_tree_predicate_needs_a_domain(client, monkeypatch):
    """node/6 lives in every domain module, so an unqualified goal names none of them."""
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    response = await client.post(
        "/memory/query",
        json={"query": "node(n1, P, C, A, O, V)"},
        headers={"X-Admin-Key": "correct-horse"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "ambiguous" in detail
    assert "energy" in detail


async def test_a_tree_predicate_runs_in_the_domain_it_names(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    headers = {"X-Admin-Key": "correct-horse"}

    energy = await client.post(
        "/memory/query",
        json={"query": "node(n1, P, C, A, O, V)", "domain": "energy"},
        headers=headers,
    )
    assert energy.status_code == 200, energy.text
    assert energy.json()["count"] == 1
    assert energy.json()["bindings"][0]["A"] == "dispatch_energy"
    # The domain id is a routing label, not a module name: energy is implemented by
    # the decision_tree module. What matters is that each domain resolves to its own.
    assert energy.json()["query"].startswith("decision_tree:")

    # The same goal against another domain must not see energy's node.
    cyber = await client.post(
        "/memory/query",
        json={"query": "node(n1, P, C, A, O, V)", "domain": "cyber"},
        headers=headers,
    )
    assert cyber.status_code == 200
    assert cyber.json()["count"] == 0
    assert cyber.json()["query"].startswith("cyber:")


async def test_a_tree_predicate_rejects_a_domain_that_does_not_exist(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    response = await client.post(
        "/memory/query",
        json={"query": "get_leaves(L)", "domain": "no_such_domain"},
        headers={"X-Admin-Key": "correct-horse"},
    )
    assert response.status_code == 400
    assert "Unknown domain" in response.json()["detail"]


async def test_a_runtime_domain_has_no_module_for_a_tree_goal(client, monkeypatch, unique):
    """Its nodes live in tree_registry, so there is no module to qualify against."""
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    domain_id = f"green_{unique}"
    spec = {
        "id": domain_id,
        "label": "Greenhouse",
        "input_keys": ["soil_moisture_pct"],
        "nodes": [
            {
                "id": f"{domain_id}_root",
                "parent": None,
                "condition": "true",
                "action": "none",
                "owner": "system",
                "version": 1,
            }
        ],
    }
    assert (await client.post("/memory/domain", json=spec)).status_code == 201
    await client.post(f"/memory/domain/{domain_id}/activate?owner=alex")

    response = await client.post(
        "/memory/query",
        json={"query": "get_leaves(L)", "domain": domain_id},
        headers={"X-Admin-Key": "correct-horse"},
    )
    assert response.status_code == 400
    assert "runtime domain" in response.json()["detail"]


async def test_the_admin_key_is_checked_before_the_goal_is_understood(client, monkeypatch):
    """A rejected key must not leak whether a predicate would have been allowed."""
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    response = await client.post(
        "/memory/query",
        json={"query": "listing_files('/')"},
        headers={"X-Admin-Key": "wrong"},
    )
    assert response.status_code == 403
    assert "not allowed" not in response.json()["detail"]


@pytest.mark.parametrize("domain", ["energy", "air_traffic", "manufacturing", "cyber"])
async def test_a_condition_evaluates_through_every_domain_module(
    client, monkeypatch, domain
):
    """eval_condition/2 has to resolve under every module, not just decision_tree.

    The goal is qualified with the domain's own module, so a module that does not
    re-export it answers with an existence error. That reads as a routing fault: the
    same query works for one domain and 400s for the rest.
    """
    monkeypatch.setenv("ADMIN_API_KEY", "correct-horse")
    response = await client.post(
        "/memory/query",
        json={"query": "eval_condition(flag_test > 10, Input)", "domain": domain},
        headers={"X-Admin-Key": "correct-horse"},
    )
    assert response.status_code == 200, response.text
    # The goal succeeds structurally and enumerates over the unbound Input; what
    # matters is that it ran in that module rather than erroring out.
    assert "existence_error" not in response.text


# node_timestamp/1 is re-exported by all four modules too, but deliberately not on
# the /memory/query allowlist: widening that allowlist is a trust-boundary change
# governed by invariant 7. kb/test_trees.pl covers that export directly instead.


# ---------------------------------------------------------------------------
# Decision-node overrides
# ---------------------------------------------------------------------------


async def test_node_override_requires_the_owning_agent(client):
    """Permission comes from the KB's can_write/2, not from the API."""
    forbidden = await client.post(
        "/memory/node/n3/override", json={"action": "do_whatever", "owner": "forecaster"}
    )
    assert forbidden.status_code == 403
    assert "not authorised" in forbidden.json()["detail"]


async def test_node_override_by_the_owner_is_audited(client):
    response = await client.post(
        "/memory/node/n3/override", json={"action": "escalate_cooling", "owner": "optimizer"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["new_action"] == "escalate_cooling"
    assert body["diff_hash"]

    audit = await client.get("/memory/node/n3/audit")
    assert audit.status_code == 200
    entries = audit.json()["entries"]
    assert entries[0]["owner"] == "optimizer"
    assert entries[0]["new_action"] == "escalate_cooling"


async def test_override_unknown_node_is_404(client):
    response = await client.post(
        "/memory/node/nope/override", json={"action": "x", "owner": "optimizer"}
    )
    assert response.status_code == 404
