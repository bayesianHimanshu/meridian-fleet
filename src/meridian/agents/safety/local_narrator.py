"""safety.local_narrator 1.0.2: a plain-language summary of one safety finding, by the
local model, checked before acceptance.

The same contract as rbqm.narrator 1.1.0: the model sees the finding's kind, site,
severity, title, summary and its evidence with display, role, direction and reference,
must lead with primary evidence, call supporting evidence supporting, and take every
number from a display. The answer goes through validator 1.1.0 with its reason codes.
What differs, because nothing leaves this machine (the safety_local zone reaches only
the local model):

* Subject ids are allowed, but only ids that appear in the finding itself; any other is
  rejected (subject_not_in_finding). Validator 1.1.0's subject_id rule does not apply.
* Every date or date-time the model writes must be copied exactly from a display string
  (date_not_displayed:<value>); dates are then left out of the number check.

1.0.1 (step 3b2):

* The host writes the opening (the headline), not the model: "Site <id>", for a
  subject-level finding ", subject <USUBJID>", then the check's plain name. The model
  writes only the explanation that follows it. 1.0.0 lost 60 of 77 findings to
  missing_site; now the site is there by construction. The check stays, as a guard on
  the opening.
* Dates reach the model as readable display strings ("20 Jan 2013, 22:02 UTC"), and the
  validator matches the dates the model writes against those (an ISO date is no longer
  in the input, so the model writing one is date_not_displayed).
* filler_sentence: a sentence that only restates the severity, the status, or that a
  discrepancy exists ("This discrepancy highlights a medium severity issue.") is
  rejected, with one code per kind: filler_sentence_severity, filler_sentence_status,
  filler_sentence_discrepancy.

1.0.2 (step 4a): validator 1.1.1 of rbqm.narrator, so selector_leak applies here too
(evidence selectors and field names such as "CASEID=SC-..." or "AESEQ 17.0, AESER = N"
copied into the text are rejected).

A reasoning trace is never read (thinking_present) and a truncated answer is rejected
(truncated, as max_tokens for the cloud narrator).
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

from meridian.agents.rbqm import narrator
from meridian.agents.rbqm.narrator_v1_0 import SUBJECT_ID, Validation, strip_code_fence
from meridian.domain import safety as s
from meridian.domain.findings import Finding
from meridian.ledger.hashing import canonical_json
from meridian.llm.local import LocalModelOptions, LocalModelRequest, LocalModelResponse

AGENT_ID = "safety.local_narrator"
VERSION = "1.0.2"
NARRATES: dict[str, str] = {"safety.sae_reconciler": "1.1.1"}
ZONES = frozenset({"safety"})
SUBJECT_MASK = "the subject"

# An ISO 8601 date, optionally with a time (and seconds and an offset).
ISO_DATE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:\d{2})?)?"
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
# Every way of writing a date the validator recognises in an answer: ISO, day first
# (the readable display form, with an optional time) and month first.
DATE = re.compile(
    ISO_DATE.pattern
    + r"|\b\d{1,2} "
    + _MONTH
    + r" \d{4}(?:, \d{2}:\d{2}(?::\d{2})?(?: UTC)?)?"
    + r"|\b"
    + _MONTH
    + r" \d{1,2}(?:st|nd|rd|th)?,? \d{4}"
)

# The check's plain name, for the opening.
PLAIN_NAMES: dict[str, str] = {
    s.SAE_MISSING: "serious AE with no safety case",
    s.CASE_WITHOUT_SAE: "safety case with no serious AE in the clinical database",
    s.ONSET_MISMATCH: "onset differs from the safety case",
    s.OUTCOME_MISMATCH: "outcome differs from the safety case",
    s.CAUSALITY_MISMATCH: "causality differs from the safety case",
    s.TERM_MISMATCH: "preferred term differs from the safety case",
    s.LATE_REPORT: "serious AE reported to the sponsor late",
    s.LATE_SITE: "serious AEs reported to the sponsor late",
}


def may_narrate(finding: Finding) -> bool:
    if finding.agent_id.split(".", 1)[0] not in ZONES:
        return False
    return NARRATES.get(finding.agent_id) == finding.agent_version


def readable(text: str) -> str:
    """ISO dates and date-times written for a reader: "2013-01-20" is "20 Jan 2013",
    "2013-01-20T22:02:00+00:00" is "20 Jan 2013, 22:02 UTC" (converted to UTC; seconds
    only when not zero), and a date-time without an offset keeps its clock time without
    "UTC" (its zone is not known)."""

    def one(m: re.Match[str]) -> str:
        year, month, day, hour, _minute, _second, offset = m.groups()
        if hour is None:
            return f"{int(day)} {MONTHS[int(month) - 1]} {year}"
        when = datetime.fromisoformat(m.group(0).replace("Z", "+00:00"))
        if offset is not None:
            when = when.astimezone(UTC)
        clock = when.strftime("%H:%M") + (when.strftime(":%S") if when.second else "")
        date = f"{when.day} {MONTHS[when.month - 1]} {when.year}"
        return f"{date}, {clock}" + (" UTC" if offset is not None else "")

    return ISO_DATE.sub(one, text)


def subjects(finding: Finding) -> list[str]:
    """The subjects the finding's evidence names (by USUBJID in a selector), in order."""
    found = [m.group(0) for e in finding.evidence for m in SUBJECT_ID.finditer(e.selector)]
    return list(dict.fromkeys(found))


