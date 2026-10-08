#!/usr/bin/env python3
"""
Correctness evaluation script for the Regulated Decision-Tree Agent Hub.
Compares actual leaf nodes against expected ground truth from fixtures.
"""
import asyncio
import csv
import json
from dataclasses import dataclass
from typing import List, Dict
import httpx

from _config import api_base, use_utf8_console


API_BASE = api_base()


@dataclass
class CorrectnessResult:
    demand: float
    temperature: float
    humidity: float
    expected_leaf: str
    expected_action: str
    expected_owner: str
    actual_leaf: str
    actual_action: str
    actual_owner: str
    correct: bool


async def run_correctness_test(fixtures_path: str = "eval/fixtures.csv") -> List[CorrectnessResult]:
    """Run correctness test using fixtures with ground truth."""
    fixtures = []
    with open(fixtures_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            fixtures.append(row)
    
    print(f"Running correctness test on {len(fixtures)} cases...")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        results = []
        for i, fixture in enumerate(fixtures):
            demand = float(fixture['demand'])
            temperature = float(fixture['temperature'])
            humidity = float(fixture['humidity'])
            payload = {
                "inputs": {
                    "demand": demand,
                    "temperature": temperature,
                    "humidity": humidity,
                }
            }

            try:
                response = await client.post(f"{API_BASE}/decide", json=payload)
                if response.status_code == 200:
                    data = response.json()
                    actual_leaf = data.get("leaf_node", "")
                    actual_action = data.get("action", "")
                    actual_owner = data.get("owner", "")

                    correct = (
                        actual_leaf == fixture['expected_leaf'] and
                        actual_action == fixture['expected_action'] and
                        actual_owner == fixture['expected_owner']
                    )

                    result = CorrectnessResult(
                        demand=demand,
                        temperature=temperature,
                        humidity=humidity,
                        expected_leaf=fixture['expected_leaf'],
                        expected_action=fixture['expected_action'],
                        expected_owner=fixture['expected_owner'],
                        actual_leaf=actual_leaf,
                        actual_action=actual_action,
                        actual_owner=actual_owner,
                        correct=correct
                    )
                else:
                    result = CorrectnessResult(
                        demand=demand,
                        temperature=temperature,
                        humidity=humidity,
                        expected_leaf=fixture['expected_leaf'],
                        expected_action=fixture['expected_action'],
                        expected_owner=fixture['expected_owner'],
                        actual_leaf="ERROR",
                        actual_action="ERROR",
                        actual_owner="ERROR",
                        correct=False
                    )
            except Exception as e:
                result = CorrectnessResult(
                    demand=demand,
                    temperature=temperature,
                    humidity=humidity,
                    expected_leaf=fixture['expected_leaf'],
                    expected_action=fixture['expected_action'],
                    expected_owner=fixture['expected_owner'],
                    actual_leaf=f"EXCEPTION: {e}",
                    actual_action="ERROR",
                    actual_owner="ERROR",
                    correct=False
                )
            
            results.append(result)
            
            status = "✓" if result.correct else "✗"
            print(f"  {status} Case {i+1}: demand={result.demand}, temp={result.temperature}, hum={result.humidity} -> {result.actual_leaf} ({result.actual_action})")
    
    return results


def print_statistics(results: List[CorrectnessResult]) -> None:
    """Print correctness statistics."""
    total = len(results)
    correct = sum(1 for r in results if r.correct)
    incorrect = total - correct
    
    # Group by leaf
    by_leaf: Dict[str, Dict] = {}
    for r in results:
        if r.expected_leaf not in by_leaf:
            by_leaf[r.expected_leaf] = {"total": 0, "correct": 0}
        by_leaf[r.expected_leaf]["total"] += 1
        if r.correct:
            by_leaf[r.expected_leaf]["correct"] += 1
    
    print("\n" + "=" * 60)
    print("CORRECTNESS EVALUATION RESULTS")
    print("=" * 60)
    print(f"Total cases: {total}")
    print(f"Correct: {correct}")
    print(f"Incorrect: {incorrect}")
    print(f"Accuracy: {correct/total*100:.1f}%")
    
    print("\nPer-leaf breakdown:")
    for leaf, stats in sorted(by_leaf.items()):
        acc = stats["correct"] / stats["total"] * 100
        print(f"  {leaf}: {stats['correct']}/{stats['total']} ({acc:.1f}%)")
    
    # Show mismatches
    mismatches = [r for r in results if not r.correct]
    if mismatches:
        print("\nMismatches:")
        for r in mismatches:
            print(f"  demand={r.demand}, temp={r.temperature}, hum={r.humidity}")
            print(f"    Expected: {r.expected_leaf} / {r.expected_action} / {r.expected_owner}")
            print(f"    Actual:   {r.actual_leaf} / {r.actual_action} / {r.actual_owner}")


def save_results(results: List[CorrectnessResult], output_path: str = "eval/correctness_results.json") -> None:
    """Save results to JSON file."""
    total = len(results)
    correct = sum(1 for r in results if r.correct)
    
    data = {
        "summary": {
            "total": total,
            "correct": correct,
            "incorrect": total - correct,
            "accuracy": correct / total if total > 0 else 0
        },
        "results": [
            {
                "demand": r.demand,
                "temperature": r.temperature,
                "humidity": r.humidity,
                "expected_leaf": r.expected_leaf,
                "expected_action": r.expected_action,
                "expected_owner": r.expected_owner,
                "actual_leaf": r.actual_leaf,
                "actual_action": r.actual_action,
                "actual_owner": r.actual_owner,
                "correct": r.correct
            }
            for r in results
        ]
    }
    
    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to {output_path}")


async def main():
    use_utf8_console()
    import sys
    fixtures_path = sys.argv[1] if len(sys.argv) > 1 else "eval/fixtures.csv"
    
    results = await run_correctness_test(fixtures_path)
    print_statistics(results)
    save_results(results)


if __name__ == "__main__":
    asyncio.run(main())