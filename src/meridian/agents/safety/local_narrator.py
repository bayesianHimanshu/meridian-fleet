"""safety.local_narrator 1.0.0: a plain-language summary of one safety finding, by the
local model, checked before acceptance.

The same contract as rbqm.narrator 1.1.0: the model sees the finding's kind, site,
severity, title, summary and its evidence with display, role, direction and reference
(meridian.agents.rbqm.narrator.narrator_input), must lead with primary evidence, call
supporting evidence supporting, and take every number from a display. The answer goes
through validator 1.1.0 with its reason codes. Two things differ, because nothing leaves
this machine (the safety_local zone reaches only the local model):

* Subject ids are allowed, but only ids that appear in the finding itself; any other is
  rejected (subject_not_in_finding). Validator 1.1.0's subject_id rule does not apply.
* Every date or date-time the model writes must be copied exactly from a display string
  (date_not_displayed:<value>); dates are then left out of the number check.

A reasoning trace is never read (thinking_present) and a truncated answer is rejected
(truncated, as max_tokens for the cloud narrator).
"""

import json
import re
from typing import Any

from meridian.agents.rbqm import narrator
from meridian.agents.rbqm.narrator_v1_0 import SUBJECT_ID, Validation, strip_code_fence
from meridian.domain.findings import Finding
from meridian.ledger.hashing import canonical_json
from meridian.llm.local import LocalModelOptions, LocalModelRequest, LocalModelResponse

AGENT_ID = "safety.local_narrator"
VERSION = "1.0.0"
NARRATES: dict[str, str] = {"safety.sae_reconciler": "1.1.1"}
ZONES = frozenset({"safety"})
# An ISO 8601 date, optionally with a time (and seconds and an offset).
DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)?")
SUBJECT_MASK = "the subject"


def may_narrate(finding: Finding) -> bool:
    if finding.agent_id.split(".", 1)[0] not in ZONES:
        return False
    return NARRATES.get(finding.agent_id) == finding.agent_version


narrator_input = narrator.narrator_input

SYSTEM_PROMPT = """\
You explain one clinical trial safety finding to a medical monitor. The finding comes as \
JSON: its kind, site_id, severity, title, a template summary, and its evidence. Each \
evidence item has a display (the value written as it should be read), a role (primary: \
what the finding rests on; supporting: context only), a direction and expected.

Reply with one JSON object and nothing else (no code fences, no other text): \
{"headline": "...", "explanation": "..."}

Rules:
1. headline: one short line, at most 100 characters, about the primary evidence only.
2. explanation: at most 2 sentences and at most 350 characters, in plain language. Lead \
with the primary evidence. If you mention a supporting item, say it is supporting context.
3. Name the site exactly as "Site <site_id>".
4. You may name the subject by the subject id written in the finding. Name no other \
subject.
5. Every number you write must be copied exactly from a display (or be the site id, or a \
whole-number count from the summary). Every date must be copied exactly from a display. \
Do not round, compute or restate numbers or dates.
6. Describe the finding itself; do not refer to "the summary" or "the title". No advice, \
next steps, causes or speculation.
7. Do not use the em dash character (U+2014); use a comma, colon or period instead.
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


def _mask(text: str, inp: dict[str, Any]) -> tuple[str, list[str]]:
    """The text with dates and the finding's subject ids masked, and the problems found:
    a subject id not in the finding, or a date not copied from a display."""
    problems: list[str] = []
    known = {m.group(0) for s in _strings(inp) for m in SUBJECT_ID.finditer(s)}
    displayed = {m.group(0) for d in _displays(inp) for m in DATE.finditer(d)}

    def subject(m: re.Match[str]) -> str:
        if m.group(0) not in known:
            problems.append("subject_not_in_finding")
        return SUBJECT_MASK

    def date(m: re.Match[str]) -> str:
        if m.group(0) not in displayed:
            problems.append(f"date_not_displayed:{m.group(0)}")
        return " "

    return DATE.sub(date, SUBJECT_ID.sub(subject, text)), problems


def validate(response: LocalModelResponse, inp: dict[str, Any]) -> Validation:
    """Validator 1.1.0 of rbqm.narrator on the masked answer, plus the local rules."""
    reasons: list[str] = []
    if response.thinking_chars:
        reasons.append("thinking_present")
    stop = "max_tokens" if response.done_reason == "length" else None
    try:
        data = json.loads(strip_code_fence(response.text))
    except ValueError:
        return Validation(None, None, (*reasons, "bad_json"))
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        return Validation(None, None, (*reasons, "bad_json"))
    masked: dict[str, str] = {}
    for key, value in data.items():
        masked[key], problems = _mask(value, inp)
        reasons += problems
    base = narrator.validate(json.dumps(masked), inp, stop)
    reasons += base.reasons
    if reasons:
        return Validation(None, None, tuple(dict.fromkeys(reasons)))
    return Validation(data["headline"].strip(), data["explanation"].strip(), ())
