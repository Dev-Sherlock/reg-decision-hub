#!/usr/bin/env python3
"""
Latency evaluation script for the Regulated Decision-Tree Agent Hub.
Measures /decide endpoint latency over 100 requests.
"""
import asyncio
import csv
import json
import statistics
import time
from dataclasses import dataclass
from typing import List
import httpx

from _config import api_base, use_utf8_console


API_BASE = api_base()


@dataclass
class LatencyResult:
    demand: float
    temperature: float
    humidity: float
    latency_ms: float
    success: bool
    leaf_node: str = ""
    fallback_used: bool = False


async def measure_latency(client: httpx.AsyncClient, demand: float, temp: float, humidity: float) -> LatencyResult:
    """Measure latency for a single request."""
    payload = {
        "inputs": {"demand": demand, "temperature": temp, "humidity": humidity}
    }
    
    start = time.perf_counter()
    try:
        response = await client.post(f"{API_BASE}/decide", json=payload, timeout=30.0)
        latency_ms = (time.perf_counter() - start) * 1000
        
        if response.status_code == 200:
            data = response.json()
            return LatencyResult(
                demand=demand,
                temperature=temp,
                humidity=humidity,
                latency_ms=latency_ms,
                success=True,
                leaf_node=data.get("leaf_node", ""),
                fallback_used=data.get("fallback_used", False)
            )
        else:
            return LatencyResult(
                demand=demand,
                temperature=temp,
                humidity=humidity,
                latency_ms=latency_ms,
                success=False
            )
    except Exception:
        latency_ms = (time.perf_counter() - start) * 1000
        return LatencyResult(
            demand=demand,
            temperature=temp,
            humidity=humidity,
            latency_ms=latency_ms,
            success=False
        )


async def run_latency_test(fixtures_path: str = "eval/fixtures.csv", num_runs: int = 100) -> List[LatencyResult]:
    """Run latency test using fixtures."""
    # Load fixtures
    fixtures = []
    with open(fixtures_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            fixtures.append({
                'demand': float(row['demand']),
                'temperature': float(row['temperature']),
                'humidity': float(row['humidity']),
            })
    
    # If we need more runs than fixtures, cycle through them
    test_cases = []
    for i in range(num_runs):
        test_cases.append(fixtures[i % len(fixtures)])
    
    print(f"Running {num_runs} latency tests...")
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Warmup
        for _ in range(5):
            await measure_latency(client, 50, 25, 50)
        
        # Actual tests
        results = []
        for i, case in enumerate(test_cases):
            result = await measure_latency(client, case['demand'], case['temperature'], case['humidity'])
            results.append(result)
            if (i + 1) % 20 == 0:
                print(f"  Completed {i + 1}/{num_runs}")
    
    return results


def print_statistics(results: List[LatencyResult]) -> None:
    """Print latency statistics."""
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    
    latencies = [r.latency_ms for r in successful]
    
    print("\n" + "=" * 60)
    print("LATENCY EVALUATION RESULTS")
    print("=" * 60)
    print(f"Total requests: {len(results)}")
    print(f"Successful: {len(successful)}")
    print(f"Failed: {len(failed)}")
    print(f"Success rate: {len(successful)/len(results)*100:.1f}%")
    
    if latencies:
        print("\nLatency (ms):")
        print(f"  Mean: {statistics.mean(latencies):.2f}")
        print(f"  Median: {statistics.median(latencies):.2f}")
        print(f"  Stdev: {statistics.stdev(latencies):.2f}" if len(latencies) > 1 else "  Stdev: N/A")
        print(f"  Min: {min(latencies):.2f}")
        print(f"  Max: {max(latencies):.2f}")
        print(f"  P50: {statistics.median(latencies):.2f}")
        print(f"  P95: {sorted(latencies)[int(len(latencies)*0.95)]:.2f}")
        print(f"  P99: {sorted(latencies)[int(len(latencies)*0.99)]:.2f}")
        
        # Target check
        target = 800
        under_target = sum(1 for l in latencies if l <= target)
        print(f"\nTarget (<= {target}ms): {under_target}/{len(latencies)} ({under_target/len(latencies)*100:.1f}%)")
    
    fallback_count = sum(1 for r in successful if r.fallback_used)
    print(f"\nFallback used: {fallback_count}/{len(successful)} ({fallback_count/len(successful)*100:.1f}%)" if successful else "")


def save_results(results: List[LatencyResult], output_path: str = "eval/latency_results.json") -> None:
    """Save results to JSON file."""
    data = {
        "summary": {
            "total": len(results),
            "successful": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
            "mean_ms": statistics.mean([r.latency_ms for r in results if r.success]) if any(r.success for r in results) else 0,
            "median_ms": statistics.median([r.latency_ms for r in results if r.success]) if any(r.success for r in results) else 0,
        },
        "results": [
            {
                "demand": r.demand,
                "temperature": r.temperature,
                "humidity": r.humidity,
                "latency_ms": r.latency_ms,
                "success": r.success,
                "leaf_node": r.leaf_node,
                "fallback_used": r.fallback_used
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
    num_runs = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    
    results = await run_latency_test(fixtures_path, num_runs)
    print_statistics(results)
    save_results(results)


if __name__ == "__main__":
    asyncio.run(main())