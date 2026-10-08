"""
Fact CRUD, rule inference and wildcard queries — the memory's core contract.

These tests drive the HTTP API so they exercise the whole write-through path
(Prolog assert + PostgreSQL upsert + audit entry) rather than either store in
isolation.
"""


async def test_store_and_read_fact(client, unique):
    response = await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "temperature", "value": 22},
    )
    assert response.status_code == 201, response.text
    stored = response.json()
    assert stored["subject"] == unique
    assert stored["predicate"] == "temperature"
    assert stored["value"] == 22
    assert stored["version"] == 1

    fetched = await client.get(f"/memory/fact/{unique}/temperature")
    assert fetched.status_code == 200
    assert fetched.json()["value"] == 22


async def test_store_structured_value(client, unique):
    value = {"languages": ["english", "spanish"], "remote": True, "seats": 3}
    response = await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "profile", "value": value},
    )
    assert response.status_code == 201
    assert response.json()["value"] == value


async def test_store_null_value(client, unique):
    response = await client.post(
        "/memory/fact", json={"subject": unique, "predicate": "occupant", "value": None}
    )
    assert response.status_code == 201
    assert response.json()["value"] is None


async def test_assert_replaces_value_and_bumps_version(client, unique):
    await client.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 22}
    )
    second = await client.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 25}
    )
    assert second.status_code == 201
    assert second.json()["value"] == 25
    assert second.json()["version"] == 2

    listed = await client.get(f"/memory/fact?subject={unique}")
    assert listed.json()["count"] == 1


async def test_delete_fact(client, unique):
    await client.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 22}
    )
    deleted = await client.delete(f"/memory/fact/{unique}/temperature")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    missing = await client.get(f"/memory/fact/{unique}/temperature")
    assert missing.status_code == 404


async def test_delete_missing_fact_is_404(client, unique):
    assert (await client.delete(f"/memory/fact/{unique}/nope")).status_code == 404


async def test_list_facts_wildcards(client, unique):
    for predicate, value in (("temperature", 22), ("humidity", 45), ("occupied", True)):
        await client.post(
            "/memory/fact",
            json={"subject": unique, "predicate": predicate, "value": value},
        )

    by_subject = await client.get(f"/memory/fact?subject={unique}")
    assert by_subject.json()["count"] == 3

    by_predicate = await client.get(f"/memory/fact?predicate={unique}_missing")
    assert by_predicate.json()["count"] == 0

    star = await client.get("/memory/fact?subject=*&predicate=*")
    assert star.status_code == 200
    assert star.json()["count"] >= 3


async def test_prolog_mirrors_postgres(client, unique):
    await client.post(
        "/memory/fact",
        json={"subject": unique, "predicate": "works_at", "value": "Acme Corp"},
    )
    facts = await client.get(f"/memory/fact?subject={unique}")
    assert facts.json()["count"] == 1

    provenance = await client.get(f"/memory/fact/{unique}/works_at/provenance")
    assert provenance.status_code == 200
    entries = provenance.json()
    assert len(entries) == 1
    assert entries[0]["change_type"] == "insert"
    assert entries[0]["new_value"] == "Acme Corp"


async def test_override_requires_existing_fact(client, unique):
    missing = await client.post(
        f"/memory/fact/{unique}/temperature/override",
        json={"value": 30, "owner": "alice"},
    )
    assert missing.status_code == 404

    await client.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 22}
    )
    overridden = await client.post(
        f"/memory/fact/{unique}/temperature/override",
        json={"value": 30, "owner": "alice"},
    )
    assert overridden.status_code == 200
    assert overridden.json()["value"] == 30


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

COLLEAGUES_RULE = {
    "name": "colleagues",
    "head": "fact(X, colleague_of, Y)",
    "body": "[fact(X, works_at, Y)]",
}


async def test_store_and_read_rule(client, unique):
    rule = {"name": f"{unique}_colleagues", "head": COLLEAGUES_RULE["head"], "body": COLLEAGUES_RULE["body"]}
    created = await client.post("/memory/rule", json=rule)
    assert created.status_code == 201
    assert created.json()["name"] == rule["name"]

    fetched = await client.get(f"/memory/rule/{rule['name']}")
    assert fetched.status_code == 200
    assert fetched.json()["head"] == rule["head"]


async def test_get_unknown_rule_is_404(client, unique):
    assert (await client.get(f"/memory/rule/{unique}_missing")).status_code == 404


