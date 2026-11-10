# Meridian Agent Fleet

A learning project: a fleet of clinical-trial oversight agents for Meridian Therapeutics,
a fictional pharma company.

## Phase 1 status

Phase 1 is closed. Three RBQM agents (rbqm.kri_monitor 2.1.0, rbqm.qtl_detector 2.0.0,
rbqm.csm 2.0.0) run on blinded cuts of the CDISC pilot study. The rule checks agree 100%
with condition truth on all 30 cuts and the detectors raise at most one false positive
on the clean cut, but detector recall is 0.67 against a target of 0.8: AE
under-reporting is not detectable on this data. The criteria were revised after step 4,
and the report shows the original ones next to the revised ones. See
[docs/phase1-summary.md](docs/phase1-summary.md), and
[docs/eval-phase1.md](docs/eval-phase1.md) for every number.

## Phase 2 status

Phase 2 is closed: see [docs/phase2-summary.md](docs/phase2-summary.md) for what
containment proves, costs, known gaps and lessons.

Step 1: agents run in NVIDIA OpenShell sandboxes, one per run, with no network, no ledger
and no credentials. Sandboxed drafts are byte-identical to in-process ones. See
[docs/openshell-notes.md](docs/openshell-notes.md) and
[docs/threat-model.md](docs/threat-model.md).

```sh
uv run meridian image build                  # docker build meridian-agent:<version>, print digest
uv run meridian policy render                # policies/*.yaml and profiles/ (committed)
uv run meridian sandbox verify               # in process vs sandbox, byte for byte
uv run meridian sandbox conformance --zone all   # each zone policy in a real sandbox
uv run meridian sandbox reap                 # delete tracked sandboxes left behind
uv run meridian agents run                   # openshell by default
uv run pytest -m openshell                   # needs openshell, a gateway and the image
```

`[runtime] executor` in config/default.toml picks the default executor: openshell, so a
plain `meridian agents run` runs every agent in a sandbox. The in-process executor is used
only when asked for (`--executor inprocess`), and only for evaluation cuts. Operational cuts
(`[cuts] operational`, DC-BASE) are processed only by the executor in
`[cuts] operational_executor` (openshell): the runner refuses any other one for them before
it records anything. `meridian agents run --executor inprocess --evaluation-only` runs
evaluation cuts in process and leaves operational cuts pending.

Step 3: the first LLM use. rbqm.narrator explains a finding in plain language for a
clinical operations reader; detection stays deterministic, and a reply is recorded only if
it passes validation against the evidence. It runs in its own sandbox
(policies/rbqm.narrator.yaml) whose only egress is the Anthropic Messages API, with the
key supplied by an OpenShell provider the user creates (see docs/openshell-notes.md).
Results: [docs/narrator-eval.md](docs/narrator-eval.md).

```sh
uv run meridian sandbox conformance --policy rbqm.narrator   # first, after any image build
uv run meridian agents narrate [--cut DC-BASE] [--max-calls 20]
uv run python scripts/narrator_eval.py      # counts, tokens, cost, accepted narratives
```

Step 4: evidence carries a role, direction, reference and display; detectors kri_monitor
2.2.0, qtl_detector 2.1.0 and csm 2.1.0 flag exactly what their previous versions did;
narrator 1.1.0 uses the displays, and its validator is measured on a labelled set.

```sh
uv run python scripts/detector_equivalence.py       # same findings on every cut
uv run python scripts/build_narrator_labels.py      # data/narrator_eval/labelled.jsonl
uv run python scripts/narrator_validator_eval.py    # validator 1.0.0 vs 1.1.0
uv run python scripts/narrator_rerun_table.py       # live 1.1.0 vs 1.0.0
```

## Phase 3 status

Phase 3 is complete: [docs/phase3-summary.md](docs/phase3-summary.md) (what was built, the
evidence by ledger seq, the evaluation including the misses, known gaps and lessons).