def opening(finding: Finding, check: str) -> str:
    """The headline, written by the host: site, subject (if the finding names one) and
    the check's plain name."""
    parts = [f"Site {finding.site_id}"] if finding.site_id else []
    parts += [f"subject {sid}" for sid in subjects(finding)]
    name = PLAIN_NAMES.get(check, check.replace("_", " "))
    return f"{', '.join(parts)}: {name}" if parts else name[:1].upper() + name[1:]


def narrator_input(finding: Finding, check: str) -> dict[str, Any]:
    """rbqm.narrator's input with every date made readable, and the opening the host
    writes (the model continues after it)."""
    inp = narrator.narrator_input(finding)
    out: dict[str, Any] = {
        **inp,
        "title": readable(inp["title"]),
        "summary": readable(inp["summary"]),
        "evidence": [
            {**e, "display": readable(e["display"]) if e.get("display") else e.get("display")}
            for e in inp["evidence"]
        ],
    }
    out["opening"] = opening(finding, check)
    return out


SYSTEM_PROMPT = """\
You explain one clinical trial safety finding to a medical monitor. The finding comes as \
JSON: its kind, site_id, severity, title, a template summary, its evidence, and the \
opening. Each evidence item has a display (the value written as it should be read), a \
role (primary: what the finding rests on; supporting: context only), a direction and \
expected. The opening is already written: it names the site, the subject and the check, \
and the reader sees it as the headline.

Reply with one JSON object and nothing else (no code fences, no other text): \
{"explanation": "..."}

Rules:
1. explanation: what follows the opening, at most 2 sentences and at most 350 \
characters, in plain language. Do not repeat the opening. Lead with the primary \
evidence. If you mention a supporting item, say it is supporting context.
2. You may name the subject by the subject id written in the finding. Name no other \
subject.
3. Every number you write must be copied exactly from a display (or be the site id, or a \
whole-number count from the summary). Every date must be copied exactly as a display \
writes it, for example "5 Jun 2013, 09:04 UTC". Do not round, compute or restate numbers \
or dates.
4. Every sentence states facts from the evidence. Do not add a sentence that only says \
that there is a discrepancy, how severe the finding is, or what its status is.
5. Describe the finding itself; do not refer to "the summary" or "the title". No advice, \
next steps, causes or speculation.
6. Do not use the em dash character (U+2014); use a comma, colon or period instead.
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
        user="Finding:\n" + canonical_json(inp),
        options=LocalModelOptions(
            temperature=temperature, seed=seed, num_ctx=num_ctx, num_predict=num_predict
        ),
        think=think,
    )


def _strings(obj: object) -> list[str]:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return [obj] if isinstance(obj, str) else []


def _displays(inp: dict[str, Any]) -> list[str]:
    return [str(e.get("display") or "") for e in inp.get("evidence", []) if isinstance(e, dict)]


def displayed_dates(inp: dict[str, Any]) -> set[str]:
    """The dates a display writes, and the date part of each displayed date-time."""
    out = set()
    for d in _displays(inp):
        for m in DATE.finditer(d):
            out.add(m.group(0))
            out.add(m.group(0).split(",", 1)[0].split("T", 1)[0])
    return out


def _mask(text: str, inp: dict[str, Any]) -> tuple[str, list[str]]:
    """The text with dates and the finding's subject ids masked, and the problems found:
    a subject id not in the finding, or a date not copied from a display."""
    problems: list[str] = []
    known = {m.group(0) for s in _strings(inp) for m in SUBJECT_ID.finditer(s)}
    displayed = displayed_dates(inp)

    def subject(m: re.Match[str]) -> str:
        if m.group(0) not in known:
            problems.append("subject_not_in_finding")
        return SUBJECT_MASK

    def date(m: re.Match[str]) -> str:
        if m.group(0) not in displayed:
            problems.append(f"date_not_displayed:{m.group(0)}")
        return " "

    return DATE.sub(date, SUBJECT_ID.sub(subject, text)), problems


# filler_sentence: what a sentence says when it says nothing about the evidence.
FILLER: dict[str, re.Pattern[str]] = {
    "filler_sentence_severity": re.compile(
        r"\b(?:low|medium|moderate|high|critical)[- ]severity\b|\bseverity\b"
        r"|\b(?:serious|significant|important|critical|major|notable) "
        r"(?:issue|concern|problem|matter)\b"
        r"|\b(?:is|are|was|were) (?:significant|important|concerning|critical|noteworthy)\b",
        re.IGNORECASE,
    ),
    "filler_sentence_status": re.compile(
        r"\b(?:requires?|needs?|warrants?|calls? for) (?:further )?"
        r"(?:attention|review|follow[- ]up|investigation|action)\b"
        r"|\bshould be (?:reviewed|investigated|addressed|followed up|resolved)\b"
        r"|\b(?:is|was|has been|remains) (?:flagged|open|pending|unresolved|under review)\b",
        re.IGNORECASE,
    ),
    "filler_sentence_discrepancy": re.compile(
        r"\bhighlight\w*|\bunderscor\w*"
        r"|\b(?:this|the|these|that) (?:discrepanc\w*|mismatch\w*|inconsistenc\w*)"
        r"|\bthere (?:is|are) (?:a |an )?(?:discrepanc\w*|mismatch\w*|inconsistenc\w*)"
        r"|\bindicat\w* (?:a|an) (?:discrepanc\w*|mismatch\w*|inconsistenc\w*|issue|problem)",
        re.IGNORECASE,
    ),
}
# Words of a display that say nothing particular to this finding.
_GENERIC = frozenset(
    {
        "case",
        "cases",
        "matching",
        "serious",
        "recorded",
        "version",
        "subject",
        "onset",
        "late",
        "median",
        "hours",
        "after",
        "awareness",
        "days",
        "apart",
        "tolerance",
        "aeseq",
        "aeser",
    }
)
_WORD = re.compile(r"[A-Za-z][A-Za-z/-]{3,}")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _content_words(inp: dict[str, Any]) -> set[str]:
    words = {w.lower() for d in _displays(inp) for w in _WORD.findall(d)}
    return words - _GENERIC


def filler_sentences(text: str, inp: dict[str, Any]) -> list[str]:
    """One code per kind for each sentence that matches a filler pattern and carries no
    evidence: no digit and no word of a display (other than generic ones)."""
    content = _content_words(inp)
    codes = []
    for sentence in _SENTENCE.split(text.strip()):
        if any(ch.isdigit() for ch in sentence):
            continue
        if {w.lower() for w in _WORD.findall(sentence)} & content:
            continue
        codes += [code for code, pattern in FILLER.items() if pattern.search(sentence)]
    return list(dict.fromkeys(codes))


def validate(response: LocalModelResponse, inp: dict[str, Any]) -> Validation:
    """Validator 1.1.0 of rbqm.narrator on the opening and the masked explanation, plus
    the local rules. The headline is the opening the host wrote."""
    reasons: list[str] = []
    if response.thinking_chars:
        reasons.append("thinking_present")
    stop = "max_tokens" if response.done_reason == "length" else None
    try:
        data = json.loads(strip_code_fence(response.text))
    except ValueError:
        return Validation(None, None, (*reasons, "bad_json"))
    if (
        not isinstance(data, dict)
        or set(data) != {"explanation"}
        or not isinstance(data["explanation"], str)
    ):
        return Validation(None, None, (*reasons, "bad_json"))
    headline = str(inp.get("opening") or "")
    explanation = data["explanation"].strip()
    masked: dict[str, str] = {}
    for key, value in (("headline", headline), ("explanation", explanation)):
        masked[key], problems = _mask(value, inp)
        reasons += problems
    base = narrator.validate(json.dumps(masked), inp, stop)
    reasons += base.reasons
    reasons += filler_sentences(explanation, inp)
    if reasons:
        return Validation(None, None, tuple(dict.fromkeys(reasons)))
    return Validation(headline, explanation, ())
