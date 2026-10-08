"""
Prolog bridge for the symbolic memory.

SWI-Prolog runs embedded in this process via pyswip, so there is no Prolog
container and no network hop. Five modules are consulted on startup:

    /kb/symbolic_memory.pl   the generic fact/rule store (this system's core)
    /kb/tree_engine.pl       the domain-agnostic walk and condition evaluator
    /kb/tree_registry.pl     which domains exist, and which module holds each
    /kb/decision_tree.pl     the energy dispatch example domain
    /kb/air_traffic.pl       + /kb/manufacturing.pl + /kb/cyber.pl

Responsibilities
    * translate JSON values to and from Prolog terms
    * run read-only goals and hand back bindings
    * write through: a fact or rule change hits Prolog and PostgreSQL together,
      so the two stores cannot silently diverge

Every module name that reaches a Prolog goal in module position comes from
kb/tree_registry.pl via registry(). Nothing here accepts a module name from a
request — that would let a caller aim a retract at an arbitrary predicate.

Everything that touches SWI-Prolog runs in a worker thread and is serialised
behind a lock — the SWI-Prolog interpreter is not re-entrant across threads.

Value mapping
    JSON null    <-> Prolog atom '@none'
    JSON bool    <-> Prolog atom true / false
    JSON string  <-> Prolog atom (quoted on the way out)
    JSON number  <-> Prolog number
    JSON array   <-> Prolog list
    JSON object  <-> Prolog dict

The atoms true, false and '@none' are reserved. A JSON string that would read
back as one of them is escaped to '@s:<value>' on the way into Prolog and
unescaped on the way out, which keeps the mapping reversible.
"""
import asyncio
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pyswip import Prolog

logger = logging.getLogger(__name__)

KB_PATH = os.getenv("KB_PATH", "/kb/symbolic_memory.pl")
DECISION_TREE_PATH = os.getenv("DECISION_TREE_PATH", "/kb/decision_tree.pl")
TREE_ENGINE_PATH = os.getenv("TREE_ENGINE_PATH", "/kb/tree_engine.pl")
TREE_REGISTRY_PATH = os.getenv("TREE_REGISTRY_PATH", "/kb/tree_registry.pl")

# One path per example domain. They are data, not code paths in this module: a new
# domain means a new .pl file plus a registry entry, and nothing here changes.
DOMAIN_TREE_PATHS = {
    "air_traffic": os.getenv("AIR_TRAFFIC_TREE_PATH", "/kb/air_traffic.pl"),
    "manufacturing": os.getenv("MANUFACTURING_TREE_PATH", "/kb/manufacturing.pl"),
    "cyber": os.getenv("CYBER_TREE_PATH", "/kb/cyber.pl"),
}

MEMORY_MODULE = "symbolic_memory"
ENGINE_MODULE = "tree_engine"
REGISTRY_MODULE = "tree_registry"

# There is deliberately no constant here for a *domain's* module name. The domain
# to module mapping lives only in kb/tree_registry.pl; a Python constant for it
# would be a second copy that could disagree, and would give somewhere to build a
# qualified goal from a value that did not come through the registry.

# Atoms that would be ambiguous once they come back as Python values.
RESERVED_ATOMS = {"true", "false", "@none"}
STRING_ESCAPE_PREFIX = "@s:"

# pyswip hands back unbound variables as printed names such as "_123".
UNBOUND_VAR_RE = re.compile(r"^_G?\d+$|^_\d+$")


class PrologError(RuntimeError):
    """A Prolog goal failed or returned something unusable."""


class PrologUnavailable(RuntimeError):
    """The embedded interpreter could not be started."""


class UnknownDomainError(PrologError):
    """No registered domain claims any of the supplied input keys.

    Carries every domain's declared keys so the caller can answer with something
    actionable. Silently picking a default domain would hand back a confident
    answer from a tree that was never consulted about these inputs.
    """

    def __init__(self, message: str, keys_by_domain: Optional[Dict[str, List[str]]] = None) -> None:
        super().__init__(message)
        self.keys_by_domain: Dict[str, List[str]] = keys_by_domain or {}


class AmbiguousDomainError(PrologError):
    """Two domains claim the same number of the supplied input keys.

    Raised rather than resolved by a priority order: the tie means the inputs
    genuinely do not identify a tree, and picking one by convention would hide
    that from the caller.
    """

    def __init__(self, message: str, candidates: Optional[List[str]] = None) -> None:
        super().__init__(message)
        self.candidates: List[str] = candidates or []


