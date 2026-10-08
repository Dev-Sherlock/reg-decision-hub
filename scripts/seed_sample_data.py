#!/usr/bin/env python3
"""Load the mixed-domain sample data into a running symbolic memory.

kb/sample_data.json describes six unrelated domains — homeserver, IoT, cyber,
energy, lab and people — as plain subject/predicate/value facts, plus rules that
join across them. Nothing here is special-cased in the memory layer: the point of
the dataset is that `account_alex.works_at = host_web_01` can be joined to
`host_web_01.mfa_enabled = false` by a rule that has never heard of either domain.

The data is pushed through the HTTP API rather than asserted into Prolog because
PostgreSQL is the system of record: a fact written here is audited, embedded for
semantic search, and mirrored into the Prolog KB by the normal write path. Facts
placed in a .pl file would never reach `/memory/fact`, would have no audit trail
and no embedding, and would vanish on the next API restart.

Writes are upserts keyed on (subject, predicate), so re-running is safe.

Usage
    # Validate and print a summary (no services needed)
    python scripts/seed_sample_data.py

    # Load into a running stack
    python scripts/seed_sample_data.py --push

    # Load, after removing anything a previous run left behind
    python scripts/seed_sample_data.py --push --clear

    # Load and also persist what the rules derive
    python scripts/seed_sample_data.py --push --store-derived

    # Write the API payloads without touching the network
    python scripts/seed_sample_data.py --emit sample_payload.json

Run it from the project root, or pass --data to point at another dataset.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_data_path() -> Path:
    """Locate kb/sample_data.json.

    The API container bind-mounts the repository's kb/ at /kb and copies scripts/
    to /app/scripts, so the script's own parent-parent is /app — which has no kb/.
    The dataset lives beside the KB the API is configured with, so KB_PATH's
    directory finds it in the container and on the host without a second
    environment variable that the two would have to agree on.
    """
    kb_path = os.getenv("KB_PATH")
    if kb_path:
        beside_kb = Path(kb_path).parent / "sample_data.json"
        if beside_kb.exists():
            return beside_kb
    return PROJECT_ROOT / "kb" / "sample_data.json"


DEFAULT_DATA = default_data_path()

# fact(Subject, Predicate, Value) inside a rule body. Only used for reporting
# which predicates a rule reads; the derivation itself is the KB's job.
FACT_GOAL_RE = re.compile(r"fact\(\s*[^,]+,\s*(?P<predicate>[^,]+?)\s*,")


class DatasetError(ValueError):
    """The dataset file is not shaped the way the loader expects."""


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def load_dataset(path: Path) -> dict[str, Any]:
    """Parse the dataset file and reject anything structurally wrong.

    Every check here corresponds to a way the file can silently misbehave: a
    duplicated (subject, predicate) makes the second write overwrite the first,
    a subject in two domains makes the per-domain reporting a lie, and a rule
    that is not fact/3 will derive triples the API cannot store.
    """
    if not path.exists():
        raise DatasetError(f"{path} does not exist")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DatasetError(f"{path} is not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise DatasetError("dataset root must be an object")

    facts = data.get("facts")
    rules = data.get("rules")
    domains = data.get("domains")

    if not isinstance(facts, list) or not facts:
        raise DatasetError("dataset must have a non-empty 'facts' list")
    if not isinstance(rules, list):
        raise DatasetError("dataset must have a 'rules' list (it may be empty)")
    if not isinstance(domains, dict) or not domains:
        raise DatasetError("dataset must have a non-empty 'domains' object")

    seen: set[tuple[str, str]] = set()
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise DatasetError(f"facts[{index}] must be an object")
        subject, predicate = fact.get("subject"), fact.get("predicate")
        if not isinstance(subject, str) or not subject:
            raise DatasetError(f"facts[{index}] needs a non-empty string 'subject'")
        if not isinstance(predicate, str) or not predicate:
            raise DatasetError(f"facts[{index}] needs a non-empty string 'predicate'")
        if "value" not in fact:
            raise DatasetError(f"facts[{index}] ({subject}.{predicate}) needs a 'value'")
        key = (subject, predicate)
        if key in seen:
            # An upsert would drop the earlier value without any warning.
            raise DatasetError(
                f"duplicate fact {subject}.{predicate}: "
                "a fact is keyed on (subject, predicate), so the second write "
                "would silently replace the first"
            )
        seen.add(key)

    declared: dict[str, str] = {}
    for domain, subjects in domains.items():
        if not isinstance(subjects, list) or not subjects:
            raise DatasetError(f"domain {domain!r} must list at least one subject")
        for subject in subjects:
            if subject in declared:
                raise DatasetError(
                    f"subject {subject!r} is claimed by both "
                    f"{declared[subject]!r} and {domain!r}"
                )
            declared[subject] = domain

    undeclared = sorted({fact["subject"] for fact in facts} - set(declared))
    if undeclared:
        raise DatasetError(
            "every fact subject must belong to exactly one domain; "
            f"missing: {', '.join(undeclared)}"
        )

    names: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise DatasetError(f"rules[{index}] must be an object")
        name = rule.get("name")
        if not isinstance(name, str) or not name:
            raise DatasetError(f"rules[{index}] needs a non-empty string 'name'")
        if name in names:
            raise DatasetError(f"duplicate rule name {name!r}")
        names.add(name)
        head = rule.get("head")
        body = rule.get("body")
        if not isinstance(head, str) or not head.strip().startswith("fact("):
            # run_rule/2 unpacks the head as fact(S, P, V); anything else makes
            # it derive triples the API cannot store.
            raise DatasetError(f"rule {name!r}: 'head' must be a fact/3 goal")
        if not isinstance(body, str) or not body.strip().startswith("["):
            raise DatasetError(f"rule {name!r}: 'body' must be a Prolog goal list")
        joins = rule.get("joins", [])
        if not isinstance(joins, list) or any(not isinstance(s, str) for s in joins):
            raise DatasetError(f"rule {name!r}: 'joins' must be a list of subject names")

    for rule in rules:
        for subject in rule.get("joins", []):
            if subject not in declared:
                raise DatasetError(
                    f"rule {rule['name']!r} joins unknown subject {subject!r}; "
                    "every joined subject must appear in 'domains'"
                )

    return data


def subject_domains(dataset: dict[str, Any]) -> dict[str, str]:
    """Flatten the domains object into subject -> domain."""
    return {
        subject: domain
        for domain, subjects in dataset["domains"].items()
        for subject in subjects
    }


def rule_predicates(rule: dict[str, Any]) -> list[str]:
    """The predicates a rule body reads, in order, without duplicates."""
    predicates: list[str] = []
    for match in FACT_GOAL_RE.finditer(rule["body"]):
        predicate = match.group("predicate").strip()
        if predicate and predicate not in predicates:
            predicates.append(predicate)
    return predicates


def rule_domains(
    dataset: dict[str, Any], subjects: Iterable[str]
) -> list[str]:
    """The domains a set of subjects belongs to, sorted.

    Used on the subjects a rule declares in `joins` — the ones its body reads
    through the shared variable. Measuring the *derived* subjects instead would
    call a rule single-domain whenever it happens to write for one subject,
    which is exactly what `site_over_capacity_on_battery` does while reading a
    UPS on the other side of the domain boundary.
    """
    lookup = subject_domains(dataset)
    return sorted({lookup[subject] for subject in subjects if subject in lookup})


# ---------------------------------------------------------------------------
# Pushing
# ---------------------------------------------------------------------------


def push(
    dataset: dict[str, Any],
    api_url: str,
    timeout: float,
    clear: bool,
    store_derived: bool,
) -> int:
    """Write the dataset, then report what each rule derives.

    Returns a process exit code.
    """
    with httpx.Client(base_url=api_url, timeout=timeout) as client:
        if not _api_reachable(client):
            print(
                f"error: no API at {api_url}. Start the stack with "
                "`docker compose up -d`, or pass --api-url.",
                file=sys.stderr,
            )
            return 1

        if clear:
            removed = clear_dataset(client, dataset, timeout)
            print(f"cleared {removed['facts']} facts and {removed['rules']} rules")

        written = 0
        for fact in dataset["facts"]:
            response = client.post("/memory/fact", json=fact)
            if response.status_code not in (200, 201):
                print(
                    f"error: {fact['subject']}.{fact['predicate']} -> "
                    f"HTTP {response.status_code}: {response.text[:160]}",
                    file=sys.stderr,
                )
                return 1
            written += 1
        print(f"stored {written} facts across "
              f"{len(subject_domains(dataset))} subjects")

        stored_rules = 0
        for rule in dataset["rules"]:
            payload = {"name": rule["name"], "head": rule["head"], "body": rule["body"]}
            response = client.post("/memory/rule", json=payload)
            if response.status_code not in (200, 201):
                print(
                    f"error: rule {rule['name']} -> HTTP {response.status_code}: "
                    f"{response.text[:160]}",
                    file=sys.stderr,
                )
                return 1
            stored_rules += 1
        print(f"stored {stored_rules} rules")

        report_derivations(client, dataset, store_derived)

    return 0


def _api_reachable(client: httpx.Client) -> bool:
    try:
        response = client.get("/health/all")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def clear_dataset(
    client: httpx.Client, dataset: dict[str, Any], timeout: float
) -> dict[str, int]:
    """Delete this dataset's own facts and rules, leaving other data alone."""
    facts = 0
    for fact in dataset["facts"]:
        response = client.request(
            "DELETE",
            f"/memory/fact/{fact['subject']}/{fact['predicate']}",
            timeout=timeout,
        )
        if response.status_code == 200:
            facts += 1
    rules = 0
    for rule in dataset["rules"]:
        response = client.request("DELETE", f"/memory/rule/{rule['name']}")
        if response.status_code == 200:
            rules += 1
    return {"facts": facts, "rules": rules}


