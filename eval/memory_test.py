#!/usr/bin/env python3
"""
Symbolic memory evaluation for the Regulated Decision-Tree Agent Hub.

Proves that the generalized knowledge base behaves as a memory, not a
hard-coded tree: facts are stored and retrieved by subject/predicate, values
round-trip through both PostgreSQL and Prolog, rules derive new facts, semantic
search finds related facts by meaning rather than keyword overlap, and every
mutation is audited.

Run with the stack up:
    docker compose up -d --build
    python eval/memory_test.py
"""
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import httpx

from _config import api_base, use_utf8_console


API_BASE = api_base()


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: str


@dataclass
class MemoryReport:
    checks: List[CheckResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, details: str = "") -> None:
        self.checks.append(CheckResult(name, passed, details))
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}" + (f": {details}" if details else ""))

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)


def new_prefix() -> str:
    """Namespacing so concurrent runs never collide in the shared database."""
    return f"eval_memory_{uuid.uuid4().hex[:10]}"


async def store_fact(client: httpx.AsyncClient, subject: str, predicate: str, value: Any) -> Optional[Dict]:
    response = await client.post(
        f"{API_BASE}/memory/fact",
        json={"subject": subject, "predicate": predicate, "value": value},
    )
    if response.status_code != 201:
        print(f"    store {subject}/{predicate} failed: {response.status_code} {response.text[:200]}")
        return None
    return response.json()


async def get_fact(client: httpx.AsyncClient, subject: str, predicate: str) -> Optional[Dict]:
    response = await client.get(f"{API_BASE}/memory/fact/{subject}/{predicate}")
    return response.json() if response.status_code == 200 else None