Step 1: the Fleet API, the read and decision layer for the Command Center. Development
identity only (headers checked against config/people.toml; not authentication), on
127.0.0.1. Contract: [openapi/meridian-api.json](openapi/meridian-api.json). Notes:
[docs/api-notes.md](docs/api-notes.md).

```sh
uv run meridian api serve            # http://127.0.0.1:8765/api/v1 (docs at /api/docs)
uv run meridian api openapi          # regenerate openapi/meridian-api.json
curl -s http://127.0.0.1:8765/api/v1/review-queue \
  -H 'X-Meridian-Actor: asha.rao' -H 'X-Meridian-Role: clinical_ops_lead'
```

Step 2: the Command Center (`web/`), a React client of the Fleet API built from the design
mock. It reads live API data only; the role switcher is development identity, not sign-in.
Run it with two commands, in two terminals, from the repository root:

```sh
uv run meridian api serve                      # the Fleet API on 127.0.0.1:8765
cd web && npm ci && npm run dev                # the Command Center on http://127.0.0.1:5173
```

Checks: `npm run typecheck`, `npm run lint`, `npm test`, `npm run check:api` (in `web/`).
The API types in `web/src/api/schema.ts` are generated from `openapi/meridian-api.json`
with `npm run gen:api`.

Step 3b: a local model (Ollama on the host, reached only from the safety.local_model
sandbox) judges whether two event terms are one event and explains safety findings with
subject ids: [docs/local-model-notes.md](docs/local-model-notes.md). Step 3b2:
safety.term_adjudicator 1.1.0 asks two models and says "yes" or "no" only when both do,
safety.local_narrator 1.0.1 has a host-written opening, and the terms fault set runs the
path end to end: [docs/local-model-eval.md](docs/local-model-eval.md).

```sh
uv run meridian agents adjudicate [--cut DC-...]      # term adjudicator, both models
uv run meridian agents narrate-local [--cut DC-...]   # local narrator
uv run python scripts/term_agreement_eval.py          # dev pairs: 1.0.0, agreement, unsure prompt
uv run meridian safety inject --set terms             # 10 cuts with recoded and look-alike terms
```

Step 4a: the first quality agent, quality.tmf_completeness 1.0.0 (deterministic): is the
Trial Master File complete and filed on time? It reads a synthetic eTMF index per cut
(data/etmf, metadata only; staged as /stage/tmf, which only the quality zone reads) and
blinded DM for site milestones. The expected records are config/tmf_expected.toml (TMF
Reference Model names, no numbers). The Command Center's Quality and audit item shows the
TMF page; the API serves `GET /api/v1/quality/{cut}/tmf`. Notes:
[docs/tmf-notes.md](docs/tmf-notes.md); evaluation: [docs/tmf-eval.md](docs/tmf-eval.md).

```sh
uv run meridian tmf generate                 # a clean eTMF snapshot for every recorded cut
uv run meridian tmf inject --set dev         # dev, holdout (30 cuts each) or clean (10)
uv run meridian tmf run [--set dev]          # in OpenShell sandboxes (DC-BASE without --set)
uv run meridian tmf eval --set dev           # precision and recall per check
PYTHONPATH=. uv run python scripts/tmf_stress.py   # the hand-written stress set (run once)
uv run python scripts/term_adjudicator_rerun.py    # term adjudicator 1.1.1 on the dev pairs
```

Step 4b: quality.audit_trail_reviewer 1.0.0 reviews the ledger itself from an audit
extract (identifiers and hashes only, staged as /stage/audit for policies/quality.audit_trail.yaml),
and the QA auditor raises observations that someone else decides. Nobody decides a finding
about their own actions. Notes: [docs/audit-notes.md](docs/audit-notes.md); evaluation:
[docs/audit-eval.md](docs/audit-eval.md).

```sh
uv run meridian audit review                      # one review of the whole ledger, sandboxed
PYTHONPATH=. uv run python scripts/audit_stress.py   # the hand-written audit stress set (run once)
```

