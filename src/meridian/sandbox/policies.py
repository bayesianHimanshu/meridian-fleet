"""OpenShell sandbox policies, one per agent zone, rendered from the data below.

`meridian policy render` writes policies/<zone>.yaml; the files are committed and a test
checks that rendering reproduces them byte for byte.

Read-only paths are the minimum the agent image needs, found by removing entries one at a
time inside a real sandbox (docs/openshell-notes.md):

* /usr/local/bin/python3.14 and /usr/local/lib: the interpreter and its standard library
  (the venv's python is a symlink to the interpreter).
* /usr/lib/x86_64-linux-gnu: the C libraries numpy, scipy and polars load.
* /opt/meridian: the venv with meridian and its locked dependencies, and the config.
* /usr/bin/bash, cat, mkdir and tar: the sandbox's main process is bash, and OpenShell
  extracts uploads by piping a tar stream through bash, cat, mkdir and tar inside the
  sandbox, under the same Landlock rules as the agent.

The cut is uploaded to /tmp/cut, inside the one read-write path: an upload is extracted by
the sandboxed process itself, so it can only land somewhere writable.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

POLICY_VERSION = 1
READ_ONLY: tuple[str, ...] = (
    "/usr/local/bin/python3.14",
    "/usr/local/lib",
    "/usr/lib/x86_64-linux-gnu",
    "/opt/meridian",
    "/usr/bin/bash",
    "/usr/bin/cat",
    "/usr/bin/mkdir",
    "/usr/bin/tar",
)
READ_WRITE: tuple[str, ...] = ("/tmp",)  # noqa: S108 (a path inside the sandbox)
# Every agent zone (meridian.agents.base.Zone), as plain strings: rendering a policy must
# not import agent code.
ZONES: tuple[str, ...] = (
    "portfolio",
    "preclinical",
    "study_ops",
    "rbqm",
    "safety",
    "quality",
    "unblinded",
)
POLICIES_DIR = Path("policies")


@dataclass(frozen=True, slots=True)
class HttpRule:
    method: str
    path: str


@dataclass(frozen=True, slots=True)
class NetworkRule:
    """One OpenShell network policy: an endpoint, the requests allowed, and who may call."""

    name: str
    host: str
    port: int
    rules: tuple[HttpRule, ...]
    binaries: tuple[str, ...]
    protocol: str = "rest"
    enforcement: str = "enforce"


@dataclass(frozen=True, slots=True)
class ZonePolicy:
    zone: str
    read_only: tuple[str, ...] = READ_ONLY
    read_write: tuple[str, ...] = READ_WRITE
    landlock: str = "hard_requirement"
    run_as_user: str = "sandbox"
    run_as_group: str = "sandbox"
    # No zone policy has egress. An agent policy may name network rules and the
    # OpenShell providers attached to its sandboxes (phase 2 step 3: rbqm.narrator).
    network: tuple[NetworkRule, ...] = ()
    providers: tuple[str, ...] = ()
    name: str = ""  # the agent id for an agent policy; empty for a zone policy

    @property
    def policy_name(self) -> str:
        return self.name or self.zone


POLICIES: dict[str, ZonePolicy] = {zone: ZonePolicy(zone) for zone in ZONES}

# The meridian-anthropic provider profile (profiles/anthropic.yaml, imported by the user).
# The proxy names a binary by its real path: the venv's python is a symlink to this file.
PYTHON = "/usr/local/bin/python3.14"
ANTHROPIC_HOST = "api.anthropic.com"
MESSAGES = HttpRule("POST", "/v1/messages")
# GET /v1/models is in the provider profile only for `meridian sandbox conformance`
# (status 200 shows the proxy substituted the credential); the narrator never calls it.
MODELS = HttpRule("GET", "/v1/models")


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    id: str
    display_name: str
    description: str
    category: str
    credential: str  # the credential's name in the profile
    env_var: str
    header_name: str
    endpoint: NetworkRule
    file: str  # profiles/<file>


PROFILES: dict[str, ProviderProfile] = {
    "meridian-anthropic": ProviderProfile(
        id="meridian-anthropic",
        display_name="Anthropic (Meridian narrator)",
        description="Anthropic Messages API for the rbqm.narrator agent",
        category="inference",
        credential="api_key",
        env_var="ANTHROPIC_API_KEY",
        header_name="x-api-key",
        endpoint=NetworkRule(
            name="meridian-anthropic",
            host=ANTHROPIC_HOST,
            port=443,
            rules=(MESSAGES, MODELS),
            binaries=(PYTHON,),
        ),
        file="anthropic.yaml",
    )
}
PROFILES_DIR = Path("profiles")

# rbqm.narrator: the rbqm filesystem rules, plus egress to the Messages API for the
# python binary only. The provider attached at create adds its own rule for the same
# endpoint (with the credential); this file states what the narrator itself needs, so
# the prover can check it against the narrator boundary.
NARRATOR_POLICY = "rbqm.narrator"
AGENT_POLICIES: dict[str, ZonePolicy] = {
    NARRATOR_POLICY: ZonePolicy(
        "rbqm",
        name=NARRATOR_POLICY,
        network=(
            NetworkRule(
                name="anthropic_messages",
                host=ANTHROPIC_HOST,
                port=443,
                rules=(MESSAGES,),
                binaries=(PYTHON,),
            ),
        ),
        providers=("meridian-anthropic",),
    )
}
# Every committed policy file: one per zone, one per agent with its own policy.
ALL_POLICIES: dict[str, ZonePolicy] = {**POLICIES, **AGENT_POLICIES}

# What OpenShell 0.1.7 adds to the effective filesystem policy whenever a policy has any
# network rule (measured: the same with or without a provider). There is no setting to
# turn it off; docs/threat-model.md records what it means for the narrator.
NETWORK_BASELINE_READ_ONLY: tuple[str, ...] = (
    "/usr",
    "/lib",
    "/etc",
    "/var/log",
    "/proc",
    "/dev/urandom",
)
NETWORK_BASELINE_READ_WRITE: tuple[str, ...] = ("/dev/null",)


def _list(key: str, items: Sequence[str]) -> list[str]:
    return [f"  {key}:", *(f"    - {item}" for item in items)]


def _network_block(key: str, rule: NetworkRule) -> list[str]:
    lines = [
        f"  {key}:",
        f"    name: {key}",
        "    endpoints:",
        f"      - host: {rule.host}",
        f"        port: {rule.port}",
        f"        protocol: {rule.protocol}",
        f"        enforcement: {rule.enforcement}",
        "        rules:",
    ]
    for r in rule.rules:
        lines += [
            "          - allow:",
            f"              method: {r.method}",
            f"              path: {r.path}",
        ]
    lines.append("    binaries:")
    lines += [f"      - path: {b}" for b in rule.binaries]
    return lines


def _network(blocks: Sequence[tuple[str, NetworkRule]]) -> list[str]:
    if not blocks:
        return ["network_policies: {}"]
    out = ["network_policies:"]
    for key, rule in sorted(blocks, key=lambda b: b[0]):
        out += _network_block(key, rule)
    return out


def _header(policy: ZonePolicy) -> list[str]:
    if policy.name:
        return [
            f"# OpenShell policy for the {policy.name} agent (zone {policy.zone}). Generated by",
            "# `meridian policy render` from src/meridian/sandbox/policies.py; do not edit by",
            "# hand. Sandboxes using it are created with --provider "
            f"{', '.join(policy.providers)}.",
        ]
    return [
        f"# OpenShell policy for the {policy.zone} zone. Generated by `meridian policy render`",
        "# from src/meridian/sandbox/policies.py; do not edit by hand.",
    ]


def _body(
    policy: ZonePolicy,
    read_only: Sequence[str],
    read_write: Sequence[str],
    network: Sequence[tuple[str, NetworkRule]],
) -> list[str]:
    return [
        f"version: {POLICY_VERSION}",
        "filesystem_policy:",
        "  include_workdir: false",
        *_list("read_only", read_only),
        *_list("read_write", read_write),
        "landlock:",
        f"  compatibility: {policy.landlock}",
        "process:",
        f"  run_as_user: {policy.run_as_user}",
        f"  run_as_group: {policy.run_as_group}",
        *_network(network),
    ]


def render(policy: ZonePolicy, header: Sequence[str] | None = None) -> str:
    """The policy as YAML, in the layout `openshell sandbox get --policy-only` prints."""
    lines = [
        *(header or _header(policy)),
        *_body(
            policy,
            policy.read_only,
            policy.read_write,
            [(rule.name, rule) for rule in policy.network],
        ),
    ]
    return "\n".join(lines) + "\n"


def provider_key(provider: str) -> str:
    """The name 0.1.7 gives a provider's rule in the effective policy."""
    return "_provider_" + provider.replace("-", "_")


