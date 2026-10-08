#!/usr/bin/env python3
"""
LLM + memory interaction evaluation.

The point of the hub is that an LLM answers *from* symbolic memory rather than
from its own weights. This script checks that the wiring holds:

  1. /llm/health reports the provider's real state
  2. /agent/ask retrieves relevant facts and returns them alongside the answer
  3. the answer is grounded - it reflects what memory says, not what the model
     happens to remember
  4. /llm/ask is honest when the model is unavailable: response=null plus an
     error, never an exception and never a hallucinated answer
  5. an unanswerable question still returns the retrieved context

A missing LLM is not a failure. The memory checks must pass either way; only the
checks that genuinely require a model are reported as skipped.

Run with the stack up:
    docker compose up -d --build
    python eval/llm_memory_test.py
"""
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import List, Optional
import httpx

from _config import api_base, use_utf8_console


API_BASE = api_base()

# Stored before asking, so retrieval has something domain-neutral to find.
# These are deliberately about a different subject than the decision tree, which
# demonstrates the same memory serving a different domain.
SEED_FACTS = {
    "heliostat": {"field": "solar_thermal", "operator": "acme_energy", "aperture_m2": 1200},
    "flux_cap": {"field": "material_science", "operator": "acme_energy", "critical_field_tesla": 22},
    "cryostat": {"field": "superconducting_magnets", "operator": "globex_labs", "operating_temp_k": 4},
}

# Question whose answer is only obtainable from the seeded facts.
GROUNDED_QUESTION = (
    "Which operator runs the cryostat, and what temperature does it operate at?"
)
GROUNDED_TERMS = ["globex_labs", "4"]


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: str
    skipped: bool = False


@dataclass
class LLMMemoryReport:
    checks: List[CheckResult] = field(default_factory=list)
    llm_available: bool = False
    answer: Optional[str] = None

    def add(self, name: str, passed: bool, details: str = "", skipped: bool = False) -> None:
        self.checks.append(CheckResult(name, passed, details, skipped))
        if skipped:
            status = "SKIP"
        else:
            status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}" + (f": {details}" if details else ""))

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed and not c.skipped)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.passed and not c.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for c in self.checks if c.skipped)


async def seed(client: httpx.AsyncClient) -> None:
    for subject, facts in SEED_FACTS.items():
        for predicate, value in facts.items():
            response = await client.post(
                f"{API_BASE}/memory/fact",
                json={"subject": subject, "predicate": predicate, "value": value},
            )
            if response.status_code != 201:
                print(f"    seeding {subject}/{predicate} failed: {response.status_code}")


