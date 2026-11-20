# Input staging: getting a pass's inputs into the cluster (phase 4b step 4)

On GKE each run spent about 8 of its 10 s uploading its input archive from the laptop
through the one `kubectl port-forward` (about 140 KB/s in total, shared by concurrent
runs; docs/gke-plan.md section 9). This document measures what a pass uploads, compares
ways to move less, and records the choice. Subject-level safety inputs are part of every
pass, so for each option it says what rests where, for how long, and how the leak check
shows it is gone.

## A. What a full pass uploads today (measured, not estimated)

`agents run --executor zoned --rerun --all-cuts`: every enabled agent subscribed to
DataCut, on every recorded cut, staged exactly as the runner stages them (stage_run, the
same archive), counted on the laptop without running anything.

| | Value |
|---|---:|
| Runs (all on Substrate) | 910: rbqm 546, safety.sae_reconciler 182, quality 182 |
| Cuts | 182 |
| Bytes uploaded today (one whole archive per run) | **814.2 MB** (mean 895 KB, 809 to 942 KB) |
| Distinct input sets (the data files of a run, by sha256) | 476, 396.8 MB if each set went once |
| Distinct data files (by sha256) | **731, 12.3 MB** if each file went once |
| Per-run part (meta/: trigger.json and inputs_manifest.json) | 1.68 MB in all, about 1,850 bytes a run |
| Compression (gzip of one archive) | 942 KB to 816 KB: parquet is already compressed |

The inputs repeat at the level of files, not of sets: consecutive cuts change a few
tables and keep the rest byte for byte, and the zones share the blinded tables. A cache
keyed by whole input set would still move 397 MB; keyed by file, 12.3 MB plus about
5 KB of reference per run.

What each option moves, and the upload alone at 140 KB/s:

| Option | Moved from the laptop | Upload time |
|---|---:|---:|
| Today: one archive per run through the tunnel | 814.2 MB | about 97 min |
| Cache by input set | 396.8 MB + refs | about 47 min |
| **Cache by file, in the dispatcher's memory (chosen)** | 12.3 MB + about 5 KB a run | about 2 min |
| Cache by file, in a GCS inputs bucket | 12.3 MB (gcloud storage) + refs through the tunnel | 2 min or less |

Measured on kind after the change: 12,294,802 bytes of files, exactly the distinct set,
and 15.9 MB in all (section D).

## B. The options

### 1. A content-addressed store in the dispatcher's memory, one pass long (chosen)

The laptop splits each run's archive into its data files and its meta/. Each distinct
file goes once per pass into the dispatcher's MemoryInputStore, named by its sha256
(`PUT /v1/inputs/<sha256>`, after `POST /v1/inputs/missing` asks which it lacks). A run
sends a RunRef: the member list (path, size, sha256) and meta/ inline. The dispatcher
rebuilds the archive byte for byte, checks that its sha256 is the one the laptop computed,
and sends it to the actor as before. The actor's checks do not change: it verifies the
archive sha256 and the inputs manifest as in step 3, so the agent image is unchanged.

- **Full pass on GKE:** about 2 min of upload (12.3 MB), overlapped with the runs, and
  the runs at Substrate's own pace: about 1.8 s each inside the cluster at 12 at a time
  (section 9 of the plan), so 910 runs in about 2.5 min. **About 4 to 5 minutes**, against
  about 1.7 hours today.
- **What rests where, how long:** the dispatcher process's memory only. From a file's
  first upload to the end of the pass: DispatcherExecutor.drain() drops the store
  (`DELETE /v1/inputs`, every buffer overwritten with zeros, then released) and checks
  it reads empty; if the laptop never comes back, the store drops itself after 15 minutes
  without use. Never on disk (read-only root filesystem; nothing writes /tmp), never in a
  log (the request log names the path, so a digest, never a body). Capped at 256 MB in a
  pod limited to 1 GiB.
- **Limit, said plainly:** zeroing covers the store's own buffers. The copies Python makes
  while reading a request and building the tar are freed, not zeroed, and stay in the
  process's heap until reused or until the process exits. The kernel zeroes a process's
  pages before giving them to another. So "verified gone" is: the store reads empty after
  the pass (checked), and the process that held the inputs ends when the workloads stop
  (park.sh, teardown), or with `kubectl rollout restart` at the end of a session. GKE nodes
  have no swap; on kind the node is a container on the laptop, which holds the inputs
  anyway.
