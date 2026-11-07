# OpenShell notes (phase 2, step 1)

Agents now run inside NVIDIA OpenShell sandboxes. The agents themselves did not change;
what changed is where they run and what they can reach. Measured on OpenShell 0.1.7 with
the docker compute driver.

## One run, start to finish

1. **Stage.** The host copies `<cut>/blinded/*.parquet` and `<cut>/ops/*.parquet` into a
   temporary folder named `cut`, and writes the DataCut event as `cut/trigger.json`. Never
   `unblinded/`, never the manifest (it lists the unblinded files), never ground truth.
   The folder is checked twice, once after copying and once just before upload. Last,
   the host writes `cut/inputs_manifest.json`: path, size and sha256 of every staged file.
2. **Create.** `openshell sandbox create --name mer-km-7n-1a2b3c4d --from
   meridian-agent:0.1.0 --policy policies/rbqm.yaml --detach --upload <stage>/cut:/tmp`.
   One sandbox per agent run, from the agent image, with the zone's policy.
3. **Run.** `openshell sandbox exec ... --timeout 120 -- meridian-worker run --agent ...
   --trigger /tmp/cut/trigger.json --cut-dir /tmp/cut`. Before reading any data the worker
   checks every file against the inputs manifest and exits 3 on a missing, extra or
   changed file (the cut sits in writable /tmp, and a failed upload can leave a partial
   one). It prints one canonical JSON document on stdout, and logs go to stderr.
4. **Check the policy.** `openshell sandbox get <name> --policy-only` returns the policy
   the sandbox enforces. The host checks it matches the committed file and stores its
   sha256 on the ActorSuspended event, next to the image digest and the inputs manifest
   sha256.
5. **Delete.** Always, even when create, upload, exec or parsing fails, but off the
   critical path: the sandbox goes to a background deleter (a thread pool of
   `[runtime] delete_workers`) and the run returns. The runner drains the deleter before
   the pass ends. With `keep_failed = true` a failed sandbox is kept and its name logged.
   Every sandbox is tracked in `var/sandboxes.jsonl` (append-only: created,
   delete_requested, deleted, delete_failed); `meridian sandbox reap` deletes the tracked
   ones not yet deleted, and never anything else.
6. **Record.** Only the host runner writes to the ledger.

`meridian sandbox verify` runs every agent both ways and compares the drafts byte for byte.
On DC-BASE, DC-RBQM-CORE, DC-NULL-S6 and DC-d5ada06c all 12 runs matched.

## The policy, field by field (policies/<zone>.yaml)

* `include_workdir: false`. The working directory gets no automatic access. Everything
  the agent may touch is listed explicitly.
* `read_only`: `/usr/local/bin/python3.14` and `/usr/local/lib` (the interpreter and its
  standard library), `/usr/lib/x86_64-linux-gnu` (the C libraries numpy, scipy and
  polars load), `/opt/meridian` (the venv and config), and `/usr/bin/{bash,cat,mkdir,tar}`.
  The sandbox's main process is bash, and an upload is extracted with cat, mkdir and tar.
  I found the list by removing entries until something broke. `/usr`, `/etc` and `/lib64`
  are not needed.
* `read_write: /tmp` only. The cut lands in `/tmp/cut`, because an upload is extracted by
  the sandboxed process itself and can only go somewhere writable.
* `landlock: hard_requirement`. If the kernel cannot enforce the rules, the sandbox does
  not start. `best_effort` would silently run an agent with full file access.
* `process: sandbox / sandbox`. This is a non-root user (uid 1000) that the image creates.
* `network_policies: {}`. No egress for any zone.

Filesystem, Landlock and process settings apply when the sandbox starts and cannot
change afterwards. That is one more reason to create a fresh sandbox for every run.

## Landlock

Landlock is a Linux security module. A process can lock itself into a list of allowed
paths, and the kernel refuses everything else with EACCES, even when ordinary Unix
permissions would allow it. A process cannot undo the lock, and neither can its children.
OpenShell applies the lock before it starts the agent's process.

## Why no network and no ledger

An agent reads a cut and returns findings. It needs nothing else, so it gets nothing else.
Without network it cannot leak study data or fetch code. Without the ledger, or any
credential, a buggy or compromised agent cannot write, rewrite or forge findings. The host
validates what comes back (dedup prefix, agent id and version) before anything is
appended. Agents also became pure functions: the clock is fixed at the trigger's
recorded_at, so the same inputs give the same bytes anywhere.

## What the negative checks showed

These are shell commands run inside a sandbox with the rbqm policy
(`pytest -m openshell`):

