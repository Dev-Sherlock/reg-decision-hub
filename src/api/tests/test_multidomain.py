"""
Several decision domains, one routing rule.

The point of this module is that /decide takes no domain. The registry declares an
input key set per domain and the endpoint routes on which of those keys the caller
sent, so a decision tree can be added to the KB without touching a route, a schema
or a request. What is tested here is that rule and its two failure modes: inputs
that identify no domain, and inputs that identify two equally.

Per-domain leaf/action correctness for the three example trees is covered in
kb/test_trees.pl; the cases below are about getting to the right tree and reporting
which one answered.
"""
import asyncio

import pytest

# One case per domain: inputs, leaf, action, owner.
DOMAIN_CASES = [
    (
        "energy",
        {"demand": 90, "temperature": 35, "humidity": 80},
        "n7",
        "activate_dehumidifier",
        "optimizer",
    ),
    (
        "air_traffic",
        {"region": "north", "ice_on_wing_pct": 15},
        "at3",
        "takeoff_denied_deice",
        "safety_officer",
    ),
    (
        "manufacturing",
        {"defect_rate_pct": 2, "hardness_hrc": 40},
        "qa3",
        "rework_lot",
        "process_engineer",
    ),
    (
        "cyber",
        {"blast_radius_pct": 40, "data_exposed": True},
        "sec4",
        "notify_ciso_and_regulator",
        "ciso",
    ),
]


@pytest.mark.parametrize("domain,inputs,leaf,action,owner", DOMAIN_CASES)
async def test_domain_is_detected_from_the_input_keys(
    client, domain, inputs, leaf, action, owner
):
    response = await client.post("/decide", json={"inputs": inputs})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["domain"] == domain
    assert body["leaf_node"] == leaf
    assert body["action"] == action
    assert body["owner"] == owner
    assert body["proof_trace"][0]["node_id"] in {
        "root",
        "at_root",
        "qa_root",
        "sec_root",
    }


