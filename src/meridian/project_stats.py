"""The project's numbers, computed from the ledger (wrap-up): `meridian stats` and
scripts/project_stats.py. Every number the README and docs/project-summary.md quote comes
from this output, with the ledger seq it was computed at.

Read only: the ledger is opened, read in full (restricted events included, as the
unblinded statistician reads them, since counting needs every event) and verified;
nothing is written. Leak checks are not ledger events: they are result files in
var/leakcheck (and phase 4a's canary in var/substrate), reported apart and marked so.
"""

import json
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from meridian.domain.enums import EventType, Role
from meridian.domain.events import (
    ActorSuspended,
    AttributionCorrection,
    AuditReviewRequested,
    ContainmentCheckCompleted,
    DataCut,
    Event,
    FindingAssessment,
    FindingNarrative,
    FindingRaised,
    GateDecision,
    GateRequest,
    HumanDecision,
    InventoryLoaded,
    NoaelDecision,
    PolicyDenied,
    PortfolioReviewRequested,
    SendStudyLoaded,
    SignalScanRequested,
)
from meridian.ledger.store import SqliteLedger
from meridian.ledger.verify import verify

Stats = dict[str, Any]


def _sorted(counter: Mapping[str, int]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def _cluster(image: str | None) -> str:
    """kind pushes to the local registry; GKE pulls from Artifact Registry."""
    return "gke" if image and "pkg.dev" in image else "kind"


def compute(
    ledger: SqliteLedger,
    *,
    operational_cuts: Collection[str],
    people: Mapping[str, str],
    var_dir: Path,
) -> Stats:
    """people maps each person id to their role (config/people.toml)."""
    events = [
        e for e in ledger.read(viewer_role=Role.UNBLINDED_STATISTICIAN) if isinstance(e, Event)
    ]
    report = verify(ledger)
    at_seq = events[-1].seq if events else 0

    by_type = Counter(e.event_type.value for e in events)

    # The cut each trigger names, and whether it is operational (the study's own data, a
    # live scan, a review or gate request, an audit of the real ledger) or evaluation.
    cut_of: dict[int, str] = {}
    scope_of: dict[int, str] = {}
    for e in events:
        p = e.payload
        if isinstance(p, DataCut | SendStudyLoaded | InventoryLoaded):
            cut_of[e.seq] = p.cut_id
            scope_of[e.seq] = "operational" if p.cut_id in operational_cuts else "evaluation"
            if isinstance(p, InventoryLoaded):
                scope_of[e.seq] = "operational"  # the asset's one inventory
        elif isinstance(p, SignalScanRequested):
            cut_of[e.seq], scope_of[e.seq] = p.scan_id, "operational"
        elif isinstance(p, PortfolioReviewRequested):
            cut_of[e.seq], scope_of[e.seq] = p.review_id, "operational"
        elif isinstance(p, GateRequest):
            cut_of[e.seq], scope_of[e.seq] = p.gate_request_id, "operational"
        elif isinstance(p, AuditReviewRequested):
            cut_of[e.seq], scope_of[e.seq] = f"AUDIT-{e.seq}", "operational"
        elif isinstance(p, NoaelDecision | HumanDecision):
            scope_of[e.seq] = "operational"  # a decision on the study's data

    # Findings: by zone, agent and status (the latest decision on each, or open).
    raised = [e for e in events if isinstance(e.payload, FindingRaised)]
    latest: dict[str, str] = {}
    for e in events:
        if isinstance(e.payload, HumanDecision):
            latest[e.payload.decision.finding_id] = e.payload.decision.action.value
    by_zone: Counter[str] = Counter()
    by_agent: Counter[str] = Counter()
    by_status: Counter[str] = Counter()
    by_zone_status: dict[str, Counter[str]] = defaultdict(Counter)
    by_person: Counter[str] = Counter()
    for e in raised:
        assert isinstance(e.payload, FindingRaised)
        f = e.payload.finding
        zone = f.agent_id.split(".")[0]
        status = latest.get(f.finding_id or "", "open (no decision)")
        by_zone[zone] += 1
        by_agent[f"{f.agent_id} {f.agent_version}"] += 1
        by_status[status] += 1
        by_zone_status[zone][status] += 1
        if e.actor in people:
            by_person[people[e.actor]] += 1

    # Human decisions: every event whose actor is a person, by role and action.
    decisions: Counter[str] = Counter()
    decision_seqs: dict[str, list[int]] = defaultdict(list)
    for e in events:
        if e.actor not in people:
            continue
        p = e.payload
        role = people[e.actor]
        if isinstance(p, HumanDecision):
            action = p.decision.action.value
        elif isinstance(p, NoaelDecision):
            action = "noael_decision"
        elif isinstance(p, GateDecision):
            action = f"gate_decision:{p.decision}"
        elif isinstance(p, GateRequest):
            action = "gate_request"
        elif isinstance(p, PortfolioReviewRequested):
            action = "portfolio_review_request"
        elif isinstance(p, FindingRaised):
            action = "observation_raised"
        else:
            action = e.event_type.value
        key = f"{role}: {action}"
        decisions[key] += 1
        decision_seqs[key].append(e.seq)

    # Runs: completed runs (ActorSuspended) by executor, cluster and cut scope.
    runs_by_executor: Counter[str] = Counter()
    runs_by_scope: Counter[str] = Counter()
    runs_by_executor_scope: dict[str, Counter[str]] = defaultdict(Counter)
    agents: dict[str, set[str]] = defaultdict(set)
    for e in events:
        p = e.payload
        if isinstance(p, ActorSuspended):
            executor = p.executor or "inprocess (phase 1, before sandboxes)"
            if p.executor == "substrate" and p.substrate is not None:
                executor = f"substrate ({_cluster(p.substrate.image)})"
            scope = scope_of.get(p.trigger_seq, "evaluation")
            runs_by_executor[executor] += 1
            runs_by_scope[scope] += 1
            runs_by_executor_scope[executor][scope] += 1
            if p.agent_version:
                agents[p.agent_id].add(p.agent_version)
        elif isinstance(p, FindingNarrative | FindingAssessment):
            agents[p.agent_id].add(p.agent_version)
        elif isinstance(p, FindingRaised):
            agents[p.finding.agent_id].add(p.finding.agent_version)
    woke = by_type.get(EventType.ACTOR_WOKE.value, 0)
    suspended = by_type.get(EventType.ACTOR_SUSPENDED.value, 0)

    # Containment: refusals recorded, and conformance runs.
    refusals_by_source: Counter[str] = Counter()
    refusals_by_zone: Counter[str] = Counter()
    for e in events:
        if isinstance(e.payload, PolicyDenied):
            refusals_by_source[e.payload.source or "unrecorded"] += 1
            refusals_by_zone[e.payload.zone or "unrecorded"] += 1
    conformance: Counter[str] = Counter()
    for e in events:
        if isinstance(e.payload, ContainmentCheckCompleted):
            executor = getattr(e.payload, "executor", None) or "openshell"
            conformance[f"{executor}: {'passed' if e.payload.passed else 'failed'}"] += 1

    corrections = [
        {"seq": e.seq, "corrects_seq": e.payload.corrects_seq, "field": e.payload.field}
        for e in events
        if isinstance(e.payload, AttributionCorrection)
    ]

    return {
        "computed_at_seq": at_seq,
        "first_recorded_at": events[0].recorded_at.isoformat() if events else None,
        "last_recorded_at": events[-1].recorded_at.isoformat() if events else None,
        "verification": report.model_dump(mode="json"),
        "events": {"total": len(events), "by_type": _sorted(by_type)},
        "findings": {
            "total": len(raised),
            "by_zone": _sorted(by_zone),
            "by_agent_version": dict(sorted(by_agent.items())),
            "by_status": _sorted(by_status),
            "by_zone_and_status": {z: _sorted(c) for z, c in sorted(by_zone_status.items())},
            "raised_by_a_person_by_role": _sorted(by_person),
        },
        "human_decisions": {
            "total": sum(decisions.values()),
            "by_role_and_action": _sorted(decisions),
            "seqs": {k: _ranges(v) for k, v in sorted(decision_seqs.items())},
            "attribution_corrections": corrections,
        },
        "runs": {
            "completed": suspended,
            "started": woke,
            "started_not_completed": woke - suspended,
            "by_executor": _sorted(runs_by_executor),
            "by_cut_scope": _sorted(runs_by_scope),
            "by_executor_and_scope": {
                x: _sorted(c) for x, c in sorted(runs_by_executor_scope.items())
            },
        },
        "containment": {
            "policy_refusals_recorded": sum(refusals_by_source.values()),
            "policy_refusals_by_source": _sorted(refusals_by_source),
            "policy_refusals_by_zone": _sorted(refusals_by_zone),
            "conformance_runs": _sorted(conformance),
        },
        "leak_checks": leak_checks(var_dir),
        "agents": {a: sorted(v, key=_version_key) for a, v in sorted(agents.items())},
    }


def _version_key(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _ranges(seqs: list[int]) -> str:
    """1, 2, 3, 7 as "1 to 3, 7"."""
    out: list[str] = []
    start = prev = seqs[0]
    for s in [*seqs[1:], None]:
        if s is not None and s == prev + 1:
            prev = s
            continue
        out.append(str(start) if start == prev else f"{start} to {prev}")
        if s is not None:
            start = prev = s
    return ", ".join(out)


def leak_checks(var_dir: Path) -> Stats:
    """Leak check result files (not ledger events): var/leakcheck/<cluster>-<stamp>.json
    from `meridian leakcheck` (phase 4b), and phase 4a's canary files in var/substrate."""
    rows = []
    for path in sorted((var_dir / "leakcheck").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        sources = data.get("sources") or []
        rows.append(
            {
                "file": f"var/leakcheck/{path.name}",
                "cluster": path.name.split("-")[0],
                "passed": bool(data.get("passed")),
                "runs": len(data["runs"]) if isinstance(data.get("runs"), list) else None,
                "sources": len(sources) if isinstance(sources, list | dict) else None,
                "failures": [str(f)[:160] for f in data.get("failures") or []],
            }
        )
    canaries = sorted(
        f"var/substrate/{p.name}" for p in (var_dir / "substrate").glob("canary*.json")
    )
    return {
        "note": "result files, not ledger events",
        "run": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "by_cluster": _sorted(Counter(str(r["cluster"]) for r in rows)),
        "files": rows,
        # Phase 4a's C1 canary proof (before `meridian leakcheck`): no verdict field in the
        # file; docs/substrate-eval.md reports the result.
        "phase4a_canary_files": canaries,
    }


def markdown(s: Stats) -> str:
    """The same numbers as a Markdown page."""

    def table(title: str, rows: Mapping[str, object], head: str = "Count") -> list[str]:
        out = [f"| {title} | {head} |", "|---|---:|"]
        out += [f"| {k} | {v} |" for k, v in rows.items()]
        return [*out, ""]

    v = s["verification"]
    lines = [
        "# Project stats",
        "",
        f"Computed from the ledger at seq **{s['computed_at_seq']}** by `meridian stats` "
        "(src/meridian/project_stats.py). Do not edit by hand: run "
        "`uv run python scripts/project_stats.py` again.",
        "",
        f"Ledger verification: **{'ok' if v['ok'] else 'BROKEN'}**, {v['events_checked']} events "
        f"checked. First event recorded {s['first_recorded_at']}, last {s['last_recorded_at']}.",
        "",
        "## Events by type",
        "",
        *table("Event type", s["events"]["by_type"]),
        f"Total: {s['events']['total']}.",
        "",
        "## Findings",
        "",
        f"{s['findings']['total']} findings raised. Status is the latest human decision on "
        "each finding, or open; a finding replaced by a newer agent version keeps its own "
        "status here.",
        "",
        *table("Zone", s["findings"]["by_zone"]),
        *table("Status", s["findings"]["by_status"]),
        "| Zone | Status counts |",
        "|---|---|",
        *[
            f"| {z} | {', '.join(f'{k} {n}' for k, n in c.items())} |"
            for z, c in s["findings"]["by_zone_and_status"].items()
        ],
        "",
        *table("Agent and version", s["findings"]["by_agent_version"]),
        "## Human decisions",
        "",
        "Every event whose actor is a person (config/people.toml), by the person's role. "
        "People act only through the Fleet API.",
        "",
        "| Role: action | Count | Ledger seqs |",
        "|---|---:|---|",
        *[
            f"| {k} | {n} | {s['human_decisions']['seqs'][k]} |"
            for k, n in s["human_decisions"]["by_role_and_action"].items()
        ],
        "",
        f"Total: {s['human_decisions']['total']}. Attribution corrections: "
        + (
            ", ".join(
                f"seq {c['seq']} corrects seq {c['corrects_seq']} ({c['field']})"
                for c in s["human_decisions"]["attribution_corrections"]
            )
            or "none"
        )
        + ".",
        "",
        "## Runs",
        "",
        f"{s['runs']['completed']} runs completed (ActorSuspended); {s['runs']['started']} "
        f"started (ActorWoke); {s['runs']['started_not_completed']} started and not "
        "completed (failed, retried as a new run).",
        "",
        *table("Executor", s["runs"]["by_executor"]),
        *table("Cut scope", s["runs"]["by_cut_scope"]),
        "| Executor | By scope |",
        "|---|---|",
        *[
            f"| {x} | {', '.join(f'{k} {n}' for k, n in c.items())} |"
            for x, c in s["runs"]["by_executor_and_scope"].items()
        ],
        "",
        "## Containment",
        "",
        f"{s['containment']['policy_refusals_recorded']} policy refusals recorded (PolicyDenied).",
        "",
        *table("Refusal source", s["containment"]["policy_refusals_by_source"]),
        *table("Refusals by zone", s["containment"]["policy_refusals_by_zone"]),
        *table("Conformance runs", s["containment"]["conformance_runs"]),
        "## Leak checks (result files, not ledger events)",
        "",
        f"{s['leak_checks']['run']} run, {s['leak_checks']['passed']} passed.",
        "",
        "| File | Cluster | Passed | Runs | Why it failed |",
        "|---|---|---|---:|---|",
        *[
            f"| {r['file']} | {r['cluster']} | {'yes' if r['passed'] else 'no'} | "
            f"{r['runs'] if r['runs'] is not None else ''} | {'; '.join(r['failures'])} |"
            for r in s["leak_checks"]["files"]
        ],
        "",
        "Phase 4a's canary proof files (no verdict field; docs/substrate-eval.md, C1): "
        + (", ".join(s["leak_checks"]["phase4a_canary_files"]) or "none")
        + ".",
        "",
        "## Agents and versions",
        "",
        f"{len(s['agents'])} agents.",
        "",
        "| Agent | Versions |",
        "|---|---|",
        *[f"| {a} | {', '.join(v)} |" for a, v in s["agents"].items()],
        "",
    ]
    return "\n".join(lines)


__all__ = ["Stats", "compute", "leak_checks", "markdown"]
