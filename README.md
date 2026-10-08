# Regulated Decision-Tree Agent Hub

A minimal, reproducible multi-agent decision system demonstrating reliable, explainable and
governable AI, with a **universal symbolic memory** at its core. The memory is a generic
subject/predicate/value knowledge base plus inference rules — the energy dispatch tree is
just one domain loaded into it, not something the code is written around.

## Overview

Three properties drive every design decision here:

- **Reliability** — the LLM is optional. Decisions come from Prolog; when the model is
  missing, corrupt or stopped, `/decide` is unaffected and `/agent/ask` still returns the
  facts it grounded on.
- **Explainability** — every decision returns the symbolic proof trace that produced it.
  Natural-language answers are generated from retrieved facts, never the reverse.
- **Governance** — writes are permission-checked against a rule the knowledge base itself
  defines, and every mutation lands in an append-only audit trail.

## Architecture

```
                        ┌──────────────┐
   browser ────────────►│  React UI    │  localhost:3000
                        └──────┬───────┘
                               │ HTTP
                        ┌──────▼───────┐        ┌──────────────┐
   localhost:8000 ──────►│  FastAPI     │───────►│  LLM service │  localhost:8001
                        │  (api)       │  HTTP  │  llama.cpp   │  (optional)
                        └──┬────────┬──┘        └──────────────┘
                           │        │
        pyswip (in-process,│        │  asyncpg
       no Prolog container)│        │
                    ┌──────────────▼───────────┐
                    │  PostgreSQL 16 + pgvector│  localhost:5432
                    │  facts · rules · audit   │
                    └──────────────────────────┘
```

Four services, and deliberately no Prolog container: SWI-Prolog is embedded in the API
process through `pyswip`, so there is no socket protocol or separate lifecycle to manage.

| Service    | Port | Role |
|------------|------|------|
| `api`      | 8000 | FastAPI. Owns the Prolog bridge, the database, and the LLM client. |
| `llm`      | 8001 | `llama-cpp-python` serving a local GGUF. Optional — the API degrades without it. |
| `postgres` | 5432 | Durable facts, rules, embeddings, audit trail. |
| `ui`       | 3000 | React + Vite frontend. |

If 8000 is already taken on your machine, set `API_HOST_PORT` (and `VITE_API_URL` to match,
since the browser calls the API directly) in `.env`:

```
API_HOST_PORT=8010
VITE_API_URL=http://localhost:8010
```

The evaluation scripts read `EVAL_API_BASE` and `EVAL_LLM_BASE` the same way, and default to
the ports above.

### The memory model

A fact is a `(subject, predicate, value)` triple. Values are arbitrary JSON, so a fact can
be a number, a string, a nested object or a list — the Prolog codec round-trips all of them.
A rule is a Prolog head/body pair given as source text:

```
POST /memory/rule
{ "name": "escalates",
  "head": "fact(X, priority, immediate)",
  "body": "[fact(X, regime, peak), fact(X, humidity, high)]" }
```

Each fact carries an embedding (`VECTOR(384)`, all-MiniLM-L6-v2) so retrieval is semantic
rather than lexical. Writes are **write-through**: PostgreSQL first (it is the system of
record), then the Prolog KB (the derived, queryable view), then the audit entry. A Prolog
failure therefore degrades to a KB that is rebuilt correctly on the next restart, never to
lost or divergent data.

### The sample dataset

`kb/sample_data.json` is a worked example of the memory model, holding **79 facts across 17
subjects in six unrelated domains**:

| Domain | Subjects | What it describes |
|---|---|---|
| `homeserver` | `nas_01`, `ups_01`, `backup_nas` | a NAS on degraded RAID behind a UPS that is discharging |
| `iot` | `sensor_living_room_01`, `sensor_garage_01`, `thermostat_hall` | room sensors and a thermostat overshooting its setpoint |
| `cyber` | `host_web_01`, `host_db_01`, `host_lab_01` | hosts with differing patch age, CVEs, MFA and encryption |
| `energy` | `site_north`, `site_south` | two sites, one over its capacity cap |
| `lab` | `cryostat`, `heliostat`, `flux_cap` | superconducting magnets and a solar-thermal field |
| `people` | `account_alex`, `oncall_alex`, `oncall_robin` | accounts and on-call rotations |

The point is what the KB is *not* told. Thirteen rules ship with the dataset, and the rule
below joins two of those domains without naming either:

