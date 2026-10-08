"""
Decision tree over arbitrary inputs.

The energy tree in kb/decision_tree.pl is the example domain. What matters is
that it is driven by a generic key/value map and a condition evaluator that was
never told about demand, temperature or humidity — so a different KB needs no
API change.
"""
import pytest

from src.api.prolog.client import PrologError, to_prolog

ENERGY_CASES = [
    ({"demand": 90, "temperature": 35, "humidity": 80}, "n7", "activate_dehumidifier"),
    ({"demand": 90, "temperature": 35, "humidity": 50}, "n8", "increase_cooling_only"),
    ({"demand": 90, "temperature": 25, "humidity": 70}, "n9", "adjust_airflow"),
    ({"demand": 90, "temperature": 25, "humidity": 50}, "n10", "keep_cooling_only"),
    ({"demand": 50, "temperature": 30, "humidity": 50}, "n5", "monitor_only"),
    ({"demand": 30, "temperature": 20, "humidity": 50}, "n6", "do_nothing"),
    ({"demand": 80, "temperature": 30, "humidity": 50}, "n10", "keep_cooling_only"),
    ({"demand": 0, "temperature": 0, "humidity": 0}, "n6", "do_nothing"),
]


@pytest.mark.parametrize("inputs,leaf,action", ENERGY_CASES)
async def test_decision_tree_cases(client, inputs, leaf, action):
    response = await client.post("/decide", json={"inputs": inputs})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["leaf_node"] == leaf
    assert body["action"] == action
    # Detected from the keys, never asked for: these three are energy's.
    assert body["domain"] == "energy"


async def test_decision_returns_a_full_proof_trace(client):
    response = await client.post(
        "/decide", json={"inputs": {"demand": 90, "temperature": 35, "humidity": 80}}
    )
    body = response.json()
    trace = body["proof_trace"]

    assert [step["node_id"] for step in trace] == ["root", "n1", "n3", "n7"]
    for step in trace:
        assert step["condition"]
        assert step["owner"]
    assert trace[-1]["action"] == "activate_dehumidifier"
    assert body["owner"] == "optimizer"


async def test_decision_does_not_call_the_llm(client, monkeypatch):
    """/decide is symbolic; explanations live behind /agent/ask."""
    from src.llm.base import get_llm_provider

    class ExplodingLLM:
        async def ask(self, *args, **kwargs):
            raise AssertionError("/decide must not call the LLM")

        async def is_healthy(self) -> bool:
            return True

        async def close(self) -> None:
            return None

    from src.api.main import app

    app.dependency_overrides[get_llm_provider] = lambda: ExplodingLLM()
    try:
        response = await client.post(
            "/decide", json={"inputs": {"demand": 90, "temperature": 35, "humidity": 80}}
        )
    finally:
        app.dependency_overrides.pop(get_llm_provider, None)

    assert response.status_code == 200
    assert "nl_explanation" not in response.json()


async def test_partial_inputs_stop_at_the_deepest_justified_node(client):
    """A dict missing humidity cannot choose between n7 and n8, so n3 answers."""
    response = await client.post("/decide", json={"inputs": {"demand": 90, "temperature": 35}})
    assert response.status_code == 200
    body = response.json()
    assert body["leaf_node"] == "n3"
    assert "n3" in [step["node_id"] for step in body["proof_trace"]]


async def test_empty_inputs_match_no_domain(client):
    """No domain claims a key that is not there, so {} is a 400, not a root.

    This is a deliberate change from the single-tree behaviour, where the root's
    `true` condition matched anything including nothing. Routing on declared keys
    means the empty dict identifies no tree at all, and answering from an arbitrary
    one would be a guess. The detail lists what each domain does declare so the
    caller can send something usable.
    """
    response = await client.post("/decide", json={"inputs": {}})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_domain"
    keys = detail["keys_by_domain"]
    assert "energy" in keys
    assert "demand" in keys["energy"]
    assert all(keys[domain] for domain in keys)


