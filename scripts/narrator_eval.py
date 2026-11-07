"""Summarize rbqm.narrator's FindingNarrative events for docs/narrator-eval.md.

    uv run python scripts/narrator_eval.py [--cut DC-BASE --cut ...]

Prints counts by status, rejection reasons, tokens and an approximate cost, and every
accepted narrative next to its finding's summary and evidence. Reads the ledger only.
"""

import argparse
import collections
from pathlib import Path

from meridian.agents.rbqm import narrator
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType
from meridian.domain.events import Event, FindingNarrative, FindingRaised
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import SystemClock
from meridian.platform.config import Settings

# USD per million tokens for claude-haiku-5-5 (prompts up to 100K tokens), from the
# Anthropic model table cached 2026-10-06. Approximate: check current pricing.
PRICE_IN = 0.10
PRICE_OUT = 0.50
CUTS = ("DC-BASE", "DC-RBQM-CORE", "DC-51448ab6")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cut", action="append", dest="cuts", default=None)
    parser.add_argument("--version", default=narrator.VERSION, help="narrator version")
    args = parser.parse_args()
    cuts = set(args.cuts or CUTS)
    s = Settings()
    ledger = SqliteLedger(
        path=Path(s.ledger_path), clock=SystemClock(), finding_id_prefixes=s.finding_id_prefixes
    )
    raised: dict[int, tuple[FindingRaised, str]] = {}
    for e in ledger.read(event_types=[EventType.FINDING_RAISED]):
        if isinstance(e, Event) and isinstance(e.payload, FindingRaised):
            raised[e.seq] = (e.payload, parse_dedup_key(e.payload.dedup_key).cut_id)
    rows: list[tuple[int, FindingNarrative, FindingRaised, str]] = []
    for e in ledger.read(event_types=[EventType.FINDING_NARRATIVE]):
        if not isinstance(e, Event) or not isinstance(e.payload, FindingNarrative):
            continue
        n = e.payload
        finding, cut = raised[n.finding_seq]
        if n.agent_version == args.version and cut in cuts:
            rows.append((e.seq, n, finding, cut))
    ledger.close()

    by_status = collections.Counter(n.status for _, n, _, _ in rows)
    reasons = collections.Counter(r for _, n, _, _ in rows for r in n.rejection_reasons)
    tokens_in = sum(n.input_tokens or 0 for _, n, _, _ in rows)
    tokens_out = sum(n.output_tokens or 0 for _, n, _, _ in rows)
    findings = {f.finding.finding_id for _, _, f, _ in rows}
    print(f"findings narrated: {len(findings)} in cuts {', '.join(sorted(cuts))}")
    print(f"events: {len(rows)}")
    print("| Status | Events |\n| --- | ---: |")
    for status in ("accepted", "rejected", "refused", "error"):
        print(f"| {status} | {by_status.get(status, 0)} |")
    print("\nrejection or refusal reasons:", dict(reasons) or "none")
    for _, n, _, _ in rows:
        if n.status == "error":
            print(f"error {n.finding_id}: {n.error}")
    cost = tokens_in / 1e6 * PRICE_IN + tokens_out / 1e6 * PRICE_OUT
    latencies = sorted(n.latency_ms for _, n, _, _ in rows if n.latency_ms is not None)
    print(f"\ntokens: input {tokens_in}, output {tokens_out}; approximate cost ${cost:.4f}")
    if latencies:
        print(f"latency ms: median {latencies[len(latencies) // 2]}, max {latencies[-1]}")
    sandboxes = sorted({n.sandbox for _, n, _, _ in rows if n.sandbox})
    print(f"sandboxes: {len(sandboxes)}")
    for seq, n, f, cut in rows:
        if n.status != "accepted":
            continue
        fi = f.finding
        print(f"\n### {n.finding_id} ({cut}, {fi.kind.value}, site {fi.site_id}) seq {seq}")
        print(f"summary: {fi.summary}")
        for ev in fi.evidence:
            thr = "" if ev.threshold is None else f" (threshold {ev.threshold:.4g})"
            print(f"- {ev.statistic} = {ev.value:.4g}{thr}")
        print(f"headline: {n.headline}")
        print(f"explanation: {n.explanation}")


if __name__ == "__main__":
    main()
