# Agent Substrate: discovery and design (phase 4a steps 1 and 2)

Parts 0 and 1 are the step 1 design. Part 2 records the decisions taken after it and what
step 2 built, including where the build departs from the design. Measurements and proofs are
in docs/substrate-eval.md.

Step 1 was design only: no executor code, no cluster, nothing installed. Every claim about Substrate cites
a path in github.com/agent-substrate/substrate at commit
**94e0a26ac90151886a76410568aaac2664a54d90** (main, 5 Nov 2026), cloned read-only to
~/src/substrate. Paths below are relative to that checkout. **Guess** marks anything not stated
by the code or docs.

One premise of the brief is out of date: Substrate is no longer pre-1.0. v1.0.0 was released on
5 Nov 2026 (`gh release list`: "v1.0.0 Fully armed and operational", after rc.1 to rc.3);
main is 66 commits past that tag. The design still treats the API as young.

## Part 0: prerequisites and the project

### This laptop

| Item | Found |
|---|---|
| Docker | 29.8.2 (Engine, linux/amd64), cgroup v2, systemd driver, overlay2; runtimes runc only |
| Go | go1.27.0 at /usr/local/go; GOTOOLCHAIN=auto |
| kubectl | v1.33.4 (the Google Cloud SDK build at /usr/bin/kubectl) |
| kind | not installed (Substrate builds it through Go, see below) |
| runsc (gVisor) | not installed on the host (Substrate downloads it into the node, see below) |
| CPU | 16 threads, Intel i7-12650H, VT-x |
| RAM | 14 GiB total, about 8.7 GiB available |
| Disk | 64 GB free of 468 GB (86% used) |
| Kernel | 7.0.0-42-generic, Ubuntu 26.04.1 LTS |
| /dev/kvm | present, this user has rw through an ACL |
| Ollama | listening on 172.17.0.1:11434 only |

Can gVisor run inside kind? Substrate does not use a containerd runsc handler or a
RuntimeClass at all: atelet downloads a pinned gVisor release from the SandboxConfig
(`manifests/ate-install/sandboxconfig-gvisor.yaml:15-21`, release 20261102.0, sha256-checked)
and the `ateworker-gvisor` pod runs runsc itself as root with nine added capabilities and
seccomp and AppArmor unconfined (`cmd/atecontroller/internal/controllers/workerpool_apply.go:377-460`,
`docs/reference/api-guide.md:41-77`). So nothing has to change in this host's Docker; cgroup v2
and a 7.0 kernel are what gVisor's systrap platform needs (**guess**: not verified until it
runs). MicroVM sandboxes need /dev/kvm in the node; it is present here.

Missing or to decide before the demo: kind (fetched by Go, no system install), the Go
1.27.3 toolchain (fetched automatically by `go run`, because go.mod says `toolchain go1.27.3`),
and kubectl version skew: kind v0.33.0 (`hack/tools/kind/go.mod:21`) brings a v1.36 node
image by default (comment at `hack/create-kind-cluster.sh:106`; **guess** until the cluster
exists), and kubectl 1.33 is three minor versions behind, outside the supported skew of one.

### The project

Quickstart for kind (`docs/get-started/quickstart.md:31-51`):

```bash
hack/create-kind-cluster.sh
export ATE_INSTALL_KIND=true
go run ./cmd/ate-setup deploy ate-system --credential-provider='{"name":"k8s.io"}' \
  --ingress-allowed-spiffe-ids=spiffe://cluster.local/ns/ate-system/sa/atenet-client
go run ./cmd/ate-setup deploy demo counter
go install ./cmd/kubectl-ate
kubectl ate create actor my-counter-1 -a ate-demo-counter --template counter
kubectl ate proxy actor my-counter-1 -a ate-demo-counter
```

What `hack/create-kind-cluster.sh` does:
- It starts a `kind-registry` container (`registry:3`, `--restart=always`) on 127.0.0.1:5001
  (lines 54-71).
- It pulls busybox to probe /dev/kvm (79-86).
- It writes `bin/kind-config.yaml`: one control-plane node, /dev/kvm mounted when present
  (89-138).
