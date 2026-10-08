#!/usr/bin/env python3
"""
Graceful degradation evaluation for the Regulated Decision-Tree Agent Hub.

The system has a hard dependency on symbolic memory (PostgreSQL + the embedded
Prolog KB) and a soft dependency on the LLM. This script proves that by stopping
the LLM container and checking that:

  1. /agent/ask still returns the retrieved facts, flagged used_fallback=true
  2. /decide is completely unaffected (it never touches the LLM)
  3. /memory/fact still accepts writes
  4. everything recovers once the LLM is back

Note that /decide is deliberately NOT the thing being measured for LLM
degradation: it is pure Prolog, so its behaviour cannot change with the LLM.
"""
import asyncio
import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import httpx

from _config import api_base, use_utf8_console, llm_base


API_BASE = api_base()
LLM_CONTAINER = "reg-decision-llm"
PROBE_SUBJECT = "eval_degradation_probe"

DECIDE_CASES = [
    (90, 35, 80),
    (50, 20, 50),
    (90, 25, 65),
]


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: str
    # A check that could not be measured in this environment (no model file on
    # disk, say). Counted separately from a failure: a skip is missing coverage,
    # not a broken invariant.
    skipped: bool = False


@dataclass
class DegradationReport:
    checks: List[CheckResult] = field(default_factory=list)
    baseline_answer: Optional[str] = None
    degraded_answer: Optional[str] = None
    recovered_answer: Optional[str] = None

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed and not c.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.checks if c.skipped)

    @property
    def measured(self) -> int:
        return sum(1 for c in self.checks if not c.skipped)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.measured * 100) if self.measured else 0.0


async def llm_model_loaded(client: httpx.AsyncClient) -> tuple[bool, str]:
    """Is the LLM actually able to answer, as opposed to merely running?

    The llm container is expected to start without a model and report
    `unhealthy` with a reason rather than crash-looping, so "the container is
    up" says nothing about whether a generated answer is possible.
    """
    try:
        response = await client.get(
            llm_base() + "/health", timeout=10.0
        )
    except Exception as error:
        return False, f"LLM service unreachable: {error}"
    if response.status_code != 200:
        return False, f"LLM health check returned HTTP {response.status_code}"
    body = response.json()
    if body.get("status") == "healthy":
        return True, "model loaded"
    return False, body.get("reason") or f"status={body.get('status')}"


async def ask(client: httpx.AsyncClient, prompt: str) -> Dict:
    """Call /agent/ask and normalise the outcome."""
    try:
        response = await client.post(
            f"{API_BASE}/agent/ask", json={"prompt": prompt, "top_k": 5}, timeout=60.0
        )
    except Exception as error:  # connection refused etc.
        return {"error": str(error)}
    if response.status_code != 200:
        return {"error": f"HTTP {response.status_code}: {response.text[:200]}"}
    return response.json()


async def decide(client: httpx.AsyncClient, demand: float, temp: float, humidity: float) -> str:
    try:
        response = await client.post(
            f"{API_BASE}/decide",
            json={"inputs": {"demand": demand, "temperature": temp, "humidity": humidity}},
            timeout=30.0,
        )
        if response.status_code != 200:
            return f"HTTP {response.status_code}"
        return response.json().get("leaf_node", "")
    except Exception as error:
        return f"EXCEPTION: {error}"


