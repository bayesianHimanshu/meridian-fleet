# Architecture

This is the architecture as it stands at the wrap-up, with the decision behind each part
and the phase summary that records it. The diagram is in the [README](../README.md#how-it-fits-together).

## The parts

| Part | What it is | Where |
|---|---|---|
| Domain | Pydantic v2 types for events, findings, decisions and identity; imports nothing else from the project | src/meridian/domain |
| Ledger | One SQLite table, STRICT, WAL; triggers refuse UPDATE, DELETE and any INSERT that does not extend the chain; a verifier | src/meridian/ledger |
| Data | The CDISC pilot as MRD-201 (blinded, ops and unblinded folders); SEND for MRD-101; cut writers; per-zone readers | src/meridian/data |
| Agents | A protocol (a pure function of a trigger and a reader, returning drafts), a registry, and the runner | src/meridian/agents |
| Staging and policies | Per run, only the zone's folders and an inputs manifest; one policy per zone, rendered and proved | src/meridian/sandbox, policies/ |
| Executors | In process (evaluation only), OpenShell 0.1.7, Agent Substrate v1.0.0 through the dispatcher, chosen per policy (`[runtime.executors]`) | src/meridian/sandbox |
| Dispatcher | In the cluster: the only Substrate identity; holds inputs in memory by content; refuses suspend, revert, tag and proxy | src/meridian/dispatcher_serve.py, deploy/dispatcher |
| Fleet API | FastAPI on 127.0.0.1; reads the ledger, records people's decisions; visibility and who may decide in one module | src/meridian/api |
| Command Center | React client of the API; generated types; role switcher for development identity | web/ |
| Evaluation | Matching, metrics, scorecards, stress sets, leak checks; never imports agents | src/meridian/evals, scripts/ |

## The decisions, and why

**Agents are pure functions; the runner owns everything else.**
- An agent gets a trigger event and a reader over its staged inputs, and returns drafts.
- The runner wakes it, records ActorWoke and ActorSuspended with the full provenance
  (image digest, policy hash, inputs manifest hash, template and snapshot), and issues
  finding ids.
- Dedup keys make a rerun append nothing.
- Agents never read the ledger: the host writes what they need (the NOAEL extract, the
  audit extract, the portfolio facts) into their zone's folder.
- Sources: phase 1 and phase 4a.

**Agents raise and people decide.**
- A decision is a HumanDecision, NoaelDecision or GateDecision event, under the person's
  own id, recorded only through the API.
- The rules about who may decide live in `src/meridian/api/visibility.py`:
  - qa_auditor never decides;
  - nobody decides a finding about their own actions;
  - the NOAEL is the toxicologist's;
  - the gate is the portfolio lead's.
- Report agents (the outlook, the gate pack) write events that nobody decides; the gate
  pack has no verdict field.
- Sources: [phase3-summary.md](phase3-summary.md) and [phase5-summary.md](phase5-summary.md).

**Identity is typed.**
- An actor is a person (config/people.toml), an operator (`operator:<os user>`), an agent
  or a system component.
- The CLI refuses a person's id.
- The audit reviewer flags any event that names a person but was not written by the API.
- This was added after the attribution incident (phase 5 step 3b).

**Containment is by infrastructure.**
- Staging sends a zone only its folders; the actor or sandbox refuses anything else.
- There is no network, except the narrator's Anthropic endpoint, the local model's
  address and the signal scout's two openFDA paths, each through its own policy.
- What is proved, and how:
  - policies are proved against a boundary in CI (openshell-prover);
  - conformance runs in real sandboxes, with refusals logged by the platform where it logs
    them;
  - leak checks plant a canary and search every store, each with a positive control.
- What the platforms cannot do is said plainly:
  - OpenShell 0.1.7 does not log Landlock refusals;
  - Substrate v1.0.0 cannot deny suspend to an editor;
  - Substrate does not log DNS refusals.
- Sources: [threat-model.md](threat-model.md), [phase2-summary.md](phase2-summary.md),
  [phase4a-summary.md](phase4a-summary.md) and [phase4b-summary.md](phase4b-summary.md).

**The ledger has one writer, and runs are ordered.**
- Runs go out in parallel (up to the pool's free workers), but their events are appended in
  (trigger seq, agent id) order.
- Sequential and jittered parallel passes give the same ledger, which is tested.

**Evaluation is designed to be able to fail.**
- Targets are pre-registered and frozen before holdouts, and the commit is named.
- Rules are scored against condition truth, and detectors against planted faults.
- Every stress set states who wrote it.
- Sources: [phase1-summary.md](phase1-summary.md) and every *-eval.md.

## Where it differs from the plan, and why

| Planned | Now | Why |
|---|---|---|
| preclinical.translational_bridge on the local model (phase 3 design) | Deterministic rules with a cited mapping (config/translational_monitoring.toml) | Mapping a target organ to standard monitoring is a rule with sources, not a judgement a small model should make; an unmapped organ is reported, not guessed ([translational-notes.md](translational-notes.md)) |
| Every zone in Agent Substrate (phase 4a brief) | The narrator, the local model and the signal scout stay on OpenShell | They are the only zones with a network rule (and the narrator a provider). Their OpenShell containment was already proved: an endpoint, a binary and a method per rule, logged denials, and a provider that keeps the API key out of the sandbox. Substrate could inject a key too, by TLS interception, but that would have needed proving again. Decision 4 of [substrate-design.md](substrate-design.md) Part 2 keeps them on OpenShell, and `meridian.sandbox.zoned` refuses Substrate for any policy with a network rule or a provider |
| The ledger in the cluster, or a shared database (implicit in a GKE move) | The ledger stays on the laptop, one writer | The ledger is the evidence. One writer and a fixed append order make passes reproducible, and the verifier needs nothing but the file. The cluster gets inputs, never the ledger ([phase4b-summary.md](phase4b-summary.md)) |
| The laptop's runner as a Substrate identity with a client-side guard (phase 4a) | One in-cluster dispatcher, the only member of the atespace and of the router's allow-list (phase 4b) | A guard in the laptop's process was the weakest control. In the cluster the refusals sit next to the platform, and the laptop holds no Substrate identity |
| A GCS bucket for each pass's inputs (phase 4b plan) | Inputs by content, in the dispatcher's memory, dropped after each pass | Measured first: inputs repeat by file (731 files, 12.3 MB), not by set (476 sets, 397 MB), so memory was enough and nothing touches disk ([input-staging.md](input-staging.md)) |
| GKE as the place it runs | GKE for two measured sessions, then torn down; kind for everything else | Cost and proof: the sessions proved the design on real nodes for about USD 3.50; a parked cluster costs USD 2.43 a day |
| One LLM narrator that also checks itself | Host-side validators; two-model agreement for yes/no answers; host-written openings | The validator's misses and the models' failure to abstain were measured (phases 2 and 3) |
| Cut ids naming their scenario | Opaque cut ids from phase 1 step 5 (`DC-` plus 8 hex of the scenario's sha256) | Sweep ids named their faults; agents never read cut ids, but a reviewer should not either (backlog: from a canonical scenario form) |
| An outlook listing every fact | At most 5 risks per asset, the rest counted (pipeline_intelligence 1.1.0, wrap-up) | The first outlook listed 93 open findings for MRD-101 |

## How a run goes, end to end (Substrate)

1. **The trigger.** A trigger event is on the ledger: a DataCut, SendStudyLoaded,
   NoaelDecision, InventoryLoaded, review or gate request, or audit request.
2. **Planning.** The runner finds each pending (agent, trigger) pair. A request is answered
   once; other triggers are rerun by a new agent version. The runner refuses an in-process
   executor for operational data.
3. **Staging.** The host stages the zone's folders and writes an inputs manifest. For the
   portfolio, it first builds the facts as the portfolio lead (through the Repository, so
   visibility applies).
4. **The dispatcher.** The runner sends the run to the dispatcher through one port-forward,
   with a bearer token and an HMAC proof. The dispatcher rebuilds the archive from its
   in-memory store, checks its sha256, clones a fresh actor from the zone's golden
   snapshot, and posts the archive.
5. **The actor.** The actor checks the manifest, runs the agent and returns the drafts.
6. **Checks and teardown.** The dispatcher checks that the actor holds no snapshot but its
   golden one, and deletes it.
7. **Recording.** The runner checks each draft's dedup key and visibility, then appends
   ActorWoke, the findings or the report event, and ActorSuspended, in order.

## Operating notes

- **Changing the image.** Rebuild the image (`meridian image build`, from a clean tree),
  then run `meridian substrate deploy` (new templates, named by digest and actor size),
  then run conformance.
- **The monthly portfolio review.** `meridian portfolio review-due` is meant for a cron
  line, which the operator installs.
- **The numbers.** `meridian stats` gives every number in this documentation, from the
  ledger.
