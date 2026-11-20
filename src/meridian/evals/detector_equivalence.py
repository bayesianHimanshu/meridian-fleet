"""Do the step 4 detector versions flag exactly what the previous versions did, on every cut?

For each cut both versions of an agent ran on (ActorSuspended), compares the sets of
(kind, site, check) flagged by the previous version (kri_monitor 2.1.0, qtl_detector
2.0.0, csm 2.0.0) and the new one (2.2.0, 2.1.0, 2.1.0), and checks that every evidence
item of each old finding (statistic, value, threshold) is in the new finding unchanged.
Reads the ledger only. scripts/detector_equivalence.py prints the result.
"""

from collections import defaultdict
from dataclasses import dataclass, field

from meridian.data.datacut import data_cut_events
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType
from meridian.domain.events import ActorSuspended, DataCut, Event, FindingRaised
from meridian.ledger.store import SqliteLedger

PAIRS: dict[str, tuple[str, str]] = {
    "rbqm.kri_monitor": ("2.1.0", "2.2.0"),
    "rbqm.qtl_detector": ("2.0.0", "2.1.0"),
    "rbqm.csm": ("2.0.0", "2.1.0"),
}
Key = tuple[str, str, str]


@dataclass
class Comparison:
    cuts: int = 0
    pairs: int = 0
    findings_old: int = 0
    findings_new: int = 0
    differences: list[str] = field(default_factory=list)

    @property
    def equivalent(self) -> bool:
        return not self.differences and self.pairs > 0


def compare(ledger: SqliteLedger, study_id: str) -> Comparison:
    cut_of = {
        e.seq: e.payload.cut_id
        for e in data_cut_events(ledger, study_id)
        if isinstance(e.payload, DataCut)
    }
    ran: set[tuple[str, str | None, str]] = set()
    for e in ledger.read(event_types=[EventType.ACTOR_SUSPENDED]):
        if isinstance(e, Event) and isinstance(e.payload, ActorSuspended):
            p = e.payload
            if p.trigger_seq in cut_of:
                ran.add((p.agent_id, p.agent_version, cut_of[p.trigger_seq]))
    found: dict[tuple[str, str, str], dict[Key, set[tuple[str, float, float | None]]]] = (
        defaultdict(dict)
    )
    for e in ledger.read(event_types=[EventType.FINDING_RAISED]):
        if not isinstance(e, Event) or not isinstance(e.payload, FindingRaised):
            continue
        f, parts = e.payload.finding, parse_dedup_key(e.payload.dedup_key)
        key: Key = (f.kind.value, f.site_id or "STUDY", parts.check)
        found[(f.agent_id, f.agent_version, parts.cut_id)][key] = {
            (x.statistic, x.value, x.threshold) for x in f.evidence
        }
    out = Comparison(cuts=len(cut_of))
    for agent, (old, new) in PAIRS.items():
        for cut in sorted(set(cut_of.values())):
            if (agent, old, cut) not in ran or (agent, new, cut) not in ran:
                continue
            out.pairs += 1
            a, b = found[(agent, old, cut)], found[(agent, new, cut)]
            out.findings_old += len(a)
            out.findings_new += len(b)
            if set(a) != set(b):
                out.differences.append(
                    f"{agent} {cut}: only {old} {sorted(set(a) - set(b))}, "
                    f"only {new} {sorted(set(b) - set(a))}"
                )
                continue
            for key in sorted(a):
                if not a[key] <= b[key]:
                    out.differences.append(f"{agent} {cut} {key}: an old evidence item changed")
    return out
