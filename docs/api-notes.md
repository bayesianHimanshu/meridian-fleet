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
* `policy_sha256`, the hash of the policy the sandbox reported as effective,
* `inputs_manifest_sha256`, the hash of the staged inputs the worker verified.

To check: `docker image inspect` the image and compare digests; recompute the effective
policy hash with `meridian sandbox` tooling (`policies.expected_effective`); compare the
policy file's own hash with `GET /fleet`; and confirm every event in between with
`GET /ledger?since=` and `GET /ledger/verify`.

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
