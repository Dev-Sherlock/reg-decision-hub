#!/usr/bin/env python3
"""
Governance enforcement evaluation script for the Regulated Decision-Tree Agent Hub.
Tests permission checks and audit logging.
"""
import asyncio
import json
from dataclasses import dataclass
from typing import List
import httpx

from _config import api_base, use_utf8_console


API_BASE = api_base()


@dataclass
class GovernanceResult:
    test_name: str
    passed: bool
    details: str


async def test_forecaster_can_write_own_nodes() -> GovernanceResult:
    """Test that forecaster can write to their own nodes (n1, n2, n5, n6)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        original = await get_node_action(client, "n1")
        try:
            # Try to override n1 (forecaster owned)
            response = await client.post(
                f"{API_BASE}/memory/node/n1/override",
                json={"action": "test_override", "owner": "forecaster"}
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    return GovernanceResult(
                        "forecaster_can_write_n1",
                        True,
                        f"Successfully overridden n1: {data}"
                    )
            return GovernanceResult(
                "forecaster_can_write_n1",
                False,
                f"Failed: {response.status_code} - {response.text}"
            )
        finally:
            await restore_node_action(client, "n1", "forecaster", original)


async def test_optimizer_can_write_own_nodes() -> GovernanceResult:
    """Test that optimizer can write to their own nodes (n3, n4, n7, n8, n9, n10)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        original = await get_node_action(client, "n3")
        try:
            response = await client.post(
                f"{API_BASE}/memory/node/n3/override",
                json={"action": "test_override", "owner": "optimizer"}
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    return GovernanceResult(
                        "optimizer_can_write_n3",
                        True,
                        f"Successfully overridden n3: {data}"
                    )
            return GovernanceResult(
                "optimizer_can_write_n3",
                False,
                f"Failed: {response.status_code} - {response.text}"
            )
        finally:
            await restore_node_action(client, "n3", "optimizer", original)


async def test_forecaster_cannot_write_optimizer_nodes() -> GovernanceResult:
    """Test that forecaster CANNOT write to optimizer nodes."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE}/memory/node/n3/override",
            json={"action": "test_override", "owner": "forecaster"}
        )
        if response.status_code == 403:
            return GovernanceResult(
                "forecaster_cannot_write_n3",
                True,
                "Correctly rejected with 403"
            )
        return GovernanceResult(
            "forecaster_cannot_write_n3",
            False,
            f"Expected 403, got {response.status_code}: {response.text}"
        )


async def test_optimizer_cannot_write_forecaster_nodes() -> GovernanceResult:
    """Test that optimizer CANNOT write to forecaster nodes."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE}/memory/node/n1/override",
            json={"action": "test_override", "owner": "optimizer"}
        )
        if response.status_code == 403:
            return GovernanceResult(
                "optimizer_cannot_write_n1",
                True,
                "Correctly rejected with 403"
            )
        return GovernanceResult(
            "optimizer_cannot_write_n1",
            False,
            f"Expected 403, got {response.status_code}: {response.text}"
        )


