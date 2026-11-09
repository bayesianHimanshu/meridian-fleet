"""safety.term_adjudicator 1.0.0: do two event terms describe the same event?

safety.sae_reconciler decides nothing about meaning: when a subject's only unmatched
serious AE and only unmatched case have different preferred terms it raises
sae_term_mismatch, and when the terms are too different even for that, an
sae_missing_in_safety_db and a case_without_clinical_sae. This agent asks the local model
whether the two terms are one event, for

* term_mismatch: each sae_term_mismatch finding, and
* unpaired_pair: each sae_missing_in_safety_db and case_without_clinical_sae finding of
  the same subject in the same cut, with onsets within the reconciler's pairing window
  (fallback_window_days).

The model sees the two preferred terms and the two verbatim terms only: no date, no
subject id, no case id (they do not help the judgement). The host reads the verbatim terms
from the records the findings' evidence names (AE.AETERM by USUBJID and AESEQ; the case's
REPORTED_TERM by CASEID and VERSION). It answers {"same_event": "yes" | "no" | "unsure",
"reason": "..."}; the validator below decides whether the answer may be shown. The
deterministic findings never change: the outcome is a FindingAssessment a person reads.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from meridian.agents.rbqm.narrator_v1_0 import EM_DASH, SUBJECT_ID, sentence_count, strip_code_fence
from meridian.agents.safety.sae_reconciler import Onset, gap, parse_onset
from meridian.domain import safety as s
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.events import AssessmentCandidate, Event, FindingRaised, Verdict
from meridian.domain.findings import Evidence, Finding
from meridian.ledger.hashing import canonical_json
from meridian.llm.local import LocalModelOptions, LocalModelRequest, LocalModelResponse

AGENT_ID = "safety.term_adjudicator"
VERSION = "1.0.0"
VALIDATOR_VERSION = "1.0.0"
# FindingRaised events of exactly these detector versions.
ADJUDICATES: dict[str, str] = {"safety.sae_reconciler": "1.1.1"}
VERDICTS: tuple[Verdict, ...] = ("yes", "no", "unsure")
MAX_REASON = 200
MAX_REASON_SENTENCES = 1
INPUT_KEYS = frozenset({"record_a", "record_b"})
RECORD_KEYS = frozenset({"preferred_term", "verbatim_term"})
# Dates and times in any common form, for the input check.
_DATE = re.compile(r"\d{4}-\d{2}(-\d{2})?|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|\b(19|20)\d{2}\b")
_CASE_ID = re.compile(r"\bSC-[0-9A-F]{6,}\b")


@dataclass(frozen=True, slots=True)
class Term:
    preferred: str
    verbatim: str


@dataclass(frozen=True, slots=True)
class ClinicalRef:
    cut_id: str
    usubjid: str
    aeseq: int


@dataclass(frozen=True, slots=True)
class CaseRef:
    cut_id: str
    caseid: str
    version: int | None


@dataclass(frozen=True, slots=True)
class Candidate:
    """Two records that may be one event, and the findings that raised them."""

    kind: AssessmentCandidate
    events: tuple[Event, ...]  # the FindingRaised events, in finding id order
    clinical: ClinicalRef
    case: CaseRef

    @property
    def finding_ids(self) -> tuple[str, ...]:
        return tuple(_finding(e).finding_id or "" for e in self.events)


class TermLookup:
    """Reads (preferred, verbatim) of a clinical AE and of a safety case on the host."""

    def __init__(
        self,
        clinical: Callable[[ClinicalRef], Term | None],
        case: Callable[[CaseRef], Term | None],
    ) -> None:
        self.clinical = clinical
        self.case = case


def _finding(event: Event) -> Finding:
    assert isinstance(event.payload, FindingRaised)
    return event.payload.finding


def check_of(event: Event) -> str:
    assert isinstance(event.payload, FindingRaised)
    return s.check_name(parse_dedup_key(event.payload.dedup_key).check)


def cut_of(event: Event) -> str:
    assert isinstance(event.payload, FindingRaised)
    return parse_dedup_key(event.payload.dedup_key).cut_id


def may_adjudicate(finding: Finding) -> bool:
    return ADJUDICATES.get(finding.agent_id) == finding.agent_version


def _selector(selector: str) -> dict[str, str]:
    out = {}
    for part in selector.split(";"):
        key, _, value = part.strip().partition("=")
        if key:
            out[key.strip()] = value.strip()
    return out


def _evidence(finding: Finding, statistic: str) -> Evidence | None:
    return next((e for e in finding.evidence if e.statistic == statistic), None)


def _clinical_ref(finding: Finding, cut_id: str, statistic: str) -> ClinicalRef | None:
    e = _evidence(finding, statistic)
    if e is None:
        return None
    sel = _selector(e.selector)
    if "USUBJID" not in sel or "AESEQ" not in sel:
        return None
    return ClinicalRef(cut_id, sel["USUBJID"], int(float(sel["AESEQ"])))


def _case_ref(finding: Finding, cut_id: str, statistic: str) -> CaseRef | None:
    e = _evidence(finding, statistic)
    if e is None:
        return None
    sel = _selector(e.selector)
    if "CASEID" not in sel:
        return None
    version = int(sel["VERSION"]) if sel.get("VERSION", "").isdigit() else None
    return CaseRef(cut_id, sel["CASEID"], version)


def _subject(finding: Finding, statistic: str) -> str | None:
    e = _evidence(finding, statistic)
    return None if e is None else _selector(e.selector).get("USUBJID")


def _onset(finding: Finding, statistic: str) -> Onset | None:
    e = _evidence(finding, statistic)
    return None if e is None else parse_onset(e.display)


def candidates(raised: Sequence[Event], window_days: int) -> list[Candidate]:
    """Every candidate among the given FindingRaised events of adjudicated versions,
    in a stable order (cut, then finding ids)."""
    out: list[Candidate] = []
    missing: dict[tuple[str, str], list[Event]] = {}
    orphans: dict[tuple[str, str], list[Event]] = {}
    for event in sorted(raised, key=lambda e: e.seq):
        finding = _finding(event)
        if not may_adjudicate(finding) or finding.finding_id is None:
            continue
        check, cut = check_of(event), cut_of(event)
        if check == s.TERM_MISMATCH:
            c = _clinical_ref(finding, cut, "preferred_term:clinical")
            k = _case_ref(finding, cut, "preferred_term:safety_db")
            if c is not None and k is not None:
                out.append(Candidate("term_mismatch", (event,), c, k))
        elif check == s.SAE_MISSING:
            subject = _subject(finding, "preferred_term:clinical")
            if subject:
                missing.setdefault((cut, subject), []).append(event)
        elif check == s.CASE_WITHOUT_SAE:
            subject = _subject(finding, "seriousness:safety_db")
            if subject:
                orphans.setdefault((cut, subject), []).append(event)
    for key in sorted(set(missing) & set(orphans)):
        cut = key[0]
        for m in missing[key]:
            fm = _finding(m)
            om = _onset(fm, "onset:clinical")
            c = _clinical_ref(fm, cut, "preferred_term:clinical")
            for o in orphans[key]:
                fo = _finding(o)
                oo = _onset(fo, "onset:safety_db")
                k = _case_ref(fo, cut, "seriousness:safety_db")
                if om is None or oo is None or c is None or k is None:
                    continue
                if gap(om, oo) <= window_days:
                    pair = tuple(sorted((m, o), key=lambda e: _finding(e).finding_id or ""))
                    out.append(Candidate("unpaired_pair", pair, c, k))
    return sorted(out, key=lambda c: (cut_of(c.events[0]), c.finding_ids))


def adjudicator_input(clinical: Term, case: Term) -> dict[str, Any]:
    """The only fields the model sees: two preferred terms and two verbatim terms."""
    return {
        "record_a": {"preferred_term": clinical.preferred, "verbatim_term": clinical.verbatim},
        "record_b": {"preferred_term": case.preferred, "verbatim_term": case.verbatim},
    }


def input_problems(inp: dict[str, Any]) -> list[str]:
    """Why the input may not be sent: a field outside the two terms, or a date, subject id
    or case id inside a term. Empty when it may."""
    problems = []
    if set(inp) != INPUT_KEYS:
        problems.append("unexpected_field")
    for record in inp.values():
        if not isinstance(record, dict) or set(record) != RECORD_KEYS:
            problems.append("unexpected_field")
            continue
        for value in record.values():
            text = str(value)
            if SUBJECT_ID.search(text):
                problems.append("subject_id")
            if _DATE.search(text):
                problems.append("date")
            if _CASE_ID.search(text):
                problems.append("case_id")
    return list(dict.fromkeys(problems))


SYSTEM_PROMPT = """\
You compare two adverse event records from a clinical trial and judge whether they \
describe the same medical event. Each record has a MedDRA preferred term and the verbatim \
term the reporter wrote.

