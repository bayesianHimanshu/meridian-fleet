# Local model notes (phase 3 step 3b)

## Why patient-level data stays local

A safety finding names a subject (USUBJID), a case id and dates of a serious adverse
event. The cloud narrator never receives one (rbqm.narrator's EXCLUDED_ZONES). The local
model runs in Ollama on this machine, so a safety finding can be explained with its
subject id in it: the request goes from a sandbox to the host and no further. The two
agents that use it are narrow. safety.term_adjudicator judges whether two event terms
are one event; safety.local_narrator writes a summary a medical monitor reads.

## How the sandbox reaches the host model, and nothing else

* **Two containers.** An OpenShell 0.1.7 sandbox on the Docker driver is two containers.
  The agent's container has network mode `none`: its only interface is its own `lo`, so
  127.0.0.1 inside it is the sandbox, not the host. The supervisor's container runs in
  the host network namespace and holds the policy proxy, which is how all egress leaves.
* **Loopback cannot be reached.** The proxy refuses loopback, link-local and unspecified
  addresses whatever a policy says ("falls within always-blocked range"). A private
  address passes only when the rule lists it in `allowed_ips`. So Ollama listens on the
  docker0 address only, `OLLAMA_HOST=172.17.0.1:11434` (a systemd override set by the
  user), never 0.0.0.0. ufw drops 11434 inbound on the LAN interface and on docker0, so
  neither another machine nor another container reaches it. The proxy connects from the
  host namespace, which ufw's inbound rules do not stop.
* **One address, two requests, one binary.** policies/safety.local_model.yaml (zone
  safety_local) has one rule: host 172.17.0.1, port 11434, `allowed_ips` 172.17.0.1/32,
  `protocol: rest` (the proxy reads plain HTTP), GET /api/tags and POST /api/chat only,
  for /usr/local/bin/python3.14 only. Every other Ollama endpoint gets 403
  `policy_denied` from the proxy (pull, delete, show, ps, version).
* **Reads.** Of the staged areas, it reads only /stage/lm, the job the host wrote.
* **Conformance.** `meridian sandbox conformance --policy safety.local_model` passes:
  - GET /api/tags gives 200;
  - GET /api/version gets 403;
  - the /tmp control succeeds;
  - 192.0.2.1:443, HTTPS to api.anthropic.com and HTTPS to example.com are refused, each
    with the supervisor's DENIED line in the ledger;
  - every staged area but lm is refused by Landlock.

## Why the model judges and never decides

Detection stays deterministic. The reconciler says only that two terms differ; whether
HYPERSENSITIVITY and DRUG HYPERSENSITIVITY are one event is a judgement, and a model can
be wrong. So the adjudicator writes a FindingAssessment next to the findings and never
changes, closes or supersedes one; a person reads it and decides. Its input is the two
preferred and two verbatim terms only, never a date, subject id or case id: they do not
help the judgement, and an input check refuses them. Its output is
`{"same_event": "yes" | "no" | "unsure", "reason"}`, and a validator decides whether it
may be shown:
* valid JSON with exactly those keys and an allowed verdict;
* a reason of one sentence and at most 200 characters, with no em dash;
* no reasoning trace and not truncated.
A rejected answer is recorded with its reason codes and its text is never shown. A wrong
"yes" is the costly mistake: it invites someone to close a real discrepancy, so the
model choice puts it first.

## Digest pinning

A model name is a moving label: `ollama pull` can replace the weights behind it. The
config pins each model by the digest /api/tags reports ([local_model.digests]). Before
sending anything, the worker in the sandbox reads /api/tags. If the served digest is not
the pinned one, it sends nothing and every result is a digest_mismatch error; the host
checks the same again. Every call records:
* model and digest;
* the sha256 of the request body sent and of the response body received;
* tokens and latency.
Requests are deterministic: temperature 0, seed 20261109, num_ctx 4096, format json,
stream false, think false. With `think` left out, qwen3.5 writes about 560 tokens of
reasoning; with `think: false` it writes none. A reasoning trace is never response text:
the client keeps only message.content and counts any message.thinking, and a validator
rejects an answer that had one.

## How the zone policies now differ

Until this step the seven zone policies applied one policy (they differed only in their
header comment). Now staged inputs land in /stage, one folder per area (meta, blinded,
ops, unblinded, narr, lm), created in the image, and each policy lists only the areas its
zone may use:

| Policy | Areas | Applied policy sha256 |
|---|---|---|
| portfolio, preclinical, study_ops, rbqm, quality | meta, blinded, ops | 089a1cfd...81d32f |
| safety, safety.sae_reconciler | meta, blinded | 386a4763...297298 |
| unblinded | meta, blinded, ops, unblinded | b196795a...caa08b3 |
| rbqm.narrator | narr (and the Messages API) | 56cd1e77...0a3fae7 |
| safety.local_model | lm (and Ollama) | 49b0c54b...678d4a |

A blinded zone can never open /stage/unblinded; an upload into an area a policy does not
list fails, so a staging bug fails closed. /stage itself is in no policy and cannot be
listed, so each file is uploaded straight into its area (a folder upload lands in a
subfolder and needs a writable parent). That costs about 1.4 s per run for 14 files.
Every policy's conformance opens each area: allowed where listed, refused elsewhere.

## The model choice

On the 61 dev pairs (docs/local-model-eval.md), with the rule fixed before the run:

| Model | Accuracy | "no" recall, look-alikes | Wrong "yes", look-alikes | Rejected | Median latency | At num_ctx 4096 |
|---|---|---|---|---|---|---|
| granite4:3b | 0.951 | 0.923 | 2 | 0 | 458 ms | 100% GPU, 2.5 GB |
| phi4-mini:3.8b-q4_K_M | 0.967 | 0.962 | 1 | 0 | 1000 ms | 16% CPU, 3.2 GB |
| qwen3.5:4b-q4_K_M | 0.934 | 0.962 | 0 | 3 | 2589 ms | 42% CPU, 3.7 GB |

* granite4 is not eligible: it said "yes" to two look-alikes (gastritis and gastric
  ulcer; atrial fibrillation and atrial flutter).
* Of the two eligible models, the more accurate wins: phi4-mini. Its one wrong "yes" is
  atrial fibrillation and atrial flutter.
* No model ever said "unsure".
