# Fleet API notes (phase 3 step 1)

The Fleet API (`meridian api serve`, `src/meridian/api/`) is the trusted read and decision
layer between the ledger and the Command Center. Agents raise findings; people close them
here. It runs on the host, binds to 127.0.0.1, and never updates or deletes anything.

## Why visibility lives in one module

Who may see what is decided in `api/visibility.py` and nowhere else: a finding is visible
when its `visible_to` holds the caller's role and, if restricted, only to the unblinded
statistician; ledger events are redacted exactly as `SqliteLedger.read(viewer_role=...)`
redacts them; a narrative or a decision is visible exactly when its finding is.

* **One rule, one place to review.** If each endpoint filtered on its own, the rules would
  drift: the review queue might hide a restricted finding while the portfolio count still
  counted it. Here `Repository` builds one snapshot per request through `visibility`, and
  every endpoint reads that snapshot.
* **Enforced, not hoped for.** `tests/contract/test_api_layering.py` fails if a route
  handler imports the ledger or the visibility module; only `deps.py` and `repository.py`
  touch the store. The property test (`tests/api/test_visibility.py`) builds random
  ledgers and checks that no GET response for any blinded role contains a string found only
  in restricted payloads, finding ids included.

## Why a hidden finding is 404, not 403

A 403 says "it exists, but not for you". For a restricted finding that is already a leak:
a blinded user could probe ids (DSMB-0001, DSMB-0002, ...) and learn how many unblinded
findings there are, and when they appear. So a finding the caller may not see answers
exactly like one that does not exist: 404 with the same body, `finding not found`, for
reads and decisions alike. 403 is kept for what is public anyway: qa_auditor may see a
finding but may never decide, and a role outside RBQM may not open the site heatmap.

## Why the ledger redacts events about findings you may not see

Until phase 3 step 3a the ledger view redacted only restricted events, so a blinded finding
outside a role's visible_to (an RBQM finding, for portfolio_lead) still showed in full in
the event list while its detail page was 404. Safety findings made that a real leak: a
subject-level reconciliation finding names the subject. Now the visibility module also
redacts every FindingRaised, narrative and decision whose finding the caller may not see,
in the same redacted form as a restricted event (seq, type, time and hashes, so the chain
can still be checked). For the roles RBQM findings are for, the ledger is unchanged.

## Why decisions are events, not status updates

There is no status column. Status (open, accepted, escalated, dismissed) is derived from
the latest HumanDecision for the finding, and each decision is a new hash-chained event
with the person, the role and the reason.

* **History is the record.** "Escalated by the medical monitor, then dismissed by clinical
  ops with this reason" survives; an update would keep only the last word.
* **Tamper-evident.** The decision is in the hash chain; `GET /ledger/verify` (any role)
  recomputes it.
* **The finding is never changed.** Detection output stays exactly what the agent wrote.
* **Concurrency without a lock table.** `SqliteLedger.append_guarded` runs the rule check
  ("is the latest decision accepted or dismissed?") inside the append's `BEGIN IMMEDIATE`
  transaction, so of two simultaneous decisions one is written and the other gets 409,
  across threads or processes.

## How a reviewer traces a finding to its image and policy

`GET /api/v1/findings/{id}` returns `run`: the ActorWoke written before the finding and the
ActorSuspended written after it, for the same agent, version and DataCut (the runner always
writes them in that order). The ActorSuspended carries:

* `executor` (`openshell` for a sandboxed run),
* `image_digest`, the content digest of the agent image the sandbox ran,
* `policy_sha256`, the hash of the policy the sandbox reported as effective (the API
  serves it as `applied_policy_sha256`),
* `policy_file` and `policy_file_sha256`, the committed file passed to
  `openshell sandbox create` and the hash of its bytes (runs from step 3a polish on; null
  on earlier runs),
* `inputs_manifest_sha256`, the hash of the staged inputs the worker verified.

