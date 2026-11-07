"""meridian sandbox verify: the same drafts in process and in a sandbox, byte for byte.

Runs each enabled agent on each cut through both executors directly (nothing is written
to the ledger) and compares the canonical JSON of the drafts documents.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from meridian.agents.execute import DraftsDocument
from meridian.agents.registry import AgentRegistry
from meridian.data.datacut import Ledger, data_cut_events
from meridian.domain.events import DataCut, Event
from meridian.sandbox.executor import ExecutionResult, Executor


@dataclass(frozen=True, slots=True)
class VerifyRow:
    agent_id: str
    cut_id: str
    identical: bool
    drafts: int
    sandbox_ms: int
    inprocess_ms: int
    policy_sha256: str | None


def document(agent_id: str, version: str, result: ExecutionResult) -> str:
    return DraftsDocument(agent_id=agent_id, agent_version=version, drafts=result.drafts).dumps()


def trigger_for(ledger: Ledger, study_id: str, cut_id: str) -> Event:
    for event in data_cut_events(ledger, study_id):
        if isinstance(event.payload, DataCut) and event.payload.cut_id == cut_id:
            return event
    raise LookupError(f"no DataCut event for {cut_id} in the ledger")


def verify(
    *,
    ledger: Ledger,
    study_id: str,
    registry: AgentRegistry,
    inprocess: Executor,
    sandboxed: Executor,
    cuts: Sequence[str],
) -> list[VerifyRow]:
    rows: list[VerifyRow] = []
    for cut_id in cuts:
        trigger = trigger_for(ledger, study_id, cut_id)
        for registered in registry.enabled():
            agent = registered.agent
            here = inprocess.run(agent.agent_id, trigger, cut_id)
            there = sandboxed.run(agent.agent_id, trigger, cut_id)
            a = document(agent.agent_id, agent.version, here)
            b = document(agent.agent_id, agent.version, there)
            rows.append(
                VerifyRow(
                    agent_id=agent.agent_id,
                    cut_id=cut_id,
                    identical=a.encode() == b.encode(),
                    drafts=len(here.drafts),
                    sandbox_ms=there.duration_ms,
                    inprocess_ms=here.duration_ms,
                    policy_sha256=there.policy_sha256,
                )
            )
    return rows