async def test_system_cannot_write_any() -> GovernanceResult:
    """Test that system agent cannot write to any nodes."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE}/memory/node/n1/override",
            json={"action": "test_override", "owner": "system"}
        )
        if response.status_code == 403:
            return GovernanceResult(
                "system_cannot_write_n1",
                True,
                "Correctly rejected with 403"
            )
        return GovernanceResult(
            "system_cannot_write_n1",
            False,
            f"Expected 403, got {response.status_code}: {response.text}"
        )


async def get_node_action(client: httpx.AsyncClient, node_id: str) -> str:
    """The action a node currently has."""
    response = await client.get(f"{API_BASE}/tree/{node_id}")
    if response.status_code != 200:
        raise RuntimeError(f"GET /tree/{node_id} returned {response.status_code}")
    return response.json()["action"]


async def restore_node_action(
    client: httpx.AsyncClient, node_id: str, owner: str, action: str
) -> None:
    """Put a node's action back after a test changed it.

    An override is persistent for the life of the container. Without this, the
    audit tests leave nodes carrying their test actions and correctness.py then
    fails on decisions that were never wrong — the leak looks like a tree bug.
    """
    response = await client.post(
        f"{API_BASE}/memory/node/{node_id}/override",
        json={"action": action, "owner": owner},
    )
    if response.status_code != 200:
        print(
            f"warning: could not restore {node_id} to {action!r}: "
            f"{response.status_code} {response.text[:120]}"
        )


async def test_audit_log_created() -> GovernanceResult:
    """Test that audit log entry is created on successful override."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        original = await get_node_action(client, "n2")
        try:
            # Override a node
            response = await client.post(
                f"{API_BASE}/memory/node/n2/override",
                json={"action": "audit_test_action", "owner": "forecaster"}
            )
            if response.status_code != 200:
                return GovernanceResult(
                    "audit_log_created",
                    False,
                    f"Override failed: {response.status_code}"
                )

            # Check audit log
            audit_response = await client.get(f"{API_BASE}/memory/node/n2/audit")
            if audit_response.status_code == 200:
                audits = audit_response.json().get("entries", [])
                # Find our test entry
                test_entry = next(
                    (a for a in audits if a.get("new_action") == "audit_test_action"), None
                )
                if test_entry:
                    return GovernanceResult(
                        "audit_log_created",
                        True,
                        f"Audit entry found: {test_entry}"
                    )
            return GovernanceResult(
                "audit_log_created",
                False,
                f"Audit log not found or empty: {audit_response.text}"
            )
        finally:
            await restore_node_action(client, "n2", "forecaster", original)


async def test_audit_log_includes_diff_hash() -> GovernanceResult:
    """Test that audit log includes diff hash."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        original = await get_node_action(client, "n5")
        try:
            # Override a node
            response = await client.post(
                f"{API_BASE}/memory/node/n5/override",
                json={"action": "hash_test_action", "owner": "forecaster"}
            )
            if response.status_code != 200:
                return GovernanceResult(
                    "audit_log_includes_diff_hash",
                    False,
                    f"Override failed: {response.status_code}"
                )

            audit_response = await client.get(f"{API_BASE}/memory/node/n5/audit")
            if audit_response.status_code == 200:
                audits = audit_response.json().get("entries", [])
                test_entry = next(
                    (a for a in audits if a.get("new_action") == "hash_test_action"), None
                )
                if test_entry and test_entry.get("diff_hash"):
                    return GovernanceResult(
                        "audit_log_includes_diff_hash",
                        True,
                        f"Diff hash present: {test_entry['diff_hash']}"
                    )
            return GovernanceResult(
                "audit_log_includes_diff_hash",
                False,
                "Diff hash missing from audit entry"
            )
        finally:
            await restore_node_action(client, "n5", "forecaster", original)


async def test_nonexistent_node_returns_404() -> GovernanceResult:
    """Test that overriding non-existent node returns 404."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE}/memory/node/nonexistent/override",
            json={"action": "test", "owner": "forecaster"}
        )
        if response.status_code == 404:
            return GovernanceResult(
                "nonexistent_node_returns_404",
                True,
                "Correctly returned 404"
            )
        return GovernanceResult(
            "nonexistent_node_returns_404",
            False,
            f"Expected 404, got {response.status_code}"
        )


async def test_fact_override_audited() -> GovernanceResult:
    """A fact override is governed too: it records the owner who made the change."""
    subject, predicate = "eval_governance_probe", "policy"
    async with httpx.AsyncClient(timeout=30.0) as client:
        create = await client.post(
            f"{API_BASE}/memory/fact",
            json={"subject": subject, "predicate": predicate, "value": "original"},
        )
        if create.status_code != 201:
            return GovernanceResult(
                "fact_override_audited",
                False,
                f"Fact creation failed: {create.status_code} - {create.text}"
            )

        override = await client.post(
            f"{API_BASE}/memory/fact/{subject}/{predicate}/override",
            json={"value": "amended", "owner": "compliance_officer"},
        )
        if override.status_code != 200:
            return GovernanceResult(
                "fact_override_audited",
                False,
                f"Override failed: {override.status_code} - {override.text}"
            )

        audit = await client.get(f"{API_BASE}/memory/audit?subject={subject}&predicate={predicate}")
        entries = audit.json().get("entries", []) if audit.status_code == 200 else []
        # Audit rows carry the fact's value itself, not a wrapper object, so
        # new_value is the bare "amended" for a string-valued fact.
        recorded = any(
            entry.get("owner") == "compliance_officer" and entry.get("new_value") == "amended"
            for entry in entries
        )
        if recorded:
            return GovernanceResult(
                "fact_override_audited",
                True,
                "Fact override recorded with owner and new value",
            )
        return GovernanceResult(
            "fact_override_audited",
            False,
            f"No matching audit entry: {audit.text}",
        )