async def run_tests() -> LLMMemoryReport:
    report = LLMMemoryReport()
    namespace = f"eval_llm_{uuid.uuid4().hex[:8]}"
    print(f"Running LLM + memory evaluation (namespace: {namespace})...")

    async with httpx.AsyncClient(timeout=120.0) as client:
        # ------------------------------------------------------------------
        print("\n[1/5] LLM health")
        # ------------------------------------------------------------------
        health = await client.get(f"{API_BASE}/llm/health")
        body = health.json() if health.status_code == 200 else {}
        report.llm_available = bool(body.get("reachable")) and body.get("status") == "healthy"
        report.add(
            "llm_health_responds",
            health.status_code == 200 and "reachable" in body,
            f"reachable={body.get('reachable')}, status={body.get('status')!r}, "
            f"reason={body.get('reason')!r}",
        )

        # ------------------------------------------------------------------
        print("\n[2/5] Seeding facts for retrieval")
        # ------------------------------------------------------------------
        await seed(client)
        probe = await client.get(f"{API_BASE}/memory/fact/cryostat/operating_temp_k")
        report.add(
            "seed_facts_retrievable",
            probe.status_code == 200,
            f"cryostat/operating_temp_k -> {probe.status_code}",
        )

        # ------------------------------------------------------------------
        print("\n[3/5] Agent retrieval")
        # ------------------------------------------------------------------
        ask = await client.post(
            f"{API_BASE}/agent/ask",
            json={"prompt": GROUNDED_QUESTION, "top_k": 5},
        )
        ask_body = ask.json() if ask.status_code == 200 else {}
        facts = ask_body.get("retrieved_facts", [])
        report.add(
            "agent_ask_returns_200",
            ask.status_code == 200,
            f"status={ask.status_code}",
        )
        report.add(
            "agent_ask_retrieves_facts",
            len(facts) > 0,
            f"{len(facts)} facts retrieved for a {len(GROUNDED_QUESTION)}-char prompt",
        )
        report.add(
            "retrieval_is_relevant",
            any(hit.get("subject") == "cryostat" for hit in facts),
            f"subjects={[hit.get('subject') for hit in facts]}",
        )
        report.add(
            "retrieved_value_matches_stored_fact",
            any(
                hit.get("subject") == "cryostat"
                and hit.get("predicate") == "operator"
                and hit.get("value") == "globex_labs"
                for hit in facts
            ),
            "retrieved value is the one memory holds, not a paraphrase",
        )

        # ------------------------------------------------------------------
        print("\n[4/5] Grounding the answer")
        # ------------------------------------------------------------------
        report.answer = ask_body.get("response")
        if not report.llm_available:
            report.add(
                "answer_grounded_in_memory",
                True,
                f"no LLM available ({body.get('reason')!r}); checked the degraded path instead",
                skipped=True,
            )
        elif ask_body.get("response"):
            answer = ask_body["response"].lower()
            missing = [term for term in GROUNDED_TERMS if term.lower() not in answer]
            if missing:
                # Quote the answer: a model that paraphrases or drops a character
                # and a model that invents a unit look identical from the summary,
                # and they call for different fixes.
                excerpt = " ".join(ask_body["response"].split())[:300]
                stored = [
                    fact.get("value")
                    for fact in facts
                    if fact.get("subject") == "cryostat"
                ]
                report.add(
                    "answer_grounded_in_memory",
                    False,
                    f"answer omitted {missing}; memory holds {stored}; "
                    f"answer was: {excerpt!r}",
                )
            else:
                report.add(
                    "answer_grounded_in_memory",
                    True,
                    "answer mentions the operator and temperature from memory",
                )
        else:
            report.add(
                "answer_grounded_in_memory",
                False,
                f"LLM reported healthy but no answer was returned: {ask_body.get('error')!r}",
            )

        # ------------------------------------------------------------------
        print("\n[5/5] Honest failure and unanswerable questions")
        # ------------------------------------------------------------------
        if report.llm_available:
            report.add(
                "llm_answer_not_fallback",
                ask_body.get("used_fallback") is False,
                f"used_fallback={ask_body.get('used_fallback')}",
            )
        else:
            report.add(
                "degraded_response_is_honest",
                ask_body.get("used_fallback") is True
                and ask_body.get("response") is None
                and bool(ask_body.get("error")),
                f"response={ask_body.get('response')!r}, error present={bool(ask_body.get('error'))}",
            )

        unanswerable = await client.post(
            f"{API_BASE}/agent/ask",
            json={
                "prompt": "What is the approved procedure for decommissioning a nuclear reactor?",
                "top_k": 3,
            },
        )
        unanswerable_body = unanswerable.json() if unanswerable.status_code == 200 else {}
        report.add(
            "unanswerable_question_still_returns_context",
            unanswerable.status_code == 200,
            f"status={unanswerable.status_code}, "
            f"{len(unanswerable_body.get('retrieved_facts', []))} facts returned",
        )

        # /llm/ask is the raw bridge with no retrieval and no fallback: when the
        # model is down it must refuse loudly (503) rather than invent an answer.
        direct = await client.post(
            f"{API_BASE}/llm/ask",
            json={"prompt": "Summarise the cryostat operating policy."},
        )
        # Parsed on every status: the refusal check below reads the 503 body,
        # and discarding it unless the call succeeded made that check impossible
        # to pass.
        try:
            direct_body = direct.json()
        except ValueError:
            direct_body = {}
        if not isinstance(direct_body, dict):
            direct_body = {}
        if report.llm_available:
            report.add(
                "direct_llm_ask_answers",
                direct.status_code == 200 and bool(direct_body.get("response")),
                f"status={direct.status_code}, response present={bool(direct_body.get('response'))}",
            )
        else:
            detail = direct_body.get("detail")
            report.add(
                "direct_llm_ask_refuses_cleanly",
                direct.status_code == 503 and bool(detail),
                f"status={direct.status_code}, detail={detail!r}",
            )

    return report


def print_summary(report: LLMMemoryReport) -> None:
    print("\n" + "=" * 60)
    print("LLM + MEMORY EVALUATION RESULTS")
    print("=" * 60)
    print(f"LLM available: {report.llm_available}")
    print(f"Total checks: {len(report.checks)}")
    print(f"Passed: {report.passed}")
    print(f"Failed: {report.failed}")
    print(f"Skipped: {report.skipped}")
    # Denominator has to exclude the skips, or the label below contradicts the
    # pass_rate that save_results writes.
    graded = len(report.checks) - report.skipped
    if graded:
        print(f"Pass rate: {report.passed / graded * 100:.1f}% (of non-skipped)")
    print(f"\nAnswer: {report.answer!r}")


def save_results(
    report: LLMMemoryReport, output_path: str = "eval/llm_memory_results.json"
) -> None:
    graded = len(report.checks) - report.skipped
    data = {
        "summary": {
            "total": len(report.checks),
            "passed": report.passed,
            "failed": report.failed,
            "skipped": report.skipped,
            "llm_available": report.llm_available,
            "pass_rate": report.passed / graded if graded else 0,
        },
        "answer": report.answer,
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
    report = await run_tests()
    print_summary(report)
    save_results(report)
    raise SystemExit(1 if report.failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