def expected_effective(policy: ZonePolicy) -> str:
    """What the installed CLI (0.1.7) reports as the effective policy for this file.

    It drops comments and an empty network_policies, and ends without a newline. With any
    network rule it also appends the network baseline paths and adds one rule per attached
    provider (the provider profile's endpoint, under _provider_<name>), sorted by key.
    """
    if not policy.network:
        text = render(policy)
        kept = [
            line
            for line in text.splitlines()
            if not line.startswith("#") and line != "network_policies: {}"
        ]
        return "\n".join(kept)
    blocks = [(rule.name, rule) for rule in policy.network]
    blocks += [(provider_key(p), PROFILES[p].endpoint) for p in policy.providers]
    return "\n".join(
        _body(
            policy,
            (*policy.read_only, *NETWORK_BASELINE_READ_ONLY),
            (*policy.read_write, *NETWORK_BASELINE_READ_WRITE),
            blocks,
        )
    )


# Boundaries for openshell-prover (policies/boundaries/<name>.yaml). On 0.1.7 a boundary
# is an allow-list in the policy format, and a candidate filesystem path must appear in it
# literally, so the blinded boundary is exactly what a blinded zone may have: these paths,
# no network rules, run_as_user sandbox, landlock hard_requirement.
BOUNDARIES_DIR = "boundaries"
# The narrator boundary is the blinded one plus the Messages API for python only: the
# only policy allowed egress, and only there (phase 2 step 3).
BOUNDARIES: dict[str, ZonePolicy] = {
    "blinded": ZonePolicy("blinded"),
    "narrator": ZonePolicy("narrator", network=AGENT_POLICIES[NARRATOR_POLICY].network),
}
# The boundary each zone is proved against. The unblinded zone is reserved and holds the
# same policy for now; it gets its own boundary once it may read the unblinded staging
# path.
ZONE_BOUNDARY: dict[str, str] = dict.fromkeys(ZONES, "blinded")
# Every committed policy and the boundary it is proved against.
POLICY_BOUNDARY: dict[str, str] = {**ZONE_BOUNDARY, NARRATOR_POLICY: "narrator"}


