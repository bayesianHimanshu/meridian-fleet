# OpenShell notes (phase 2, step 1)

Agents now run inside NVIDIA OpenShell sandboxes. The agents themselves did not change;
what changed is where they run and what they can reach. Measured on OpenShell 0.1.7 with
the docker compute driver.

## One run, start to finish

1. **Stage.** The host copies `<cut>/blinded/*.parquet` and `<cut>/ops/*.parquet` into a
   temporary folder named `cut`, and writes the DataCut event as `cut/trigger.json`. Never
   `unblinded/`, never the manifest (it lists the unblinded files), never ground truth.
   The folder is checked twice, once after copying and once just before upload.
2. **Create.** `openshell sandbox create --name mer-km-7n-1a2b3c4d --from
   meridian-agent:0.1.0 --policy policies/rbqm.yaml --detach --upload <stage>/cut:/tmp`.
   One sandbox per agent run, from the agent image, with the zone's policy.
3. **Run.** `openshell sandbox exec ... --timeout 120 -- meridian-worker run --agent ...
   --trigger /tmp/cut/trigger.json --cut-dir /tmp/cut`. The worker prints one canonical
   JSON document on stdout, and logs go to stderr.
4. **Check the policy.** `openshell sandbox get <name> --policy-only` returns the policy
   the sandbox enforces. The host checks it matches the committed file and stores its
   sha256 on the ActorSuspended event, next to the image digest.
5. **Delete.** Always, in a `finally`, even when create, upload, exec or parsing fails.
   With `keep_failed = true` a failed sandbox is kept and its name is logged.
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