| Attempt | Result | Refused by |
| --- | --- | --- |
| `cat /etc/passwd` | `cat: /etc/passwd: Permission denied` | Landlock: the file is 0644, so Unix permissions would allow it |
| `echo x > /var/tmp/evil.txt` | `bash: line 1: /var/tmp/evil.txt: Permission denied` | Landlock: /var/tmp is 1777 |
| `exec 3<>/dev/tcp/pypi.org/443` | `pypi.org: Temporary failure in name resolution` | the sandbox's policy DNS |
| `exec 3<>/dev/tcp/151.101.0.223/443` | `connect: Permission denied` | the policy proxy; the sandbox log for the same connect from Python: `NET:OPEN [MED] DENIED /usr/local/bin/python3.14(0) -> 151.101.0.223:443 [reason:transparent_tcp_policy_denied]` |
| `urllib.request.urlopen("https://pypi.org/simple/")` | `URLError: [Errno -3] Temporary failure in name resolution` | policy DNS |

Landlock denials leave no log line; the kernel just returns EACCES. Network denials are
logged by the sandbox's supervisor as OCSF events.

## Timings

Each run costs about 8.1 s in a sandbox, against 6 to 50 ms in process:

* create and upload: 1.5 s
* worker exec: 1.4 s, mostly importing numpy, scipy and polars cold
* policy read: 0.04 s
* delete: 5.4 s

Precompiling bytecode in the image cut the exec from 3.0 s to 1.4 s, because the sandbox
cannot write `.pyc` files. Deleting a sandbox that never ran an exec takes 0.3 s, so most
of the delete time is cleanup after a session.

With deletion in the background (step 2, `scripts/teardown_timing.py`, 3 agents on
DC-BASE, two runs each):

| | Per-run critical path | Pass wall time |
| --- | ---: | ---: |
| Synchronous delete (step 1) | 8.5 s | 25.5 s |
| Background delete, 4 workers | 2.9 s | 14.6 s |

The pass still ends about 5.4 s after the last run, waiting for its delete; runs are
still sequential, so the critical path is what a longer pass saves on.