Reply with one JSON object and nothing else: \
{"same_event": "yes" | "no" | "unsure", "reason": "..."}

Rules:
1. "yes" only when both records name the same condition, for example the same words in \
another order, British and American spelling, an abbreviation and its full form, or a \
more specific and a more general term for the same condition.
2. "no" when they name different conditions, including opposites (low and high, hypo- \
and hyper-, fast and slow) and different conditions of the same organ.
3. "unsure" when you cannot tell. A wrong "yes" is worse than "unsure".
4. reason: one sentence, at most 200 characters, about the terms only.
5. Do not use the em dash character (U+2014).
"""


def build_request(
    inp: dict[str, Any],
    *,
    model: str,
    seed: int,
    num_ctx: int,
    num_predict: int,
    temperature: float = 0.0,
    think: bool = False,
) -> LocalModelRequest:
    return LocalModelRequest(
        model=model,
        system=SYSTEM_PROMPT,
        user="Records:\n" + canonical_json(inp),
        options=LocalModelOptions(
            temperature=temperature, seed=seed, num_ctx=num_ctx, num_predict=num_predict
        ),
        think=think,
    )


@dataclass(frozen=True, slots=True)
class Judgement:
    verdict: Verdict | None
    reason: str | None
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.reasons


def validate(response: LocalModelResponse) -> Judgement:
    """Validator 1.0.0. Only response.text is read; a reasoning trace never is."""
    reasons: list[str] = []
    if response.thinking_chars:
        reasons.append("thinking_present")
    if response.done_reason == "length":
        reasons.append("truncated")
    try:
        data = json.loads(strip_code_fence(response.text))
    except ValueError:
        return Judgement(None, None, (*reasons, "bad_json"))
    if not isinstance(data, dict) or set(data) != {"same_event", "reason"}:
        return Judgement(None, None, (*reasons, "bad_json"))
    verdict, reason = data["same_event"], data["reason"]
    if verdict not in VERDICTS:
        reasons.append("bad_verdict")
    if not isinstance(reason, str) or not reason.strip():
        return Judgement(None, None, (*reasons, "reason_empty"))
    reason = reason.strip()
    if len(reason) > MAX_REASON:
        reasons.append("reason_too_long")
    if sentence_count(reason) > MAX_REASON_SENTENCES:
        reasons.append("too_many_sentences")
    if EM_DASH in reason:
        reasons.append("em_dash")
    if reasons:
        return Judgement(None, None, tuple(dict.fromkeys(reasons)))
    return Judgement(verdict, reason, ())
