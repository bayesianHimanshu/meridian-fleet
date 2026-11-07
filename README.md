# Meridian Agent Fleet

A learning project: a fleet of clinical-trial oversight agents for Meridian Therapeutics,
a fictional pharma company.

Phase 1, step 1 is the foundation only: project scaffold, domain types, and an
append-only, hash-chained event ledger. There are no agents and no data ingestion yet.

## Setup

```sh
uv python install 3.14.10
uv sync
```

## Checks

```sh
uv run ruff format
uv run ruff check
uv run mypy src
uv run pytest
```

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

## Configuration

Settings come from `config/default.toml` and can be overridden by environment variables
prefixed `MERIDIAN_`, for example `MERIDIAN_LEDGER_PATH=/tmp/ledger.sqlite3`. Point at a
different TOML file with `MERIDIAN_CONFIG_FILE`. Precedence, highest first: constructor
arguments, environment, TOML file, field defaults.

## Layout

```
src/meridian/
  domain/    pure Pydantic v2 types; imports nothing from meridian outside domain
  ledger/    hashing (canonical JSON, SHA-256 chain), SQLite store, verifier
  data/      fetch, XPT reader, pure CDISC transform, ops synthesis, cut writer, readers
  faults/    fault catalog, scenarios, injector and FullCutReader (never imported by agents)
  agents/    agent protocol, registry, runner, and the rbqm agents
scripts/     calibration_report.py (reads only; nothing in src imports it)
  platform/  Clock, Settings, Container (explicit constructor wiring, no globals)
  cli.py     typer app exposed as the `meridian` console script
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