- It deletes any cluster named `kind`, then creates the new one (140-159).
- It only warns about the host neighbour-table sysctl; it does not change it (191-194).
- It sets proxy_arp inside the node (197-204).
- It runs `chmod 666 /dev/kvm` inside the node (209-214). The device is a bind mount, so this
  likely changes the host's /dev/kvm mode too (**guess**).
- It writes the registry mirror (217-224).

It uses no sudo and installs no host packages. kind itself comes from
`hack/run-tool.sh` running `go tool kind` (`hack/tools/kind/go.mod`, kind v0.33.0).

`ate-setup deploy ate-system` flags that matter here (`cmd/ate-setup/internal/config/setting.go`,
`docs/guides/operator-install.md:280-331`):
- `--credential-provider` (required): `{"name":"k8s.io"}` deploys the bundled provider. It
  reads Secrets cluster-wide, with a default-deny namespace policy ConfigMap
  (`cmd/ate-setup/internal/config/config.go:570-760`,
  `cmd/ate-setup/internal/steps/credprovider.go:26-67`). `{"enabled":false}` turns it off.
- `--ingress-allowed-spiffe-ids`: exact SPIFFE ids that may call actors through atenet-router.
  Empty means no traffic. `kubectl ate proxy` presents
  `spiffe://cluster.local/ns/ate-system/sa/atenet-client` (`internal/ateclient/router.go:53`).
- `--sandbox-classes`: default `gvisor,microvm`. microvm makes ate-setup download Kata 4.3.1
  and Cloud Hypervisor v53.0 from github.com (`cmd/ate-setup/internal/steps/microvm.go:59-87`).
- `--atenet-dataplane`: envoy (default) or agentgateway.
- `--experimental-additional-egress-extproc-service`: an extra L7 filter.
- `--snapshot-location`: the kind profile sets `gs://ate-snapshots/`, served by rustfs
  (`cmd/ate-setup/internal/config/load.go:331-363`).
- `--setup-csi`: default none. nfs would need `sudo modprobe nfsd`.

CRDs (group `ate.dev/v1`; Go types in `api/infra/k8s/ate/v1/*_types.go`; YAML in
`manifests/ate-install/generated/`):
- **WorkerPool** (namespaced):
  - spec: `replicas`, `workerImage` (ateworker-gvisor or ateworker-microvm),
    `sandboxClasses[1]` {`name`, `configRef`}, `template` {labels, nodeSelector,
    tolerations, resources, serviceAccountName}.
  - status: `readyReplicas`, `sandboxClasses[].conditions`.
  - No hash or observedGeneration.
- **SandboxConfig** (cluster): `sandboxClass`, `defaultVersion`, `versions[]` {name, state,
  assets[arch][name] {url, sha256}}.
- **CSIDriverConfig** (cluster): `driverName`, `controllerEndpoint`, `tls`.

API objects are not CRDs. They are protobuf messages behind the gRPC `Control` service
(`api/ateapi/v1/ateapi.proto:25-263`), stored as rows in Postgres
(`cmd/ateapi/internal/store/atepg/migrations/000001_initial.sql`). Each carries
`metadata` {atespace, name, uid, version, create/update/delete time} (lines 412-479).
- **Atespace** (1121-1133): only metadata. It is the isolation and authorization scope.
- **ActorTemplate** (1153-1218): immutable, with no Update RPC.
  - `workerSelector.matchLabels`.
  - `containers[]`: image pinned by digest (1330-1337), command, args, literal `env`,
    `wakeupProbe`, `volumeMounts`, `securityContext.capabilities`.
  - `volumes[]`: one of `durableDir`, `image` (digest-pinned), `systemInfo`, or `external` CSI
    (preview).
  - `resources.limits` {cpuMillicores, memoryBytes}.
  - `snapshotConfig`: `preferredFidelity` VOLUMES, ROOTFS or MEMORY (default MEMORY); optional
    `storageLocation`; `goldenSnapshotConfig.mode` ENABLED or DISABLED (1293-1311).
  - `goldenEgressPolicy` (preview).
  - `status.goldenSnapshotStatus.goldenTag`.
