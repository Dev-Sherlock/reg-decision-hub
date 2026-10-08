# AGENTS.md

Working notes for anyone (human or agent) changing this repository. The README explains
what the system does; this file explains how to work on it and which invariants to preserve.

## Commands

```bash
# Build and run the whole stack (api, llm, postgres, ui)
docker compose up --build

# Start without the UI while iterating on the backend
docker compose up --build api postgres

# API test suite
docker compose exec api pytest src/api/tests -v

# Prolog unit tests — Prolog is embedded in the api container, so there is no
# `prolog` service to exec into
docker compose exec api swipl -g "consult('/kb/test_symbolic_memory.pl')" -t "run_tests"
docker compose exec api swipl -g "consult('/kb/test_decision.pl')" -t "run_tests"
docker compose exec api swipl -g "consult('/kb/test_trees.pl')" -t "run_tests"

# Static checks (run these before claiming a change is done)
python -m compileall -q src scripts eval kb
pyflakes src scripts eval
npx --prefix ui tsc -p ui/tsconfig.json

# Evaluations — need the stack running
python eval/run_all.py --skip-degradation     # degradation.py stops/starts the llm container
python eval/run_all.py                        # everything, degradation included

# Sample data — the bare form needs no services, --push needs the stack
python scripts/seed_sample_data.py            # validate + summarise kb/sample_data.json
python scripts/seed_sample_data.py --push     # load it; --clear reloads from a clean slate
```

The eval scripts talk to `EVAL_API_BASE` / `EVAL_LLM_BASE`, which default to the canonical
ports. Set them when the API is not on 8000. A phase that crashes writes no results file and
`run_all.py` exits non-zero, so check the exit code rather than the last line of output.