## Setup

```sh
uv python install 3.14.10
uv sync
```

## Checks

```sh
scripts/check.sh            # every check CI runs locally; non-zero on the first failure
scripts/check.sh --no-web   # the same without web/
```

It runs ruff format --check, ruff check, mypy --strict (src, tests, scripts), pytest
without the slow and openshell marks, the policy render check, and in `web/` typecheck,
lint, tests and the generated API types check. A pre-commit hook in `.githooks/` runs it
before every commit. Enable it once per clone (this repository only, never `--global`):

```sh
git config core.hooksPath .githooks
```

Two commits (ae3a1fd, 52429fb) went in with a failing check before the hook existed. The
hook checks the working tree, not only what is staged, so stage everything you changed
or stash the rest. `git commit --no-verify` skips it; don't, unless the commit is known
to fail and says so.

## CLI

```sh
uv run meridian ledger init                 # create the SQLite ledger (WAL, STRICT, triggers)
uv run meridian ledger append-demo          # append a DataCut and one restricted FindingRaised
uv run meridian ledger show                 # JSON lines; restricted events are redacted
uv run meridian ledger show --since 2 --role unblinded_statistician
uv run meridian ledger verify               # exit 0 when the chain is intact, 1 when broken
```

## Data (phase 1, step 2)

```sh
uv run meridian data fetch                  # download the CDISC pilot SDTM files, hash-checked
uv run meridian data ingest --cut DC-BASE   # curate MRD-201, synthesize ops data, record a DataCut
uv run meridian data show --cut DC-BASE     # tables and a per-site summary (blinded and ops only)
uv run pytest -m slow                       # end to end against the real files (needs network)
```

A cut lives in `data/curated/MRD-201/<cut>/` with `blinded/`, `ops/`, `unblinded/` and a
`manifest.json`. Agents read cuts only through `ParquetStudyReader`, which cannot see
`unblinded/`. See [docs/data-notes.md](docs/data-notes.md) for what the data contains and
why the blinding split is what it is.

## Faults, agents and findings (phase 1, step 3)

```sh
uv run meridian faults inject --scenario config/scenarios/rbqm_core.toml   # DC-RBQM-CORE + ground truth
uv run meridian agents list                         # known agents and whether they are enabled
uv run meridian agents run                          # one pass over every pending DataCut
uv run meridian findings show --cut DC-RBQM-CORE    # one line per finding
uv run python scripts/calibration_report.py         # writes docs/calibration-step3.md
```

The fault injector writes a faulted copy of a cut that carries no trace of the faults;
what was planted lives only in `data/ground_truth/` (gitignored). Agents run on every
DataCut through the blinded `StudyReader` and can never import `meridian.faults`. See
[docs/rbqm-notes.md](docs/rbqm-notes.md) for what each check measures, and
[docs/calibration-step3.md](docs/calibration-step3.md) for the untuned starting point.

## Evaluation (phase 1, step 4)

```sh
uv run meridian eval run --cut DC-RBQM-CORE [--agent-version 2.0.0]   # needs ground truth
uv run meridian eval null --cut DC-BASE                               # every finding is a FP
uv run meridian eval suite      # null cuts, holdout, sweep, agents, scorecards, report
uv run meridian eval report     # rewrite docs/eval-phase1.md from the latest scorecards
uv run python scripts/tune_phase1.py   # the tuning loop (dev and null cuts only)
```

Scorecards are written once to `data/evals/<run_id>.json` (gitignored). The phase 1
result is in [docs/eval-phase1.md](docs/eval-phase1.md) and the tuning record in
[docs/tuning-phase1.md](docs/tuning-phase1.md). `meridian.evals` never imports agents.

## Rules, detectors and the AE experiment (phase 1, step 5)

```sh
uv run python scripts/ae_experiment.py   # the pre-registered AE variant experiment (dev cuts)
uv run meridian eval suite               # also holdout 2 and the AE holdouts, after the freeze
```

