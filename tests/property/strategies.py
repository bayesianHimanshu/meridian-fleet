"""Hypothesis strategies for ledger drafts."""

from datetime import UTC, datetime, timedelta, timezone

from hypothesis import strategies as st

from meridian.domain.enums import DecisionAction, FindingKind, Role, Severity, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    DataCut,
    DocumentFiled,
    EventDraft,
    EventPayload,
    FindingRaised,
    HumanDecision,
    PolicyDenied,
)
from meridian.domain.findings import Decision, Evidence, Finding
from meridian.platform.config import DEFAULT_FINDING_ID_PREFIXES

text = st.text(min_size=1, max_size=24)
ids = st.text(min_size=1, max_size=24)
finite = st.floats(allow_nan=False, allow_infinity=False)
zones = st.sampled_from(sorted(DEFAULT_FINDING_ID_PREFIXES))
offsets = st.sampled_from(
    [UTC, timezone(timedelta(hours=5, minutes=30)), timezone(timedelta(hours=-8))]
)
instants = st.datetimes(
    min_value=datetime(2000, 1, 1),  # noqa: DTZ001
    max_value=datetime(2100, 1, 1),  # noqa: DTZ001
    timezones=offsets,
)
evidence = st.builds(
    Evidence,
    source=text,
    selector=text,
    statistic=text,
    value=finite,
    threshold=st.none() | finite,
)


@st.composite
def findings(draw: st.DrawFn) -> Finding:
    return Finding(
        kind=draw(st.sampled_from(FindingKind)),
        agent_id=f"{draw(zones)}.{draw(ids)}",
        agent_version=draw(text),
        study_id=draw(ids),
        site_id=draw(st.none() | ids),
        severity=draw(st.sampled_from(Severity)),
        title=draw(text),
        summary=draw(text),
        evidence=tuple(draw(st.lists(evidence, min_size=1, max_size=3))),
        visibility=draw(st.sampled_from(Visibility)),
        visible_to=frozenset(draw(st.sets(st.sampled_from(Role)))),
        score=draw(finite),
    )


decisions = st.builds(
    Decision,
    finding_id=st.from_regex(r"\A[A-Z]{2,4}-[0-9]{4}\Z"),
    action=st.sampled_from([DecisionAction.ACCEPTED, DecisionAction.ESCALATED]),
    actor=ids,
    role=st.sampled_from([r for r in Role if r is not Role.QA_AUDITOR]),
    reason=st.none() | text,
)

payloads: st.SearchStrategy[EventPayload] = st.one_of(
    st.builds(DataCut, cut_id=ids, study_id=ids, path=text),
    st.builds(DocumentFiled, doc_id=ids, study_id=ids, zone=text, artifact=text),
    st.builds(ActorWoke, agent_id=ids, trigger_seq=st.integers(1, 10**9)),
    st.builds(ActorSuspended, agent_id=ids, trigger_seq=st.integers(1, 10**9)),
    st.builds(FindingRaised, finding=findings(), dedup_key=st.just("placeholder")),
    st.builds(PolicyDenied, agent_id=ids, resource=text, policy=text, reason=text),
    st.builds(HumanDecision, decision=decisions),
)


@st.composite
def drafts(draw: st.DrawFn) -> EventDraft:
    payload = draw(payloads)
    visibility = draw(st.sampled_from(Visibility))
    if isinstance(payload, FindingRaised) and payload.finding.visibility is Visibility.RESTRICTED:
        visibility = Visibility.RESTRICTED
    return EventDraft(
        occurred_at=draw(instants),
        actor=draw(ids),
        study_id=draw(st.none() | ids),
        visibility=visibility,
        payload=payload,
    )


def with_unique_dedup_key(draft: EventDraft, index: int) -> EventDraft:
    """Dedup keys must be unique in a ledger, so each generated finding gets its own."""
    if not isinstance(draft.payload, FindingRaised):
        return draft
    payload = draft.payload.model_copy(update={"dedup_key": f"key-{index}"})
    return draft.model_copy(update={"payload": payload})