async def test_mixed_type_inputs_do_not_match(client):
    """A numeric threshold never matches a string, so nothing is invented."""
    response = await client.post("/decide", json={"inputs": {"demand": "not-a-number"}})
    assert response.status_code == 200
    assert response.json()["leaf_node"] == "root"


async def test_extra_unused_inputs_are_ignored(client):
    """The tree decides what matters; extra keys do not break the walk."""
    response = await client.post(
        "/decide",
        json={
            "inputs": {
                "demand": 90,
                "temperature": 35,
                "humidity": 80,
                "unrelated_flag": True,
                "forecast_confidence": 0.87,
            }
        },
    )
    assert response.status_code == 200
    assert response.json()["leaf_node"] == "n7"


async def test_generic_conditions_accept_any_key(prolog):
    """The evaluator is not energy-specific — it parses any key/operator/threshold."""
    checks = [
        ("risk_score > 5", {"risk_score": 7}, True),
        ("risk_score > 5", {"risk_score": 2}, False),
        ("risk_score < 5", {"risk_score": 2}, True),
        ("region == north", {"region": "north"}, True),
        ("region == north", {"region": "south"}, False),
        ("stage =< 2", {"stage": 2}, True),
        ("stage =< 2", {"stage": 3}, False),
        ("count =:= 3", {"count": 3}, True),
        ("count =:= 3", {"count": 4}, False),
        ("emergency_active", {"emergency_active": True}, True),
        ("emergency_active", {"emergency_active": False}, False),
        ("(demand > 80, humidity > 70)", {"demand": 90, "humidity": 80}, True),
        ("(demand > 80, humidity > 70)", {"demand": 90, "humidity": 20}, False),
        ("\\+ (demand > 80)", {"demand": 50}, True),
        ("unknown_key > 1", {"demand": 50}, False),
    ]

    for condition, inputs, expected in checks:
        # to_prolog, not an f-string: Python renders False as the atom `False`,
        # which Prolog reads as an unbound variable, so `emergency_active` would
        # test as true.
        goal = f"decision_tree:eval_condition(({condition}), {to_prolog(inputs)})"
        results = await prolog.query(goal)
        assert bool(results) is expected, f"{condition} with {inputs} should be {expected}"


async def test_numeric_comparisons_never_match_strings(prolog):
    """A wrong-typed input fails the condition rather than raising or matching."""
    term = "_{demand:high}"
    assert not await prolog.query(f"decision_tree:eval_condition((demand > 80), {term})")
    assert not await prolog.query(f"decision_tree:eval_condition((demand =< 80), {term})")


async def test_string_comparisons_use_term_order(prolog):
    results = await prolog.query("decision_tree:eval_condition((region == north), _{region:north})")
    assert results
    results = await prolog.query("decision_tree:eval_condition((region == north), _{region:south})")
    assert not results


async def test_tree_endpoint_lists_every_node(client):
    """/tree is the merged forest, so assert energy's nodes are present, not the count."""
    response = await client.get("/tree")
    assert response.status_code == 200
    body = response.json()
    energy = {node["id"] for node in body["nodes"] if node["domain"] == "energy"}
    assert {"root", "n1", "n7", "n10"} <= energy
    assert len(energy) == 11
    assert body["root"] == "root"
    assert {domain["id"] for domain in body["domains"]} >= {"energy"}


async def test_get_single_node(client):
    response = await client.get("/tree/n3")
    assert response.status_code == 200
    assert response.json() == {
        "id": "n3",
        "parent": "n1",
        "condition": "temperature > 30",
        "action": "increase_cooling",
        "owner": "optimizer",
        "version": 1,
        "domain": "energy",
    }


async def test_get_unknown_node_is_404(client):
    assert (await client.get("/tree/nope")).status_code == 404


async def test_broken_goal_raises_prolog_error(prolog):
    """A bad goal surfaces as PrologError, which routes turn into a 4xx."""
    with pytest.raises(PrologError):
        await prolog.query("symbolic_memory:no_such_predicate(X)")