# ---------------------------------------------------------------------------
# JSON <-> Prolog term conversion
# ---------------------------------------------------------------------------


def quote_atom(text: str) -> str:
    """Render a Python string as a quoted Prolog atom."""
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
    )
    return f"'{escaped}'"


def to_prolog(value: Any) -> str:
    """Convert a JSON-serialisable Python value to a Prolog term string."""
    if value is None:
        return quote_atom("@none")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        if value in RESERVED_ATOMS or value.startswith(STRING_ESCAPE_PREFIX):
            return quote_atom(STRING_ESCAPE_PREFIX + value)
        return quote_atom(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(to_prolog(item) for item in value) + "]"
    if isinstance(value, dict):
        # An empty dict must stay an empty dict. "_" would be an *unbound
        # variable*: eval_condition/2 rejects anything that is not a dict, so
        # /decide with {} would find no solution, and a fact stored with an
        # empty-object value would smuggle a variable into the KB.
        if not value:
            return "_{}"
        pairs = ",".join(f"{key}:{to_prolog(item)}" for key, item in value.items())
        return "_{" + pairs + "}"
    raise PrologError(f"cannot represent {type(value).__name__} as a Prolog term")


def from_prolog(term: Any) -> Any:
    """Convert a term returned by pyswip back to a JSON value."""
    if term is None:
        return None
    if isinstance(term, bool):
        return term
    if isinstance(term, (int, float)):
        return term
    if isinstance(term, str):
        if term == "@none":
            return None
        if term == "true":
            return True
        if term == "false":
            return False
        if term.startswith(STRING_ESCAPE_PREFIX):
            return term[len(STRING_ESCAPE_PREFIX) :]
        if UNBOUND_VAR_RE.match(term):
            return term
        return term
    if isinstance(term, dict):
        return {str(key): from_prolog(item) for key, item in term.items()}
    if isinstance(term, (list, tuple)):
        return [from_prolog(item) for item in term]
    # pyswip.Blob and anything else: fall back to its printed form.
    return str(term)


_INFIX_OPERATORS = {
    ">": ">",
    "<": "<",
    ">=": ">=",
    "=<": "=<",
    "=:=": "=:=",
    "=\\=": "=\\=",
    "==": "==",
    "\\==": "\\==",
    "=": "=",
    "\\=": "\\=",
    "is": "is",
    "+": "+",
    "-": "-",
    "*": "*",
    "/": "/",
}

_FUNCTOR_HEAD = re.compile(r"^([^\s(]+)\(")


def _split_arguments(text: str) -> Optional[List[str]]:
    """Split a printed argument list on its top-level commas."""
    arguments: List[str] = []
    current: List[str] = []
    depth = 0
    in_quotes = False
    index = 0

    while index < len(text):
        character = text[index]
        if in_quotes:
            if character == "\\" and index + 1 < len(text):
                current.append(text[index : index + 2])
                index += 2
                continue
            if character == "'":
                in_quotes = False
            current.append(character)
        elif character == "'":
            in_quotes = True
            current.append(character)
        elif character in "([":
            depth += 1
            current.append(character)
        elif character in ")]":
            depth -= 1
            current.append(character)
        elif character == "," and depth == 0:
            arguments.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1

    arguments.append("".join(current).strip())
    return arguments if arguments != [""] else None


def format_condition(value: Any) -> str:
    """Render a node condition the way the KB source spells it.

    pyswip returns bindings as strings, so a condition arrives as
    `>(temperature, 30)`. That is unreadable in a proof trace or an audit
    trail, and it is not what the KB says, so the comparison, arithmetic and
    connective operators are rewritten infix. Anything that is not an operator
    application — a bare atom such as `true`, or a quoted string — is returned
    unchanged.

    Only conditions go through here. A fact value is user data and may legitimately
    look like `>(a, b)` without meaning an operator.
    """
    # pyswip hands back the atoms true/false as Python bools, which stringify
    # as "True"/"False". The KB spells them lowercase, and `true` is a valid
    # condition (the root node's), so keep the Prolog spelling.
    if isinstance(value, bool):
        return "true" if value else "false"

    text = str(value)

    match = _FUNCTOR_HEAD.match(text)
    if not match or not text.endswith(")"):
        return text

    functor = match.group(1)
    arguments = _split_arguments(text[match.end() : -1])
    if not arguments:
        return text

    if functor == "," and len(arguments) == 2:
        return f"{format_condition(arguments[0])}, {format_condition(arguments[1])}"
    if functor == "\\+" and len(arguments) == 1:
        return f"\\+ {format_condition(arguments[0])}"

    operator = _INFIX_OPERATORS.get(functor)
    if operator and len(arguments) == 2:
        return (
            f"{format_condition(arguments[0])} "
            f"{operator} "
            f"{format_condition(arguments[1])}"
        )
    return text


def _term_to_json_list(term: Any) -> List[Any]:
    """Normalise a Prolog list term returned by pyswip into a Python list."""
    if term is None:
        return []
    if isinstance(term, (list, tuple)):
        return [from_prolog(item) for item in term]
    return [from_prolog(term)]


def _fact_parts(term: Any) -> Optional[Tuple[Any, Any, Any]]:
    """Pull (subject, predicate, value) out of a derived fact term.

    run_rule/2 yields [Subject, Predicate, Value], matching all_facts/4.
    A fact/3 compound is also accepted so the client copes with a KB that
    hands back the head term instead. Returns None when the term is neither
    shape, rather than guessing.
    """
    if isinstance(term, (list, tuple)):
        parts = list(term)
    else:
        name = getattr(term, "name", None)
        arity = getattr(term, "arity", None)
        if not callable(name) or not callable(arity):
            return None
        if str(name()) != "fact" or arity() != 3:
            return None
        try:
            parts = list(term())
        except Exception:  # pragma: no cover - defensive against term shapes
            return None

    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PrologClient:
    """Async wrapper around an in-process SWI-Prolog instance."""

    def __init__(
        self,
        kb_paths: Optional[Sequence[str]] = None,
    ) -> None:
        self.kb_paths: List[str] = list(
            kb_paths
            or [
                KB_PATH,
                DECISION_TREE_PATH,
                TREE_ENGINE_PATH,
                TREE_REGISTRY_PATH,
                *DOMAIN_TREE_PATHS.values(),
            ]
        )
        self._prolog: Optional[Prolog] = None
        self._lock = asyncio.Lock()
        # Static domain metadata, resolved once. The four example domains are code
        # baked into the image, so their module and labels cannot change while the
        # process runs. Runtime domains registered through POST /memory/domain are
        # deliberately not cached here — they can appear at any time.
        self._static_domains: Optional[List[Dict[str, Any]]] = None

    # -- lifecycle ---------------------------------------------------------

    async def initialize(self) -> None:
        """Start the interpreter and consult the knowledge base. Idempotent."""
        if self._prolog is not None:
            return
        async with self._lock:
            if self._prolog is not None:
                return
            loop = asyncio.get_running_loop()
            try:
                self._prolog = await loop.run_in_executor(None, self._create_prolog)
            except Exception as error:
                logger.error("Failed to start embedded Prolog: %s", error)
                raise PrologUnavailable(str(error)) from error
            logger.info("Prolog client initialized with KB: %s", ", ".join(self.kb_paths))

    def _create_prolog(self) -> Prolog:
        prolog = Prolog()
        for kb_path in self.kb_paths:
            try:
                # imports([]), not consult/1. Every domain module exports the same
                # predicate names (node/6, decide/3, input_keys/1, …) because they
                # implement one interface, and consult/1 imports each module into
                # the caller — which then rejects the second one with "no
                # permission to import … into user". Loading without imports keeps
                # the four namespaces independent; the domains are reached by
                # qualified goal through tree_engine, which is the only caller
                # that needs them.
                #
                # list() is load-bearing: pyswip's query() returns a lazy generator,
                # so a bare call would build it and throw it away, leaving the KB
                # unloaded and only failing later at the first real query.
                list(prolog.query(f"load_files('{kb_path}',[imports([])])"))
                logger.info("Consulted %s", kb_path)
            except Exception as error:
                # A missing optional module must not stop the API, but the
                # generic memory module is required.
                if kb_path == self.kb_paths[0]:
                    raise
                logger.warning("Could not consult optional KB %s: %s", kb_path, error)
        return prolog

    async def close(self) -> None:
        async with self._lock:
            self._prolog = None
            # Cached static metadata describes the interpreter that just went away.
            self._static_domains = None

    # -- raw access --------------------------------------------------------

    async def query(self, goal: str) -> List[Dict[str, Any]]:
        """Run a Prolog goal and return one dict of bindings per solution.

        Callers pass read-only goals. The admin-only /memory/query endpoint
        applies an additional allowlist on top of this.
        """
        if self._prolog is None:
            await self.initialize()

        loop = asyncio.get_running_loop()
        logger.debug("Prolog goal: %s", goal)
        # The engine is a single SWI-Prolog instance and pyswip is not
        # thread-safe. run_in_executor would happily run two goals on
        # separate threads at once, and interleaved queries corrupt the
        # engine's query state — the next query then fails with "The
        # last query was not closed". The UI fetches /tree and /trees
        # in parallel, so this lock is what keeps goals off each other:
        # every Prolog call, read or write, runs one at a time.
        async with self._lock:
            prolog = self._prolog
            if prolog is None:
                # close() may have run between the check above and the lock.
                raise PrologError("Prolog client is closed")
            try:
                results = await loop.run_in_executor(
                    None, lambda: list(prolog.query(goal))
                )
            except Exception as error:
                logger.error("Prolog goal failed: %s — %s", goal, error)
                raise PrologError(f"{goal} failed: {error}") from error
        return results or []

    async def query_one(self, goal: str, key: str) -> Any:
        """Run a goal expected to be deterministic and return one binding."""
        results = await self.query(goal)
        if not results:
            return None
        first = results[0]
        if key not in first:
            return None
        return first[key]

    # -- facts -------------------------------------------------------------

    async def assert_fact(self, subject: str, predicate: str, value: Any) -> bool:
        """Assert a fact in Prolog. Persisting it is the caller's job."""
        goal = f"{MEMORY_MODULE}:assert_fact({quote_atom(subject)},{quote_atom(predicate)},{to_prolog(value)})"
        await self.query(goal)
        return True

    async def retract_fact(self, subject: str, predicate: str, value: Any = None) -> bool:
        """Retract a fact. A None value removes every value for the pair."""
        target = "_" if value is None else to_prolog(value)
        goal = f"{MEMORY_MODULE}:retract_fact({quote_atom(subject)},{quote_atom(predicate)},{target})"
        await self.query(goal)
        return True

    async def query_facts(
        self,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        value: Any = None,
    ) -> List[Dict[str, Any]]:
        """Read facts. None (or '*') wildcards the corresponding argument."""
        subject_term = "_" if _is_wildcard(subject) else quote_atom(str(subject))
        predicate_term = "_" if _is_wildcard(predicate) else quote_atom(str(predicate))
        value_term = "_" if value is None else to_prolog(value)
        goal = (
            f"{MEMORY_MODULE}:all_facts({subject_term},{predicate_term},{value_term},Facts)"
        )
        raw = await self.query_one(goal, "Facts")
        facts: List[Dict[str, Any]] = []
        for entry in _term_to_json_list(raw):
            if isinstance(entry, (list, tuple)) and len(entry) == 3:
                facts.append(
                    {
                        "subject": from_prolog(entry[0]),
                        "predicate": from_prolog(entry[1]),
                        "value": from_prolog(entry[2]),
                    }
                )
        return facts

    async def get_fact(self, subject: str, predicate: str) -> Optional[Dict[str, Any]]:
        """One fact, or None."""
        goal = (
            f"{MEMORY_MODULE}:get_fact({quote_atom(subject)},"
            f"{quote_atom(predicate)},Value)"
        )
        results = await self.query(goal)
        if not results:
            return None
        value = from_prolog(results[0].get("Value"))
        return {"subject": subject, "predicate": predicate, "value": value}

    async def fact_count(self) -> int:
        goal = f"{MEMORY_MODULE}:fact_count(Count)"
        value = await self.query_one(goal, "Count")
        return int(value) if isinstance(value, (int, float)) else 0

    # -- rules -------------------------------------------------------------

    async def add_rule(self, name: str, head: str, body: str) -> bool:
        """Define a rule from Prolog source text for the head and body."""
        goal = (
            f"{MEMORY_MODULE}:add_rule_from_text({quote_atom(name)},"
            f"{quote_atom(head)},{quote_atom(body)})"
        )
        await self.query(goal)
        return True

    async def retract_rule(self, name: str) -> bool:
        await self.query(f"{MEMORY_MODULE}:retract_rule({quote_atom(name)})")
        return True

    async def run_rule(self, name: str) -> List[Dict[str, Any]]:
        """Run a rule and return the ground facts it derives.

        Derived facts are NOT stored — the caller decides whether to persist
        them, so this is safe to use as a dry run.
        """
        goal = f"{MEMORY_MODULE}:run_rule({quote_atom(name)},Derived)"
        results = await self.query(goal)
        derived: List[Dict[str, Any]] = []
        for result in results:
            parts = _fact_parts(result.get("Derived"))
            if parts is not None:
                derived.append(
                    {
                        "subject": from_prolog(parts[0]),
                        "predicate": from_prolog(parts[1]),
                        "value": from_prolog(parts[2]),
                    }
                )
        return derived

    # -- provenance --------------------------------------------------------

    async def get_provenance(self, subject: str, predicate: str) -> List[Dict[str, Any]]:
        """Audit entries for one fact, oldest first."""
        goal = (
            f"{MEMORY_MODULE}:provenance_for({quote_atom(subject)},"
            f"{quote_atom(predicate)},Entries)"
        )
        raw = await self.query_one(goal, "Entries")
        entries: List[Dict[str, Any]] = []
        for entry in _term_to_json_list(raw):
            if isinstance(entry, (list, tuple)) and len(entry) == 4:
                entries.append(
                    {
                        "timestamp": from_prolog(entry[0]),
                        "change_type": from_prolog(entry[1]),
                        "old_value": from_prolog(entry[2]),
                        "new_value": from_prolog(entry[3]),
                    }
                )
        return entries

    # -- bootstrap ---------------------------------------------------------

    async def load_from_db(
        self,
        facts: Iterable[Dict[str, Any]],
        rules: Iterable[Dict[str, Any]],
    ) -> int:
        """Rebuild the KB from PostgreSQL rows.

        Uses the no-provenance load path: replaying the whole store on every
        restart must not manufacture a spurious audit history.
        """
        facts = list(facts)
        rules = list(rules)

        await self.query(f"{MEMORY_MODULE}:clear_facts")
        await self.query(f"{MEMORY_MODULE}:clear_rules")

        for fact in facts:
            goal = (
                f"{MEMORY_MODULE}:load_fact({quote_atom(fact['subject'])},"
                f"{quote_atom(fact['predicate'])},{to_prolog(fact.get('value'))})"
            )
            await self.query(goal)

        for rule in rules:
            await self.add_rule(rule["name"], rule["head"], rule["body"])

        logger.info("Loaded %d facts and %d rules into Prolog", len(facts), len(rules))
        return len(facts)

    # -- health ------------------------------------------------------------

    async def health_check(self) -> bool:
        """True when the interpreter answers a trivial goal."""
        try:
            await self.query(f"{MEMORY_MODULE}:fact_count(Count)")
            return True
        except Exception:
            return False

    # -- decision-tree domains ------------------------------------------------

    async def _static_registry(self) -> List[Dict[str, Any]]:
        """The domains that ship in the image, with their module and input keys.

        Cached because they are code: re-reading the same four modules on every
        /decide would walk an interpreter that cannot change underneath us.
        Runtime domains are never cached — one can appear between two calls.
        """
        if self._static_domains is None:
            results = await self.query(
                f"{REGISTRY_MODULE}:domain_module(Id, Module), "
                f"{REGISTRY_MODULE}:domain_label(Id, Label)"
            )
            entries: List[Dict[str, Any]] = []
            for result in results:
                module = str(from_prolog(result.get("Module")))
                entries.append(
                    {
                        "id": str(from_prolog(result.get("Id"))),
                        "module": module,
                        "label": str(from_prolog(result.get("Label"))),
                        "input_keys": await self._handle_keys(
                            f"module({quote_atom(module)})"
                        ),
                    }
                )
            self._static_domains = entries
        return self._static_domains

    async def _handle(self, domain_id: str) -> str:
        """The tree_engine handle for a domain: module(X) or runtime(id).

        Read through tree_registry rather than derived in Python so the trust
        boundary stays in Prolog: a domain that is not registered has no handle
        and therefore cannot be reached.
        """
        for entry in await self._static_registry():
            if entry["id"] == domain_id:
                return f"module({quote_atom(entry['module'])})"
        return f"runtime({quote_atom(domain_id)})"

    async def _handle_keys(self, handle: str) -> List[str]:
        results = await self.query(f"{ENGINE_MODULE}:input_keys_in({handle},Keys)")
        if not results:
            return []
        return [str(key) for key in _term_to_json_list(results[0].get("Keys"))]

    async def input_keys(self, domain_id: str) -> List[str]:
        """The input keys a domain declares. Empty for one that declares none."""
        return await self._handle_keys(await self._handle(domain_id))

    async def active_domains(self) -> List[Dict[str, Any]]:
        """Every domain /decide may route to, with its declared input keys."""
        static = {entry["id"]: entry for entry in await self._static_registry()}
        results = await self.query(f"{REGISTRY_MODULE}:active_domain_ids(Ids)")
        if not results:
            raise PrologError("tree_registry:active_domain_ids/1 produced no answer")

        domains: List[Dict[str, Any]] = []
        for raw in _term_to_json_list(results[0].get("Ids")):
            domain_id = str(raw)
            entry = static.get(domain_id)
            if entry:
                domains.append({**entry, "source": "kb"})
            else:
                domains.append(
                    {
                        "id": domain_id,
                        "module": None,
                        "label": domain_id,
                        "source": "runtime",
                        "input_keys": await self.input_keys(domain_id),
                    }
                )
        return domains

    async def registry(self) -> List[Dict[str, Any]]:
        """Every known domain including the ones that are not active yet."""
        results = await self.query(f"{REGISTRY_MODULE}:domains(Summaries)")
        if not results:
            raise PrologError("tree_registry:domains/1 produced no answer")
        summaries = _term_to_json_list(results[0].get("Summaries"))
        out: List[Dict[str, Any]] = []
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            domain_id = str(summary.get("id"))
            entry = next(
                (d for d in await self._static_registry() if d["id"] == domain_id),
                None,
            )
            out.append(
                {
                    "id": domain_id,
                    "label": str(summary.get("label") or domain_id),
                    "module": entry["module"] if entry else None,
                    "source": str(summary.get("source") or "runtime"),
                    "active": bool(summary.get("active")),
                    "input_keys": await self.input_keys(domain_id),
                }
            )
        return out

    async def resolve_domain(
        self, inputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Pick the domain an input map belongs to.

        A domain claims an input when the key is one it declares. The domain
        claiming the most of the supplied keys wins; keys no domain declares are
        ignored rather than counted as evidence. A tie is an error, not a
        tiebreak — it means these inputs do not identify a single tree, and
        picking one anyway would return a confident answer from a tree that was
        never asked whether it applies.
        """
        candidates: List[Dict[str, Any]] = []
        for domain in await self.active_domains():
            matched = sorted(set(inputs) & set(domain["input_keys"]))
            if matched:
                candidates.append({**domain, "matched": matched})

        if not candidates:
            by_domain = {
                domain["id"]: sorted(domain["input_keys"])
                for domain in await self.active_domains()
            }
            raise UnknownDomainError(
                "no registered domain declares any of the supplied input keys",
                keys_by_domain=by_domain,
            )

        best = max(len(c["matched"]) for c in candidates)
        winners = [c for c in candidates if len(c["matched"]) == best]
        if len(winners) > 1:
            raise AmbiguousDomainError(
                "input keys match more than one domain equally: "
                + ", ".join(
                    f"{c['id']} ({', '.join(c['matched'])})" for c in winners
                ),
                candidates=[c["id"] for c in winners],
            )
        return winners[0]

    async def decide(
        self, inputs: Dict[str, Any], domain_id: Optional[str] = None
    ) -> Tuple[str, str, str, str, List[Dict[str, Any]]]:
        """Evaluate a decision tree. Returns (domain, leaf, action, owner, proof).

        Without `domain_id` the domain is detected from `inputs` via
        resolve_domain/1. Passing one explicitly skips detection, which is what
        the /decide?domain= override is for — it does not skip validation, so a
        caller still cannot aim the walk at a tree that does not exist.
        """
        if domain_id is None:
            domain = await self.resolve_domain(inputs)
        else:
            domain = next(
                (d for d in await self.active_domains() if d["id"] == domain_id), None
            )
            if domain is None:
                raise UnknownDomainError(
                    f"unknown or inactive domain: {domain_id}",
                    keys_by_domain={
                        d["id"]: sorted(d["input_keys"])
                        for d in await self.active_domains()
                    },
                )

        handle = await self._handle(domain["id"])
        input_term = to_prolog(inputs)
        results = await self.query(
            f"{ENGINE_MODULE}:decide_in({handle},{input_term},Leaf,Proof)"
        )
        if not results:
            raise PrologError("no decision matched the supplied inputs")

        leaf = from_prolog(results[0].get("Leaf"))
        proof = _term_to_json_list(results[0].get("Proof"))

        action, owner = "unknown", "unknown"
        node_results = await self.query(
            f"{ENGINE_MODULE}:node_in({handle},{quote_atom(str(leaf))},_,_,"
            "Action,Owner,_)"
        )
        if node_results:
            action = from_prolog(node_results[0].get("Action")) or "unknown"
            owner = from_prolog(node_results[0].get("Owner")) or "unknown"

        proof_trace: List[Dict[str, Any]] = []
        for step in proof:
            if isinstance(step, (list, tuple)) and len(step) == 4:
                proof_trace.append(
                    {
                        "node_id": from_prolog(step[0]),
                        "condition": format_condition(step[1]),
                        "action": from_prolog(step[2]),
                        "owner": from_prolog(step[3]),
                    }
                )
        return str(domain["id"]), str(leaf), str(action), str(owner), proof_trace

    async def get_all_nodes(self, domain_id: str) -> List[Dict[str, Any]]:
        handle = await self._handle(domain_id)
        raw = await self.query_one(
            f"{ENGINE_MODULE}:collect_nodes_in({handle},Nodes)", "Nodes"
        )
        nodes: List[Dict[str, Any]] = []
        for entry in _term_to_json_list(raw):
            if isinstance(entry, dict):
                node = {str(key): from_prolog(item) for key, item in entry.items()}
                if "condition" in node:
                    node["condition"] = format_condition(node["condition"])
                nodes.append(node)
        return nodes

    async def find_domain_of_node(self, node_id: str) -> Optional[str]:
        """The domain a node id belongs to.

        Node ids are unique across every domain — a documented registry
        invariant — so the first domain that knows the id is the only one. The
        order is the registry's, not a caller-supplied preference, so this cannot
        be steered at a domain that does not own the node.
        """
        for domain in await self.active_domains():
            handle = await self._handle(domain["id"])
            if await self.query(
                f"{ENGINE_MODULE}:node_in({handle},{quote_atom(node_id)},_,_,_,_,_)"
            ):
                return domain["id"]
        return None

    async def get_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        domain_id = await self.find_domain_of_node(node_id)
        if domain_id is None:
            return None
        handle = await self._handle(domain_id)
        results = await self.query(
            f"{ENGINE_MODULE}:node_in({handle},{quote_atom(node_id)},Parent,"
            "Condition,Action,Owner,Version)"
        )
        if not results:
            return None
        result = results[0]
        return {
            "id": node_id,
            "domain": domain_id,
            "parent": from_prolog(result.get("Parent")),
            "condition": format_condition(result.get("Condition")),
            "action": from_prolog(result.get("Action")),
            "owner": from_prolog(result.get("Owner")),
            "version": from_prolog(result.get("Version")),
        }

    async def can_write(self, owner: str, node_id: str) -> bool:
        domain_id = await self.find_domain_of_node(node_id)
        if domain_id is None:
            return False
        handle = await self._handle(domain_id)
        goal = f"{ENGINE_MODULE}:can_write_in({handle},{quote_atom(owner)},{quote_atom(node_id)})"
        return bool(await self.query(goal))

    async def set_node_action(
        self, node_id: str, new_action: str, owner: str
    ) -> bool:
        """Replace a node's action, keeping the owner and bumping the version.

        Fails when the node does not exist. The parent and condition are read
        back inside the same goal that rewrites the clause, so an update cannot
        corrupt them and no separate read can race with the write.
        """
        domain_id = await self.find_domain_of_node(node_id)
        if domain_id is None:
            return False
        handle = await self._handle(domain_id)
        results = await self.query(
            f"{ENGINE_MODULE}:set_action_in({handle},{quote_atom(node_id)},"
            f"{quote_atom(new_action)},{quote_atom(owner)},_)"
        )
        if not results:
            return False
        logger.info("Node %s in domain %s updated by %s", node_id, domain_id, owner)
        return True

    # -- runtime domains -----------------------------------------------------

    async def conditions_readable(self, texts: List[str]) -> bool:
        """Whether every condition string parses to a term the evaluator can use.

        quote_atom/1, not to_prolog/1: a condition is Prolog *source*, and to_prolog
        is for values — it escapes the reserved atom `true` to `@s:true`, which then
        fails to parse as source and would make every root condition unreadable.

        Safe because the text can only ever become one quoted atom, and what comes
        back is a term that is checked and thrown away, never called.
        """
        body = "[" + ",".join(quote_atom(text) for text in texts) + "]"
        results = await self.query(f"{ENGINE_MODULE}:conditions_readable({body},_)")
        return bool(results)

    async def register_domain(
        self,
        domain_id: str,
        label: str,
        input_keys: List[str],
        nodes: List[Dict[str, Any]],
    ) -> None:
        """Create a runtime domain, inactive.

        Nodes arrive already validated by the API. Prolog is written first here:
        if the assert fails the caller gets an error and nothing was registered,
        whereas a failure after a successful Prolog write would leave a domain
        the reload from PostgreSQL does not know how to restore.
        """
        node_terms = ",".join(_normalise_node(node) for node in nodes)
        results = await self.query(
            f"{REGISTRY_MODULE}:register_domain({quote_atom(domain_id)},"
            f"{quote_atom(label)},{to_prolog(input_keys)},[{node_terms}])"
        )
        if not results:
            # forall/2 inside register_domain/4 swallows a node that fails to
            # assert, so "no solution" is the only signal left that the domain did
            # not land. Better a loud failure than a registered tree that answers
            # nothing.
            raise PrologError(f"register_domain/4 refused domain '{domain_id}'")
        logger.info(
            "Registered runtime domain %s with %d nodes", domain_id, len(nodes)
        )

    async def activate_domain(self, domain_id: str) -> None:
        results = await self.query(
            f"{REGISTRY_MODULE}:activate_domain({quote_atom(domain_id)})"
        )
        if not results:
            raise PrologError(f"activate_domain/1 refused domain '{domain_id}'")
        logger.info("Activated runtime domain %s", domain_id)

    async def deactivate_domain(self, domain_id: str) -> None:
        """Take a runtime domain out of the routable set, keeping it registered.

        retractall/1 always succeeds, so "no solution" here would mean the
        goal did not run at all rather than that the domain was already
        inactive — deactivation is idempotent by design.
        """
        results = await self.query(
            f"{REGISTRY_MODULE}:deactivate_domain({quote_atom(domain_id)})"
        )
        if not results:
            raise PrologError(f"deactivate_domain/1 refused domain '{domain_id}'")
        logger.info("Deactivated runtime domain %s", domain_id)

    async def unregister_domain(self, domain_id: str) -> None:
        """Drop a runtime domain from the registry entirely.

        clear_runtime_domain/1 removes the node facts, the declared keys,
        the label and the enabled flag, so the id is free again and
        /decide, /tree and /trees stop seeing it immediately.
        """
        results = await self.query(
            f"{REGISTRY_MODULE}:clear_runtime_domain({quote_atom(domain_id)})"
        )
        if not results:
            raise PrologError(
                f"clear_runtime_domain/1 refused domain '{domain_id}'"
            )
        logger.info("Unregistered runtime domain %s", domain_id)

    async def domain_exists(self, domain_id: str) -> bool:
        results = await self.query(
            f"{REGISTRY_MODULE}:domains(Summaries), "
            f"member(Summary, Summaries), get_dict(id, Summary, Id), "
            f"Id == {quote_atom(domain_id)}"
        )
        return bool(results)


def _is_wildcard(value: Any) -> bool:
    return value is None or value == "*"


def _normalise_node(node: Dict[str, Any]) -> str:
    """One node spec as the 6-element list tree_registry:assert_node/2 expects.

    Six fields — the domain id is added by register_domain/4 in Prolog, so it cannot
    be spelled two ways. The count is load-bearing: assert_nodes/2 uses maplist/3, so
    a spec of the wrong arity fails the whole registration instead of asserting a
    clause for some other predicate and leaving the domain looking registered while
    it answers nothing.

    The condition is the only field that is not quoted, because it is not user
    data: it is Prolog source the API validated against the condition grammar.
    Quoting it would store the atom '(x > 3)', and the node would then match
    nothing at all while still looking configured.

    parent is spelled `none` rather than a quoted atom because that is what
    root_in/2 pattern-matches on to find a tree's root.
    """
    parent = node.get("parent")
    fields = [
        quote_atom(str(node.get("id"))),
        "none" if parent in (None, "none") else quote_atom(str(parent)),
        str(node.get("condition") or "true"),
        quote_atom(str(node.get("action"))),
        quote_atom(str(node.get("owner"))),
        repr(int(node.get("version") or 1)),
    ]
    return "[" + ",".join(fields) + "]"


# ---------------------------------------------------------------------------
# FastAPI dependency / singleton
# ---------------------------------------------------------------------------

_prolog_client: Optional[PrologClient] = None


async def get_prolog_client() -> PrologClient:
    """Dependency injection for FastAPI. Lazily starts the interpreter."""
    global _prolog_client
    if _prolog_client is None:
        _prolog_client = PrologClient()
        await _prolog_client.initialize()
    return _prolog_client


async def close_prolog_client() -> None:
    global _prolog_client
    if _prolog_client is not None:
        await _prolog_client.close()
        _prolog_client = None