To check: `docker image inspect` the image and compare digests; compare the run's policy
pair with the same pair on `GET /fleet` (both labelled "policy file sha256" and "applied
policy sha256" in the UI); and confirm every event in between with `GET /ledger?since=`
and `GET /ledger/verify`.

### What each policy hash is computed over

* **policy file sha256** (`GET /fleet` `policy_file_sha256`, a run's
  `policy_file_sha256`): sha256 of the bytes of `policies/<name>.yaml`, the file
  `meridian policy render` writes and the executor hands to `openshell sandbox create`.
  It includes the header comment, which names the zone or agent.
* **applied policy sha256** (`GET /fleet` `applied_policy_sha256`, a run's
  `applied_policy_sha256`, a containment check's `applied_policy_sha256`): sha256 of what
  `openshell sandbox get --policy-only` reports for the live sandbox. OpenShell 0.1.7 drops
  comments and an empty `network_policies`, so this is the policy in effect, not the file.
  The executor refuses a run whose reported policy differs from
  `policies.expected_effective` for its file; the fleet page's value is that expected text's
  hash, so a run's value equals the fleet's when the sandbox applied the committed policy.

Until phase 3 step 3b every policy without network rules rendered the same rules and
differed only in its comment, so all eight had one applied hash, 01637db6...b0805d. Since
step 3b each policy lists only the staged areas its zone may read, so the applied hashes
differ where the rules differ: five blinded zones share one, safety and
safety.sae_reconciler another, unblinded, rbqm.narrator and safety.local_model each their
own (docs/local-model-notes.md). A test
(`tests/unit/test_executor.py`) checks that a run records the hash of the exact file passed
to create and the hash the sandbox reported, that a comment-only change moves only the file
hash, and that a rule change moves both.

## Run counts on the fleet page

`GET /fleet` gives each agent `runs_7d`, `failed_7d` and `last_failure_at`, over every
version of the agent. A detector run is an ActorWoke; it failed when no ActorSuspended for
the same agent, version and trigger follows it before that key's next ActorWoke (so a
retry's success never closes the failed attempt before it). A narrator run is a
FindingNarrative; it failed when its status is `error`. A started detector run without its
ActorSuspended is `running` (last_outcome) and not counted as failed until
`[runtime] exec_timeout_seconds` after its ActorWoke (step 4a); after that it is a failed
run (`not_completed`). `run_event_types` and `GET /ledger?agent=<id>&type=...` give the
filtered ledger the fleet page links to. The agent filter matches only events the caller
reads in full, so it never tells which agent a redacted event belongs to. Failed events are
not changed or hidden: they are the ActorWoke events with no ActorSuspended after them.

## Before this could use real authentication

The identity headers are claims, not proof: anyone who can reach the port can be anyone in
`config/people.toml`. Every response says so (`X-Meridian-Auth: development`). Before real
use:

1. **Authenticate.** Replace the headers with tokens from an identity provider (OIDC), with
   signature, issuer, audience and expiry checks; derive the actor from the token subject.
2. **Authorize from the directory, not the client.** Roles come from the identity provider
   or a server-side mapping, never from a request header; a person with two roles picks one
   per session, and the choice is logged.
3. **Transport and sessions.** TLS, CSRF protection for the browser client, short sessions,
   and no wildcard CORS.
4. **Audit and signature.** Record authentication events; for decisions that matter
   (21 CFR Part 11 style), a re-authenticated, signed decision with meaning ("approved").
5. **Rate limits and lockout,** and the 404 rule kept for every new endpoint.
6. **Move off a local SQLite file** for multi-user use, keeping append-only semantics and
   the guarded append's atomicity.

## Known gaps

* **Sweep cut ids name their fault.** The phase 1 sweep cuts carry the fault and its
  magnitude in their id (for example DC-SWEEP-DISCONTINUATION-DRIFT-PLUS0P1), and the API
  serves cut ids everywhere. That goes against the rule that the API never exposes fault
  labels; only opaque ids (DC- plus a hash) honour it. Not fixed in phase 3 step 2; see
  the backlog item below.

## Backlog

* **Cut ids from a canonical scenario form.** Derive every evaluation cut id from a
  canonical form of its scenario (parsed, sorted, normalised) rather than from the raw file
  bytes or a readable name, and re-issue the sweep cuts under such ids, keeping the old
  names only in ground truth.

## Subjects of reconciliation items

GET /safety/{cut}/reconciliation reads the ledger only. An item's subject_id is the first
USUBJID in its finding's evidence. From safety.sae_reconciler 1.1.0, the evidence names the
clinical record (USUBJID, AESEQ) and the safety record (case id, version, USUBJID), so every
subject-level item has its subject, late reports included. Late-report findings of 1.0.0
named only the case, so they have subject_id null; 1.1.0 supersedes them. The site-level
finding never has a subject. (Step 3a's lookup in the cut's safety case table is removed.)
From 1.1.1 a late report's evidence also names the case (id, version, subject) as its own
line and, when the case pairs with a clinical SAE, that record (USUBJID, AESEQ).

## Display of times and origins

Titles, summaries and evidence displays keep ISO 8601 in the ledger and the API. The web
client shows each instant in them as "20 Jan 2013, 22:02 UTC", with the ISO value in the
element's title attribute. A finding opened from the Safety, RBQM, ledger or review queue
page links back there (the origin travels in the link's history state); otherwise it links
to the review queue.

## Assessments and local narratives (phase 3 step 3b)

A finding's detail carries `assessments`: FindingAssessment events of
safety.term_adjudicator about it (and, for a pair, one other finding). An assessment is
indexed under every finding it names and shown only when the role may see all of them; a
rejected or failed one carries its status and reason codes, never the model's text. Each
narrative carries `narrator_id` (rbqm.narrator or safety.local_narrator) and
`model_digest` (the local model's served digest; null for the cloud narrator). The fleet
lists safety.term_adjudicator (role adjudicator) and safety.local_narrator under policy
safety.local_model, each counted from its own events.

Step 3b2: an assessment of term adjudicator 1.1.0 (the agreement rule) also carries
`answers`, one per model asked: model, digest, status, verdict and reason (only when that
answer passed validation) and `text`, the raw answer as received. The page shows both raw
answers under the agreed verdict. `model` is the two models joined by "+" and
`model_digest` is null; each answer has its own digest. 1.0.0 assessments have no answers.

## TMF completeness (phase 3 step 4a)

`GET /quality/{cut}/tmf` serves the TMF of one cut: completeness and timely filing per site
and for the trial, the expected-against-filed matrix (one cell per site and site-level
artifact, plus the trial-level list) and the current quality.tmf_completeness findings the
caller may see, each linked from its cell. Unlike the reconciliation view, it reads data as
well as the ledger: the matrix is computed by the agent's own `assess()` from the cut's
eTMF snapshot and blinded DM (site milestone dates only), so the page and the agent cannot
disagree. Who may call it is decided in the visibility module (`can_see_tmf`): exactly the
roles in the agent's `visible_to` (clinical_ops_lead, medical_monitor, qa_auditor,
portfolio_lead). Others get 403, as for the RBQM site metrics; a cut without an eTMF
snapshot is 404. Nothing in it names a subject.