def report_derivations(
    client: httpx.Client, dataset: dict[str, Any], store_derived: bool
) -> None:
    """Dry-run every rule and print what it derives and what it had to join."""
    print("\nderivations (dry run, nothing stored):")
    cross_domain = 0
    silent: list[str] = []

    for rule in dataset["rules"]:
        response = client.post(f"/memory/rule/{rule['name']}/run", params={"store": "false"})
        if response.status_code != 200:
            print(f"  {rule['name']}: HTTP {response.status_code} {response.text[:120]}")
            silent.append(rule["name"])
            continue

        derived = response.json().get("derived", [])
        if not derived:
            print(f"  {rule['name']}: nothing derived")
            silent.append(rule["name"])
            continue

        subjects = sorted({fact["subject"] for fact in derived})
        joins = rule.get("joins", [])
        domains = rule_domains(dataset, joins) if joins else []
        if len(domains) > 1:
            cross_domain += 1
        print(f"  {rule['name']}: {len(derived)} fact(s) for {', '.join(subjects)}")
        if joins:
            verdict = " + ".join(domains)
            print(f"    joins {', '.join(joins)} across {verdict}")
        else:
            print(f"    reads one subject only ({subjects[0]})")

        if store_derived:
            stored = client.post(
                f"/memory/rule/{rule['name']}/run", params={"store": "true"}
            )
            if stored.status_code != 200:
                print(f"    could not store: HTTP {stored.status_code}")

    print(f"\n{cross_domain} rule(s) reach across a domain boundary")
    if silent:
        print(f"no derivation: {', '.join(silent)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def summarise(dataset: dict[str, Any], data_path: Path) -> None:
    lookup = subject_domains(dataset)
    print(f"{data_path}: {len(dataset['facts'])} facts, {len(dataset['rules'])} rules")
    for domain in sorted(dataset["domains"]):
        subjects = dataset["domains"][domain]
        count = sum(1 for fact in dataset["facts"] if lookup[fact["subject"]] == domain)
        print(f"  {domain:<12} {len(subjects)} subjects, {count} facts")
    print("\nrules:")
    for rule in dataset["rules"]:
        predicates = ", ".join(rule_predicates(rule))
        joins = rule.get("joins", [])
        if joins:
            domains = " + ".join(rule_domains(dataset, joins))
            print(f"  {rule['name']:<38} reads {predicates}")
            print(f"  {'':<38} joins {', '.join(joins)} ({domains})")
        else:
            print(f"  {rule['name']:<38} reads {predicates}")


def default_api_url() -> str:
    """Where the API probably is: environment first, then .env, then 8000.

    The port the stack is actually published on lives in .env, so a hardcoded
    8000 sends everyone who had to move the port running this against a
    stranger's service.
    """
    override = os.environ.get("EVAL_API_BASE")
    if override:
        return override

    env_file = PROJECT_ROOT / ".env"
    settings: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            settings[key.strip()] = value.strip()

    if settings.get("EVAL_API_BASE"):
        return settings["EVAL_API_BASE"]
    if settings.get("API_HOST_PORT"):
        return f"http://localhost:{settings['API_HOST_PORT']}"
    return "http://localhost:8000"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="dataset JSON file")
    parser.add_argument("--emit", type=Path, help="write the API payloads to this file")
    parser.add_argument("--push", action="store_true", help="load the data into a running API")
    parser.add_argument("--clear", action="store_true", help="delete this data before loading")
    parser.add_argument(
        "--store-derived",
        action="store_true",
        help="also persist what the rules derive (off by default: a dry run proves more)",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="API base URL (default: EVAL_API_BASE, then .env, else http://localhost:8000)",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        dataset = load_dataset(args.data)
    except DatasetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not args.emit and not args.push:
        summarise(dataset, args.data)
        return 0

    if args.emit:
        args.emit.parent.mkdir(parents=True, exist_ok=True)
        args.emit.write_text(
            json.dumps(
                {
                    "facts": dataset["facts"],
                    "rules": [
                        {"name": r["name"], "head": r["head"], "body": r["body"]}
                        for r in dataset["rules"]
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"wrote {args.emit}")

    if args.push:
        api_url = args.api_url or default_api_url()
        print(f"pushing to {api_url}")
        return push(dataset, api_url, args.timeout, args.clear, args.store_derived)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
