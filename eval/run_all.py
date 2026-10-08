#!/usr/bin/env python3
"""
Master evaluation script - runs all evaluation tests and generates a report.

Usage:
    python eval/run_all.py
    python eval/run_all.py --skip-degradation

degradation.py stops and starts the LLM container, so it is slower than the
others and mutates Docker state; --skip-degradation leaves it out.
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from _config import use_utf8_console

EVAL_DIR = Path(__file__).parent

# What each phase is supposed to leave behind. A phase that crashed leaves the
# previous run's file in place, and the report would then present stale numbers
# as current results — so the file is cleared first and its absence afterwards is
# what marks the phase as failed.
PHASE_RESULTS: Dict[str, str] = {
    "latency.py": "latency_results.json",
    "correctness.py": "correctness_results.json",
    "domains.py": "domains_results.json",
    "governance.py": "governance_results.json",
    "memory_test.py": "memory_results.json",
    "llm_memory_test.py": "llm_memory_results.json",
    "degradation.py": "degradation_results.json",
}


async def run_script(script_name: str, args: list = None) -> bool:
    """Run an evaluation script and return success."""
    cmd = [sys.executable, str(EVAL_DIR / script_name)]
    if args:
        cmd.extend(args)

    print(f"\n{'='*60}")
    print(f"Running {script_name}...")
    print(f"{'='*60}")

    result = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await result.communicate()

    # The children reconfigure their own streams, but a phase that died before
    # it got that far can still leave a byte the UTF-8 decoder rejects. Echoing
    # its output must never abort the suite that is reporting on it.
    if stdout:
        print(stdout.decode("utf-8", errors="replace"))
    if stderr:
        print(stderr.decode("utf-8", errors="replace"), file=sys.stderr)

    return result.returncode == 0


def failing_check_lines(result: dict) -> List[str]:
    """Name the checks a phase failed, with the reason.

    Counts alone leave the reader unable to act: '1 of 10 failed' does not say
    whether retrieval, permissions or generation broke.
    """
    lines = []
    for check in result.get("checks", []):
        if check.get("skipped") or check.get("passed"):
            continue
        detail = " ".join(str(check.get("details", "")).split())
        lines.append(f"  - ✗ {check.get('name')}" + (f" — {detail}" if detail else ""))
    return lines + [""] if lines else lines


def generate_report(
    missing_phases: List[str] | None = None, failed_checks: List[str] | None = None
):
    """Generate Markdown report from all results.

    `missing_phases` are the phases that produced no results at all; the report
    must not present the other phases' numbers as if the run were complete.
    `failed_checks` are phases that ran and reported failing checks.
    """
    report_path = EVAL_DIR / "report.md"
    missing_phases = missing_phases or []
    failed_checks = failed_checks or []
    results = {}

    # Load all result files
    for name in ["latency_results.json", "correctness_results.json",
                 "domains_results.json", "degradation_results.json",
                 "governance_results.json", "memory_results.json",
                 "llm_memory_results.json"]:
        path = EVAL_DIR / name
        if path.exists():
            with open(path, encoding="utf-8") as f:
                results[name.replace("_results.json", "")] = json.load(f)

    # Generate markdown
    md = ["# Regulated Decision-Tree Agent Hub - Evaluation Report\n"]
    md.append(f"*Generated: {__import__('datetime').datetime.now().isoformat()}*\n")

    # Latency
    if "latency" in results:
        s = results["latency"]["summary"]
        md.append("## Latency Evaluation\n")
        md.append(f"- **Total requests:** {s['total']}")
        md.append(f"- **Successful:** {s['successful']}")
        md.append(f"- **Mean latency:** {s['mean_ms']:.2f} ms")
        md.append(f"- **Median latency:** {s['median_ms']:.2f} ms")
        md.append(f"- **Target (≤800ms):** {'✓ PASS' if s['mean_ms'] <= 800 else '✗ FAIL'}\n")

    # Correctness
    if "correctness" in results:
        s = results["correctness"]["summary"]
        md.append("## Correctness Evaluation\n")
        md.append(f"- **Total cases:** {s['total']}")
        md.append(f"- **Correct:** {s['correct']}")
        md.append(f"- **Accuracy:** {s['accuracy']*100:.1f}%\n")

    # Domain routing
    if "domains" in results:
        s = results["domains"]["summary"]
        md.append("## Domain Routing Evaluation\n")
        md.append(
            "Which decision tree answers, and what happens when the inputs name "
            "none or more than one.\n"
        )
        md.append(f"- **Total checks:** {s['total']}")
        md.append(f"- **Passed:** {s['passed']}")
        md.append(f"- **Failed:** {s['failed']}")
        md.append(f"- **Pass rate:** {s['pass_rate']*100:.1f}%\n")
        md.extend(failing_check_lines(results["domains"]))

    # Degradation
    if "degradation" in results:
        s = results["degradation"]["summary"]
        md.append("## Graceful Degradation Evaluation\n")
        md.append("LLM stopped: the system must keep serving decisions and memory.\n")
        md.append(f"- **Total checks:** {s['total']}")
        md.append(f"- **Passed:** {s['passed']}")
        md.append(f"- **Failed:** {s['failed']}")
        # pass_rate is a percentage here, not the 0-1 fraction the other
        # sections carry.
        md.append(f"- **Skipped:** {s.get('skipped', 0)}")
        md.append(f"- **Pass rate:** {s['pass_rate']:.1f}% (of non-skipped)\n")
        for check in results["degradation"].get("checks", []):
            # A skipped check carries passed=false, so printing it as a failure
            # would overstate what actually failed.
            if check.get("skipped"):
                status = "–"
            else:
                status = "✓" if check["passed"] else "✗"
            md.append(f"  - {status} {check['name']}")
        md.append("")

    # Governance
    if "governance" in results:
        s = results["governance"]["summary"]
        md.append("## Governance Enforcement Evaluation\n")
        md.append(f"- **Total tests:** {s['total']}")
        md.append(f"- **Passed:** {s['passed']}")
        md.append(f"- **Pass rate:** {s['pass_rate']*100:.1f}%\n")

    # Memory
    for key, title in (("memory", "Symbolic Memory Evaluation"),
                       ("llm_memory", "LLM + Memory Grounding Evaluation")):
        if key in results:
            s = results[key]["summary"]
            md.append(f"## {title}\n")
            md.append(f"- **Total checks:** {s['total']}")
            md.append(f"- **Passed:** {s['passed']}")
            md.append(f"- **Failed:** {s['failed']}")
            # pass_rate is a fraction of the non-skipped checks here, so the
            # skipped count has to be on the page or 9/9 reads as a 10th pass.
            skipped = s.get("skipped", 0)
            if skipped:
                md.append(f"- **Skipped:** {skipped}")
                md.append(f"- **Pass rate:** {s['pass_rate']*100:.1f}% (of non-skipped)\n")
            else:
                md.append(f"- **Pass rate:** {s['pass_rate']*100:.1f}%\n")
            md.extend(failing_check_lines(results[key]))

    # Overall summary
    md.append("## Overall Assessment\n")
    all_pass = not missing_phases and not failed_checks
    if missing_phases:
        md.append(
            "**These phases did not produce results and cannot be called passing: "
            + ", ".join(missing_phases)
            + "**\n"
        )
    if failed_checks:
        md.append(
            "**These phases ran and reported failing checks: "
            + ", ".join(failed_checks)
            + ". The per-check detail above says which.**\n"
        )
    if "latency" in results:
        all_pass &= results["latency"]["summary"]["mean_ms"] <= 800
    if "correctness" in results:
        all_pass &= results["correctness"]["summary"]["accuracy"] >= 0.95
    if "domains" in results:
        all_pass &= results["domains"]["summary"]["failed"] == 0
    if "degradation" in results:
        all_pass &= results["degradation"]["summary"]["failed"] == 0
    if "governance" in results:
        all_pass &= results["governance"]["summary"]["pass_rate"] == 1.0
    for key in ("memory", "llm_memory"):
        if key in results:
            all_pass &= results[key]["summary"]["failed"] == 0

    md.append(f"**Overall: {'✓ ALL TESTS PASS' if all_pass else '✗ SOME TESTS FAIL'}**\n")

    # Architecture note
    md.append("## Architecture\n")
    md.append("Four Docker services:\n")
    md.append("- **api** — FastAPI; embeds SWI-Prolog via `pyswip` in-process, owns the PostgreSQL fact/rule/audit store, and drives the LLM over HTTP. There is no Prolog container.")
    md.append("- **llm** — `llama-cpp-python` serving a local GGUF model (SmolLM2-135M-Instruct by default). Optional: the API degrades gracefully when it is absent.")
    md.append("- **postgres** — PostgreSQL 16 + pgvector: facts, rules, embeddings, and the audit trail")
    md.append("- **ui** — React/Vite frontend\n")
    md.append("Decisions are produced by Prolog alone, so `/decide` behaviour is independent of the LLM. The LLM only ever produces natural-language answers grounded in retrieved facts.\n")

    # The report carries ≤, ✓, ✗ and em dashes, so it has to be written as
    # UTF-8 explicitly: the platform default (cp1252 on Windows) raises
    # UnicodeEncodeError on them and takes the whole suite's report with it.
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"\nReport generated: {report_path}")

    # Try to convert to PDF
    try:
        subprocess.run(["pandoc", report_path, "-o", str(EVAL_DIR / "report.pdf")], check=True)
        print(f"PDF generated: {EVAL_DIR / 'report.pdf'}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Note: pandoc not available, PDF not generated. Install pandoc for PDF output.")


async def main():
    """Run all evaluations."""
    use_utf8_console()
    skip_degradation = "--skip-degradation" in sys.argv
    print("Starting full evaluation suite...")

    # Run all tests
    tests = [
        ("latency.py", ["eval/fixtures.csv", "100"]),
        ("correctness.py", ["eval/fixtures.csv"]),
        ("domains.py", []),
        ("governance.py", []),
        ("memory_test.py", []),
        ("llm_memory_test.py", []),
    ]
    if not skip_degradation:
        tests.append(("degradation.py", []))

    # --skip-degradation leaves the previous run's results file behind, and the report
    # renders any file that exists — so without this it would present an old run's
    # recovery numbers as this run's. Deleting it makes the report omit the section,
    # which is the honest outcome for a phase that did not run. Deliberately skipped
    # is not a failure, so nothing is added to failed_checks.
    skipped_results = EVAL_DIR / PHASE_RESULTS["degradation.py"]
    if skip_degradation and skipped_results.exists():
        skipped_results.unlink()
        print(f"\nSkipped degradation: removing stale {skipped_results.name}")

    # A phase can fail two different ways, and they need different words: one
    # produced numbers that say something failed, the other produced nothing.
    missing_phases: List[str] = []
    failed_checks: List[str] = []
    for script, args in tests:
        # Clear the previous results first: a phase that crashes must not be
        # reported using the numbers it wrote last time.
        results_file = EVAL_DIR / PHASE_RESULTS[script]
        if results_file.exists():
            results_file.unlink()

        exited_cleanly = await run_script(script, args)
        if not results_file.exists():
            missing_phases.append(script)
        elif not exited_cleanly:
            failed_checks.append(script)

    # Generate report
    print("\n" + "="*60)
    print("Generating report...")
    print("="*60)
    generate_report(missing_phases, failed_checks)

    if failed_checks or missing_phases:
        if failed_checks:
            print(f"\nPhases with failing checks: {', '.join(failed_checks)}")
        if missing_phases:
            print(f"Phases without results: {', '.join(missing_phases)}")
        sys.exit(1)
    print("\n✓ Evaluation complete!")


if __name__ == "__main__":
    asyncio.run(main())