"""
Tests for kb/sample_data.json and the loader that pushes it.

The dataset is the worked example of invariant 8: the memory layer is
subject/predicate/value and has never heard of a homeserver, a thermostat or a
CISO. These tests hold that claim in both directions. The loader has to reject
anything that would smuggle domain knowledge into the store, and every rule has to
derive exactly the facts it declares — including the one rule that must derive
nothing, because a dataset where every rule fires proves nothing.

Two habits from AGENTS.md shape the assertions here. The test database is never
reset, so a stored rule fires over every fact any earlier run left behind; each
derivation expectation is therefore filtered to the dataset's own subjects rather
than compared against the whole `derived` list. And facts are keyed on
(subject, predicate), so nothing may be asserted about a table being empty —
only about what a filter selects.
"""
import json
from collections import Counter
from typing import Any, Dict, List

import pytest

from scripts.seed_sample_data import (
    DatasetError,
    default_api_url,
    default_data_path,
    load_dataset,
    main,
    rule_domains,
    rule_predicates,
    subject_domains,
)

# Loaded at import so the parametrize list below can name the rules. A broken
# dataset should fail collection loudly rather than quietly parametrize nothing.
DATASET: Dict[str, Any] = load_dataset(default_data_path())
RULE_NAMES: List[str] = [rule["name"] for rule in DATASET["rules"]]


def _rule(name: str) -> Dict[str, Any]:
    for rule in DATASET["rules"]:
        if rule["name"] == name:
            return rule
    raise AssertionError(f"{name} is not in the dataset")


def _rule_payload(rule: Dict[str, Any]) -> Dict[str, Any]:
    """The three fields POST /memory/rule actually accepts."""
    return {"name": rule["name"], "head": rule["head"], "body": rule["body"]}


def _derived_triples(response: Any) -> set:
    return {
        (fact["subject"], fact["predicate"], fact["value"])
        for fact in response["derived"]
    }


def _dataset_triples(rule: Dict[str, Any]) -> set:
    return {tuple(entry) for entry in rule["expect"]}


# ---------------------------------------------------------------------------
# Loader validation
#
# Every case below corresponds to a way the file can go wrong quietly. The
# message matters as much as the rejection: it is the only place the reason a
# dataset is unusable gets explained.
# ---------------------------------------------------------------------------


def _write(tmp_path, data: Dict[str, Any]):
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _minimal() -> Dict[str, Any]:
    return {
        "version": 1,
        "domains": {"alpha": ["a1"]},
        "facts": [{"subject": "a1", "predicate": "role", "value": "widget"}],
        "rules": [
            {
                "name": "alpha_rule",
                "head": "fact(X, derived, ok)",
                "body": "[fact(X, role, widget)]",
                "expect": [["a1", "derived", "ok"]],
            }
        ],
    }


def test_a_minimal_dataset_is_accepted(tmp_path):
    assert load_dataset(_write(tmp_path, _minimal()))["domains"] == {"alpha": ["a1"]}