async def test_override_of_missing_fact_returns_404() -> GovernanceResult:
    """Overriding a fact that was never stored must not silently create it."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE}/memory/fact/eval_governance_absent/policy/override",
            json={"value": "invented", "owner": "forecaster"},
        )
        if response.status_code == 404:
            return GovernanceResult(
                "override_of_missing_fact_returns_404",
                True,
                "Correctly returned 404",
            )
        return GovernanceResult(
            "override_of_missing_fact_returns_404",
            False,
            f"Expected 404, got {response.status_code}: {response.text}",
        )


# ---------------------------------------------------------------------------
# Non-energy domains
#
# Every case above reads the energy tree, so a permission check that only ever
# consults decision_tree's can_write/2 would pass all of them. These use the other
# three example domains, whose owners are named differently and whose nodes live
# in their own modules.
# ---------------------------------------------------------------------------


async def test_non_energy_owner_can_write_own_node() -> GovernanceResult:
    """sec4 belongs to ciso, so ciso may write it."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        original = await get_node_action(client, "sec4")
        try:
            response = await client.post(
                f"{API_BASE}/memory/node/sec4/override",
                json={"action": "governance_test_action", "owner": "ciso"},
            )
            if response.status_code != 200:
                return GovernanceResult(
                    "ciso_can_write_sec4",
                    False,
                    f"Expected 200, got {response.status_code}: {response.text[:120]}",
                )
            current = await get_node_action(client, "sec4")
            if current != "governance_test_action":
                return GovernanceResult(
                    "ciso_can_write_sec4",
                    False,
                    f"Node still reports {current!r}",
                )
            return GovernanceResult("ciso_can_write_sec4", True, "ciso wrote sec4")
        finally:
            await restore_node_action(client, "sec4", "ciso", original)


async def test_non_owner_cannot_write_across_domains() -> GovernanceResult:
    """soc_lead owns sec1/sec3/sec5 but not sec4, so sec4 must be refused.

    A check that only compared the owner's name against *a* domain would let this
    through: soc_lead does own nodes, just not this one.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{API_BASE}/memory/node/sec4/override",
            json={"action": "should_not_apply", "owner": "soc_lead"},
        )
        if response.status_code != 403:
            return GovernanceResult(
                "soc_lead_cannot_write_sec4",
                False,
                f"Expected 403, got {response.status_code}: {response.text[:120]}",
            )
        return GovernanceResult(
            "soc_lead_cannot_write_sec4", True, "Cross-domain owner refused"
        )


async def test_non_energy_override_is_audited_with_a_diff_hash() -> GovernanceResult:
    """The audit row for a non-energy node carries old, new and diff_hash."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        original = await get_node_action(client, "at3")
        marker = "audited_ice_action"
        try:
            response = await client.post(
                f"{API_BASE}/memory/node/at3/override",
                json={"action": marker, "owner": "safety_officer"},
            )
            if response.status_code != 200:
                return GovernanceResult(
                    "non_energy_override_audited",
                    False,
                    f"Override failed: {response.status_code}",
                )

            audit = await client.get(f"{API_BASE}/memory/node/at3/audit")
            if audit.status_code != 200:
                return GovernanceResult(
                    "non_energy_override_audited",
                    False,
                    f"Audit returned {audit.status_code}",
                )
            entry = next(
                (
                    e
                    for e in audit.json().get("entries", [])
                    if e.get("new_action") == marker
                ),
                None,
            )
            if entry is None:
                return GovernanceResult(
                    "non_energy_override_audited",
                    False,
                    f"No audit entry for {marker!r}",
                )
            if entry.get("old_action") != original:
                return GovernanceResult(
                    "non_energy_override_audited",
                    False,
                    f"old_action was {entry.get('old_action')!r}, expected {original!r}",
                )
            if not entry.get("diff_hash"):
                return GovernanceResult(
                    "non_energy_override_audited",
                    False,
                    "Audit entry carries no diff_hash",
                )
            return GovernanceResult(
                "non_energy_override_audited",
                True,
                f"at3 override audited: {entry['diff_hash']}",
            )
        finally:
            await restore_node_action(client, "at3", "safety_officer", original)