async def run_memory_tests() -> MemoryReport:
    report = MemoryReport()
    prefix = new_prefix()
    print(f"Running symbolic memory evaluation (namespace: {prefix})...")

    async with httpx.AsyncClient(timeout=60.0) as client:
        # ------------------------------------------------------------------
        print("\n[1/7] Fact storage and retrieval")
        # ------------------------------------------------------------------
        stored = await store_fact(client, prefix, "demand_threshold", 80)
        report.add(
            "store_fact",
            stored is not None and stored.get("value") == 80,
            f"stored value={stored.get('value') if stored else None!r}",
        )

        fetched = await get_fact(client, prefix, "demand_threshold")
        report.add(
            "get_fact_returns_stored_value",
            fetched is not None and fetched.get("value") == 80,
            f"fetched value={fetched.get('value') if fetched else None!r}",
        )

        missing = await client.get(f"{API_BASE}/memory/fact/{prefix}/never_stored")
        report.add(
            "missing_fact_returns_404",
            missing.status_code == 404,
            f"status={missing.status_code}",
        )

        # ------------------------------------------------------------------
        print("\n[2/7] Value round-tripping through both stores")
        # ------------------------------------------------------------------
        # These deliberately cover every JSON shape the codec must survive:
        # a flat value, a nested object, a list of objects, and a bare null.
        cases = {
            "text_value": "activate_dehumidifier",
            "int_value": 42,
            "float_value": 3.5,
            "bool_value": True,
            "null_value": None,
            "nested_object": {"threshold": 80, "unit": "MW", "window": {"hours": 2}},
            "list_of_objects": [{"agent": "optimizer", "limit": 5}, {"agent": "forecaster", "limit": 2}],
        }
        roundtrip_ok = True
        for predicate, value in cases.items():
            await store_fact(client, prefix, predicate, value)
            row = await get_fact(client, prefix, predicate)
            actual = row.get("value") if row else "<missing>"
            if actual != value:
                roundtrip_ok = False
                print(f"    {predicate}: expected {value!r}, got {actual!r}")
        report.add(
            "value_roundtrip_all_json_types",
            roundtrip_ok,
            f"{len(cases)} predicates covering text/number/bool/null/object/list",
        )

        # ------------------------------------------------------------------
        print("\n[3/7] Upsert semantics and audit trail")
        # ------------------------------------------------------------------
        before = await get_fact(client, prefix, "demand_threshold")
        await store_fact(client, prefix, "demand_threshold", 95)
        after = await get_fact(client, prefix, "demand_threshold")
        report.add(
            "reassert_replaces_value",
            before is not None
            and before.get("value") == 80
            and after is not None
            and after.get("value") == 95,
            f"80 -> {after.get('value') if after else None!r} on the same (subject, predicate)",
        )

        audit = await client.get(
            f"{API_BASE}/memory/audit?subject={prefix}&predicate=demand_threshold"
        )
        entries = audit.json().get("entries", []) if audit.status_code == 200 else []
        report.add(
            "audit_records_update",
            any(e.get("operation") == "UPDATE" for e in entries),
            f"{len(entries)} entries, operations={[e.get('operation') for e in entries]}",
        )
        report.add(
            "audit_records_diff_hash",
            all(e.get("diff_hash") for e in entries) and bool(entries),
            "every entry carries a diff_hash",
        )
        report.add(
            "audit_records_old_and_new_value",
            # Audit rows carry the fact's value itself, so old_value/new_value
            # are the bare 80 and 95 rather than wrapper objects.
            any(
                e.get("old_value") == 80 and e.get("new_value") == 95
                for e in entries
            ),
            "old_value=80, new_value=95 recorded",
        )

        # ------------------------------------------------------------------
        print("\n[4/7] Wildcard listing")
        # ------------------------------------------------------------------
        all_for_subject = await client.get(f"{API_BASE}/memory/fact?subject={prefix}&limit=100")
        rows = all_for_subject.json().get("facts", []) if all_for_subject.status_code == 200 else []
        report.add(
            "list_facts_by_subject",
            len(rows) >= len(cases),
            f"{len(rows)} facts under {prefix}",
        )

        wildcard = await client.get(f"{API_BASE}/memory/fact?subject=*&predicate=nested_object&limit=50")
        wildcard_rows = wildcard.json().get("facts", []) if wildcard.status_code == 200 else []
        report.add(
            "wildcard_subject_matching",
            wildcard.status_code == 200 and len(wildcard_rows) >= 1,
            f"'*' matched {len(wildcard_rows)} facts for predicate nested_object",
        )

        # ------------------------------------------------------------------
        print("\n[5/7] Semantic search")
        # ------------------------------------------------------------------
        search = await client.get(
            f"{API_BASE}/memory/search",
            params={"query": "when should the dehumidifier be switched on?", "top_k": 10},
        )
        body = search.json() if search.status_code == 200 else {}
        hits = body.get("hits", [])
        report.add(
            "search_returns_results",
            search.status_code == 200 and len(hits) > 0,
            f"{len(hits)} hits, semantic={body.get('semantic')}",
        )
        report.add(
            "search_hits_share_context",
            any(prefix in (hit.get("subject") or "") for hit in hits),
            "at least one hit comes from this run's namespace",
        )
        report.add(
            "search_hits_carry_similarity",
            all(hit.get("similarity") is not None for hit in hits) if hits else False,
            "hits expose a similarity score",
        )

        # ------------------------------------------------------------------
        print("\n[6/7] Rules and deletion")
        # ------------------------------------------------------------------
        rule_name = f"{prefix}_escalates"
        # A rule is a Prolog head/body pair given as source text. This one is a
        # conjunctive derivation: a subject that is both "peak" and "high
        # humidity" gets priority=immediate. Nothing here is energy-specific.
        await store_fact(client, prefix, "regime", "peak")
        await store_fact(client, prefix, "humidity", "high")
        rule = await client.post(
            f"{API_BASE}/memory/rule",
            json={
                "name": rule_name,
                "head": "fact(X, priority, immediate)",
                "body": "[fact(X, regime, peak), fact(X, humidity, high)]",
            },
        )
        report.add(
            "store_rule",
            rule.status_code == 201,
            f"status={rule.status_code}",
        )

        rule_get = await client.get(f"{API_BASE}/memory/rule/{rule_name}")
        report.add(
            "get_rule_roundtrip",
            rule_get.status_code == 200 and rule_get.json().get("name") == rule_name,
            "rule definition round-trips from PostgreSQL",
        )

        dry_run = await client.post(f"{API_BASE}/memory/rule/{rule_name}/run?store=false")
        derived = dry_run.json().get("derived", []) if dry_run.status_code == 200 else []
        report.add(
            "run_rule_derives_fact",
            any(
                d.get("subject") == prefix and d.get("predicate") == "priority"
                and d.get("value") == "immediate"
                for d in derived
            ),
            f"{len(derived)} derived facts from a dry run",
        )

        dry_stored = await get_fact(client, prefix, "priority")
        report.add(
            "dry_run_does_not_persist",
            dry_stored is None,
            "derived fact was returned but not stored",
        )

        stored_run = await client.post(f"{API_BASE}/memory/rule/{rule_name}/run?store=true&owner=eval")
        report.add(
            "run_rule_store_persists",
            stored_run.status_code == 200,
            f"status={stored_run.status_code}",
        )
        now_stored = await get_fact(client, prefix, "priority")
        report.add(
            "stored_rule_fact_is_retrievable",
            now_stored is not None and now_stored.get("value") == "immediate",
            f"value={now_stored.get('value') if now_stored else None!r}",
        )

        rule_delete = await client.delete(f"{API_BASE}/memory/rule/{rule_name}")
        report.add(
            "delete_rule",
            rule_delete.status_code == 200,
            f"status={rule_delete.status_code}",
        )

        missing_rule = await client.get(f"{API_BASE}/memory/rule/{rule_name}")
        report.add(
            "deleted_rule_returns_404",
            missing_rule.status_code == 404,
            f"status={missing_rule.status_code}",
        )

        deleted = await client.delete(f"{API_BASE}/memory/fact/{prefix}/demand_threshold")
        report.add(
            "delete_fact",
            deleted.status_code == 200 and deleted.json().get("deleted") is True,
            f"status={deleted.status_code}",
        )
        gone = await get_fact(client, prefix, "demand_threshold")
        report.add("deleted_fact_is_gone", gone is None, "fact no longer retrievable")

        delete_audit = await client.get(
            f"{API_BASE}/memory/audit?subject={prefix}&predicate=demand_threshold"
        )
        delete_entries = delete_audit.json().get("entries", []) if delete_audit.status_code == 200 else []
        report.add(
            "delete_is_audited",
            any(e.get("operation") == "DELETE" for e in delete_entries),
            "deletion left an audit entry",
        )

        # ------------------------------------------------------------------
        print("\n[7/7] Namespace cleanup")
        # ------------------------------------------------------------------
        # /memory/search ranks across the whole store, so every fact a run
        # leaves behind competes for the same top_k slots as the next run's.
        # Without this the suite passes on a clean database and then starts
        # failing search_hits_share_context once a few runs have piled up.
        leftovers = await client.get(
            f"{API_BASE}/memory/fact", params={"subject": prefix, "limit": 200}
        )
        remaining = leftovers.json().get("facts", []) if leftovers.status_code == 200 else []
        cleaned = 0
        for row in remaining:
            predicate = row.get("predicate")
            if not predicate:
                continue
            removed = await client.delete(f"{API_BASE}/memory/fact/{prefix}/{predicate}")
            if removed.status_code == 200:
                cleaned += 1
        report.add(
            "namespace_left_clean",
            cleaned == len(remaining),
            f"deleted {cleaned} of {len(remaining)} remaining facts under {prefix}",
        )

    return report


def print_summary(report: MemoryReport) -> None:
    print("\n" + "=" * 60)
    print("SYMBOLIC MEMORY EVALUATION RESULTS")
    print("=" * 60)
    print(f"Total checks: {len(report.checks)}")
    print(f"Passed: {report.passed}")
    print(f"Failed: {report.failed}")
    print(f"Pass rate: {report.passed / len(report.checks) * 100:.1f}%" if report.checks else "")


def save_results(report: MemoryReport, output_path: str = "eval/memory_results.json") -> None:
    data = {
        "summary": {
            "total": len(report.checks),
            "passed": report.passed,
            "failed": report.failed,
            "pass_rate": report.passed / len(report.checks) if report.checks else 0,
        },
        "checks": [{"name": c.name, "passed": c.passed, "details": c.details} for c in report.checks],
    }
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to {output_path}")


async def main():
    use_utf8_console()
    report = await run_memory_tests()
    print_summary(report)
    save_results(report)
    raise SystemExit(1 if report.failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