async def test_undeclared_keys_do_not_dilute_the_match(client):
    """Extra keys are ignored, so adding one cannot change which tree answers."""
    response = await client.post(
        "/decide",
        json={
            "inputs": {
                "region": "north",
                "ice_on_wing_pct": 15,
                "turbulence_severity": "severe",
                "fuel_kg": 4200,
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["domain"] == "air_traffic"


async def test_air_traffic_atom_comparison_survives_the_api(client):
    r"""`region == north` is strict unification: only the atom matches.

    The same ice reading has to give different answers per region, and a report that
    omits engine_out must still resolve rather than dead-ending on `\+ engine_out`.
    """
    denied = await client.post(
        "/decide", json={"inputs": {"region": "north", "ice_on_wing_pct": 15}}
    )
    assert denied.status_code == 200
    assert denied.json()["leaf_node"] == "at3"
    assert denied.json()["action"] == "takeoff_denied_deice"

    # Not north: the identical reading takes the standard-procedure branch instead.
    standard = await client.post(
        "/decide", json={"inputs": {"region": "south", "ice_on_wing_pct": 15}}
    )
    assert standard.status_code == 200
    assert standard.json()["leaf_node"] == "at9"
    assert standard.json()["action"] == "line_await_clearance"

    # The string "north" is not the atom north.
    as_string = await client.post("/decide", json={"inputs": {"region": "north "}})
    assert as_string.status_code == 200
    assert as_string.json()["leaf_node"] != "at3"


async def test_a_missing_key_dead_ends_at_the_branch_node(client):
    """at1's children both need ice_on_wing_pct, so at1 is the leaf.

    The walk stops at the last node whose condition held rather than reaching for a
    sibling to supply the missing number.
    """
    response = await client.post("/decide", json={"inputs": {"region": "north"}})
    assert response.status_code == 200
    body = response.json()
    assert body["leaf_node"] == "at1"
    assert body["action"] == "evaluate_icing"


async def test_inputs_that_identify_two_domains_are_refused(client):
    """One key each ties them. Picking one by convention would hide the tie."""
    response = await client.post(
        "/decide", json={"inputs": {"demand": 90, "defect_rate_pct": 2}}
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "ambiguous_domain"
    assert set(detail["candidates"]) == {"energy", "manufacturing"}


async def test_the_domain_with_most_matching_keys_wins(client):
    """A tie needs an actual tie: two of energy's keys beat manufacturing's one."""
    response = await client.post(
        "/decide", json={"inputs": {"demand": 90, "temperature": 35, "defect_rate_pct": 2}}
    )
    assert response.status_code == 200
    assert response.json()["domain"] == "energy"


async def test_inputs_that_identify_no_domain_list_what_each_one_declares(client):
    response = await client.post("/decide", json={"inputs": {"unknown_key": 1}})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_domain"
    keys = detail["keys_by_domain"]
    assert {"energy", "air_traffic", "manufacturing", "cyber"} <= set(keys)
    assert "demand" in keys["energy"]
    assert "region" in keys["air_traffic"]


async def test_explicit_domain_resolves_a_tie_without_skipping_validation(client):
    tied = {"demand": 90, "defect_rate_pct": 2}
    chosen = await client.post(
        "/decide", params={"domain": "manufacturing"}, json={"inputs": tied}
    )
    assert chosen.status_code == 200
    assert chosen.json()["domain"] == "manufacturing"

    missing = await client.post(
        "/decide", params={"domain": "no_such_domain"}, json={"inputs": tied}
    )
    assert missing.status_code == 400
    assert missing.json()["detail"]["error"] == "unknown_domain"


# ---------------------------------------------------------------------------
# /trees and /tree across domains
# ---------------------------------------------------------------------------


async def test_trees_reports_every_domain_with_the_keys_it_declares(client):
    response = await client.get("/trees")
    assert response.status_code == 200
    domains = {d["id"]: d for d in response.json()}

    assert {"energy", "air_traffic", "manufacturing", "cyber"} <= set(domains)
    assert domains["energy"]["root"] == "root"
    assert domains["cyber"]["root"] == "sec_root"
    assert domains["cyber"]["source"] == "kb"
    # The kb domains are code and always active. Runtime domains may
    # legitimately be inactive — /trees reports them precisely so an
    # operator can see a registered-but-not-routable tree — and earlier
    # tests in this run leave some behind, so do not assert over all.
    assert all(
        domains[domain]["active"]
        for domain in ("energy", "air_traffic", "manufacturing", "cyber")
    )
    assert domains["energy"]["input_keys"] == ["demand", "temperature", "humidity"]
    assert domains["cyber"]["node_count"] > 0


async def test_overlapping_requests_do_not_wedge_the_prolog_engine(client):
    """The UI fetches /tree and /trees together, in parallel.

    pyswip is not thread-safe, and query() used to run each goal on
    the default thread pool with nothing serialising them: two
    overlapping goals interleaved on the engine's query state and the
    next query failed with "The last query was not closed", which
    reads as a 500 from /tree. The race window is milliseconds wide,
    so the burst is repeated rather than fired once — a single round
    could pass by luck even on a broken engine.
    """
    for _ in range(5):
        responses = await asyncio.gather(
            client.get("/tree"),
            client.get("/trees"),
            client.post(
                "/decide",
                json={"inputs": {"region": "north", "ice_on_wing_pct": 15}},
            ),
            client.get("/tree/at3"),
        )
        assert all(
            response.status_code == 200 for response in responses
        ), [response.status_code for response in responses]


async def test_node_ids_resolve_without_being_told_their_domain(client):
    for node_id, domain in [("n3", "energy"), ("at3", "air_traffic"), ("sec4", "cyber")]:
        response = await client.get(f"/tree/{node_id}")
        assert response.status_code == 200, response.text
        assert response.json()["domain"] == domain


async def test_tree_returns_the_merged_forest(client):
    response = await client.get("/tree")
    assert response.status_code == 200
    body = response.json()
    domains = {node["domain"] for node in body["nodes"]}
    assert {"energy", "air_traffic", "manufacturing", "cyber"} <= domains
    assert body["root"] == "root"


# ---------------------------------------------------------------------------
# Runtime domains
# ---------------------------------------------------------------------------


def _spec(domain_id: str, key: str = "soil_moisture_pct") -> dict:
    return {
        "id": domain_id,
        "label": "Greenhouse climate",
        "input_keys": [key, "greenhouse_temp_c"],
        "nodes": [
            {
                "id": f"{domain_id}_root",
                "parent": None,
                "condition": "true",
                "action": "none",
                "owner": "system",
                "version": 1,
            },
            {
                "id": f"{domain_id}_1",
                "parent": f"{domain_id}_root",
                "condition": f"{key} < 30",
                "action": "irrigate",
                "owner": "grower",
                "version": 1,
            },
            {
                "id": f"{domain_id}_2",
                "parent": f"{domain_id}_root",
                "condition": f"{key} >= 30",
                "action": "hold",
                "owner": "grower",
                "version": 1,
            },
        ],
    }


async def test_a_created_domain_is_not_routable_until_it_is_activated(client, unique):
    domain_id = f"green_{unique}"
    created = await client.post("/memory/domain", json=_spec(domain_id))
    assert created.status_code == 201, created.text
    assert created.json()["active"] is False
    assert created.json()["node_count"] == 3

    # Registered but inactive: visible in the registry, not in the active set.
    listed = {d["id"]: d for d in (await client.get("/trees")).json()}
    assert listed[domain_id]["active"] is False

    routed = await client.post("/decide", json={"inputs": {"soil_moisture_pct": 10}})
    assert routed.status_code == 400
    assert routed.json()["detail"]["error"] == "unknown_domain"

    activated = await client.post(f"/memory/domain/{domain_id}/activate?owner=alex")
    assert activated.status_code == 200
    assert activated.json()["active"] is True

    decided = await client.post("/decide", json={"inputs": {"soil_moisture_pct": 10}})
    assert decided.status_code == 200, decided.text
    assert decided.json()["domain"] == domain_id
    assert decided.json()["action"] == "irrigate"

    listed = {d["id"]: d for d in (await client.get("/trees")).json()}
    assert listed[domain_id]["active"] is True


async def test_creating_a_domain_is_audited(client, unique):
    domain_id = f"green_{unique}"
    await client.post("/memory/domain", json=_spec(domain_id))
    await client.post(f"/memory/domain/{domain_id}/activate?owner=alex")

    audit = await client.get(f"/memory/domain/{domain_id}/audit")
    assert audit.status_code == 200
    body = audit.json()
    assert body["count"] == 2
    operations = [entry["operation"] for entry in body["entries"]]
    assert operations == ["ACTIVATE", "CREATE"]
    assert all(entry["diff_hash"] for entry in body["entries"])
    assert body["entries"][-1]["new_value"]["input_keys"] == [
        "soil_moisture_pct",
        "greenhouse_temp_c",
    ]

    # The same rows are reachable through the general audit endpoint, so an
    # operator does not need to know which endpoint wrote them.
    general = await client.get("/memory/audit", params={"entity_type": "domain"})
    subjects = {entry["subject"] for entry in general.json()["entries"]}
    assert domain_id in subjects


async def test_a_runtime_domain_node_can_be_overridden_by_its_owner(client, unique):
    domain_id = f"green_{unique}"
    await client.post("/memory/domain", json=_spec(domain_id))
    await client.post(f"/memory/domain/{domain_id}/activate?owner=alex")
    node_id = f"{domain_id}_1"

    forbidden = await client.post(
        f"/memory/node/{node_id}/override", json={"action": "flood", "owner": "stranger"}
    )
    assert forbidden.status_code == 403

    allowed = await client.post(
        f"/memory/node/{node_id}/override", json={"action": "irrigate_now", "owner": "grower"}
    )
    assert allowed.status_code == 200

    decided = await client.post("/decide", json={"inputs": {"soil_moisture_pct": 10}})
    assert decided.json()["action"] == "irrigate_now"

    # The node reports itself under the runtime domain, not under any kb domain.
    node = await client.get(f"/tree/{node_id}")
    assert node.json()["domain"] == domain_id
    assert node.json()["version"] == 2


async def test_recreating_an_existing_domain_is_refused(client, unique):
    domain_id = f"green_{unique}"
    assert (await client.post("/memory/domain", json=_spec(domain_id))).status_code == 201
    again = await client.post("/memory/domain", json=_spec(domain_id))
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"]


@pytest.mark.parametrize(
    "mutate,expected_fragment",
    [
        pytest.param(
            lambda s: s["nodes"].append(dict(s["nodes"][1], id=s["nodes"][0]["id"])),
            "Duplicate node ids",
            id="duplicate-ids",
        ),
        pytest.param(
            lambda s: s["nodes"].__setitem__(
                0, dict(s["nodes"][0], parent=None)
            )
            or s["nodes"].__setitem__(1, dict(s["nodes"][1], parent=None)),
            "exactly one root",
            id="two-roots",
        ),
        pytest.param(
            lambda s: s["nodes"][1].__setitem__("parent", "nowhere"),
            "not in the domain",
            id="unresolvable-parent",
        ),
        pytest.param(
            lambda s: s["nodes"][2].__setitem__("parent", s["nodes"][2]["id"]),
            "Cycle",
            id="self-cycle",
        ),
        pytest.param(
            lambda s: s["nodes"][1].__setitem__("condition", "soil_moisture_pct >"),
            "not readable Prolog",
            id="unparseable-condition",
        ),
        pytest.param(
            lambda s: s["nodes"][1].__setitem__("condition", "demand > 80, garbage("),
            "not readable Prolog",
            id="condition-with-trailing-junk",
        ),
        pytest.param(
            lambda s: s["nodes"][1].__setitem__("id", "n3"),
            "unique across every domain",
            id="node-id-already-taken",
        ),
    ],
)
async def test_a_spec_that_could_not_be_walked_is_refused(
    client, unique, mutate, expected_fragment
):
    spec = _spec(f"green_{unique}")
    mutate(spec)
    response = await client.post("/memory/domain", json=spec)
    assert response.status_code == 400, response.text
    assert expected_fragment in response.json()["detail"]


async def test_a_rejected_domain_is_left_out_of_the_registry(client, unique):
    domain_id = f"green_{unique}"
    spec = _spec(domain_id)
    spec["nodes"][1]["condition"] = "x >"
    assert (await client.post("/memory/domain", json=spec)).status_code == 400

    listed = {d["id"] for d in (await client.get("/trees")).json()}
    assert domain_id not in listed


async def test_activating_an_unknown_domain_is_404(client):
    assert (await client.post("/memory/domain/nope/activate?owner=alex")).status_code == 404


# ---------------------------------------------------------------------------
# Deactivation and deletion
# ---------------------------------------------------------------------------


async def test_a_deactivated_domain_stops_answering_but_stays_registered(
    client, unique
):
    domain_id = f"green_{unique}"
    await client.post("/memory/domain", json=_spec(domain_id))
    await client.post(f"/memory/domain/{domain_id}/activate?owner=alex")

    decided = await client.post(
        "/decide", json={"inputs": {"soil_moisture_pct": 10}}
    )
    assert decided.status_code == 200
    assert decided.json()["domain"] == domain_id

    deactivated = await client.post(
        f"/memory/domain/{domain_id}/deactivate?owner=alex"
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["active"] is False

    # Out of the routable set: no active domain claims these keys any more.
    routed = await client.post(
        "/decide", json={"inputs": {"soil_moisture_pct": 10}}
    )
    assert routed.status_code == 400
    assert routed.json()["detail"]["error"] == "unknown_domain"

    # Still registered: visible in /trees and through the registry entry.
    listed = {d["id"]: d for d in (await client.get("/trees")).json()}
    assert listed[domain_id]["active"] is False
    detail = await client.get(f"/memory/domain/{domain_id}")
    assert detail.status_code == 200
    assert detail.json()["active"] is False

    # And it can come back.
    reactivated = await client.post(
        f"/memory/domain/{domain_id}/activate?owner=alex"
    )
    assert reactivated.status_code == 200
    decided = await client.post(
        "/decide", json={"inputs": {"soil_moisture_pct": 10}}
    )
    assert decided.status_code == 200
    assert decided.json()["domain"] == domain_id


async def test_deactivating_a_domain_is_audited(client, unique):
    domain_id = f"green_{unique}"
    await client.post("/memory/domain", json=_spec(domain_id))
    await client.post(f"/memory/domain/{domain_id}/activate?owner=alex")
    await client.post(f"/memory/domain/{domain_id}/deactivate?owner=blair")

    audit = await client.get(f"/memory/domain/{domain_id}/audit")
    assert audit.status_code == 200
    body = audit.json()
    assert [entry["operation"] for entry in body["entries"]] == [
        "DEACTIVATE",
        "ACTIVATE",
        "CREATE",
    ]
    assert body["entries"][0]["owner"] == "blair"
    assert body["entries"][0]["old_value"] == {"active": True}
    assert body["entries"][0]["new_value"] == {"active": False}
    assert all(entry["diff_hash"] for entry in body["entries"])


async def test_deactivating_an_inactive_domain_is_idempotent(client, unique):
    domain_id = f"green_{unique}"
    await client.post("/memory/domain", json=_spec(domain_id))
    deactivated = await client.post(
        f"/memory/domain/{domain_id}/deactivate?owner=alex"
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["active"] is False


async def test_deactivating_an_unknown_domain_is_404(client):
    response = await client.post("/memory/domain/nope/deactivate?owner=alex")
    assert response.status_code == 404


async def test_a_deleted_domain_vanishes_but_its_audit_trail_stays(
    client, unique
):
    domain_id = f"green_{unique}"
    spec = _spec(domain_id)
    await client.post("/memory/domain", json=spec)
    await client.post(f"/memory/domain/{domain_id}/activate?owner=alex")

    deleted = await client.delete(f"/memory/domain/{domain_id}?owner=alex")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["id"] == domain_id
    assert deleted.json()["node_count"] == 3

    # Gone from the registry, the tree view and the router.
    listed = {d["id"] for d in (await client.get("/trees")).json()}
    assert domain_id not in listed
    detail = await client.get(f"/memory/domain/{domain_id}")
    assert detail.status_code == 404
    routed = await client.post(
        "/decide", json={"inputs": {"soil_moisture_pct": 10}}
    )
    assert routed.status_code == 400
    assert routed.json()["detail"]["error"] == "unknown_domain"

    # The audit trail outlives the domain, and carries the whole spec.
    audit = await client.get(f"/memory/domain/{domain_id}/audit")
    assert audit.status_code == 200
    entries = audit.json()["entries"]
    assert [entry["operation"] for entry in entries] == [
        "DELETE",
        "ACTIVATE",
        "CREATE",
    ]
    deletion = entries[0]
    assert deletion["owner"] == "alex"
    assert deletion["new_value"] is None
    assert deletion["old_value"]["label"] == "Greenhouse climate"
    assert deletion["old_value"]["active"] is True
    assert deletion["old_value"]["input_keys"] == spec["input_keys"]
    assert [node["id"] for node in deletion["old_value"]["nodes"]] == [
        node["id"] for node in spec["nodes"]
    ]

    # The node ids are free again, so a replacement domain can take them.
    replacement = _spec(domain_id)
    created = await client.post("/memory/domain", json=replacement)
    assert created.status_code == 201, created.text


async def test_deleting_a_domain_that_ships_in_the_image_is_refused(client):
    response = await client.delete("/memory/domain/energy?owner=alex")
    assert response.status_code == 400
    assert "ships in the image" in response.json()["detail"]


async def test_deleting_an_unknown_domain_is_404(client):
    response = await client.delete("/memory/domain/nope?owner=alex")
    assert response.status_code == 404


async def test_an_inactive_domain_can_be_deleted_directly(client, unique):
    domain_id = f"green_{unique}"
    await client.post("/memory/domain", json=_spec(domain_id))
    deleted = await client.delete(f"/memory/domain/{domain_id}?owner=alex")
    assert deleted.status_code == 200
    assert deleted.json()["active"] is False
    listed = {d["id"] for d in (await client.get("/trees")).json()}
    assert domain_id not in listed