- **Leak check:** a new source, "dispatcher input store". Its positive control: during
  the pass the store held at least one of the files the canary was planted in (by sha256,
  read from the store's digest list, never its contents). Its canary check: after the
  pass's drop the store holds nothing at all. The dispatcher pod's filesystem and logs, the
  nodes, the bucket and Cloud Logging are searched as before.
- **IAM:** none. The dispatcher still holds no Google Cloud role.
- **What we lose:** a dispatcher restart in the middle of a pass loses the store; the
  next run answers 409 with the missing digests and the laptop sends them again (tested).
  The store is one more thing in the dispatcher's memory: 12.3 MB per pass today.

### 2. A separate GCS inputs bucket

The laptop uploads each distinct file once with `gcloud storage cp` to
`gs://<project>-meridian-inputs/<pass id>/<sha256>` (straight to Google, not through
the tunnel); runs send a RunRef through the tunnel; the dispatcher reads the blobs with a
read-only role on that bucket only, rebuilds and checks the archive, and the laptop deletes
the pass's objects at its end, with a 1-day lifecycle rule as a backstop.

- **Full pass on GKE:** the same 12.3 MB, uploaded at the laptop's uplink speed instead
  of the tunnel's (unknown: if the 140 KB/s was the uplink, no faster). About 4 minutes.
- **What rests where, how long:** subject-level inputs at rest in Cloud Storage
  (Google-managed encryption) from upload to the end-of-pass delete. If that delete never
  runs, the lifecycle rule removes them after at least a day, and Google applies lifecycle
  actions asynchronously, up to another day later. Two settings are required: **soft delete
  off** (`--soft-delete-duration=0`; by default Google keeps deleted objects 7 days and
  bills them) and object versioning off. Data Access audit logs, if ever enabled, record
  object names (digests), not contents.
- **Leak check:** the inputs bucket must list empty after the pass, including
  soft-deleted objects (`gcloud storage ls --soft-deleted`); positive control: a listing
  during the pass shows the planted files' digests. Plus everything option 1 searches,
  since the dispatcher still assembles archives in memory.
- **IAM:** a new bucket; the dispatcher's Workload Identity principal gets
  `roles/storage.objectViewer` on that bucket only. The step 3 check "the dispatcher holds
  no role" becomes "exactly this one binding". The dispatcher needs to reach
  storage.googleapis.com and the metadata server for its token. The laptop's user needs
  object create and delete there (you have it as owner).
- **What we lose:** a second place where subject-level data rests on disk, for minutes
  normally and up to about two days if the delete fails; a role on the dispatcher; one
  more resource in teardown and the billable check. kind cannot exercise the GCS path
  itself (it would need a fake), so its first real test would be on GKE.

### 3. Others considered

- **Cache by input set** (one blob per run's whole data): simpler keys, but 397 MB; the
  file is the unit that repeats.
- **More port-forwards in parallel:** helps only if the limit is the tunnel and not the
  uplink, and moves the same 814 MB; option 1 makes it moot.
- **Compression:** the inputs are parquet, already compressed (13% saved).
- **Inputs baked into a template's golden snapshot or an image:** the data would rest in
  the snapshot bucket or Artifact Registry for the life of the template. Rejected.
- **A Kubernetes Secret or ConfigMap:** stored in etcd at rest, 1 MiB per object.
  Rejected.
- **Running the runner in the cluster:** the ledger's only writer is the laptop runner;
  moving it is a different project, and the inputs would still have to get there.

## Recommendation

**Option 1, the in-memory content-addressed store in the dispatcher.** It moves the same
12.3 MB as the bucket without putting subject-level data at rest anywhere new, adds no
IAM role, and kind exercises all of it. The bucket's one possible advantage (bypassing the
tunnel) does not matter at 12.3 MB a pass. If inputs grow by orders of magnitude, option
2 is the fallback, with soft delete off.

## C. The implementation

- `meridian.sandbox.input_store`: the InputStore protocol (`missing`, `put`, `stats`,
  `drop`), MemoryInputStore (the dispatcher's), `split` and `assemble` (archive to RunRef
  and back, byte for byte), `archive_from` (shared with the laptop's `archive`).
- The dispatcher: `POST /v1/inputs/missing`, `PUT /v1/inputs/<sha256>` (refused unless
  the bytes hash to the name), `GET /v1/inputs` (digests and sizes, never contents),
  `DELETE /v1/inputs`, and `POST /v1/runs` taking a RunRef (409 with the missing digests).
  All authenticated as before; the idle backstop runs every 30 s.
- The laptop: DispatcherClient implements the protocol over HTTP; DispatcherExecutor
  sends each file once per pass (Uploader: a run that needs a file another run is already
  sending waits for it rather than sending it again), retries once after a 409, and drops
  and checks the store in drain(), which every pass and every leak check already calls.

## D. On kind (14 Nov 2026, 12 workers, as in step 3)

| Full pass, 910 runs | Step 3 (archive per run) | Step 4 (by content) |
|---|---:|---:|
| Wall clock | 81.5 s (83.5 s on its rerun) | 85.6 s, then **86.2 s** |
| Failed | 0 | 0 |
| Sent from the laptop | 814.2 MB | **15.9 MB**: 12.3 MB of files (731, each once) and 3.6 MB of RunRefs and calls, about 3.9 KB a run |
| Events appended | 1,820, then 0 | 1,820 (a new dispatcher image is new provenance), then **0** |

The first step 4 pass sent 13.9 MB of files: concurrent runs asked for the same file
before either had sent it. The Uploader now makes them wait for the one in flight; the
second pass sent exactly the 12,294,802 distinct bytes. On kind the pass is about 5%
slower (the dispatcher rebuilds each archive); the bytes are what matters on GKE.

**Proofs.**
- Same drafts byte for byte: all 910 runs of the first pass have the drafts sha256 and the
  inputs manifest sha256 of a recorded run of the same agent and trigger (0 differ). The
  substrate marked tests compare a run's drafts with the in-process drafts directly.
- The inputs manifest inside the actor: every run is checked by the executor (the archive
  sha256 and the inputs manifest sha256 the actor reports must equal the laptop's, or the
  run fails), so 0 failed of 910 is 910 matches.
- Nothing left after a pass, positive control first: the leak check (24 runs, all four
  zones) found 2 of the 2 planted files held in the store during the pass (16 blobs,
  3.3 MB), and the store empty after the drop; the dispatcher pod (11,269 files), its logs,
  the bucket, the node (103,096 files, 15.5 GB) and `var/` without the canary, each with its
  control found. Unit tests: the store refuses a blob not named by its sha256, zeroes its
  buffers on drop, drops itself when idle, and a dropped store is refilled once mid-pass.

## On GKE (measured, 14 Nov 2026, the second session)

| | Wall clock | Runs/s | Run p50 | Run p95 | Failed |
|---|---:|---:|---:|---:|---:|
| Bench, 200 runs, 1 worker node, 12 workers, 12 at a time | 74.3 s | 2.69 | 3.1 s | 22.0 s | 0 |
| Bench, 200 runs, 2 worker nodes, 24 workers, 12 at a time | 62.9 s | 3.18 | 2.6 s | 20.8 s | 0 |
| Bench, 200 runs, 2 worker nodes, 24 workers, 24 at a time | 50.9 s | 3.93 | 3.4 s | 28.4 s | 0 |
| **Full pass, 910 runs**, 24 at a time, 2 worker nodes | **270 s** | 3.37 | 6.5 s | 11.8 s | 0 |
| Rerun, 910 runs, 12 at a time: **0 events appended** | 453 s | 2.01 | 5.6 s | 9.4 s | 0 |

- Bytes sent per pass: 15,865,788, of which 12,294,802 are the 731 distinct files, each
  once (the same as kind). The first session's rate projected about 1.7 hours for this pass.
- A second worker node shortens the in-cluster phases (at 12 at a time, request p50 467 to
  271 ms, resume 920 to 730 ms) but does not lift the limit: every run's `tunnel` phase is
  about 1 s at p50 and about 19 s at p95 on one node or two. The single port-forward is the
  ceiling now, 2 to 4 runs/s against kind's 10 to 11.
- After each pass the dispatcher was restarted (the old pod confirmed deleted, so freed
  copies of inputs went with its process) and the leak check rerun on the new pod: pass.
  One attempt failed its own control: the dispatcher pod's tar stream was cut (5,312 of
  11,267 files) when the cluster autoscaler removed an underused Spot node at the same
  moment (a konnectivity agent most likely went with it); the canary was found nowhere, and
  the rerun passed. Scale the WorkerPool back before a leak check, and expect a node scan
  pod not to fit on a node at 98% of its CPU requests.
- Core dumps: GKE's containerd leaves RLIMIT_CORE unlimited and the node's core_pattern is
  a file path, so the dispatcher now starts under `ulimit -c 0` (soft and hard 0, checked
  after every restart). No node has swap (kubelet reports 0 bytes, /proc/swaps empty).