async def test_rule_derives_facts_as_a_dry_run(client, unique):
    employer = f"{unique}_acme"
    people = {f"{unique}_{name}" for name in ("alice", "bob")}
    for person in people:
        await client.post(
            "/memory/fact",
            json={"subject": person, "predicate": "works_at", "value": employer},
        )

    rule_name = f"{unique}_colleagues"
    await client.post(
        "/memory/rule",
        json={"name": rule_name, "head": COLLEAGUES_RULE["head"], "body": COLLEAGUES_RULE["body"]},
    )

    run = await client.post(f"/memory/rule/{rule_name}/run")
    assert run.status_code == 200

    # The rule matches every works_at fact in the database, not just this run's,
    # so assert on this run's subjects rather than on the total.
    derived = [fact for fact in run.json()["derived"] if fact["subject"] in people]
    assert {fact["subject"] for fact in derived} == people
    assert {fact["predicate"] for fact in derived} == {"colleague_of"}
    assert {fact["value"] for fact in derived} == {employer}

    # Dry run by default: each derivation this rule just produced is absent.
    for person in people:
        absent = await client.get(f"/memory/fact/{person}/colleague_of")
        assert absent.status_code == 404


async def test_rule_can_persist_derived_facts(client, unique):
    employer = f"{unique}_acme"
    await client.post(
        "/memory/fact",
        json={"subject": f"{unique}_alice", "predicate": "works_at", "value": employer},
    )
    rule_name = f"{unique}_colleagues"
    await client.post(
        "/memory/rule",
        json={"name": rule_name, "head": COLLEAGUES_RULE["head"], "body": COLLEAGUES_RULE["body"]},
    )

    run = await client.post(f"/memory/rule/{rule_name}/run?store=true")
    assert run.status_code == 200

    # Asserted on this run's own subject, not on a global count, so facts left by
    # earlier runs cannot change the outcome. The rule copies works_at into
    # colleague_of, so the employer comes across as the value.
    stored = await client.get(f"/memory/fact/{unique}_alice/colleague_of")
    assert stored.status_code == 200
    assert stored.json()["subject"] == f"{unique}_alice"
    assert stored.json()["predicate"] == "colleague_of"
    assert stored.json()["value"] == employer


async def test_transitive_rule(client, unique):
    await client.post(
        "/memory/fact", json={"subject": f"{unique}_a", "predicate": "parent_of", "value": f"{unique}_b"}
    )
    await client.post(
        "/memory/fact", json={"subject": f"{unique}_b", "predicate": "parent_of", "value": f"{unique}_c"}
    )
    rule_name = f"{unique}_grandparent"
    await client.post(
        "/memory/rule",
        json={
            "name": rule_name,
            "head": "fact(X, grandparent_of, Z)",
            "body": "[fact(X, parent_of, Y), fact(Y, parent_of, Z)]",
        },
    )

    run = await client.post(f"/memory/rule/{rule_name}/run")
    assert run.json()["count"] == 1
    derived = run.json()["derived"][0]
    assert derived["subject"] == f"{unique}_a"
    assert derived["value"] == f"{unique}_c"


async def test_rule_with_no_match_derives_nothing(client, unique):
    rule_name = f"{unique}_impossible"
    await client.post(
        "/memory/rule",
        json={
            "name": rule_name,
            "head": "fact(X, derived, Y)",
            "body": "[fact(X, no_such_predicate, Y)]",
        },
    )
    run = await client.post(f"/memory/rule/{rule_name}/run")
    assert run.status_code == 200
    assert run.json()["count"] == 0


async def test_run_unknown_rule_is_404(client, unique):
    assert (await client.post(f"/memory/rule/{unique}_missing/run")).status_code == 404


async def test_delete_rule(client, unique):
    rule_name = f"{unique}_colleagues"
    await client.post(
        "/memory/rule",
        json={"name": rule_name, "head": COLLEAGUES_RULE["head"], "body": COLLEAGUES_RULE["body"]},
    )
    deleted = await client.delete(f"/memory/rule/{rule_name}")
    assert deleted.status_code == 200
    assert (await client.get(f"/memory/rule/{rule_name}")).status_code == 404


async def test_verify_reports_stored_state(client, unique):
    await client.post(
        "/memory/fact", json={"subject": unique, "predicate": "temperature", "value": 22}
    )
    response = await client.post(
        "/memory/verify",
        json={"subject": unique, "predicate": "temperature", "value": 22},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stored"] is True
    assert body["stored_value"] == 22