```
name:   privileged_account_without_mfa
head:   fact(P, access_risk, require_mfa)
body:   [fact(P, works_at, H), fact(P, privileged, true), fact(H, mfa_enabled, false)]

# derives: account_alex.access_risk = require_mfa
```

`account_alex` is a *people* subject and `host_web_01` is a *cyber* subject. The rule only
follows the value stored in `works_at`; the KB has no concept of either domain, and the
dataset's `domains` object is bookkeeping for humans, never something the store reads. Change
`account_alex.works_at` to `host_db_01` — which has MFA enabled — and the derivation
disappears.

One rule is expected to derive **nothing**: `cryostat_below_design_temperature` fires below 4 K
and the cryostat sits at exactly 4 K. A dataset where every rule matches its first candidate
would not distinguish a correct KB from one that ignores its rule bodies.

Load it with the seed script, which validates before it writes and pushes through the HTTP API:

```bash
python scripts/seed_sample_data.py               # validate and summarise, no services needed
python scripts/seed_sample_data.py --push        # load into the running stack
python scripts/seed_sample_data.py --push --clear # reload from a clean slate
```

Writes are upserts keyed on `(subject, predicate)`, so re-running is safe. The facts go through
`POST /memory/fact` rather than being asserted into a `.pl` file because PostgreSQL is the
system of record: an API write is audited, embedded for semantic search and mirrored into the
Prolog KB by the normal write path. Facts placed in a KB file would never reach `/memory/fact`,
would have no audit trail or embedding, and would be wiped by the startup reload.

### The example decision trees

`kb/decision_tree.pl` is one domain loaded into that machinery, not a special case in the
code. Nodes are `node(ID, Parent, Condition, Action, Owner, Version)` facts, and the
condition is a Prolog term, so the whole tree is greppable and diffable:

```
root
├──           n1  demand >= 80          dispatch_energy       forecaster
│   ├──       n3  temperature > 30      increase_cooling      optimizer
│   │   ├──   n7  humidity > 70         activate_dehumidifier optimizer
│   │   └──   n8  humidity =< 70        increase_cooling_only optimizer
│   └──       n4  temperature =< 30     keep_cooling          optimizer
│       ├──   n9  humidity > 60         adjust_airflow        optimizer
│       └──   n10 humidity =< 60        keep_cooling_only     optimizer
└──           n2  demand < 80           do_nothing            forecaster
    ├──       n5  temperature > 25      monitor_only          forecaster
    └──       n6  temperature =< 25     do_nothing            forecaster
```

Three more ship with it, in the same shape and reached the same way:

| Domain | Module | Declares `input_keys` | Exercises |
|--------|--------|----------------------|-----------|
| `energy` | `kb/decision_tree.pl` | `demand`, `temperature`, `humidity` | numeric thresholds |
| `air_traffic` | `kb/air_traffic.pl` | `region`, `ice_on_wing_pct`, `crosswind_kts`, `engine_out`, `brake_temp_c` | atom comparison (`region == north`) |
| `manufacturing` | `kb/manufacturing.pl` | `defect_rate_pct`, `hardness_hrc`, `coating_um`, `ftir_flag` | a bare flag and its negation |
| `cyber` | `kb/cyber.pl` | `blast_radius_pct`, `affected_users`, `data_exposed`, `ransomware_flag` | conjunctions under a shared parent |

Each module declares the keys it reads. `/decide` takes no domain: it routes on which of the
declared keys the caller sent, and the domain that claims the most of them answers. So
`{"region": "north", "ice_on_wing_pct": 15}` reaches `at3` without anything naming
air_traffic, and `{"demand": 90, "defect_rate_pct": 2}` — one key each for two domains — is
refused rather than guessed at. Keys no domain declares are ignored, so a caller may send
extras.

`eval_condition/2` is generic: it accepts any key with `>`, `<`, `>=`, `=<`, `=:=`, `=\=`,
`==`, `\==`, `=`, `\=`, conjunctions and negation, plus a bare flag tested for false. Nothing
in it knows about demand or temperature, which is why `/decide` takes an open `inputs` map
and a new domain needs no API change. A numeric threshold never matches a string, so a
wrong-typed input yields "no decision" rather than a confidently wrong one.

The walk stops at the deepest node whose condition holds. A node with children where none
match is itself returned as the leaf, so a partial input map produces the most specific
answer the tree can justify instead of failing.

## Quick start

Every command below is given twice: once for bash (Linux, macOS, WSL, Git Bash) and once
for PowerShell (Windows). The `bash` and `curl` blocks are the only shell-specific parts of
the project — Docker, the API and the tests behave identically on both.

