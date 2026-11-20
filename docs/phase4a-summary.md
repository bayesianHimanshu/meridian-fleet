# Phase 4a summary: the no-network zones in Agent Substrate

Phase 4a moved the four zones without network rules out of OpenShell sandboxes into Agent
Substrate v1.0.0 actors (gVisor), one fresh actor per run cloned from a golden snapshot,
and ran independent runs in parallel. The narrator and the local model, the only zones with
network rules, stay on OpenShell 0.1.7. Phase 3 ended at seq 4,558; phase 4a is seq 4,559
onwards and closes at 8,458 events, `meridian ledger verify` ok.

| Step | PR | What it added |
| --- | --- | --- |
| 1 | 23 | Design from the Substrate source (docs/substrate-design.md Parts 0 and 1) |
| 2 | 23 | SubstrateExecutor, `meridian-worker serve`, zoned executor, five proofs, measurements |
| 3 | 24 | Runner identity and suspend guard, parallel runner, 512 MiB workers, image source, full pass |

## What moved

- **rbqm, safety.sae_reconciler, quality and quality.audit_trail** run in Substrate
  (`[runtime.executors]`); rbqm.narrator and safety.local_model stay on OpenShell. The
  choice is per policy in config, one Executor protocol for both.
- **A run** stages only its zone's folders on the host, posts them as one tar to a new
  actor (`meridian-worker serve` warm in the golden snapshot), reads the drafts back, checks
  the actor holds no snapshot but its golden one, and deletes it.
- **Traceability:** ActorWoke and ActorSuspended carry the release and commit, template
  and spec hash, committed file and hash, image digest and the commit it was built from,
  pool, sandbox class, golden snapshot, actor and worker pod; shown on the fleet page and in
  a finding's traceability. ActorWoke is appended after the run and carries its real start
  (`started_at`), which audit reviewer 1.2.0 and the fleet page time runs from.
- **The runner** runs as its own identity (atespace editor only) and in parallel, capped
  at the pool's free workers, with the ledger still written by the host alone, in
  (trigger seq, agent) order.

## The proofs and their evidence

| Proof | Result | Evidence |
|---|---|---|
| C1 no data at rest | canary in 20 runs found in no snapshot, node file or var/; positive control finds it | docs/substrate-eval.md C1, var/substrate/canary.json |
| C2 same results | 12 of 12 drafts byte-identical to OpenShell | `meridian substrate verify` |
| C3 no network | 4 zones: TCP to 192.0.2.1, the host, another actor, the cluster API refused, each with a gateway 403 line | seq 4559 to 4586 (step 2 image), 4614 to 4641 (final image) |
| C4 zone folders | an actor receives only its zone's areas | CI test, every zone |
| C5 DC-BASE through Substrate | same findings; reviewer 1.1.0 raised only the 7 phase 1 findings | seq 4587 to 4606 |
| Suspend refused | runner guard refuses suspend, revert, tag; Substrate refuses the identity outside its atespace | seq 4607 to 4613 |
| Parallel determinism | sequential and five jittered parallel passes give the same ledger | tests/unit/test_parallel_runner.py |
| Full pass | 910 Substrate runs on the final image, none failed; audit review 1.2.0 raised only the 7 phase 1 findings | seq 6462 to 6628 (OpenShell), 6629 to 8458 |

## Measurements against OpenShell (rbqm.kri_monitor on DC-BASE)

| | OpenShell | Substrate |
|---|---:|---:|
| One run, p50 / p95 | 10.09 s / 10.20 s (phase 2 baseline about 8 s) | 1.20 s / 1.30 s |
| Most at once on this laptop | 1 (sequential runner) | 16 (512 MiB workers), 200 runs, none failed |
| At 16 at a time, p50 / p95 | | 3.52 s / 5.57 s |
| A run after a few idle minutes | | about 2.8 s once (image cache eviction) |
| Memory per worker pod | | 20 to 55 MB idle, 289 MiB peak |

Full pass, final image (1dfee1b, `sha256:ee5a9a2b...`), each step alone, stop at 1.5 GB:

| Step | Where | Runs or calls | Failed | Wall-clock |
|---|---|---:|---:|---:|
| Four zones, every cut (`agents run --rerun --all-cuts`) | Substrate, 12 at a time | 910 runs | 0 | 198.4 s |
| Narrator (`agents narrate`) | OpenShell | 20 accepted, 187 over budget | 0 | 35.5 s |
| Term adjudicator (`agents adjudicate`) | OpenShell | 15 accepted | 0 | 86.8 s |
| Local narrator (`agents narrate-local`) | OpenShell | 104 of 173 (82 accepted, 22 rejected) | stopped 3 times | |
| Audit review 1.2.0, seq 1 to 8448 | Substrate | 1 run, 7 findings | 0 | 4.1 s |

Substrate runs: p50 2.56 s, p95 3.07 s; memory never below 1.96 GB; every ActorWoke names
1dfee1b and has `started_at`. Narrator and adjudicator ran earlier in the pass on the
previous image (from 81953dc). The 7 review findings (QA-0164 to QA-0170) are phase 1 rbqm
runs on DC-BASE without a sandbox record, as in C5. 69 local narrations of evaluation
findings stay pending for a later pass.

**On this laptop the local model and the cluster cannot run together.** llama-server
(phi4-mini) peaks at 3.65 GB RSS. With it loaded, memory fell below the 1.5 GB stop in 90 to
110 s: to 1.55 GB with 2 workers, 1.40 GB with the pool at 0 and 8 requests per sandbox
(swap full throughout). Model unloaded, 12 workers running left 1.96 GB.

## What Substrate does not give us, and the compensating controls

- **No per-path read allow-list.** Control: the host sends an actor only its zone's
  folders, and the actor refuses any other area (C4).
- **No way to forbid suspend to an identity that can run actors** (v1.0.0 roles are owner,
  editor, viewer; model.fga gives editors suspend, revert and tag). Controls: the runner's
  client refuses those verbs before sending them; the per-run snapshot check; the canary.
- **No record of refused name lookups.** The relay answers NXDOMAIN without a log line;
  recorded from the probe (`source = "harness"`).
- **No queue.** Placement fails at once when no worker has room; the runner queues per
  executor and retries a resume for up to 120 s.
- **Snapshots are not encrypted;** acceptable only because no data snapshot exists.

## Open gaps

- The suspend guard is code in the runner's process; a compromised host process with the
  token could still suspend (Substrate would allow it).
- The kind node's disk is 89% full, so atelet evicts the agent image after idle minutes;
  fixing it means freeing disk or changing atelet's flags in ate-system, the user's call.
- Throughput stops rising past about 12 concurrent runs: each run starts a `kubectl ate
  proxy` process on the host. A long-lived router connection would remove it.
- The image is built from a clean commit, but host-only changes after it are not in the
  image; `image_source_commit` names the image's sources, not the host's.
- The bootstrap identity (`ate-client`) is still a global owner, used by deploy.
- The local model and the cluster do not fit in memory together (above).

## Lessons

1. **Read the authorization model, not the role names.** "AccessPolicy" suggested fine
   grants; model.fga showed suspend is part of editor. An hour in the source settled it.
2. **A positive control is what makes a negative search mean something.** The first canary
   search found nothing because it could not read the snapshot format.
3. **Look for the per-phase timing the platform already logs.** atelet's restore breakdown
   named the slow phase at once; guessing "first restore" was wrong.
4. **Predict from a mechanism, then test it.** The GC schedule told which idle gap would be
   slow; one bench confirmed it.
5. **Keep the ledger single-writer and order commits, not runs.** Runs in parallel, appends
   in a fixed order: the same pass, the same ledger.
6. **Measure what actually limits concurrency.** At 1 GiB it was the pods' reservations;
   at 512 MiB it was host memory; throughput was limited by host CPU all along.
7. **Make immutable names carry everything that can change.** Templates are immutable;
   the name now carries the image digest and the actor size.
8. **Refuse at the tool boundary what the platform cannot refuse,** and say plainly
   that it is a weaker control.