async def test_non_energy_decision_follows_an_override() -> GovernanceResult:
    """The override must change the decision, not just the stored node.

    Without this a tree that ignored overrides entirely would still pass every
    permission and audit case above.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        node_id, owner = "qa1", "qe_engineer"
        original = await get_node_action(client, node_id)
        marker = "governance_quarantine_action"
        try:
            response = await client.post(
                f"{API_BASE}/memory/node/{node_id}/override",
                json={"action": marker, "owner": owner},
            )
            if response.status_code != 200:
                return GovernanceResult(
                    "non_energy_override_changes_decision",
                    False,
                    f"Override failed: {response.status_code}",
                )

            decided = await client.post(
                f"{API_BASE}/decide", json={"inputs": {"defect_rate_pct": 8}}
            )
            if decided.status_code != 200:
                return GovernanceResult(
                    "non_energy_override_changes_decision",
                    False,
                    f"/decide returned {decided.status_code}: {decided.text[:120]}",
                )
            body = decided.json()
            if body.get("action") != marker:
                return GovernanceResult(
                    "non_energy_override_changes_decision",
                    False,
                    f"Decision reported {body.get('action')!r}, expected {marker!r}",
                )
            if body.get("domain") != "manufacturing":
                return GovernanceResult(
                    "non_energy_override_changes_decision",
                    False,
                    f"Decision came from {body.get('domain')!r}, expected 'manufacturing'",
                )
            return GovernanceResult(
                "non_energy_override_changes_decision",
                True,
                f"qa1 decision followed the override to {marker}",
            )
        finally:
            await restore_node_action(client, node_id, owner, original)


async def run_governance_tests() -> List[GovernanceResult]:
    """Run all governance tests."""
    tests = [
        test_forecaster_can_write_own_nodes(),
        test_optimizer_can_write_own_nodes(),
        test_forecaster_cannot_write_optimizer_nodes(),
        test_optimizer_cannot_write_forecaster_nodes(),
        test_system_cannot_write_any(),
        test_audit_log_created(),
        test_audit_log_includes_diff_hash(),
        test_nonexistent_node_returns_404(),
        test_fact_override_audited(),
        test_override_of_missing_fact_returns_404(),
        test_non_energy_owner_can_write_own_node(),
        test_non_owner_cannot_write_across_domains(),
        test_non_energy_override_is_audited_with_a_diff_hash(),
        test_non_energy_decision_follows_an_override(),
    ]
    
    print("Running governance enforcement tests...")
    results = []
    for coro in tests:
        result = await coro
        results.append(result)
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"  {status}: {result.test_name} - {result.details}")
    
    return results


def print_summary(results: List[GovernanceResult]) -> None:
    """Print summary statistics."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    
    print("\n" + "=" * 60)
    print("GOVERNANCE ENFORCEMENT EVALUATION RESULTS")
    print("=" * 60)
    print(f"Total tests: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Pass rate: {passed/total*100:.1f}%")
    
    if failed > 0:
        print("\nFailed tests:")
        for r in results:
            if not r.passed:
                print(f"  - {r.test_name}: {r.details}")


def save_results(results: List[GovernanceResult], output_path: str = "eval/governance_results.json") -> None:
    """Save results to JSON file."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    
    data = {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total if total > 0 else 0
        },
        "results": [
            {
                "test_name": r.test_name,
                "passed": r.passed,
                "details": r.details
            }
            for r in results
        ]
    }
    
    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to {output_path}")


async def main():
    use_utf8_console()
    results = await run_governance_tests()
    print_summary(results)
    save_results(results)


if __name__ == "__main__":
    asyncio.run(main())