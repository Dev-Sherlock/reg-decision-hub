#!/usr/bin/env python3
"""
Domain-routing evaluation.

eval/correctness.py asks one question about one tree: does this input reach the
leaf the fixture expects? It sends demand/temperature/humidity and nothing else,
so it would pass unchanged on a system that routes by a hard-coded input set —
which is precisely the thing this phase exists to check.

The question here is the one /decide now has to answer before it can look at a
tree at all: *which* tree is this input for, and what happens when the answer is
none or more than one. Cases live in eval/fixtures/*.csv:

* air_traffic.csv, manufacturing.csv, cyber.csv — per-domain leaves, each one
  routed by its keys with no domain named.
* routing.csv — the routing rules themselves, including the two rejections, and
  cases where the answer is energy so a regression in routing cannot hide behind
  an unchanged energy tree.

Every row is expected_status 200 with a leaf/action/owner/domain, or 400 with an
expected_error naming the refusal. Nothing here overrides a node, so this phase
cannot leak an action into correctness.py the way governance.py has to undo.
"""
import asyncio
import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import httpx

from _config import api_base, use_utf8_console

API_BASE = api_base()
FIXTURE_DIR = Path(__file__).parent / "fixtures"

# Read in a fixed order so the report reads top to bottom in the order the
# concerns matter: routing first, because a routing bug invalidates every
# per-domain row underneath it.
FIXTURE_FILES = ["routing.csv", "air_traffic.csv", "manufacturing.csv", "cyber.csv"]


@dataclass
class DomainCheck:
    """One fixture row's outcome."""

    name: str
    passed: bool
    details: str = ""
    expected_leaf: str = ""
    actual_leaf: str = ""
    expected_action: str = ""
    actual_action: str = ""
    expected_owner: str = ""
    actual_owner: str = ""
    expected_domain: str = ""
    actual_domain: str = ""
    expected_status: int = 200
    actual_status: int = 0
    inputs: Dict = field(default_factory=dict)


def load_fixtures(path: Path) -> List[Dict[str, str]]:
    """Read one fixture CSV. `inputs` stays text here so a bad cell is reported
    against the row that carries it rather than aborting the file."""
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


async def check_row(client: httpx.AsyncClient, row: Dict[str, str]) -> DomainCheck:
    """Run one row against /decide and compare every field the row names."""
    name = row["name"]
    expected_status = int(row.get("expected_status") or 200)

    try:
        inputs = json.loads(row["inputs"])
    except json.JSONDecodeError as error:
        return DomainCheck(name, False, f"fixture inputs are not valid JSON: {error}")

    # query_domain is how the "resolve the tie explicitly" case names a domain; it
    # is sent as ?domain= because that is the only way a caller can say which tree
    # they mean.
    params = {}
    if row.get("query_domain"):
        params["domain"] = row["query_domain"]

    check = DomainCheck(
        name=name,
        passed=False,
        expected_leaf=row.get("expected_leaf", ""),
        expected_action=row.get("expected_action", ""),
        expected_owner=row.get("expected_owner", ""),
        expected_domain=row.get("expected_domain", ""),
        expected_status=expected_status,
        inputs=inputs,
    )

    try:
        response = await client.post(
            f"{API_BASE}/decide", params=params, json={"inputs": inputs}
        )
    except Exception as error:  # noqa: BLE001 - reported, not raised
        check.details = f"request failed: {error}"
        return check

    check.actual_status = response.status_code
    if response.status_code != expected_status:
        check.details = (
            f"expected HTTP {expected_status}, got {response.status_code}: "
            f"{response.text[:200]}"
        )
        return check

    if expected_status != 200:
        # A refusal is only correct if it refuses for the stated reason. A 400
        # from an unrelated failure would otherwise pass every rejection row.
        detail = response.json().get("detail", {})
        actual_error = detail.get("error", "") if isinstance(detail, dict) else ""
        expected_error = row.get("expected_error", "")
        if actual_error != expected_error:
            check.details = (
                f"expected error '{expected_error}', got '{actual_error}' "
                f"({json.dumps(detail)[:200]})"
            )
            return check
        check.passed = True
        return check

    body = response.json()
    check.actual_leaf = body.get("leaf_node", "")
    check.actual_action = body.get("action", "")
    check.actual_owner = body.get("owner", "")
    check.actual_domain = body.get("domain", "")

    mismatches = [
        f"{label}: expected {expected!r}, got {actual!r}"
        for label, expected, actual in (
            ("leaf", check.expected_leaf, check.actual_leaf),
            ("action", check.expected_action, check.actual_action),
            ("owner", check.expected_owner, check.actual_owner),
            ("domain", check.expected_domain, check.actual_domain),
        )
        if expected and expected != actual
    ]
    if mismatches:
        check.details = "; ".join(mismatches)
        return check

    check.passed = True
    return check


async def run_domain_tests() -> List[DomainCheck]:
    """Every fixture row, across every domain fixture file."""
    rows: List[Dict[str, str]] = []
    for name in FIXTURE_FILES:
        path = FIXTURE_DIR / name
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing. The fixtures are the ground truth for this "
                "phase; without them there is nothing to assert against."
            )
        for row in load_fixtures(path):
            row["_file"] = name
            rows.append(row)

    print(f"Running domain-routing evaluation on {len(rows)} cases...")

    results: List[DomainCheck] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for index, row in enumerate(rows, start=1):
            check = await check_row(client, row)
            # A row whose inputs fail to parse is a broken fixture, not a system
            # failure, so it is kept in the count but never read as a pass.
            results.append(check)
            status = "✓" if check.passed else "✗"
            outcome = check.actual_leaf or f"HTTP {check.actual_status}"
            print(f"  {status} Case {index}: {row['_file']}/{check.name} -> {outcome}")
            if not check.passed:
                print(f"      {check.details}")

    return results


def print_summary(results: List[DomainCheck]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.passed)

    print("\n" + "=" * 60)
    print("DOMAIN ROUTING EVALUATION RESULTS")
    print("=" * 60)
    print(f"Total cases: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")
    print(f"Pass rate: {(passed / total * 100) if total else 0:.1f}%")

    failures = [r for r in results if not r.passed]
    if failures:
        print("\nFailures:")
        for result in failures:
            print(f"  {result.name}: {result.details}")
            print(f"    inputs: {json.dumps(result.inputs)}")


def save_results(
    results: List[DomainCheck], output_path: str = "eval/domains_results.json"
) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    data = {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": passed / total if total > 0 else 0,
        },
        # Named checks rather than a bare count, so run_all.py can print which one
        # broke instead of only how many did.
        "checks": [
            {
                "name": r.name,
                "passed": r.passed,
                "skipped": False,
                "details": r.details,
                "inputs": r.inputs,
                "expected_status": r.expected_status,
                "actual_status": r.actual_status,
                "expected_leaf": r.expected_leaf,
                "actual_leaf": r.actual_leaf,
                "expected_action": r.expected_action,
                "actual_action": r.actual_action,
                "expected_owner": r.expected_owner,
                "actual_owner": r.actual_owner,
                "expected_domain": r.expected_domain,
                "actual_domain": r.actual_domain,
            }
            for r in results
        ],
    }

    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {output_path}")


async def main() -> None:
    use_utf8_console()
    results = await run_domain_tests()
    print_summary(results)
    save_results(results)
    if any(not r.passed for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