def stop_llm_container() -> bool:
    try:
        subprocess.run(["docker", "stop", LLM_CONTAINER], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        print(f"Failed to stop LLM container: {error}")
        return False


def start_llm_container() -> bool:
    try:
        subprocess.run(["docker", "start", LLM_CONTAINER], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        print(f"Failed to start LLM container: {error}")
        return False


async def wait_for_llm_healthy(timeout: int = 120) -> bool:
    start = time.time()
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.time() - start < timeout:
            try:
                response = await client.get(llm_base() + "/health")
                if response.status_code == 200 and response.json().get("status") == "healthy":
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
    return False


PROMPT = "What is the cooling policy when demand exceeds 80 and temperature is above 30?"


async def run_degradation_test() -> DegradationReport:
    report = DegradationReport()

    async with httpx.AsyncClient(timeout=30.0) as client:
        model_ready, model_note = await llm_model_loaded(client)
        print(f"[1/5] Baseline: LLM running (model loaded: {model_ready} — {model_note})")

        baseline = await ask(client, PROMPT)
        baseline_leaves = [await decide(client, *case) for case in DECIDE_CASES]
        report.baseline_answer = baseline.get("response")
        # Test the error's value, not its presence: /agent/ask always returns an
        # `error` key on a 200, null when there is nothing wrong.
        if model_ready and baseline.get("error"):
            report.checks.append(
                CheckResult("baseline_agent_ask", False, f"Baseline failed: {baseline['error']}")
            )
        elif model_ready:
            report.checks.append(
                CheckResult(
                    "baseline_agent_ask",
                    True,
                    f"retrieved {len(baseline.get('retrieved_facts', []))} facts, "
                    f"fallback={baseline.get('used_fallback')}",
                )
            )
        else:
            # Without a model there is no answer to baseline. The fallback
            # contract is still measurable, and is what step 3 checks properly.
            report.checks.append(
                CheckResult(
                    "baseline_agent_ask",
                    False,
                    f"cannot measure a generated answer: {model_note}",
                    skipped=True,
                )
            )

        print("[2/5] Stopping LLM container")
        if not stop_llm_container():
            report.checks.append(
                CheckResult("llm_stopped", False, "Could not stop the LLM container; aborting")
            )
            return report
        report.checks.append(CheckResult("llm_stopped", True, f"Stopped {LLM_CONTAINER}"))
        await asyncio.sleep(5)

        print("[3/5] Degraded: LLM down")
        degraded = await ask(client, PROMPT)
        report.degraded_answer = degraded.get("response")

        if degraded.get("error") and not degraded.get("used_fallback"):
            # used_fallback is what separates "degraded on purpose" from
            # "something actually broke". A response carrying both a fallback
            # flag and an error is the documented shape of the degraded path.
            report.checks.append(
                CheckResult(
                    "degraded_agent_ask_returns_facts",
                    False,
                    f"/agent/ask errored instead of degrading: {degraded['error']}",
                )
            )
        else:
            facts = degraded.get("retrieved_facts", [])
            report.checks.append(
                CheckResult(
                    "degraded_agent_ask_returns_facts",
                    bool(facts),
                    f"retrieved {len(facts)} facts while the LLM was down",
                )
            )
            report.checks.append(
                CheckResult(
                    "degraded_signals_fallback",
                    degraded.get("used_fallback") is True and degraded.get("response") is None,
                    f"used_fallback={degraded.get('used_fallback')}, "
                    f"response={degraded.get('response')!r}, error={degraded.get('error')!r}",
                )
            )

        degraded_leaves = [await decide(client, *case) for case in DECIDE_CASES]
        report.checks.append(
            CheckResult(
                "decide_unaffected_by_llm_loss",
                degraded_leaves == baseline_leaves,
                f"baseline={baseline_leaves} degraded={degraded_leaves}",
            )
        )

        write = await client.post(
            f"{API_BASE}/memory/fact",
            json={"subject": PROBE_SUBJECT, "predicate": "status", "value": "writable_without_llm"},
        )
        report.checks.append(
            CheckResult(
                "memory_write_survives_llm_loss",
                write.status_code == 201,
                f"POST /memory/fact -> {write.status_code} {write.text[:160]}",
            )
        )

        print("[4/5] Restarting LLM container")
        start_llm_container()
        healthy = await wait_for_llm_healthy(120)
        report.checks.append(
            CheckResult(
                "llm_restarted",
                healthy,
                "LLM reported healthy" if healthy else "LLM did not report healthy within 120s",
                skipped=not healthy and not model_ready,
            )
        )

        print("[5/5] Recovered: asking again")
        recovered = await ask(client, PROMPT)
        report.recovered_answer = recovered.get("response")
        report.checks.append(
            CheckResult(
                "recovers_after_llm_restart",
                # Same reasoning as the baseline check: `error` is always a key
                # in the 200 body, so only its value distinguishes a failure.
                not recovered.get("error") and recovered.get("used_fallback") is False,
                f"fallback={recovered.get('used_fallback')}, "
                f"answer={'present' if recovered.get('response') else 'missing'}",
                skipped=not model_ready,
            )
        )

    return report


def print_summary(report: DegradationReport) -> None:
    print("\n" + "=" * 60)
    print("GRACEFUL DEGRADATION EVALUATION RESULTS")
    print("=" * 60)
    print(f"Total checks: {len(report.checks)}")
    print(f"Passed: {report.passed}")
    print(f"Failed: {report.failed}")
    print(f"Skipped: {report.skipped}")
    print(f"Pass rate: {report.pass_rate:.1f}% (of non-skipped)")

    print("\nChecks:")
    for check in report.checks:
        status = "SKIP" if check.skipped else ("PASS" if check.passed else "FAIL")
        print(f"  [{status}] {check.name}: {check.details}")

    print(f"\nBaseline answer:  {report.baseline_answer!r}")
    print(f"Degraded answer:  {report.degraded_answer!r}")
    print(f"Recovered answer: {report.recovered_answer!r}")


def save_results(report: DegradationReport, output_path: str = "eval/degradation_results.json") -> None:
    data = {
        "summary": {
            "total": len(report.checks),
            "passed": report.passed,
            "failed": report.failed,
            "skipped": report.skipped,
            "pass_rate": report.pass_rate,
        },
        "answers": {
            "baseline": report.baseline_answer,
            "degraded": report.degraded_answer,
            "recovered": report.recovered_answer,
        },
        "checks": [
            {"name": c.name, "passed": c.passed, "skipped": c.skipped, "details": c.details}
            for c in report.checks
        ],
    }
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to {output_path}")


async def main():
    use_utf8_console()
    report = await run_degradation_test()
    print_summary(report)
    save_results(report)


if __name__ == "__main__":
    asyncio.run(main())