def test_loader_rejects_a_duplicate_subject_predicate(tmp_path):
    data = _minimal()
    # A fact is keyed on (subject, predicate), so the second write would replace
    # the first with no warning and no audit row for the loss.
    data["facts"].append({"subject": "a1", "predicate": "role", "value": "gadget"})
    with pytest.raises(DatasetError, match="duplicate fact"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_a_subject_claimed_by_two_domains(tmp_path):
    data = _minimal()
    data["domains"]["beta"] = ["a1"]
    with pytest.raises(DatasetError, match="claimed by both"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_an_undeclared_subject(tmp_path):
    data = _minimal()
    data["facts"].append({"subject": "stray", "predicate": "role", "value": "widget"})
    with pytest.raises(DatasetError, match="must belong to exactly one domain"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_an_empty_domain(tmp_path):
    data = _minimal()
    data["domains"]["beta"] = []
    with pytest.raises(DatasetError, match="at least one subject"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_a_fact_without_a_value(tmp_path):
    data = _minimal()
    del data["facts"][0]["value"]
    with pytest.raises(DatasetError, match="needs a 'value'"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_a_fact_without_a_subject(tmp_path):
    data = _minimal()
    data["facts"][0]["predicate"] = ""
    with pytest.raises(DatasetError, match="non-empty string 'predicate'"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_a_head_that_is_not_a_fact(tmp_path):
    data = _minimal()
    # run_rule unpacks the head as fact(S, P, V); anything else derives triples
    # the API has no shape for.
    data["rules"][0]["head"] = "derived(X, ok)"
    with pytest.raises(DatasetError, match="must be a fact/3 goal"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_a_body_that_is_not_a_goal_list(tmp_path):
    data = _minimal()
    data["rules"][0]["body"] = "fact(X, role, widget)"
    with pytest.raises(DatasetError, match="Prolog goal list"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_a_duplicate_rule_name(tmp_path):
    data = _minimal()
    data["rules"].append(dict(data["rules"][0]))
    with pytest.raises(DatasetError, match="duplicate rule name"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_a_join_to_an_undeclared_subject(tmp_path):
    data = _minimal()
    data["rules"][0]["joins"] = ["ghost"]
    with pytest.raises(DatasetError, match="joins unknown subject"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_joins_that_are_not_subject_names(tmp_path):
    data = _minimal()
    data["rules"][0]["joins"] = "a1"
    with pytest.raises(DatasetError, match="must be a list of subject names"):
        load_dataset(_write(tmp_path, data))


def test_loader_rejects_missing_top_level_sections(tmp_path):
    with pytest.raises(DatasetError, match="non-empty 'facts' list"):
        load_dataset(_write(tmp_path, {"facts": [], "rules": [], "domains": {"a": ["x"]}}))
    with pytest.raises(DatasetError, match="'rules' list"):
        load_dataset(_write(tmp_path, {"facts": [{"subject": "x", "predicate": "p", "value": 1}]}))
    with pytest.raises(DatasetError, match="non-empty 'domains' object"):
        load_dataset(_write(tmp_path, {"facts": [{"subject": "x", "predicate": "p", "value": 1}], "rules": []}))


def test_loader_rejects_a_root_that_is_not_an_object(tmp_path):
    with pytest.raises(DatasetError, match="root must be an object"):
        load_dataset(_write(tmp_path, ["not", "an", "object"]))


def test_loader_rejects_a_missing_file(tmp_path):
    with pytest.raises(DatasetError, match="does not exist"):
        load_dataset(tmp_path / "absent.json")


def test_loader_rejects_invalid_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(DatasetError, match="not valid JSON"):
        load_dataset(path)


# ---------------------------------------------------------------------------
# Dataset helpers
# ---------------------------------------------------------------------------


def test_rule_predicates_are_ordered_and_deduplicated():
    rule = {"body": "[fact(X, role, R), fact(X, role, R2), fact(X, temp_c, T)]"}
    assert rule_predicates(rule) == ["role", "temp_c"]


def test_default_data_path_locates_the_shipped_dataset():
    assert default_data_path().name == "sample_data.json"
    assert default_data_path().exists()


def test_default_data_path_follows_kb_path(monkeypatch, tmp_path):
    # The container has no /app/kb; the dataset sits beside the KB the API was
    # pointed at, which is why KB_PATH's directory is searched first.
    (tmp_path / "sample_data.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("KB_PATH", str(tmp_path / "symbolic_memory.pl"))
    assert default_data_path() == tmp_path / "sample_data.json"


def test_default_api_url_is_a_url():
    assert default_api_url().startswith("http")


# ---------------------------------------------------------------------------
# The shipped dataset is internally consistent
# ---------------------------------------------------------------------------


def test_every_declared_domain_has_facts():
    lookup = subject_domains(DATASET)
    facts_per_domain = Counter(lookup[fact["subject"]] for fact in DATASET["facts"])
    for domain in DATASET["domains"]:
        assert facts_per_domain[domain] > 0, f"domain {domain} declares subjects but no facts"


def test_every_fact_subject_is_declared_exactly_once():
    lookup = subject_domains(DATASET)
    subjects = [fact["subject"] for fact in DATASET["facts"]]
    assert set(subjects) == set(lookup)
    # Subject names double as a namespace across domains, so a subject appearing
    # under two facts' predicates is fine but appearing in two domains is not.
    assert len(lookup) == sum(len(group) for group in DATASET["domains"].values())


def test_every_rule_declares_what_it_derives():
    for rule in DATASET["rules"]:
        assert "expect" in rule, f"{rule['name']} does not declare its derivation"
        assert "note" in rule, f"{rule['name']} does not explain itself"
        for entry in rule["expect"]:
            assert len(entry) == 3, f"{rule['name']} has a malformed expect entry"


def test_expect_entries_reference_declared_subjects():
    lookup = subject_domains(DATASET)
    for rule in DATASET["rules"]:
        for subject, _predicate, _value in rule["expect"]:
            assert subject in lookup, f"{rule['name']} expects a fact for undeclared {subject}"


def test_at_least_one_rule_is_expected_to_derive_nothing():
    # cryostat_below_design_temperature sits at exactly 4 K against a strict < 4
    # bound. A dataset where every rule fires on its first matching subject would
    # not distinguish a correct KB from one that ignores its bodies.
    assert any(not rule["expect"] for rule in DATASET["rules"]), (
        "every rule derives something, so nothing here tests a bound that holds"
    )


def test_at_least_one_join_crosses_a_domain_boundary():
    spanning = {
        rule["name"]: rule_domains(DATASET, rule["joins"])
        for rule in DATASET["rules"]
        if rule.get("joins")
    }
    assert spanning, "no rule joins two subjects at all"
    assert any(len(domains) > 1 for domains in spanning.values()), (
        f"no join crosses a domain boundary: {spanning}"
    )


def test_rule_domains_measures_the_join_not_the_derivation():
    # The rule writes for site_north (energy) but reads ups_01 (homeserver).
    # Reporting the domains of what a rule *writes* would call this single-domain,
    # which is exactly the join worth documenting.
    rule = _rule("site_over_capacity_on_battery")
    assert _dataset_triples(rule) == {("site_north", "grid_action", "shed_noncritical_load")}
    assert rule_domains(DATASET, rule["joins"]) == ["energy", "homeserver"]


# ---------------------------------------------------------------------------
# Round-trip through the real write path
# ---------------------------------------------------------------------------


async def test_every_sample_value_survives_the_write_path(client, prolog):
    """JSON -> PostgreSQL -> Prolog -> JSON, for every value in the dataset.

    Includes the cases the codec is easiest to get wrong: booleans that read back
    as atoms, lists of strings, floats and integers that must not be swapped.
    """
    for fact in DATASET["facts"]:
        response = await client.post("/memory/fact", json=fact)
        assert response.status_code in (200, 201), (
            f"{fact['subject']}.{fact['predicate']} -> {response.status_code}: {response.text}"
        )

    for fact in DATASET["facts"]:
        stored = await client.get(f"/memory/fact/{fact['subject']}/{fact['predicate']}")
        assert stored.status_code == 200, f"{fact['subject']}.{fact['predicate']} did not read back"
        assert stored.json()["value"] == fact["value"]

        mirrored = await prolog.get_fact(fact["subject"], fact["predicate"])
        assert mirrored is not None, f"Prolog mirror lost {fact['subject']}.{fact['predicate']}"
        assert mirrored["value"] == fact["value"]


async def test_the_dataset_is_searchable_by_value(client):
    for fact in DATASET["facts"]:
        await client.post("/memory/fact", json=fact)

    found = await client.get("/memory/fact", params={"subject": "nas_01"})
    values = {fact["predicate"]: fact["value"] for fact in found.json()["facts"]}
    assert values["raid_state"] == "degraded"
    assert values["services"] == ["smb", "nfs", "ssh"]


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_kb(prolog):
    """The dataset's facts asserted into the Prolog KB.

    Rules fire over Prolog, so this is everything a derivation needs. Pushing
    through POST /memory/fact instead would pay an embedding per fact for every
    rule under test; the HTTP write path is covered once, end to end, by
    test_every_sample_value_survives_the_write_path.
    """
    for fact in DATASET["facts"]:
        await prolog.assert_fact(fact["subject"], fact["predicate"], fact["value"])
    return prolog


@pytest.mark.parametrize("rule_name", RULE_NAMES)
async def test_rule_derives_exactly_what_it_declares(client, sample_kb, rule_name):
    rule = _rule(rule_name)
    created = await client.post("/memory/rule", json=_rule_payload(rule))
    assert created.status_code == 201, created.text

    run = await client.post(f"/memory/rule/{rule['name']}/run")
    assert run.status_code == 200

    # Scoped to the dataset's own subjects: the rule fires over the whole store,
    # so `derived` grows with every fact any earlier run left behind.
    subjects = set(subject_domains(DATASET))
    derived = {
        triple
        for triple in _derived_triples(run.json())
        if triple[0] in subjects
    }
    assert derived == _dataset_triples(rule)


async def test_a_rule_with_no_match_derives_nothing(client, sample_kb):
    rule = _rule("cryostat_below_design_temperature")
    await client.post("/memory/rule", json=_rule_payload(rule))
    run = await client.post(f"/memory/rule/{rule['name']}/run")

    subjects = set(subject_domains(DATASET))
    derived = [t for t in _derived_triples(run.json()) if t[0] in subjects]
    assert derived == []
    assert rule["expect"] == []


async def test_a_dry_run_does_not_persist_the_derivation(client, unique):
    subject = f"{unique}_widget"
    await client.post("/memory/fact", json={"subject": subject, "predicate": "role", "value": "widget"})
    await client.post(
        "/memory/rule",
        json={
            "name": f"{unique}_derived",
            "head": "fact(X, derived_flag, yes)",
            "body": "[fact(X, role, widget)]",
        },
    )

    run = await client.post(f"/memory/rule/{unique}_derived/run")
    assert run.json()["count"] == 1
    # The default is a dry run, so a rule can be tested before it is trusted.
    assert (await client.get(f"/memory/fact/{subject}/derived_flag")).status_code == 404


# ---------------------------------------------------------------------------
# Cross-domain joins
# ---------------------------------------------------------------------------


async def test_a_rule_derives_by_joining_two_domains(client, sample_kb):
    """The dataset's headline claim.

    The body names account_alex's works_at and follows the value. It never
    mentions people or cyber, and the KB has no way to know those exist.
    """
    rule = _rule("privileged_account_without_mfa")
    lookup = subject_domains(DATASET)
    assert lookup["account_alex"] == "people"
    assert lookup["host_web_01"] == "cyber"

    await client.post("/memory/rule", json=_rule_payload(rule))
    run = await client.post(f"/memory/rule/{rule['name']}/run")
    assert ("account_alex", "access_risk", "require_mfa") in _derived_triples(run.json())


async def test_the_join_follows_the_stored_value_not_the_domain_label(client, sample_kb):
    """Repointing the fact changes the answer; the domain names do not matter."""
    rule = _rule("privileged_account_without_mfa")
    await client.post("/memory/rule", json=_rule_payload(rule))
    before = _derived_triples((await client.post(f"/memory/rule/{rule['name']}/run")).json())
    assert ("account_alex", "access_risk", "require_mfa") in before

    # host_db_01 is the same cyber domain and has mfa enabled, so the join
    # following account_alex.works_at is what decides this, not "host_web_01".
    await client.post("/memory/fact", json={"subject": "host_web_01", "predicate": "mfa_enabled", "value": True})
    after = _derived_triples((await client.post(f"/memory/rule/{rule['name']}/run")).json())
    assert ("account_alex", "access_risk", "require_mfa") not in after


async def test_a_three_subject_join_reaches_across_two_domains(client, sample_kb):
    """oncall_alex (people) -> host_web_01 (cyber) via certified + works_at."""
    rule = _rule("certified_oncall_for_vulnerable_host")
    assert rule_domains(DATASET, rule["joins"]) == ["cyber", "people"]

    await client.post("/memory/rule", json=_rule_payload(rule))
    run = await client.post(f"/memory/rule/{rule['name']}/run")
    assert ("oncall_alex", "oncall_assignment", "primary_for_emergency_patch") in _derived_triples(run.json())


async def test_a_rule_can_read_across_a_boundary_without_writing_across_it(client, sample_kb):
    """site_over_capacity_on_battery writes for a site and reads a UPS.

    Measuring a rule by what it writes would report one domain; the interesting
    part is the read on the other side of the boundary.
    """
    rule = _rule("site_over_capacity_on_battery")
    assert subject_domains(DATASET)["ups_01"] == "homeserver"

    await client.post("/memory/rule", json=_rule_payload(rule))
    run = await client.post(f"/memory/rule/{rule['name']}/run")
    derived = _derived_triples(run.json())

    assert ("site_north", "grid_action", "shed_noncritical_load") in derived
    # site_south is under its capacity cap, so the UPS being on battery is not
    # enough on its own — the boundary was crossed, not the threshold dropped.
    assert ("site_south", "grid_action", "shed_noncritical_load") not in derived


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_emit_writes_exactly_the_api_payloads(tmp_path):
    target = tmp_path / "payload.json"
    # No services needed: --emit is the offline path.
    assert main(["--emit", str(target), "--data", str(default_data_path())]) == 0

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload["facts"]) == len(DATASET["facts"])
    assert len(payload["rules"]) == len(DATASET["rules"])
    # expect/joins/note are the dataset's own bookkeeping and are not API fields.
    assert set(payload["rules"][0]) == {"name", "head", "body"}
    assert payload["facts"][0] == DATASET["facts"][0]


def test_summary_runs_without_services(capsys):
    assert main(["--data", str(default_data_path())]) == 0
    printed = capsys.readouterr().out
    assert f"{len(DATASET['facts'])} facts, {len(DATASET['rules'])} rules" in printed
    for domain in DATASET["domains"]:
        assert domain in printed


def test_main_reports_a_bad_dataset(tmp_path, capsys):
    broken = tmp_path / "bad.json"
    broken.write_text(json.dumps({"facts": [], "rules": [], "domains": {}}), encoding="utf-8")
    assert main(["--data", str(broken)]) == 1
    assert "error:" in capsys.readouterr().err