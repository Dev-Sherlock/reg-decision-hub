#!/usr/bin/env python3
"""Migrate the energy decision tree from node/6 facts to generic fact/3 triples.

kb/decision_tree.pl predates the universal symbolic memory: it stores a rigid
node/6 record per tree node. This script rewrites each of those as the
subject/predicate/value triples the generic memory understands, so the energy
domain becomes just another set of facts:

    node(n1, root, demand > 80, dispatch_energy, forecaster, 1)
      ->  fact(n1, parent,    root)
           fact(n1, condition, "demand > 80")
           fact(n1, action,    dispatch_energy)
           fact(n1, owner,     forecaster)
           fact(n1, version,   1)

The tree itself is left untouched — decision_tree.pl stays the domain example
and /decide keeps working from it. Only the memory store gains the triples.

Usage
    # Write the migrated facts to a JSON file (no services needed)
    python scripts/migrate_decision_tree.py --emit migrated_facts.json

    # Load them into a running stack
    python scripts/migrate_decision_tree.py --push
    python scripts/migrate_decision_tree.py --push --api-url http://localhost:8000

Run it from the project root, or pass --kb to point at another file.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KB = PROJECT_ROOT / "kb" / "decision_tree.pl"

# node(Id, Parent, Condition, Action, Owner, Version).
# Conditions may themselves contain commas inside a conjunction, so the body is
# scanned with a depth counter rather than a naive split.
NODE_RE = re.compile(r"^\s*node\(\s*(?P<body>.+?)\s*\)\s*\.\s*$", re.DOTALL)


def split_top_level(body: str) -> list[str]:
    """Split a predicate body on commas that are not nested in (), {}, [] or ''."""
    fields: list[str] = []
    depth = 0
    quoted = False
    current: list[str] = []

    for char in body:
        if quoted:
            current.append(char)
            if char == "'":
                quoted = False
            continue
        if char == "'":
            quoted = True
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            fields.append("".join(current).strip())
            current = []
        else:
            current.append(char)

    if current:
        fields.append("".join(current).strip())
    return fields


def parse_atom(raw: str, line_no: int) -> str:
    """Strip surrounding quotes from a Prolog atom written in the KB."""
    raw = raw.strip()
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("\\'", "'")
    if raw in ("true", "false"):
        return raw
    if re.fullmatch(r"[a-z][A-Za-z0-9_]*", raw):
        return raw
    raise ValueError(f"line {line_no}: cannot read {raw!r} as a node id")


def node_to_facts(fields: list[str], line_no: int) -> list[dict[str, Any]]:
    node_id, parent, condition, action, owner, version = fields
    node_id = parse_atom(node_id, line_no)
    return [
        {"subject": node_id, "predicate": "parent", "value": parse_atom(parent, line_no)},
        {"subject": node_id, "predicate": "condition", "value": condition},
        {"subject": node_id, "predicate": "action", "value": parse_atom(action, line_no)},
        {"subject": node_id, "predicate": "owner", "value": parse_atom(owner, line_no)},
        {"subject": node_id, "predicate": "version", "value": int(version)},
    ]


def extract_facts(kb_path: Path) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    for line_no, line in enumerate(kb_path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        # Only node/6 *facts* — a clause head or a call inside a rule body ends
        # in a comma, and must not be picked up.
        if not (stripped.startswith("node(") and stripped.endswith(").")):
            continue
        match = NODE_RE.match(stripped)
        if not match:
            raise ValueError(f"line {line_no}: malformed node/6 fact: {stripped!r}")
        fields = split_top_level(match.group("body"))
        if len(fields) != 6:
            raise ValueError(
                f"line {line_no}: expected 6 node/6 fields, got {len(fields)}: {stripped!r}"
            )
        facts.extend(node_to_facts(fields, line_no))

    if not facts:
        raise ValueError(f"no node/6 facts found in {kb_path}")
    return facts


def push(facts: list[dict[str, Any]], api_url: str, timeout: float) -> int:
    written = 0
    with httpx.Client(base_url=api_url, timeout=timeout) as client:
        for fact in facts:
            response = client.post("/memory/fact", json=fact)
            response.raise_for_status()
            written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB, help="path to decision_tree.pl")
    parser.add_argument("--emit", type=Path, help="write the migrated facts to this JSON file")
    parser.add_argument("--push", action="store_true", help="POST the facts to a running API")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    if not args.kb.exists():
        print(f"error: {args.kb} does not exist", file=sys.stderr)
        return 1

    try:
        facts = extract_facts(args.kb)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    subjects = sorted({fact["subject"] for fact in facts})
    print(f"parsed {len(subjects)} nodes from {args.kb} into {len(facts)} facts")

    if args.emit:
        args.emit.parent.mkdir(parents=True, exist_ok=True)
        args.emit.write_text(json.dumps(facts, indent=2), encoding="utf-8")
        print(f"wrote {args.emit}")

    if args.push:
        try:
            written = push(facts, args.api_url, args.timeout)
        except httpx.HTTPError as error:
            print(f"error: pushing to {args.api_url} failed: {error}", file=sys.stderr)
            return 1
        print(f"stored {written} facts via {args.api_url}/memory/fact")

    if not args.emit and not args.push:
        for fact in facts:
            print(json.dumps(fact))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