`openshell sandbox delete` returns once the gateway accepts the deletion ("cleanup is
pending"), sometimes in 0.4 s, and the sandbox can stay in `sandbox list` for seconds
after. The deleter therefore records `deleted` only when the sandbox is no longer
listed, polling for at most `delete_confirm_seconds`; otherwise it records
`delete_failed` ("still listed") and reap tries again.

## The policy prover (`meridian policy prove`)

`openshell-prover check <zone policy> --boundary policies/boundaries/blinded.yaml
--output json` runs for every zone. A zone passes only on `within_boundary` with coverage
of filesystem, process and landlock; anything else fails with the prover's reason. CI
runs it on every pull request with the prover from the 0.1.7 release, checksum verified.

On 0.1.7 a boundary is an allow-list in the policy format. Process and Landlock are
proved properly: `run_as_user: root` or `best_effort` give `exceeds_boundary` with a
counterexample. Filesystem is not: a candidate path that the boundary does not list
returns `unsupported` (`unresolved_filesystem_path`, "use matching paths in candidate and
boundary") rather than a proof that the path is out of bounds, and a boundary cannot
forbid a path. So the prover only confirms that a policy's paths match the boundary's
allow-list. The policy render test (committed files equal the rendering, only allowed
paths, no network) stays the primary filesystem check; the prover is a second one.

## Where the installed CLI (0.1.7) differs from the docs

1. **Sandbox names are at most 19 characters.** `mer-kri-monitor-275-1a2b3c4d` was
   refused with `name exceeds maximum length (26 > 19)`. Names are now
   `mer-<2-letter agent>-<seq base 36>-<8 hex>`.
2. **Uploads need writable targets.** The extraction runs inside the sandbox, under its
   own Landlock rules, through bash, cat, mkdir and tar. A read-only `/cut` cannot receive
   an upload, and the tools must be readable. So the cut is at `/tmp/cut`.
3. **A directory upload `DIR:DEST` lands at `DEST/<basename of DIR>`**, not at DEST.
4. **A failed upload leaves the sandbox behind.** The CLI says "was created and still
   exists".
5. **`--upload` applies `.gitignore` inside a git work tree.** We pass `--no-git-ignore`.
6. **The effective policy (`get --policy-only`) is not the file we sent.** It drops
   comments and an empty `network_policies`, and has no trailing newline.
   `policy get --full` adds a header with the gateway's own hash.
7. **The CLI has no gateway start or stop, and no telemetry switch.** The gateway is the
   systemd user unit `openshell-gateway.service`, and the global settings hold OCSF
   logging keys.
8. **A sandbox's main process is the image's shell** (`/bin/bash`), so the policy must
   allow it, or the sandbox ends in phase Error with `failed to spawn sandbox entrypoint
   process '/bin/bash' ... Permission denied`.
9. **`exec` takes the name as a positional argument or as `-n/--name`**, and has
   `--no-tty` and `--no-login-shell`; the login shell otherwise sources startup files.

## Conformance check (`meridian sandbox conformance --zone ZONE|all`)

A unit test for a zone's committed policy, in a real sandbox from the production image:
read `/etc/os-release`, write `/var/tmp/conformance` and `/opt/meridian/conformance`,
connect to 192.0.2.1:443, and a `/tmp` control. A refusal counts only when the error
names the target: `touch` is not in the allow-list, so `touch X` would be refused before
it ever tried X, and the check would prove nothing. Writes use bash redirection.

The `NET:OPEN ... DENIED` line appears in `openshell logs` about 1 s after the connect,
with the sandbox's `ocsf_json_enabled` setting never set; so the check does not set it
and does not wait for its 10 s poll. It polls the log for at most
`conformance_log_timeout_seconds`. Results: PolicyDenied events (`harness` for files,
`openshell_log` with the raw line for the network) and one ContainmentCheckCompleted.

## The narrator's provider (phase 2 step 3)

OpenShell 0.1.7 has no `inference` command and no inference.local route. A sandbox calls
the provider's own API, and a provider supplies the credential:

* **Profile.** `profiles/anthropic.yaml` (rendered by `meridian policy render`, no secrets)
  declares the credential `ANTHROPIC_API_KEY` sent as header `x-api-key`, the endpoint
  `api.anthropic.com:443` with `protocol: rest` (L7 inspection: `POST /v1/messages` and
  `GET /v1/models` only), and the binary `/usr/local/bin/python3.14`. The proxy names a
  binary by its real path, so it is the interpreter, not the venv's symlink
  `/opt/meridian/venv/bin/python`. Lint refuses an L4-only credentialed endpoint unless
  `allow_uninspected_credentials: true`; we inspect instead. There are no built-in
  profiles (`profile list` is empty); the schema is not documented with 0.1.7, so it was
  read from lint's errors. Once imported, `profile lint` fails on the same id ("already
  exists").
* **GET /v1/models is in the profile only for `meridian sandbox conformance --policy
  rbqm.narrator`**: status 200 there shows the proxy substituted the credential at the
  allowed endpoint. The narrator's client only ever sends `POST /v1/messages`, and the
  narrator's own policy rule allows only that.
* **Creating the provider** (done by the user, never by this code):
  `openshell profile import -f profiles/anthropic.yaml`, then `openshell provider create
  --name meridian-anthropic --type meridian-anthropic --credential ANTHROPIC_API_KEY`.
  * `--from-existing` does not work for a custom profile type on 0.1.7: "no existing local
    credentials/config found for provider type 'meridian-anthropic'".
  * `--credential ANTHROPIC_API_KEY` (a name with no `=VALUE`) stores the **value** of
    that variable from the calling shell, not the literal name: with the provider attached,
    GET /v1/models returns 200 (a literal name would give 401), while the sandbox's
    `ANTHROPIC_API_KEY` is a placeholder that neither starts with `sk-ant-` nor equals the
    variable's name. `provider get` shows only "Credential keys: ANTHROPIC_API_KEY".
* **Attaching.** `sandbox create --provider meridian-anthropic` (with
  `--no-auto-providers`). The effective policy then gains a rule
  `_provider_meridian_anthropic` with the profile's endpoint, next to the policy's own.
* **TLS.** The proxy is transparent (no `HTTPS_PROXY`). The supervisor sets
  `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` to
  `/run/openshell-supervisor-ca/material/ca-bundle.crt` (the system CAs plus its own; the
  sandbox can read it under Landlock) and `NODE_EXTRA_CA_CERTS` to `ca.crt` beside it.
  The client builds its context from `SSL_CERT_FILE` (certifi otherwise); verification is
  never turned off.
* **Any network rule widens the filesystem.** With a rule in the policy (with or without a
  provider), 0.1.7 appends `/usr`, `/lib`, `/etc`, `/var/log`, `/proc` and `/dev/urandom`
  to the read-only paths and `/dev/null` to read-write. There is no setting for it;
  `expected_effective` pins it, so a change shows up as a policy mismatch.
* **Log lines.** An allowed connection names the rule that allowed it: `NET:OPEN [INFO]
  ALLOWED /usr/local/bin/python3.14(0) -> api.anthropic.com:443
  [policy:_provider_meridian_anthropic engine:opa]`. A refused HTTPS GET to example.com is
  `DENIED ... -> example.com:443 [reason:transparent_tcp_policy_denied]`, with the host
  name, not an address.
* **The model.** claude-haiku-5-5 answers HTTP 400 "`temperature` is deprecated for this
  model.", so requests leave temperature out; `thinking: {type: disabled}` is accepted.
