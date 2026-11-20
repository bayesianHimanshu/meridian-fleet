"""Match each rbqm.narrator 1.1.0 narrative to its 1.0.0 counterparts, for docs/narrator-eval.md.

    uv run python scripts/narrator_rerun_table.py

Counterparts share (cut, kind, site, check). 1.0.0 narrated findings of several detector
versions, so a 1.1.0 narrative can have several counterparts or none (csm 1.0.0 findings
were keyed with check "-"; the 2.x detectors do not raise terminal-digit findings at all).
Prints the match table, the five known-bad 1.0.0 cases with their counterparts' text, and
1.1.0's tokens. Reads the ledger only.
"""

from collections import defaultdict
from pathlib import Path

from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType
from meridian.domain.events import Event, FindingNarrative, FindingRaised
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import SystemClock
from meridian.platform.config import Settings

CUTS = ("DC-BASE", "DC-RBQM-CORE", "DC-51448ab6")
KNOWN_BAD = ("RBQM-0001", "RBQM-0006", "RBQM-0010", "RBQM-0035", "RBQM-0037")
Key = tuple[str, str, str, str]


def _text(n: FindingNarrative) -> str:
    if n.status == "accepted":
        return f"{n.headline}. {n.explanation}"
    return f"({n.status}: {', '.join(n.rejection_reasons) or n.error})"


def main() -> None:
    s = Settings()
    ledger = SqliteLedger(
        path=Path(s.ledger_path), clock=SystemClock(), finding_id_prefixes=s.finding_id_prefixes
    )
    key_of: dict[int, Key] = {}
    version_of: dict[int, str] = {}
    for e in ledger.read(event_types=[EventType.FINDING_RAISED]):
        if isinstance(e, Event) and isinstance(e.payload, FindingRaised):
            f, p = e.payload.finding, parse_dedup_key(e.payload.dedup_key)
            key_of[e.seq] = (p.cut_id, f.kind.value, f.site_id or "STUDY", p.check)
            version_of[e.seq] = f"{f.agent_id.split('.')[1]} {f.agent_version}"
    by_version: dict[str, dict[Key, list[FindingNarrative]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for e in ledger.read(event_types=[EventType.FINDING_NARRATIVE]):
        if isinstance(e, Event) and isinstance(e.payload, FindingNarrative):
            n = e.payload
            if n.status != "error" and key_of[n.finding_seq][0] in CUTS:
                by_version[n.agent_version][key_of[n.finding_seq]].append(n)
    ledger.close()
    new, old = by_version["1.1.0"], by_version["1.0.0"]

    print("| Cut | Kind | Site | Check | 1.1.0 (detector) | Status | 1.0.0 counterparts |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for key in sorted(new):
        for n in new[key]:
            olds = old.get(key, [])
            counterpart = "; ".join(f"{o.finding_id} {o.status}" for o in olds) if olds else "none"
            print(
                f"| {key[0]} | {key[1]} | {key[2]} | {key[3]} | {n.finding_id} "
                f"({version_of[n.finding_seq]}) | {n.status} | {counterpart} |"
            )
    print("\n## Known-bad 1.0.0 cases\n")
    olds_by_id = {n.finding_id: (k, n) for k, ns in old.items() for n in ns}
    for fid in KNOWN_BAD:
        key, o = olds_by_id[fid]
        print(f"### {fid} {key}\nold: {_text(o)}")
        matches = new.get(key, [])
        if not matches:
            print("new: no 1.1.0 counterpart (the new detector versions raise no finding with")
            print("     this (cut, kind, site, check))")
        for n in matches:
            print(f"new ({n.finding_id}): {_text(n)}")
        print()
    narratives = [n for ns in new.values() for n in ns]
    t_in = sum(n.input_tokens or 0 for n in narratives)
    t_out = sum(n.output_tokens or 0 for n in narratives)
    print(f"1.1.0: {len(narratives)} narratives, input {t_in}, output {t_out} tokens")


if __name__ == "__main__":
    main()