- **Actor** (538-586, status 891-950):
  - spec: `actorTemplate`, `workerSelector`, `sourceTag`.
  - status: `state` (RESUMING, RUNNING, SUSPENDING, SUSPENDED, CRASHED, DELETING, REVERTING),
    `workerAssignment` {pod, node, sandboxRuntime, workerSpiffeId}.
  - `snapshots[]` {uuid, generation, fidelity, actorTemplateUid, durableSnapshot.uri}.
- **EgressPolicy** (588-875): one per actor, named `default`, up to 256 rules. Each rule is one of:
  - `http` {hostnames, ports, effects}
  - `https` {hostnames, ports, effects} (MITM)
  - `tlsPassthrough` {hostnames, ports}
  - `effects.replaceHeaders[]` {header, prefix, credentialUri | actorJwt}.
- **Tag** (1085-1119), **Worker**, **AccessPolicy** (3085-3130).
- **Secrets:** there is no Secret API and no secret volume. Credentials live in Kubernetes
  Secrets that the credential provider reads, referenced as
  `ate-secret://k8s.io/default/<namespace>/<secret>/<key>`
  (`docs/guides/egress-credential-injection.md:199-214`). The provider protocol is
  `api/credprovider/v1/credprovider.proto`.

## Part 1: design

### 1. Run model

Substrate has no job mode. Actors are long-lived HTTP servers with no "succeeded" state
(`ateapi.proto:879-888`); requests arrive over HTTP through atenet-router, chosen by the header
`ate-target-actor: <atespace>/<actor>` (`docs/concepts/architecture.md:349-431`,
`internal/atunnel/ingress/ingress.go:41-262`), on port 80 by default. A process that exits
cannot be suspended (`docs/reference/api-guide.md:714-718`).

Proposed mapping:

- **One atespace per zone policy** (`meridian-rbqm`, `meridian-safety`, `meridian-quality`,
  `meridian-audit`, `meridian-narrator`, `meridian-local`). The atespace is the isolation,
  authorization and credential-policy unit (`docs/guides/authorization.md`;
  `egress-credential-injection.md:245-267`), as a zone policy is today.
- **One ActorTemplate per (zone, image digest)**, committed in the repo as YAML. It runs a
  small resident server (`meridian-actor`) wrapping meridian-worker, with no durableDir and
  `goldenSnapshotConfig` enabled.