These commands are shell-agnostic — `docker`, `python` and `pyflakes` behave the same in
PowerShell and cmd. The one exception is the model download, which has a `.sh` and a `.ps1`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1   # Windows
bash scripts/download_model.sh                                       # bash
```

`docker compose exec` needs `-T` when the command is scripted from PowerShell without a TTY,
otherwise it warns about the missing terminal but still works.

Local `.venv` has the Python dependencies, but **not** SWI-Prolog or `pyswip`. Anything
that touches Prolog has to run in the `api` container.

## Architecture in one paragraph

PostgreSQL is the durable system of record for facts, rules and the audit trail. Prolog is
embedded in the API process via `pyswip` and holds the same facts as a queryable,
explainable view — it is *derived* state, rebuilt from PostgreSQL on startup. The LLM is a
separate HTTP service that only ever produces natural-language answers from facts handed to
it; no decision depends on it. `pgvector` provides semantic retrieval over an embedding of
each fact.

Decision trees are *domains*: a Prolog module exporting `node/6`, `decide/3`, `trace/2`,
`can_write/2` and `input_keys/1`. Four ship in the image — `energy` (`decision_tree.pl`),
`air_traffic`, `manufacturing`, `cyber` — and callers may register more at runtime through
`POST /memory/domain`. `kb/tree_registry.pl` maps domain id → module, and `kb/tree_engine.pl`
is the only thing that dispatches into them. `/decide` names no domain: it routes on which of
a domain's declared `input_keys` the caller sent.

## Invariants

Breaking any of these will produce a system that looks like it works and is wrong.

1. **PostgreSQL is written first.** In `_write_fact` / `store_rule` / `override_node`, the
   database write happens before the Prolog mirror. A Prolog failure is logged and swallowed
   because the next startup reloads the KB from the database. Never invert this order.
2. **Prolog failures are not HTTP 500s** for fact writes. The data is safe; failing the
   request would make callers think it was not stored.
3. **Every mutation is audited.** `db.log_audit` with a `diff_hash` is not optional. If you
   add an endpoint that writes, add the audit call.
4. **Permission is decided by the KB, never the API.** `override_node` asks
   `prolog.can_write/2`. Do not hard-code ownership checks in Python.
5. **The LLM is optional everywhere.** `/decide` must never call the LLM. `/agent/ask` must
   never raise when the LLM is down — it returns `used_fallback=true`, `response=null`, and
   the retrieved facts.
6. **Embeddings are optional too.** `EmbeddingUnavailable` must be caught and degraded from
   (keyword search) rather than propagated. Failed model loads are cached behind
   `LOAD_RETRY_COOLDOWN` so a missing model does not re-pay the load cost on every request.
7. **Arbitrary Prolog stays locked down.** `/memory/query` requires `ADMIN_API_KEY`, checks
   the predicate allowlist, *and* rejects forbidden tokens. Extending the allowlist means
   reading `_validate_query` and asking whether the predicate can mutate or do I/O. A goal
   is qualified with the module that owns it rather than run in `user`: the KB loads with
   `imports([])` so the four domain namespaces stay independent, so `fact/3` and `node/6`
   share no namespace and `:` cannot be used to pick one (the token filter forbids it).
   `MEMORY_QUERY_PREDICATES` resolve on their own; `TREE_QUERY_PREDICATES` need `domain` on
   the request or the goal is ambiguous.
8. **Facts are general.** The memory layer is subject/predicate/value and knows nothing
   about energy, demand or temperature. If you find domain vocabulary in
   `src/api/routes/memory.py`, `database.py` or `prolog/`, it belongs in `kb/` or a test
   fixture instead. `kb/sample_data.json` is the standing check on this: it spans six
   domains (homeserver, iot, cyber, energy, lab, people) and its rules join them through
   stored values, so anything that hard-codes one of those words into the store shows up
   as a failing derivation in `test_sample_data.py`.
9. **UI follows the API.** `ui/src/api.ts` is the single client; components should not call
   `fetch` directly.

## API reference

`/health` reports per-dependency status (prolog, postgresql, embeddings, llm).
`/health/all` is the cheap liveness probe Docker uses and touches no dependency, so
it reports nothing per-dependency. On `/health`, `degraded` means the regulated path
still works — it is not the same as `down`.

### Memory

| Method | Path | Body / query | Returns |
|--------|------|--------------|---------|
| `POST` | `/memory/fact` | `{subject, predicate, value, owner?, reindex?}` | `FactResponse` (201) |
| `GET` | `/memory/fact` | `?subject=&predicate=&limit=&offset=`, `*` = wildcard | `{facts, count}` |
| `GET` | `/memory/fact/{subject}/{predicate}` | — | `FactResponse` or 404 |
| `DELETE` | `/memory/fact/{subject}/{predicate}` | `?owner=` | `{deleted, subject, predicate, value, version}` |
| `GET` | `/memory/fact/{subject}/{predicate}/provenance` | — | `[{timestamp, change_type, old_value, new_value}]` |
| `POST` | `/memory/fact/{subject}/{predicate}/override` | `{value, owner}` | `FactResponse`; 404 if the fact does not exist |
| `POST` | `/memory/rule` | `{name, head, body}` — Prolog source text | `RuleResponse` (201) |
| `GET` | `/memory/rule` | — | `{rules, count}` |
| `GET` | `/memory/rule/{name}` | — | `RuleResponse` or 404 |
| `DELETE` | `/memory/rule/{name}` | — | `{deleted, rule}` |
| `POST` | `/memory/rule/{name}/run` | `?store=false&owner=` | `{rule, derived, count}` — dry run unless `store=true` |
| `GET` | `/memory/search` | `?query=&top_k=` | `{query, hits, count, semantic}` |
| `POST` | `/memory/verify` | `{subject, predicate, value}` | `{stored, stored_value, derivable_from_rules, consistent}` |
| `GET` | `/memory/audit` | `?subject=&predicate=&rule_name=&entity_type=&limit=` | `{entries, count}` |
| `POST` | `/memory/query` | `{query, limit, domain?}` + `X-Admin-Key` | `{query, bindings, count}`; `domain` is required for the tree predicates |

`value` is arbitrary JSON — scalar, object or list. The Prolog codec round-trips all of them.

`semantic=false` on `/memory/search` means the embedding model was unavailable and results
came from keyword matching. It is a degraded result, not a different kind of result.

### Governed overrides

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `POST` | `/memory/node/{node_id}/override` | `{action, owner}` | 404 unknown node, 403 not the owner |
| `GET` | `/memory/node/{node_id}/audit` | `?limit=` | `{node_id, entries, count}` with `old_action`, `new_action`, `owner`, `diff_hash` |

Node ids are unique across every domain, so `POST /memory/node/sec4/override` needs no
domain — the KB resolves the bare id. Permission still comes from that node's own
`can_write/2`, so ownership does not leak between domains.

### Decision domains

| Method | Path | Body / query | Returns |
|--------|------|--------------|---------|
| `GET` | `/trees` | — | Every registered domain with `id`, `label`, `root`, `input_keys`, `node_count`, `active`, `source` |
| `POST` | `/memory/domain` | `{id, label, input_keys, nodes[]}` — one spec, one domain | 201 `{domain, active: false, node_count}` |
| `POST` | `/memory/domain/{id}/activate` | `?owner=` | 200 the domain, now `active: true` |
| `POST` | `/memory/domain/{id}/deactivate` | `?owner=` | 200 the domain, now `active: false`; idempotent |
| `DELETE` | `/memory/domain/{id}` | `?owner=` | 200 the deleted domain; 400 for a kb domain, 404 unknown |
| `GET` | `/memory/domain/{id}/audit` | `?limit=` | `{domain, entries, count}` with `operation`, `old_value`, `new_value`, `diff_hash` |

A created domain is **inactive**: registered, visible in `/trees`, audited — but never
routed to, because a half-validated tree that answered decisions would be worse than one
that did not answer at all. Activation is explicit and audited, deactivation takes a
domain out of the routable set while keeping it registered, and deletion removes a
runtime domain entirely — its audit rows remain, and the DELETE response's `old_value`
carries the whole spec so the deletion is reconstructable from the trail alone. A spec is
rejected outright
(400, nothing registered) if it has duplicate node ids, an id another domain already uses,
zero or two roots, a parent that is not in the domain, a parent cycle, or a condition that
is not readable Prolog. Cycles are detected over parent pointers, not children: each node
has one parent, so a child-walk cannot loop, and a self-parented node is otherwise reported
as an unreachable branch.

### Decisions

| Method | Path | Body | Returns |
|--------|------|------|---------|
| `POST` | `/decide` | `{inputs: {…}}`, optional `?domain=` | `{leaf_node, action, owner, domain, proof_trace}` |
| `GET` | `/tree` | — | The merged forest: every node of every active domain, each tagged with its `domain` |
| `GET` | `/tree/{node_id}` | — | One node, with its `domain` |

`/decide` takes no domain. It routes on the caller's `inputs`: each active domain declares
`input_keys`, and the domain sharing the most declared keys with the inputs wins. Keys no
domain declares are ignored, so a caller may send extras. A 400 carries one of three
`error` values in its detail:

- `unknown_domain` — no active domain claims any of these keys (`keys_by_domain` lists what
  each one would have accepted).
- `ambiguous_domain` — two domains claim equally many (`candidates` names them). `?domain=`
  resolves it.
- `no_matching_leaf` — the domain was found but no node's condition held; `domain` and
  `reason` say which tree and why.

`?domain=` skips routing but not validation, so naming a domain whose keys you did not send
is still a 400. `{}` is `unknown_domain`, not the energy root: no declared key was sent, so
no tree identified itself.

### LLM and agent

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `POST` | `/agent/ask` | `{prompt, top_k}` | Always 200. `used_fallback=true` + `response=null` + `retrieved_facts` when the LLM is down. |
| `POST` | `/llm/ask` | `{prompt, context_facts?}` | 503 when the model is unreachable. `context_facts` bypasses retrieval. |
| `GET` | `/llm/health` | — | `{reachable, status, reason}` |

## LLM provider configuration

`LLM_PROVIDER` selects the implementation in `src/llm/base.py`:

- `http` (default) — `HttpLLMProvider` calls `LLM_SERVICE_URL`, which by default is the
  `llm` container running `src/llm/server.py`. Use this for local inference.
- `openrouter` — `OpenRouterProvider` calls the OpenRouter API with `OPENROUTER_API_KEY` and
  `OPENROUTER_MODEL`. No local model or container needed.

Adding a provider means subclassing `LLMProvider` (`ask`, `is_healthy`) and registering it in
the factory in `src/llm/base.py`. Nothing in `src/api/routes/` should need to change: routes
depend on the interface and on `LLMUnavailable` for the degraded path.

The `llm` container is allowed to start without a model. It then reports `unhealthy` with a
reason instead of crash-looping, so `docker compose up` succeeds on a fresh clone before
`scripts/download_model.sh` has been run. The load is retried on every request rather than
cached, so a model that appears later is picked up without a restart.

Every route that touches the LLM — including `/health` — must resolve it through
`Depends(get_llm_provider)`. Calling `get_llm_provider()` directly makes the dependency
overrides in `conftest.py` invisible, so a test that injects a down provider would read the
real container's state instead.

## Local model selection

`src/llm/server.py` loads whatever `MODEL_PATH` names and reads the GGUF's own chat template.
Two details are load-bearing, both because quantised sub-1.1B models misbehave without them:

- **`create_chat_completion` when a template exists** (`_chat_capable`). Raw completion treats
  the prompt as a document to continue, so the model answers and then re-asks the question in a
  loop. `GET /health` on the LLM service reports which path is live as `mode`.
- **`stop` + `repeat_penalty` (`REPEAT_PENALTY`, default 1.15)**. Cheap insurance against the
  loop on models whose template still does not stop them.

Presets live in both download scripts and both resolve to a plain `<repo> <filename>` pair, so
adding one means adding a `case` in `scripts/download_model.sh` and an entry in the `$Presets`
hashtable in `scripts/download_model.ps1`:

| Preset | Model | Size | Notes |
|--------|-------|------|-------|
| `tiny` (default) | SmolLM2-135M-Instruct Q4_K_M | 101MiB | ~4.9s per answer on CPU |
| `tinyllama-1.1b` | TinyLlama-1.1B-Chat v1.0 Q4_K_M | 638MiB | ~14s per answer, quotes values more reliably |

There is no TinyLlama-110M GGUF on Hugging Face — the org published those weights only inside the
now-gated `TinyLlama/TinyLlama_v1.0`, and the repo named `mepha89/tinyllama-110M-Q8_0-GGUF` is a
conversion of karpathy's TinyStories model that answers with children's-story continuations. Do
not swap it in as the default.

`answer_grounded_in_memory` fails with the default `tiny` preset, so `run_all.py` exits 1. The
presets fail *differently*, so do not go looking for one shared bug: the 1.1B paraphrases
`globex_labs` into `globex lab`, while the 135M omits the operator entirely and answers with a
temperature lifted from a less relevant retrieved fact. Retrieval is not implicated —
`retrieval_is_relevant` and `retrieved_value_matches_stored_fact` both pass. Context size does
make it worse: the check asks for `top_k=5`, and once `kb/sample_data.json` is loaded a fifth
fact about an unrelated temperature joins the context. Re-asking at `top_k=2` does return an
answer containing `globex_labs`, so leave the check strict rather than narrowing it to go green.

## Conventions

- Python: type hints on public functions, `logging.getLogger(__name__)` (never `print` in
  `src/`), dataclasses in `eval/` and `scripts/`.
- Comments explain *why*, not *what*. The surrounding docstrings carry the design rationale —
  read those before changing a module.
- Prolog modules are `module`-scoped with an explicit export list; add new predicates there.
- Tests live in `src/api/tests/` and are grouped by concern (`test_memory`,
  `test_semantic_search`, `test_governance`, `test_llm_bridge`, `test_decision`,
  `test_multidomain`, `test_sample_data`, …). `conftest.py` owns the async fixtures.
- `pytest.ini` sets `asyncio_mode = auto` and `testpaths = src/api/tests`.
- The test database is **not** reset between runs, and neither is the evaluation. Never assert
  that a table is empty or that a filter returns `[]`; assert what the filter selects, or scope
  the assertion to the run's own namespace. A stored rule fires over the *whole* database, so a
  dry run's `derived` list and a `store=true` run's `count` both grow with every earlier run —
  filter them by the run's subjects instead of counting. `eval/governance.py` restores each
  node action it overrides, because a node override stays in effect until the API container
  restarts and would otherwise make `eval/correctness.py` fail on decisions that were never
  wrong. Prolog has no transactions either, so the `prolog` fixture calls
  `clear_runtime_domains/0` too: a domain registered by one test stays routable for every
  test after it unless it is dropped explicitly.

## Common tasks

**Add a fact store query.** `src/api/database.py` holds one async function per operation and
its SQLAlchemy model. Add a `select`/`insert` there, expose it from the relevant route in
`src/api/routes/`, and add a case to the matching test module.

**Change the domain.** There is no longer one domain. A new KB tree is a new module
exporting `node/6`, `can_write/2`, `trace/2`, `decide/3`, `get_all_nodes/1`,
`get_leaves/1`, `is_leaf/1` and `input_keys/1` — copy `kb/air_traffic.pl`, which is the
shortest of the three examples. It must also `reexport(tree_engine, [eval_condition/2,
node_timestamp/1])` and list both in its export list: `/memory/query` qualifies a goal with
the module that owns the predicate, so a module missing `eval_condition/2` answers that
query with an existence error while the same query against `energy` works, which reads as a
routing fault. `kb/test_trees.pl` asserts the four modules present one interface, so a new
one that forgets is caught the first time that suite runs. Add the module to
`DOMAIN_TREE_PATHS` in `src/api/prolog/client.py`, register it in `kb/tree_registry.pl`'s
static block, and add its `input_keys` to nothing else: `/decide` routing is derived from the
module itself. No Python changes are needed for a domain to answer decisions, but sibling
conditions must stay mutually exclusive — `decide_in/4` takes the first solution Prolog finds,
so an overlapping pair makes the result depend on file layout.

**Add a decision domain at runtime.** `POST /memory/domain` with a full node spec. It is
validated as a whole and registered inactive, so a malformed tree never answers. This is
the only route that can add a domain, and it is audited — see `src/api/routes/domains.py`
for what a rejection looks like. The lifecycle continues with
`POST /memory/domain/{id}/activate` (routable), `POST /memory/domain/{id}/deactivate`
(out of the routable set, still registered) and `DELETE /memory/domain/{id}` (gone;
only runtime domains — a kb domain is code in the image and is refused with 400).
Every transition is audited, and a deletion's audit row carries the whole spec.

**Add a rule.** `POST /memory/rule` takes Prolog text. A dry run (`store=false`) returns the
derived facts without persisting them — use it to check a rule before trusting it.

**Extend the sample dataset.** `kb/sample_data.json` is a `version`ed JSON file: `facts` are
subject/predicate/value, `domains` maps a domain name to its subjects, and each rule carries
`expect` (the triples it must derive, `[]` when it must derive nothing) plus `joins` (the
subjects it reads across a shared variable) and a `note`. `scripts/seed_sample_data.py`
validates that shape before it writes and is the only supported way to load it — facts go
through `POST /memory/fact` so they are audited and embedded. Adding a fact means adding its
subject to exactly one domain; adding a rule means updating its `expect`, because
`test_sample_data.py` compares the derivation against it. Report the domains of what a rule
*joins*, not what it writes — a rule can write for one subject while reading across a domain
boundary, which is the case worth documenting.

**Debug a fact that will not load at startup.** The API logs `Prolog assert failed for
subject.predicate` and carries on; compare `GET /memory/fact` with
`GET /memory/fact/{subject}/{predicate}/provenance` to see whether the KB fell behind.