### Prerequisites

- Docker Desktop (or Docker Engine + Compose)
- ~105MB disk for the model (the default SmolLM2-135M Q4 file is 101MiB), ~1.5GB RAM to run it
- Optional: ~700MB more if you also want the 1.1B preset, which is the better answerer

### 1. Download the model (optional)

```bash
bash scripts/download_model.sh           # writes models/SmolLM2-135M-Instruct-Q4_K_M.gguf
bash scripts/download_model.sh tinyllama-1.1b   # the 1.1B alternative, into models/
```

```powershell
# -ExecutionPolicy Bypass is needed on machines whose policy blocks local scripts.
powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1
powershell -ExecutionPolicy Bypass -File scripts/download_model.ps1 tinyllama-1.1b
```

Both write to `models/`, are resumable — re-run to continue an interrupted transfer — and
retry transient Hugging Face failures. Skipping this step is fine: the LLM service starts
degraded and reports `unhealthy` with a reason, and everything else keeps working.

Both scripts also accept `<repo> <filename>` for any other public GGUF, and use `HF_TOKEN`
for a gated repository. See [Choosing a model](#choosing-a-model).

### 2. Launch the stack

```bash
docker ps --filter publish=8000     # is 8000 already taken, and by what?
```

```powershell
docker ps --filter publish=8000
```

Empty output means the default ports are free. If it is not empty, move the API first — see
the port note below — because a clash fails the whole `up`.

```bash
cp .env.example .env      # optional; defaults are sane
docker compose up --build
```

```powershell
Copy-Item .env.example .env   # optional; defaults are sane
docker compose up --build
```

`docker compose up --build` is the same command in cmd and PowerShell. The build takes a few
minutes the first time; the stack is ready when the UI answers on port 3000.

Then open **http://localhost:3000**.

If 8000 is already taken on your machine, set `API_HOST_PORT` in `.env` before starting, and
point `VITE_API_URL` at the same place because the browser calls the API directly:

```
API_HOST_PORT=8010
VITE_API_URL=http://localhost:8010
```

### 3. Try it

The examples below use the default port, 8000. If you changed `API_HOST_PORT`, substitute that
number in both the host and the URL.

```bash
curl -X POST localhost:8000/decide \
  -H 'content-type: application/json' \
  -d '{"inputs": {"demand": 90, "temperature": 35, "humidity": 80}}'
```

```powershell
Invoke-RestMethod -Uri http://localhost:8000/decide -Method Post `
  -ContentType 'application/json' `
  -Body '{"inputs": {"demand": 90, "temperature": 35, "humidity": 80}}'
```

Both return:

```json
{
  "leaf_node": "n7",
  "action": "activate_dehumidifier",
  "owner": "optimizer",
  "domain": "energy",
  "proof_trace": [
    {"node_id": "root", "condition": "true", "action": "none", "owner": "system"},
    {"node_id": "n1", "condition": "demand >= 80", "action": "dispatch_energy", "owner": "forecaster"},
    {"node_id": "n3", "condition": "temperature > 30", "action": "increase_cooling", "owner": "optimizer"},
    {"node_id": "n7", "condition": "humidity > 70", "action": "activate_dehumidifier", "owner": "optimizer"}
  ]
}
```

The same endpoint answers for the other domains, because it routed on the keys rather than
being told a domain:

```bash
curl -X POST localhost:8000/decide \
  -H 'content-type: application/json' \
  -d '{"inputs": {"region": "north", "ice_on_wing_pct": 15}}'
# -> {"leaf_node": "at3", "action": "takeoff_denied_deice",
#     "owner": "safety_officer", "domain": "air_traffic", ...}
```

And when the keys do not identify a tree, it says so instead of guessing:

```bash
curl -X POST localhost:8000/decide \
  -H 'content-type: application/json' \
  -d '{"inputs": {"demand": 90, "defect_rate_pct": 2}}'
# -> 400 {"detail": {"error": "ambiguous_domain",
#                    "candidates": ["energy", "manufacturing"], ...}}
```

### 4. Load the sample dataset

The stack starts with only the facts the migration script created. Load the six-domain sample
dataset to see rules joining unrelated subjects:

```bash
python scripts/seed_sample_data.py --push
```

It prints what each rule derives and which of them cross a domain boundary, without persisting
any of it — `--push --store-derived` is there if you want the derivations kept too. Reload with
`--clear`, and see the facts, rules and cross-domain joins in the UI at
**http://localhost:3000** under **Memory**. The structure is documented in
[The sample dataset](#the-sample-dataset).

## API

### Decisions

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/decide` | `{"inputs": {…}}` → leaf, action, owner, domain, proof trace. Pure Prolog. |
| `GET`  | `/tree` | Every node of every active domain, each tagged with its domain. |
| `GET`  | `/tree/{node_id}` | One node, with its domain. |

`/decide` takes an arbitrary key/value map and names no domain. Each domain declares the
input keys it reads, and the one that claims the most of what you sent answers. A 400 means
the routing itself could not be resolved, and says which way:

| `error` | Meaning |
|---------|---------|
| `unknown_domain` | No active domain claims any of these keys. `keys_by_domain` lists what each would have accepted — `{}` lands here too. |
| `ambiguous_domain` | Two domains claim equally many keys. `candidates` names them; `?domain=` picks one. |
| `no_matching_leaf` | The domain was found, but no node's condition held. `domain` and `reason` say which tree and why. |

A missing key is not an error and not an invention: the walk stops at the last node whose
condition held, so a partial report gets the most specific answer the tree can justify.

### Decision domains

| Method | Path | Notes |
|--------|------|-------|
| `GET`  | `/trees` | Every registered domain: id, label, root, `input_keys`, node count, `active`, `source`. |
| `POST` | `/memory/domain` | Register a domain from a full node spec. 201, and `active: false`. |
| `POST` | `/memory/domain/{id}/activate` | Make a registered domain routable. Audited. |
| `POST` | `/memory/domain/{id}/deactivate` | Stop routing to it, keep it registered. Idempotent, audited. |
| `DELETE` | `/memory/domain/{id}` | Remove a runtime domain entirely. Audited; the trail keeps the whole spec. 400 for a kb domain, 404 unknown. |
| `GET`  | `/memory/domain/{id}/audit` | Create/activate/deactivate/delete history with diff hashes. |

A created domain is registered but **not** routable. That is the point of the split: the spec
is validated as a whole, so a tree that could not be walked — duplicate ids, an id another
domain already uses, zero or two roots, a parent outside the domain, a parent cycle, a
condition that is not readable Prolog — is rejected outright with 400 and nothing is
registered. What survives validation is visible and audited but not consulted, until
activation says so.

Node ids are unique across every domain, so `/tree/{node_id}` and the override audit resolve
a bare id without being told its domain, while `can_write/2` still comes from that node's own
domain — ownership does not leak between them.

### Symbolic memory

| Method | Path | Notes |
|--------|------|-------|
| `POST`   | `/memory/fact` | Store or replace a fact. |
| `GET`    | `/memory/fact` | List; `subject` and `predicate` accept `*` as wildcards. |
| `GET`    | `/memory/fact/{subject}/{predicate}` | One fact, or 404. |
| `DELETE` | `/memory/fact/{subject}/{predicate}` | Retract from both stores. |
| `GET`    | `/memory/fact/{subject}/{predicate}/provenance` | In-memory Prolog trail. |
| `POST`   | `/memory/fact/{subject}/{predicate}/override` | Governed change; requires `owner`. 404 if absent. |
| `POST`   | `/memory/rule` | Define a rule from Prolog `head`/`body` text. |
| `GET`    | `/memory/rule` · `/memory/rule/{name}` | List / read a definition. |
| `DELETE` | `/memory/rule/{name}` | Remove a rule. |
| `POST`   | `/memory/rule/{name}/run` | Derive facts. A dry run unless `store=true`; `owner` stamps the writes. |
| `GET`    | `/memory/search?query=…` | pgvector top-k, or keyword fallback (`semantic=false`). |
| `POST`   | `/memory/verify` | Is a candidate fact already stored or rule-derivable? |
| `GET`    | `/memory/audit` | Append-only trail, filterable by `subject`/`predicate`/`rule_name`/`entity_type`. |
| `POST`   | `/memory/query` | Arbitrary read-only Prolog. Requires `X-Admin-Key`; 403 when unset. |

`/memory/query` is an arbitrary-program endpoint, so it is locked down three ways: a shared
`ADMIN_API_KEY`, a predicate allowlist, and a pattern that rejects mutation and I/O terms
even inside a conjunction. With no key configured it answers 403 rather than existing in
effect.

The goal is qualified with the module that owns it, because the KB loads with `imports([])`
and the four domain namespaces share no predicate names. Memory goals resolve on their own;
the tree predicates (`node`, `trace`, `decide`, `eval_condition`, `get_all_nodes`,
`get_leaves`, `is_leaf`) exist in every domain module, so `{"domain": …}` is required for
them or the goal is ambiguous.

### Governed overrides

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/memory/node/{node_id}/override` | Change a node's action. Permission comes from the KB's own `can_write/2`. |
| `GET`  | `/memory/node/{node_id}/audit` | Override history with diff hashes. |

Ownership is data, not code: `can_write/2` in the KB decides who may change what, so another
domain ships its own rule rather than patching the API.

### LLM and agent

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/agent/ask` | Retrieve facts, then answer. Always 200; `used_fallback=true` and `response=null` when the LLM is down, with the facts still returned. |
| `POST` | `/llm/ask` | Raw bridge, no fallback. 503 when the model is unreachable. |
| `GET`  | `/llm/health` | Provider reachability, status and reason. |
| `GET`  | `/health` · `/health/all` | Per-dependency health. |

### Health

`/health/all` reports each dependency separately so a degraded system is diagnosable: an
unreachable LLM shows as `degraded`, not `down`, because the regulated path still works.

## Testing

The API suite runs inside the container, because the interpreter it exercises is embedded
there:

```bash
# API test suite (unit + integration, needs the stack)
docker compose exec api pytest src/api/tests -v

# Prolog unit tests
docker compose exec api swipl -g "consult('/kb/test_symbolic_memory.pl')" -t "run_tests"
docker compose exec api swipl -g "consult('/kb/test_decision.pl')" -t "run_tests"
docker compose exec api swipl -g "consult('/kb/test_trees.pl')" -t "run_tests"

# Static checks (no container needed)
python -m compileall -q src scripts eval kb
pyflakes src scripts eval
npx --prefix ui tsc -p ui/tsconfig.json
```

Local Python can run the static checks but not the tests: SWI-Prolog and `pyswip` only exist
inside the `api` image.

`kb/test_decision.pl` covers the energy tree's condition evaluation. `kb/test_trees.pl`
covers the registry and the three other example domains — that each module answers, that
`input_keys` is what it claims, that a runtime domain resolves through the registry, that
sibling conditions stay mutually exclusive, and that all four modules export one interface.
That last one is not ceremony: `/memory/query` qualifies a goal with the module that owns the
predicate, so a domain missing an export fails one admin query while the same query works
against another domain.

`src/api/tests/test_sample_data.py` covers the dataset and its loader: every value's round-trip
through PostgreSQL and Prolog, every rule deriving exactly the facts it declares in `expect`, the
cross-domain joins, and each of the loader's validation guards.

The test database is not reset between runs, so tests assert what a filter *selects* rather
than that a table is empty. Scoping a test to its own namespace keeps it independent of
whatever earlier runs left behind. Prolog has no transactions at all, so the fixture also
clears runtime domains between tests — otherwise a domain one test registers stays routable
for every test after it.

## Evaluations

With the stack running:

```bash
python eval/run_all.py --skip-degradation     # everything except the container-stopping phase
python eval/run_all.py                        # all of it
```

| Script | Question it answers |
|--------|---------------------|
| `correctness.py` | Does the tree still match the 30 ground-truth fixtures in `eval/fixtures.csv`? |
| `domains.py` | Does each domain's tree still match its fixtures, and does `/decide` route to the right one? |
| `latency.py` | Is `/decide` inside the 800ms budget? |
| `degradation.py` | If the LLM disappears, do decisions and memory keep working? |
| `governance.py` | Are overrides permission-checked and audited, in every domain? |
| `memory_test.py` | Does symbolic memory store, retrieve, derive and audit correctly? |
| `llm_memory_test.py` | Is the LLM's answer actually grounded in retrieved facts? |

`correctness.py` only ever sends demand/temperature/humidity, so it would pass on a system
that routed by a hard-coded input set. `domains.py` is what makes routing falsifiable: its
fixtures under `eval/fixtures/` cover each domain's leaves plus the routing rules
themselves, including both refusals. A domain that reached the wrong tree fails even when
every leaf is still correct.

Results land in `eval/*.json` with a summary at `eval/report.md`. `degradation.py` stops and
restarts the LLM container, hence the `--skip-degradation` flag.

Two things to know when reading the output:

- **Check the exit code, not the last line.** A phase that crashes writes no results file,
  and `run_all.py` exits non-zero rather than printing a cheerful summary over stale numbers.
  The closing lines distinguish the two failure modes: *phases without results* means the
  script died, *phases with failing checks* means it ran and told you what failed.
- **A skip is not a failure.** With no model downloaded, `llm_memory_test.py` cannot measure a
  generated answer and `degradation.py` cannot measure recovery; those checks are reported as
  skipped, with the reason, and the pass rate is computed over what was actually measured.

### The reference model does not pass the grounding check

Downloading a model turns on the checks that were previously skipped, and one of them fails with
both shipped presets:

```
[FAIL] answer_grounded_in_memory: answer omitted ['globex_labs']; memory holds
       ['globex_labs', 4, 'superconducting_magnets']; answer was: 'Based on the provided
       facts, the correct answer is that the operator running the cryostat is the one who
       operates the superconducting magnets ...'
```

That is the model, not the wiring. Retrieval is correct — the facts handed to the model are the
ones memory holds, and `retrieved_value_matches_stored_fact` passes — but quantised sub-1.1B
models paraphrase rather than quote. `globex_labs` comes back as `globex lab` (underscore turned
into a space), and the 135M also invents units it was never given. The check exists to catch
exactly that, so it is left strict rather than loosened to produce a green run.

How much context the model is given changes the result. The check asks for `top_k=5`, and with
[the sample dataset](#the-sample-dataset) loaded a fifth fact about an unrelated temperature
joins the context and the 135M answers with *that* reading instead. Asking the same question at
`top_k=2` returns an answer containing `globex_labs`. That is a property of the model, not a
reason to weaken the check — it is left asking for `top_k=5`.

What the two presets actually do, measured against the same seeded facts:

| Preset | Size | Wall clock for the eval answer | Failure mode |
|--------|------|---------------------------------|--------------|
| `tiny` (SmolLM2-135M-Instruct Q4_K_M) | 101MiB | ~4.9s | drops the operator, invents a temperature unit |
| `tinyllama-1.1b` (TinyLlama-1.1B-Chat Q4_K_M) | 638MiB | ~14.0s | paraphrases the operator, occasionally mislabels the unit |

Both numbers are CPU-only on the same container, prompt and `temperature=0.3`; the 135M is
roughly 3x faster, which is why it is the default. Neither is a good enough answerer to trust
with a regulated decision — which is exactly what [the architecture](#architecture) is arranged
so you never have to.

To exercise grounding properly, point `LLM_PROVIDER` at a model that can hold a value
unchanged — `openrouter` needs no container at all:

```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=meta-llama/llama-2-7b-chat:free
```

`eval/_config.py` reads `.env` so the API's address is configured in one place. It honours
`EVAL_API_BASE` / `EVAL_LLM_BASE` if you export them instead, and real environment variables
always win over the file.

## Project structure

```
reg-decision-hub/
├── docker-compose.yml
├── Dockerfile.api | Dockerfile.llm | Dockerfile.ui
├── requirements.txt | requirements-llm.txt
├── pytest.ini                        # asyncio_mode = auto, testpaths
├── .env.example                      # annotated; copy to .env
├── scripts/
│   ├── download_model.sh          # resumable GGUF download, preset-aware (bash)
│   ├── download_model.ps1         # same, for PowerShell
│   ├── seed_sample_data.py        # validate + load kb/sample_data.json (--push/--emit/--clear)
│   └── migrate_decision_tree.py  # legacy tree.json → symbolic facts
├── kb/
│   ├── symbolic_memory.pl         # the universal memory: facts, rules, provenance
│   ├── decision_tree.pl           # energy dispatch, loaded as a domain example
│   ├── tree_engine.pl             # the only thing that dispatches into a domain
│   ├── tree_registry.pl           # domain id → module, active set, runtime domains
│   ├── air_traffic.pl             #   domain: atom comparison
│   ├── manufacturing.pl           #   domain: flags and their negation
│   ├── cyber.pl                   #   domain: conjunctions, two owners
│   ├── sample_data.json           # 79 facts / 6 domains / 13 rules, 5 joining domains
│   ├── test_symbolic_memory.pl
│   ├── test_decision.pl
│   └── test_trees.pl
├── sql/init.sql                   # facts (JSONB + VECTOR(384)), rules, audit_log
├── src/
│   ├── api/
│   │   ├── main.py                # routers + startup KB bootstrap
│   │   ├── database.py            # async persistence, vector search, audit
│   │   ├── embeddings.py          # lazy, degrade-tolerant embedder
│   │   ├── prolog/client.py       # embedded pyswip + value codec
│   │   ├── routes/                # memory, llm, agent, decide, overrides, tree, domains, health
│   │   └── tests/                 # pytest suite
│   ├── llm/
│   │   ├── base.py                # provider interface + prompt assembly
│   │   ├── http_provider.py
│   │   ├── openrouter_provider.py
│   │   └── server.py              # standalone LLM service, chat-template aware
│   └── models/schemas.py
├── ui/src/                        # React components, api client, tabs
└── eval/                          # harnesses + _config.py (shared .env loading)
    └── fixtures/                  # per-domain ground truth for domains.py
```

## Configuration

All variables are optional; see `.env.example` for the annotated list.

### Host ports

Set these only when the defaults collide with something already running on your machine.
Docker Compose reads `.env`, and so does `eval/` (via `eval/_config.py`), so one file
covers both.

| Variable | Default | Description |
|----------|---------|-------------|
| `API_HOST_PORT` | `8000` | Host side of the API's published port; the container port stays 8000. |
| `VITE_API_URL` | `http://localhost:8000` | Where the browser calls the API. Must match `API_HOST_PORT`. |
| `EVAL_API_BASE` | `http://localhost:8000` | Where `eval/` looks for the API. |
| `EVAL_LLM_BASE` | `http://localhost:8001` | Where `eval/` looks for the LLM service. |

### Services and models

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PATH` | `/models/SmolLM2-135M-Instruct-Q4_K_M.gguf` | GGUF model inside the `./models` volume. |
| `N_CTX` | `2048` | Context window. |
| `N_GPU_LAYERS` | `0` | GPU offload; `0` is CPU-only. |
| `REPEAT_PENALTY` | `1.15` | Loop guard for small models; `1.0` disables it. |
| `LLM_PROVIDER` | `http` | `http` proxies to the `llm` service; `openrouter` calls OpenRouter directly. |
| `LLM_SERVICE_URL` | `http://llm:8001` | Where the `http` provider looks. |
| `LLM_SERVICE_TIMEOUT` | `120` | Seconds before the provider is declared unavailable. |
| `OPENROUTER_API_KEY` | — | Required only when `LLM_PROVIDER=openrouter`. |
| `OPENROUTER_MODEL` | `meta-llama/llama-2-7b-chat:free` | Model id for the OpenRouter provider. |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Sentence embedding model. |
| `EMBEDDING_DIM` | `384` | Vector width. Changing it requires recreating `symbolic_facts.embedding`. |
| `DATABASE_URL` | `postgresql+asyncpg://…` | Async PostgreSQL DSN. |
| `KB_PATH` | `/kb/symbolic_memory.pl` | Symbolic memory module, inside the container. |
| `DECISION_TREE_PATH` | `/kb/decision_tree.pl` | Domain tree module, inside the container. |
| `ADMIN_API_KEY` | — | When unset, `/memory/query` answers 403 for every request. |

### Choosing a model

The `llm` service loads whatever `MODEL_PATH` points at and reads that GGUF's own chat template.
Two presets are wired into both download scripts; any other GGUF works the same way.

| Preset | Repository | File | Size |
|--------|------------|------|------|
| `tiny` (default) | `bartowski/SmolLM2-135M-Instruct-GGUF` | `SmolLM2-135M-Instruct-Q4_K_M.gguf` | 101MiB |
| `tinyllama-1.1b` | `TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF` | `tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf` | 638MiB |

```bash
bash scripts/download_model.sh tinyllama-1.1b     # fetch the 1.1B alongside the default
```

The two live side by side in `./models`; switching is one line in `.env`:

```
MODEL_PATH=/models/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf
```

then `docker compose up -d llm` to reload. Confirm what is actually serving with
`curl http://localhost:8001/health`, which reports the resolved path and the mode:

```json
{"status": "healthy", "model": "/models/SmolLM2-135M-Instruct-Q4_K_M.gguf", "mode": "chat"}
```

`mode` is `chat` when the GGUF carries a chat template and `completion` when it does not, in
which case the prompt is sent as raw completion text and the model is liable to continue it
instead of answering it. Every preset here reports `chat`.

**Why not TinyLlama 110M?** There is no TinyLlama-110M GGUF on Hugging Face. The TinyLlama
org only published the 110M weights inside `TinyLlama/TinyLlama_v1.0`, which now requires a
token, and the one repository named after a 110M TinyLlama —
`mepha89/tinyllama-110M-Q8_0-GGUF` — is a conversion of `karpathy/tinyllamas`, a model trained
on TinyStories. It loads and returns HTTP 200, and produces this for the eval's question:

```
-Nobody knows.
Kristgets: What?
Just then, a little girl appeared. She was only three years old. She was wearing a white
dress and had a white bow in her hair.
```

A story-continuation model cannot ground anything, so SmolLM2-135M-Instruct takes the 110M
slot: same order of magnitude of speed and footprint, but instruction-tuned. If you have
`HF_TOKEN` and want the genuine TinyLlama 110M anyway, fetch it directly:

```bash
bash scripts/download_model.sh TinyLlama/TinyLlama_v1.0 <filename>.gguf
```

## Troubleshooting

**`Bind for 0.0.0.0:8000 failed: port is already allocated`**

Docker could not start the API because something else on your machine already owns host port
8000. Find out what:

```bash
docker ps --filter publish=8000              # another container
netstat -ano | findstr :8000                 # anything else on Windows
sudo lsof -i :8000                           # anything else on macOS/Linux
```

Then set `API_HOST_PORT` and `VITE_API_URL` in `.env` to a free port and start again:

```
API_HOST_PORT=8010
VITE_API_URL=http://localhost:8010
EVAL_API_BASE=http://localhost:8010
```

The container keeps listening on 8000; only the host side moves, so nothing inside the compose
network changes. Setting `EVAL_API_BASE` too means `python eval/run_all.py` finds the API
without an extra environment variable. Verify with
`docker compose ps` — the api row should read `0.0.0.0:8010->8000/tcp`.

**The UI loads but every request fails in the browser console**

`VITE_API_URL` did not match the port the API is on. The browser calls the API directly, so a
mismatch is invisible until you open devtools. Recheck both variables agree, then
`docker compose up -d ui` to pick up the change.

**The LLM answers, but they ramble, repeat, or contradict the facts**

Both shipped models are placeholders for wiring, not for answer quality. See
[the reference model does not pass the grounding check](#the-reference-model-does-not-pass-the-grounding-check),
and switch `LLM_PROVIDER` to `openrouter` for a model that can hold a value unchanged.

**The LLM echoes the question back instead of answering it**

The `llm` service sends the prompt through the GGUF's own chat template, which is what stops a
model treating the prompt as a document to continue. Check `curl http://localhost:8001/health`:
if `mode` reads `completion`, the loaded file has no chat template, and the raw-completion path
is being used. Pick one of the presets, or pass `stop`/`repeat_penalty` down via
`REPEAT_PENALTY` if the model still loops.

**`/llm/health` reports `unhealthy` after downloading a model**

Check the path, not just the file: `MODEL_PATH` is a path *inside* the container, and it has to
match the filename the download script wrote into `./models`. The service retries the load on
every request, so a corrected `MODEL_PATH` takes effect on the next `/agent/ask` without a
restart; `docker compose up -d llm` is only needed to change environment variables.

## Porting to a new domain

There are two routes, depending on whether the tree is yours to ship or arrives over the API.

**In the image.** Copy `kb/air_traffic.pl`, which is the shortest of the examples. Keep the
export list, keep `node/6` as the fact store, and declare the keys your conditions read in
`input_keys/1` — that list is what `/decide` routes on. Also keep the
`reexport(tree_engine, [eval_condition/2, node_timestamp/1])` line: `/memory/query`
qualifies a goal with the module that owns the predicate, so a module without it answers one
admin query with an existence error while the same query against `energy` works. Then add
the module to `DOMAIN_TREE_PATHS` in `src/api/prolog/client.py` and to the static block in
`kb/tree_registry.pl`. No Python, schema or API change.

**At runtime.** `POST /memory/domain` with `{id, label, input_keys, nodes[]}`, then
`POST /memory/domain/{id}/activate`. Nothing needs rebuilding, which is what makes this a
data operation rather than a deployment. The full lifecycle is `activate` →
`deactivate` (out of the routable set, still registered) → `DELETE` (gone; the
audit trail keeps the whole spec, so the deletion is reconstructable). Only
runtime domains can be deleted — the four example domains are code in the image.

In both cases the UI's decision tab builds its input form from the keys the domain asks
about, so it follows. One thing to preserve either way: **sibling conditions must be
mutually exclusive.** `decide_in/4` takes the first solution Prolog finds, so an overlapping
pair makes the answer depend on file layout rather than on the data — and `eval/fixtures/`
is where you find out, since a mismatch between two overlapping branches shows up as a leaf
that depends on evaluation order.

## License

This project is released under the **GNU GPL v3**. See the LICENSE file for details.