- **One actor per run.** The host runner calls `CreateActor` from the template's golden tag,
  then sends one `POST /run`. The body is a tar holding exactly what `stage_cut` /
  `stage_audit` stage today (meta/trigger.json, meta/inputs_manifest.json, the zone's areas).
  The server unpacks it into tmpfs `/tmp/stage`, runs meridian-worker as a subprocess, and
  returns the drafts document as the HTTP response body. Then the host calls `DeleteActor`.
  - No body-size limit is configured (`cmd/atenet/internal/router/xds.go:1385-1391`; the
    route timeout is 5 min, `cmd.go:104`). The tars are 1 to 3 MB today.
  - The draft comes back in the response, never on stdout. ateworker copies actor stdout and
    stderr into the worker pod's logs unredacted (`docs/guides/observability.md:273,548`),
    and today's worker prints drafts on stdout.
- Rejected: **one long-lived actor per agent version or zone.** Each run's data would stay in
  its process memory and tmpfs, and the next suspend would snapshot it.
- How the host reaches the router: `kubectl ate proxy` for the spike. Later, a client cert for
  a ServiceAccount whose SPIFFE id is in `--ingress-allowed-spiffe-ids`
  (`docs/guides/authentication.md:11-32`). **Guess** at the exact client setup for a
  non-pod caller.

The worker's inputs-manifest check stays as it is. It still proves that the actor read
exactly what the host staged.

### 2. No data in snapshots

What Substrate offers:
- **Golden snapshot.** A golden snapshot is taken by resuming a temporary actor, waiting for
  its probe plus 20 s, suspending it and publishing a tag, before any request
  (`api-guide.md:663-692`; `cmd/ateapi/internal/controlapi/template_reconciler.go:46`).
- **Clones.** Clones restore from the tag and never write to it. A clone's first own
  snapshot happens only on its own suspend (`api-guide.md:775-790`).
- **No automatic suspend.** Suspend happens only on an explicit `SuspendActor`
  (`architecture.md:497-499`). The quickstart's "when the actor becomes idle, Substrate
  checkpoints" (`quickstart.md:93`) does not match the code: the worker-initiated
  `RequestActorSuspend` client has no callers (`internal/ateworker/suspend/suspend.go`).
  Doc drift, verified by grep.
- **Delete does not checkpoint.** `DeleteActor` deletes the actor's own snapshots
  (`cmd/ateapi/internal/controlapi/workflow_delete.go:35-98`).

The rule, then:
1. **The clean snapshot is the golden snapshot.** Warm-up imports polars, numpy and scipy and
   loads the config, with no data mounted and no request served. The golden actor gets no
   egress policy. The tag is the only snapshot that may exist.
2. **Every run is a fresh clone of the golden tag.** It is never suspended and is deleted
   when the response returns. Data only ever lives in tmpfs and process memory.
3. **Never `durableDir`.** It is copied into every snapshot (`ateapi.proto:1487-1545`).
4. **After `DeleteActor`, the host reads `Actor.status.snapshots[]`** (before deletion
   completes) **and fails the run if any snapshot's owner is the actor itself.** It is
   recorded as evidence.
5. **Reseed randomness in the handler.** Memory clones share PRNG state
   (`api-guide.md:299` on frozen env vars). The microVM worker reseeds the guest RNG
   (`cmd/ateworker-microvm/restore.go:72-73`).

What makes this possible:
- explicit-only suspend
- golden tags
- delete without checkpoint

What makes it only procedural, not enforced:
- There is no per-actor or per-template "never snapshot" switch (fidelity minimum is VOLUMES,
  `ateapi.proto:1293-1296`).
- Anyone with `SuspendActor` permission on the atespace can still snapshot a live run.
- A worker eviction gives "whatever drives the actor" 30 minutes to suspend it
  (`api-guide.md:703-722`). Our driver must answer eviction by deleting, never suspending.
- Snapshots are not encrypted by Substrate (`docs/security/security.md:62,89`).
- On kind, rustfs credentials are the hard-coded `rustfsadmin`
  (`manifests/ate-install/kind/ate-api-server/kustomization.yaml:49-61`).

Mitigation: an AccessPolicy that grants the runner create, delete and get on actors in the data
atespaces, and nothing that can suspend (`docs/guides/authorization.md:112-193`; **guess** that
the RPC checks are fine-grained enough, to verify).

### 3. Containment parity

| OpenShell guarantee we rely on | Substrate | Parity |
|---|---|---|
| Per-zone read allow-list (Landlock over /stage areas) | No Landlock or per-path rules (`grep -ri landlock` finds nothing); `VolumeMount` has no readOnly. The sandbox sees its image, tmpfs and what we send. | **Partial.** Each actor holds only the areas the host put in its tar, so `assert_clean_stage` becomes the only control. We lose the second layer that refused `/stage/unblinded` even after a staging bug. |
| No network by default | Yes. No EgressPolicy means no tunnel (`cmd/atenet/internal/router/egress/egress.go:261,269`). The DNS relay answers NXDOMAIN for names outside the policy (`docs/guides/egress-traffic.md:18-23`). nftables rejects non-TCP and link-local (`internal/ateworker/actornet/sandbox.go:261-376`). The PEP fails closed (`manifests/ate-install/atenet-egress.yaml:213`). | **Yes**, per actor. No policy is applied by default. A template default is open issue #1430 (`docs/guides/observability.md:544`). |
| Egress to exactly one host:port (Ollama 172.17.0.1:11434) | Rules take hostnames and ports. IP literals are invalid (`ateapi.proto:690-693`, `internal/egresspolicy/egresspolicy.go:308-325`); an IP destination matches only `*`. There is no RFC1918 or loopback block. | **Partial.** Give the host a DNS name, for example a selectorless Service with an EndpointSlice at 172.17.0.1, and allow `http {hostnames: [ollama-host.<ns>.svc], ports: [11434]}` (**guess**, to test). The egress gateway pod, not the actor, makes the connection. Kind nodes sit on their own Docker bridge, so traffic reaches 172.17.0.1 from the node's address. Ollama already listens on 172.17.0.1, so it is reachable only if the host firewall admits that bridge (**guess**). |
| L7: only GET /api/tags and POST /api/chat; only POST /v1/messages | No method or path matching (`egresspolicy.go:231-266`). The only hook is `--experimental-additional-egress-extproc-service`, a service of ours that sees request headers and can deny (`docs/security/extensibility.md:22`). | **No**, natively. Possible with our own ext_proc filter (**guess** that it sees `:method` and `:path`). |
| Anthropic key injected; the sandbox never holds it | Yes. An `https` rule with `replaceHeaders: [{header: x-api-key, credentialUri: ate-secret://k8s.io/default/<ns>/<secret>/<key>}]`. The provider's namespace policy starts default-deny and is read at startup. The actor must trust the MITM CA through a `systemInfo` trust bundle (`egress-credential-injection.md:24-26,87-115,245-267`; `egress-trust-bundle.md:49-115`). | **Yes**, but with TLS interception, which OpenShell does too. |
| Refusal evidence (OCSF NET:OPEN DENIED lines) | ext-proc JSON logs `"egress denied: no rule allows the destination"` with actor, method, host and port (`cmd/atenet/internal/router/egress/request.go:77-104`). Envoy access logs record denied CONNECT and ClientHello (`atenet-egress.yaml:137-143,330-349`). DNS denials and UDP drops are not logged (`egress-traffic.md:25-30`). There is no OCSF and no policy version in the lines. | **Partial.** A conformance run collects the egress logs and records them as PolicyDenied events with a new source. A DNS-only refusal shows only as NXDOMAIN inside the actor. |
| Policy read back from the sandbox (`sandbox get --policy-only`) | `GetActorEgressPolicy` returns the stored policy with uid and version. Nothing reports the enforced copy (the 64-bit hash in Envoy is internal). | **Partial.** |

### 4. OpenShell and Substrate together

OpenShell 0.1.7 is not Docker-only. The CLI documents a VM driver ("A rootfs tar is staged
for the VM compute driver") and a Kubernetes driver (`--driver-config-json`), and the 0.1.7
docs list Docker, Podman, MicroVM, Kubernetes and Windows MXC drivers, plus external drivers
over `compute_driver.proto`. There is one driver per gateway
([drivers](https://docs.nvidia.com/openshell/v0.1.7/extensibility/drivers.md)). This gateway
runs only `docker` (`openshell gateway info`). Whether Landlock and the proxy behave the same
under each driver is not stated there.

Options:
- **A. Substrate replaces OpenShell.**
  - Gain: warm pools, sub-second restores and density.
  - Lose: Landlock read allow-lists, native L7 rules, OCSF evidence, the effective-policy
    readback that our applied policy sha256 rests on, and the conformance harness as written.
- **B. OpenShell inside Substrate actors.**
  - It would need a gateway and a container runtime inside gVisor. Nested sandboxing gives no
    restore benefit, because the OpenShell sandbox would be created per run again.
  - Rejected.
- **C. OpenShell for local development and for zones that need L7 or credential rules;
  Substrate behind the same `Executor` protocol for the blinded, no-network zones.**
  - Those zones are rbqm, safety.sae_reconciler, quality and quality.audit_trail.
  - There, parity is complete except Landlock, and the cost is per-run sandbox overhead.

**Recommendation: C.** Start Substrate with the no-network zones, where the only loss is the
Landlock second layer. Keep rbqm.narrator and safety.local_model on OpenShell until an ext_proc
filter proves the two-endpoint L7 limits, with refusal evidence. An OpenShell Kubernetes-driver
gateway on the same kind cluster is a separate spike worth doing later. It keeps every policy
semantic and gains scheduling, but not snapshots.

What we lose under C: one executor's simplicity (two runtimes to keep in conformance) and, for
the Substrate zones, the Landlock layer. The ledger must say which executor ran each run; it
already records `executor`.

### 5. Traceability

| Today | Substrate replacement |
|---|---|
| image digest | The container image digest, which the template requires to be pinned (`ateapi.proto:1330-1337`). Also `sandboxRuntime` (class plus `sandbox_bundle_hash`, `ateapi.proto:2512-2560`) and the SandboxConfig version, whose gVisor asset is sha256-pinned. |
| policy file sha256 | sha256 of the committed template YAML and egress-policy YAML in the repo, our own files as today. |
| applied policy sha256 | sha256 of the canonical JSON the API returns after the run for the ActorTemplate (with its `uid` and `version`) and the EgressPolicy (`uid`, `version`, `update_time`). Templates are immutable, so the template `uid` pins the spec. |
| (new) | Actor `uid`, the golden tag uid and the snapshot uuid and generation restored from, `workerAssignment` {pod, node, workerSpiffeId}, and the inputs manifest sha256 (unchanged). |

A run proves its containment when the host records, in ActorSuspended, the template uid and
version, the image digest, the sandbox runtime hash, the egress policy uid, version and hash,
and the actor uid. The host reads them back from the API, never from the actor. Gateway deny
lines are joined by actor SPIFFE id (`spiffe://substrate-actor.local/actor/<atespace>/<name>`,
`internal/resources/spiffe.go:24-40`) and time. Weak point: deny lines carry no policy version,
and OTel actor events carry the template name but not its uid (`observability.md:217-239`).

### 6. Measurement plan

Baseline (docs/openshell-notes.md): about 8 s per sandboxed run, 5.4 s of it waiting for
deletion; exec 1.4 s.

All timings are taken by the host runner: wall clock, the same staged cut each time
(DC-BASE, rbqm.kri_monitor), and the drafts must be byte-identical to an in-process run.

1. **Cold start:** a new template until its golden tag is ready, then the first run of a fresh
   actor with no node-local snapshot cache.
2. **Warm worker:** CreateActor from the golden tag on a warm WorkerPool, POST, response,
   DeleteActor issued. Split into create, ResumeActor (router-triggered), request and delete
   calls.
3. **Restore:** the same, with the golden snapshot cached on the node versus downloaded from
   rustfs (`cmd/atelet/snapshotcache.go`). Also gVisor against microVM.
4. **100 runs:** sequential, then with concurrency 4 and 8. Report p50, p95 and max of run
   latency, and the share in each phase. Pass only if p95 beats 8 s and all 100 drafts match.
5. **Density:** concurrent running actors (each at its template's limits, for example 1 CPU and
   512 MiB) until RESOURCE_EXHAUSTED or a wakeup-probe timeout. Watch node memory and kubelet
   health (`docs/guides/benchmarking.md:796-811`).
6. **Leak check per run:** no snapshot owned by the actor, no actor left over, and no draft
   text in the worker pod logs.

The repo's own tools (`docs/dev/suspend-resume-phase-breakdown.md`, locust) can cross-check the
restore phases. Its numbers are targets, not measurements: under 500 ms resume
(`README.md:30`), north-star p95 of 100 ms (`architecture.md:97-110`).

### 7. Risks

- **API churn.** v1.0.0 is days old, main is moving (66 commits past it), and many features are
  preview gates. Pin a commit or tag and record it with every run. Docs already drift from the
  code (idle suspend, CIDR rules).
- **Laptop resources.** About 8.7 GiB is free. The control plane (Postgres 1 Gi, egress
  512 Mi, rustfs, otel, Prometheus, Jaeger, router and api-server) plus the counter demo's
  WorkerPool (3 replicas, 1 Gi each) fills most of it. About 570 MB of third-party images. 64 GB
  of free disk, with the disk 86% full.
- **gVisor and our stack.** The repo has no compatibility notes for Python 3.14,
  numpy, scipy or polars. gVisor reports `max(2, ceil(cpu))` CPUs, so polars' thread pool will
  be small. /dev/shm is 64 MiB and noexec (`docs/reference/sandbox-classes.md:21-56`). This
  must be tested.
- **Host changes from the scripts.** `chmod 666 /dev/kvm` through the node; a
  `--restart=always` registry container; Go toolchain and module downloads.
- **Workers run privileged-ish** (root, SYS_ADMIN, NET_ADMIN, seccomp unconfined;
  `docs/security/security.md:65`). The sandbox, not the pod, is the boundary between actors
  (`docs/security/threat-model.md:25-29`).
- **Data at rest.** Snapshots are unencrypted, and kind's rustfs uses default credentials.
  Acceptable only because no data snapshot should exist (section 2), and that is procedure, not
  enforcement.
- **Logs.** Actor stdout reaches the pod logs. meridian-worker must stop printing drafts when
  served by the actor.
- **Host reachability** of 172.17.0.1 from the kind bridge, and ufw.
- **kubectl skew** (1.33 client against a 1.36 server).

## Part 2: decisions and what step 2 built (phase 4a step 2)

### Decisions (taken by the user after step 1)

1. **Pin v1.0.0** (`b34fbee742906494b5eb4f1f36987176bf8770a4`), as OpenShell is pinned at
   0.1.7. v1.0.0 is not an ancestor of main: it is on release-1.0, where 60 of the 66 commits
   between the merge base and 94e0a26 were cherry-picked. The 6 left over
   (`git log --cherry-pick --right-only v1.0.0...94e0a26`) and the whole tree diff touch only
   site/, docs/, demos/counter/README.md, hack/test-site-templates.sh and the benchmarking
   tools (`git diff --stat v1.0.0 94e0a26`): no API, CLI, controller, worker or manifest code.
   Verdict: nothing we use differs. ate-system was redeployed from v1.0.0 with the same flags
   (credential provider k8s.io, ingress SPIFFE id atenet-client, `--sandbox-classes=gvisor`);
   ate-setup's install record says `substrateVersion: v1.0.0`, and the runner refuses to start
   if it says anything else (`Container.check_substrate_release`). kubectl-ate was rebuilt
   from the same tag, and the gVisor worker image with ko at the tag
   (`localhost:5001/ateworker-gvisor@sha256:ad6957f4...`, config [substrate] worker_image).
2. **/dev/kvm** is back at 660 and is not touched again. microVM is not used.
3. **gVisor only.**
4. **The narrator and the local model stay on OpenShell.**
5. **Option C, accepted:** one Executor protocol, chosen per policy from config
   ([runtime] executor = "zoned", [runtime.executors]); policies with a network rule run on
   OpenShell, policies without on Substrate (rbqm, safety.sae_reconciler, quality,
   quality.audit_trail). Substrate is refused for any policy with a network rule or a
   provider (`meridian.sandbox.zoned`). **We knowingly lose the per-path read allow-lists
   (Landlock) for these zones.** The compensating control: the host sends an actor only its
   zone's folders, proven for every zone by
   `tests/unit/test_substrate_executor.py::test_an_actor_receives_only_its_zones_folders`,
   and the actor itself refuses any archive member outside the areas its template names
   (`meridian-worker serve --areas`). docs/threat-model.md section 4 records the trade.
6. **Memory:** the counter demo was removed before our pool existed; the pool starts at 2
   workers, and memory per worker is measured (docs/substrate-eval.md).

### What was built

- **`meridian-worker serve`** (src/meridian/worker_serve.py): the actor's HTTP server. Before
  it listens it builds the agent registry and reads a constant parquet file it writes itself;
  `GET /readyz` answers only after that, and the template's wakeup probe is that endpoint, so
  the golden snapshot (MEMORY fidelity) holds a warm process and no data. `POST /run` takes the
  tar and its sha256, unpacks only `<area>/<file>` regular files of the template's areas into
  a fresh folder under /tmp (tmpfs in gVisor), runs the same `execute` as `meridian-worker
  run`, answers the drafts document, the archive sha256 and the inputs manifest sha256, deletes
  the folder and answers 409 to any later run. Nothing about the run reaches stdout or stderr
  (the worker pod's log); it prints one line, "run finished, exit N".
- **Templates** (deploy/substrate/, rendered by `meridian substrate render` from
  src/meridian/sandbox/substrate_templates.py and checked by a test): one per zone, named
  `<policy>-<12 hex of the image's registry digest>` because templates are immutable, image by
  digest, `serve --areas <the policy's reads>`, 1 CPU and 768 MiB, MEMORY fidelity, golden
  snapshot on. One conformance template on the same image, pool, class and size runs
  `meridian-worker probe` instead. One WorkerPool, meridian/meridian, 2 gVisor workers of
  1 GiB.
- **`SubstrateExecutor`** (src/meridian/sandbox/substrate_executor.py): stages exactly what
  the OpenShell executor uploads (the staging is now one shared function, `stage_run`), packs
  it into a deterministic tar, creates the actor from the zone template, resumes it, posts
  through `kubectl ate proxy`, reads the actor back and fails the run if it has any snapshot
  other than its golden one, and always deletes it. `drain` waits until no actor it created
  is listed.
- **Records:** ActorWoke (the plan, before the run) and ActorSuspended (after it) carry a
  `SubstrateRun`: release and commit, atespace, template and the sha256 of its canonical spec
  as ate-api reports it, the committed template file and its sha256, the image by registry
  digest, WorkerPool, sandbox class, actor name and uid, worker pod, and the golden tag and
  snapshot id. `image_digest` stays the image id, as OpenShell records it, so the two
  executors' runs of the same image compare equal. The fleet page and a finding's
  traceability panel show the placement.
- **quality.audit_trail_reviewer 1.1.0:** substrate is a sandboxed executor when the
  completion records the image, the template spec hash and the golden snapshot id.
- **`meridian substrate`** render, deploy, status, verify, conformance, canary, bench and reap;
  `meridian sandbox conformance --executor substrate`; `meridian agents run --cut --rerun`.

### Where the build departs from Part 1

- **One atespace (`meridian`) for all four zones,** not one per zone. With no egress policy and
  no credentials, the atespace separates nothing the template does not already separate; the
  zone is the template. One atespace per zone is needed once a zone gets an egress rule.
- **The agent runs in the server's process,** not in a subprocess. A subprocess would import
  everything again (about 1.4 s, the cost the golden snapshot removes). The process handles
  one run and is deleted with its actor.
- **Port 8080, not 80:** the image runs as uid 1000 (`USER sandbox`), which cannot bind a port
  below 1024. The router forwards any port.
- **The golden warm-up is not 20 s:** with a wakeup probe on every container the controller
  takes the snapshot as soon as the probe answers (`goldenSnapshotWarmupFor` in
  `cmd/ateapi/internal/controlapi/template_reconciler.go`). Golden snapshots were ready 10 to
  28 s after a template was created.
- **Refusal is not a failed connect.** Inside an actor `connect()` to any address succeeds:
  atunnel accepts every TCP connection and tunnels it to atenet-egress, which refuses it
  (`docs/guides/egress-traffic.md`). The probe therefore connects, sends one request and
  reads: the tunnel resets the connection, and the gateway logs
  `[egress] authority=<host:port> peer_san=spiffe://substrate-actor.local/worker-for-actor/<atespace>/<actor> ... code=403`.
  Part 1's guess that the denial would appear as the ext-proc line was wrong for a
  no-policy actor: it is this access-log line. These lines become PolicyDenied events with
  source `substrate_log`.
- **Snapshots use Substrate's sparse format** (`ATESPRSE`, a u32 version, then one zstd stream
  of the populated extents; `pkg/objectstorage/sparsezstd.go`). A search of snapshot storage
  must decode it; a plain zstd decoder fails on every object.
- **DeleteActor removes the actor's own snapshot** from object storage within 30 s (seen in the
  canary's positive control), as Part 1 read in `workflow_delete.go`.
- **AccessPolicy:** not set up. The runner acts as the shared bootstrap identity
  `system:serviceaccount:ate-system:ate-client`, which can also suspend. The control that no
  run is suspended is therefore the executor's per-run snapshot check plus the canary proof,
  not authorization (docs/threat-model.md section 4).