def render_boundary(name: str) -> str:
    return render(
        BOUNDARIES[name],
        header=[
            f"# openshell-prover boundary {name}: the most any policy proved against it may",
            "# allow. Generated by `meridian policy render` from",
            "# src/meridian/sandbox/policies.py; do not edit by hand.",
        ],
    )


def boundary_path(name: str, root: Path = POLICIES_DIR) -> Path:
    return root / BOUNDARIES_DIR / f"{name}.yaml"


def policy_path(zone: str, root: Path = POLICIES_DIR) -> Path:
    return root / f"{zone}.yaml"


def render_all(root: Path = POLICIES_DIR) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for zone, policy in ALL_POLICIES.items():
        path = policy_path(zone, root)
        path.write_text(render(policy), encoding="utf-8")
        paths.append(path)
    for name in BOUNDARIES:
        path = boundary_path(name, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_boundary(name), encoding="utf-8")
        paths.append(path)
    return paths


def render_profile(profile: ProviderProfile) -> str:
    """An OpenShell 0.1.7 provider profile (no secrets): `openshell profile lint` passes."""
    e = profile.endpoint
    lines = [
        "# OpenShell 0.1.7 provider profile. Generated by `meridian policy render` from",
        "# src/meridian/sandbox/policies.py; do not edit by hand. No secrets in this file.",
        "#",
        f"# The provider created from it holds the real {profile.env_var} on the gateway.",
        "# Inside a sandbox the variable holds a placeholder; the policy proxy terminates TLS",
        "# (the sandbox trusts the supervisor CA through SSL_CERT_FILE), checks method and",
        f"# path, and writes the real key into the {profile.header_name} header only on these",
        f"# requests to {e.host}:{e.port}, and only from the image's interpreter. The proxy",
        f"# names a binary by its real path: {PYTHON} is the target of the",
        "# venv's /opt/meridian/venv/bin/python. GET /v1/models is here only for",
        "# `meridian sandbox conformance --policy rbqm.narrator`; the narrator only POSTs.",
        f"id: {profile.id}",
        f"display_name: {profile.display_name}",
        f"description: {profile.description}",
        f"category: {profile.category}",
        "credentials:",
        f"  - name: {profile.credential}",
        "    description: Anthropic API key",
        f"    env_vars: [{profile.env_var}]",
        "    required: true",
        "    auth_style: header",
        f"    header_name: {profile.header_name}",
        "endpoints:",
        f"  - host: {e.host}",
        f"    port: {e.port}",
        f"    protocol: {e.protocol}",
        f"    enforcement: {e.enforcement}",
        "    rules:",
    ]
    for r in e.rules:
        lines += ["      - allow:", f"          method: {r.method}", f"          path: {r.path}"]
    lines.append("binaries:")
    lines += [f"  - {b}" for b in e.binaries]
    return "\n".join(lines) + "\n"


def profile_path(provider: str, root: Path = PROFILES_DIR) -> Path:
    return root / PROFILES[provider].file


def render_profiles(root: Path = PROFILES_DIR) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for provider, profile in PROFILES.items():
        path = profile_path(provider, root)
        path.write_text(render_profile(profile), encoding="utf-8")
        paths.append(path)
    return paths


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