Each check is a rule (a threshold on a measured value) or a detector (a statistical
test), in `meridian.stats.checks`. Rule findings are scored against condition truth
computed from the cut; detector findings against planted faults. Cuts made from step 5
on have opaque ids: `DC-` plus 8 hex characters of the scenario file's sha256. The
experiment and its decision are in [docs/ae-experiment.md](docs/ae-experiment.md).

## Configuration

Settings come from `config/default.toml` and can be overridden by environment variables
prefixed `MERIDIAN_`, for example `MERIDIAN_LEDGER_PATH=/tmp/ledger.sqlite3`. Point at a
different TOML file with `MERIDIAN_CONFIG_FILE`. Precedence, highest first: constructor
arguments, environment, TOML file, field defaults.

## Conventions

* Append-only: ledger events, cuts, ground truth and scorecards are written once.
* Time comes from the injected Clock; randomness from seeded numpy Generators spawned
  from a SeedSequence in a fixed order.
* Every polars `group_by` whose output feeds a computation or a file either passes
  `maintain_order=True` or is followed by `.sort(...)` in the same chain. polars returns
  groups in arbitrary order, and in step 3 that order reached `np.polyfit` and made a
  cut non-reproducible. `tests/contract/test_group_by_order.py` enforces the rule.

## Layout

```
src/meridian/
  domain/    pure Pydantic v2 types; imports nothing from meridian outside domain
  ledger/    hashing (canonical JSON, SHA-256 chain), SQLite store, verifier
  data/      fetch, XPT reader, pure CDISC transform, ops synthesis, cut writer, readers
  faults/    fault catalog, scenarios, injector and FullCutReader (never imported by agents)
  agents/    agent protocol, registry, runner; rbqm, safety and quality agents
  stats/     robust z, quasi-Poisson, expected-AE variants, Poisson GLM, rule values and
             check types, shared by agents and evals
  evals/     matching, metrics, condition truth, scorecards and the phase 1 report
             (never imports agents)
  sandbox/   staging, zone policies, the agent image, executors (in process, OpenShell)
  tmf/       site milestones from DM and the synthetic eTMF index generator
  platform/  Clock, Settings, Container (explicit constructor wiring, no globals)
  cli.py     typer app exposed as the `meridian` console script
  worker.py  `meridian-worker`: one agent run on a staged cut (what runs in a sandbox)
docker/      agent.Dockerfile
policies/    one OpenShell policy per zone, rendered by `meridian policy render`
scripts/     calibration, tuning and AE experiment scripts (nothing in src imports them)
tests/
  unit/      validators, round trips, hashing, ledger behaviour, CLI
  property/  hypothesis: any draft sequence verifies; any flipped payload byte is caught
  contract/  repo rules (no U+2014, domain isolation, clock) and data rules (blinding, reader
             confinement, atomic and repeatable cuts, one DataCut per cut)
  slow/      real pilot data end to end (marked slow, needs network)
```

## Ledger design

* One `events` table, `STRICT`, in WAL mode. Triggers abort any `UPDATE` or `DELETE` with
  `ledger is append-only`, and a third trigger aborts any `INSERT` that does not extend the
  chain (next seq, correct prev_hash).
* Each event's hash is `sha256(prev_hash + canonical_json(event_without_hash))`. The genesis
  prev_hash is 64 zeros. `payload_json` is stored in exactly the canonical form that was
  hashed, and the verifier rejects any payload that is not canonical, so changing any single
  byte is always detected.
* Agent ids have the form `<zone>.<name>` (for example `rbqm.kri_sentinel`). The zone selects
  the finding id prefix, and the ledger issues ids as `<PREFIX>-0001`, `<PREFIX>-0002`, ...
* Readers that are not `unblinded_statistician` (including no role at all) get a
  `RedactedEvent` for every restricted event.
