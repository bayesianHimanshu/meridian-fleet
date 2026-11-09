"""rbqm.narrator 1.1.0: a plain-language summary of one finding, checked before acceptance.

Detection stays deterministic; this agent only explains. 1.1.0 (phase 2 step 4) narrates
findings of kri_monitor 2.2.0, qtl_detector 2.1.0 and csm 2.1.0, whose evidence carries
context (meridian.agents.rbqm.describe). The model sees each item's display, role,
direction and reference instead of raw values, and must lead with primary evidence,
describe supporting evidence as supporting, and take every number from a display.

Validator 1.1.0 keeps 1.0.0's format, length, site, dash and id checks, replaces the
tolerance number match with an exact match on display tokens, and adds one reason code per
known failure: number_not_displayed, unbacked_ratio, supporting_as_headline, zero_p_value,
misattribution, threshold_restated and direction_contradicted. The 1.0.0 version is
frozen in meridian.agents.rbqm.narrator_v1_0.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

from meridian.agents.rbqm.describe import describe
from meridian.agents.rbqm.narrator_v1_0 import (
    ALLOWED_FIELDS,
    EM_DASH,
    MAX_EXPLANATION,
    MAX_HEADLINE,
    MAX_SENTENCES,
    MINUS,
    SUBJECT_ID,
    NumberToken,
    Validation,
    numbers_in,
    sentence_count,
    strip_code_fence,
)
from meridian.agents.rbqm.narrator_v1_0 import deid_problems as _deid_problems
from meridian.domain.findings import Finding
from meridian.ledger.hashing import canonical_json
from meridian.llm.client import ModelRequest

__all__ = [
    "AGENT_ID",
    "ALLOWED_EVIDENCE_FIELDS",
    "ALLOWED_FIELDS",
    "EM_DASH",
    "EXCLUDED_ZONES",
    "NARRATES",
    "SUBSCRIBES_TO_AGENTS",
    "SYSTEM_PROMPT",
    "VERSION",
    "Validation",
    "build_request",
    "deid_problems",
    "may_narrate",
    "narrator_input",
    "validate",
]

AGENT_ID = "rbqm.narrator"
VERSION = "1.1.0"
# finding_raised events of exactly these detector versions (1.0.0 narratives of older
# versions stay as they are).
NARRATES: dict[str, str] = {
    "rbqm.kri_monitor": "2.2.0",
    "rbqm.qtl_detector": "2.1.0",
    "rbqm.csm": "2.1.0",
}
SUBSCRIBES_TO_AGENTS = frozenset(NARRATES)
# The cloud model never receives a finding of these zones, whatever NARRATES says: safety
# findings are about individual subjects; safety.local_narrator explains them with the
# local model, which nothing leaves (docs/local-model-notes.md).
EXCLUDED_ZONES = frozenset({"safety"})
if any(agent.split(".", 1)[0] in EXCLUDED_ZONES for agent in NARRATES):
    raise RuntimeError("the narrator may not subscribe to an excluded zone")


def may_narrate(finding: Finding) -> bool:
    """True only for a finding of a subscribed detector version outside excluded zones."""
    if finding.agent_id.split(".", 1)[0] in EXCLUDED_ZONES:
        return False
    return NARRATES.get(finding.agent_id) == finding.agent_version


ALLOWED_EVIDENCE_FIELDS = frozenset(
    {"source", "selector", "statistic", "role", "direction", "expected", "display"}
)


def narrator_input(finding: Finding) -> dict[str, Any]:
    """The only fields the model sees. Evidence is described (idempotent for new findings,
    and the same description for a legacy finding), and raw values are left out: numbers
    reach the model only as displays."""
    return {
        "kind": finding.kind.value,
        "site_id": finding.site_id,
        "severity": finding.severity.value,
        "title": finding.title,
        "summary": finding.summary,
        "evidence": [
            {
                "source": e.source,
                "selector": e.selector,
                "statistic": e.statistic,
                "role": e.role,
                "direction": e.direction,
                "expected": e.expected,
                "display": e.display,
            }
            for e in describe(finding.evidence)
        ],
    }


def deid_problems(serialized: str) -> list[str]:
    return _deid_problems(serialized, ALLOWED_EVIDENCE_FIELDS)


SYSTEM_PROMPT = """\
You explain one clinical trial monitoring finding to a clinical operations reader who \
is not a statistician. The finding comes as JSON: its kind, site_id, severity, title, a \
template summary, and its evidence. Each evidence item has a display (the value written \
as it should be read), a role (primary: what the finding rests on; supporting: context \
only), a direction (low or high against its reference, or none) and expected (that \
reference, already written into the display).

Reply with one JSON object and nothing else (no code fences, no other text): \
{"headline": "...", "explanation": "..."}

Rules:
1. headline: one short line, at most 100 characters, about the primary evidence only.
2. explanation: at most 2 sentences and at most 350 characters, in plain language. Lead \
with the primary evidence. If you mention a supporting item, say it is supporting context.
3. If site_id is not null, name the site exactly as "Site <site_id>". If it is null, \
the finding is study-wide.
4. Every number you write must be copied exactly from a display (or be the site id, or a \
whole-number count from the summary). Do not round, compute, or restate numbers, and do \
not write ratios such as "twice" or "N times" unless a display states that ratio.
5. A p value shown as "below 1e-12" must be written that way, never as 0.
6. Describe the finding itself; do not refer to "the summary" or "the title". No advice, \
next steps, causes or speculation.
7. Do not use the em dash character (U+2014); use a comma, colon or period instead.
"""


def build_request(
    inp: dict[str, Any],
    *,
    model: str,
    max_tokens: int,
    temperature: float | None,
    disable_thinking: bool,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        system=SYSTEM_PROMPT,
        user="Finding:\n" + canonical_json(inp),
        max_tokens=max_tokens,
        temperature=temperature,
        disable_thinking=disable_thinking,
    )


# ---- validator 1.1.0


def norm(token: NumberToken) -> str:
    """A number as written, normalised: no thousands separators, no sign, lower-case e,
    no leading zeros or plus in the exponent; a percent sign kept."""
    text = token.text.replace(",", "").replace(MINUS, "-").lstrip("+-").lower()
    mantissa, e, exp = text.partition("e")
    if e:
        sign = "-" if exp.startswith(("-", MINUS)) else ""
        text = f"{mantissa}e{sign}{int(exp.lstrip('+-' + MINUS))}"
    return text + ("%" if token.percent else "")


def _displays(inp: dict[str, Any], role: str | None = None) -> list[str]:
    return [
        str(e["display"])
        for e in inp.get("evidence", [])
        if isinstance(e, dict) and e.get("display") and (role is None or e.get("role") == role)
    ]


def _strings(obj: object) -> list[str]:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return [obj] if isinstance(obj, str) else []


def _integer_counts(inp: dict[str, Any]) -> set[str]:
    """Whole numbers written anywhere in the input (no decimals, exponent or percent)."""
    out = set()
    for s in _strings(inp):
        for t in numbers_in(s.replace("_", " ")):
            if t.decimals == 0 and t.exponent is None and not t.percent:
                out.add(norm(t))
    return out


_WORD_NUMBERS = {"two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "ten": 10.0}
_RATIO_WORDS = {
    "twice": 2.0,
    "double": 2.0,
    "doubled": 2.0,
    "doubling": 2.0,
    "half": 0.5,
    "halved": 0.5,
    "triple": 3.0,
    "tripled": 3.0,
    "thrice": 3.0,
    "quadruple": 4.0,
}
_RATIO_WORD = re.compile(r"\b(" + "|".join(_RATIO_WORDS) + r")\b", re.IGNORECASE)
_N_TIMES = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?|two|three|four|five|ten)(?:\s*-\s*|\s+)(times|fold)\b",
    re.IGNORECASE,
)
_DISPLAY_TIMES = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?) times\b")
_DISPLAY_PCT_OF = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)% of the\b")
_PCT_OF_LIMIT = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*%\s+of\s+(?:the\s+|its\s+|this\s+)?"
    r"(?:QTL|limit|threshold|quality tolerance limit|tolerance limit)\b",
    re.IGNORECASE,
)
_ZERO_P = re.compile(
    r"\bp(?:[- ]?values?)?\s*(?:=|of|is|was|were|are|at|equal to|equals)?\s*"
    r"(?:about\s+|approximately\s+|roughly\s+|essentially\s+)?0(?:\.0+)?(?!\.?\d)",
    re.IGNORECASE,
)
_MISATTRIBUTION = re.compile(
    r"\b(?:the|this|its)\s+(?:finding's\s+|template\s+)?(?:summary|title)\b"
    r"|\baccording to the (?:summary|title)\b",
    re.IGNORECASE,
)
# Supporting statistics and the words that name them, by statistic suffix.
_SUPPORTING_WORDS: dict[str, re.Pattern[str]] = {
    "terminal_zero_share": re.compile(r"\bzero(?:e?s)?\b|\bend(?:s|ing)? in 0\b", re.I),
    "terminal_zero_z": re.compile(r"\bzero(?:e?s)?\b", re.I),
    "phi": re.compile(r"\bdispersion\b|\bphi\b", re.I),
    "z": re.compile(r"\bz\b", re.I),
    "excluded_subjects": re.compile(r"\bexcluded\b", re.I),
    "per_100_subject_days": re.compile(r"\bper 100\b", re.I),
    "ratio_to_threshold": re.compile(r"\btimes\b|\btwice\b|\bdouble\b|\bhalf\b|-fold\b", re.I),
    "share_of_limit": re.compile(r"% of (?:the|its) (?:QTL|limit)", re.I),
}
_HIGH = re.compile(r"\b(more|higher|larger|greater|above|exceed\w*|longer)\b", re.I)
_LOW = re.compile(r"\b(fewer|less|lower|smaller|below|shorter|little)\b", re.I)
# A clause ends at punctuation followed by a space (never inside 1.57e-06) or a joining word.
_CLAUSE = re.compile(r"[.;,](?=\s|$)|\band\b|\bwhile\b|\bwhere\b|\bbut\b|\bwith\b", re.I)
_P_MENTION = re.compile(r"\bp\b|p[- ]values?\b", re.I)


def _ratio_values(inp: dict[str, Any]) -> list[str]:
    out = []
    for d in _displays(inp):
        out += [m.group(1) for m in _DISPLAY_TIMES.finditer(d)]
    return out


def _approx(a: float, b: float) -> bool:
    return round(a, 1) == round(b, 1)


def unbacked_ratios(text: str, inp: dict[str, Any]) -> list[str]:
    ratios = _ratio_values(inp)
    floats = [float(r) for r in ratios]
    pct_of = [float(m.group(1)) / 100 for d in _displays(inp) for m in _DISPLAY_PCT_OF.finditer(d)]
    bad = []
    for m in _RATIO_WORD.finditer(text):
        target = _RATIO_WORDS[m.group(1).lower()]
        if not any(_approx(target, r) for r in [*floats, *pct_of]):
            bad.append(m.group(0))
    for m in _N_TIMES.finditer(text):
        word = m.group(1).lower()
        if word in _WORD_NUMBERS:
            if not any(_approx(_WORD_NUMBERS[word], r) for r in floats):
                bad.append(m.group(0))
        elif word not in ratios:
            bad.append(m.group(0))
    return bad


def supporting_in_headline(headline: str, inp: dict[str, Any]) -> list[str]:
    """Supporting statistics the headline names, by word or by a number of their display
    that no primary display shares."""
    site = str(inp.get("site_id") or "")
    primary_numbers = {norm(t) for d in _displays(inp, "primary") for t in numbers_in(d)}
    head_numbers = {norm(t) for t in numbers_in(headline) if norm(t) != site}
    named = []
    for e in inp.get("evidence", []):
        if not isinstance(e, dict) or e.get("role") != "supporting":
            continue
        statistic = str(e.get("statistic", ""))
        suffix = statistic.rsplit(":", 1)[-1]
        words = _SUPPORTING_WORDS.get(suffix)
        numbers = {norm(t) for t in numbers_in(str(e.get("display") or ""))} - primary_numbers
        if (words is not None and words.search(headline)) or (numbers & head_numbers):
            named.append(statistic)
    return list(dict.fromkeys(named))


def _finding_direction(inp: dict[str, Any]) -> str | None:
    """The one direction of the primary items that have one, or None if none or mixed."""
    directions = {
        e.get("direction")
        for e in inp.get("evidence", [])
        if isinstance(e, dict)
        and e.get("role") == "primary"
        and e.get("direction") in ("low", "high")
    }
    return directions.pop() if len(directions) == 1 else None


def direction_contradictions(text: str, inp: dict[str, Any]) -> list[str]:
    """Direction words that contradict the evidence. In a clause about a p value the
    sense must be low (p below its threshold); elsewhere it must match the primary
    evidence's direction."""
    expected_sense = _finding_direction(inp)
    bad = []
    for clause in _CLAUSE.split(text):
        if not clause:
            continue
        want = "low" if _P_MENTION.search(clause) else expected_sense
        if want is None:
            continue
        wrong = _HIGH if want == "low" else _LOW
        bad += [m.group(0) for m in wrong.finditer(clause)]
    return bad


def threshold_restatements(text: str, inp: dict[str, Any]) -> list[str]:
    allowed = {m.group(1) for d in _displays(inp) for m in _DISPLAY_PCT_OF.finditer(d)}
    return [m.group(0) for m in _PCT_OF_LIMIT.finditer(text) if m.group(1) not in allowed]


def undisplayed_numbers(masked: str, inp: dict[str, Any]) -> list[str]:
    displayed = {norm(t) for d in _displays(inp) for t in numbers_in(d)}
    counts = _integer_counts(inp)
    bad = []
    for t in numbers_in(masked):
        n = norm(t)
        if n in displayed:
            continue
        if t.decimals == 0 and t.exponent is None and not t.percent and n in counts:
            continue
        bad.append(t.text + ("%" if t.percent else ""))
    return list(dict.fromkeys(bad))


def validate(text: str, inp: dict[str, Any], stop_reason: str | None = None) -> Validation:
    """Validator 1.1.0."""
    reasons: list[str] = []
    if stop_reason == "refusal":
        reasons.append("model_refusal")
    elif stop_reason == "max_tokens":
        reasons.append("truncated")
    try:
        data = json.loads(strip_code_fence(text))
    except ValueError:
        return Validation(None, None, (*reasons, "bad_json"))
    if (
        not isinstance(data, dict)
        or set(data) != {"headline", "explanation"}
        or not all(isinstance(data[k], str) and data[k].strip() for k in data)
    ):
        return Validation(None, None, (*reasons, "bad_json"))
    headline, explanation = data["headline"].strip(), data["explanation"].strip()
    both = f"{headline}\n{explanation}"
    if len(headline) > MAX_HEADLINE or len(explanation) > MAX_EXPLANATION:
        reasons.append("too_long")
    if sentence_count(explanation) > MAX_SENTENCES:
        reasons.append("too_many_sentences")
    if EM_DASH in both:
        reasons.append("em_dash")
    if SUBJECT_ID.search(both):
        reasons.append("subject_id")
    site = inp.get("site_id")
    masked = both
    if site:
        pattern = re.compile(rf"(?<![\w-]){re.escape(str(site))}(?![\w-])")
        if not pattern.search(both):
            reasons.append("missing_site")
        masked = pattern.sub(" ", both)
    reasons += [f"number_not_displayed:{b}" for b in undisplayed_numbers(masked, inp)]
    reasons += [f"unbacked_ratio:{b}" for b in unbacked_ratios(both, inp)]
    reasons += [f"supporting_as_headline:{s}" for s in supporting_in_headline(headline, inp)]
    if _ZERO_P.search(both):
        reasons.append("zero_p_value")
    if _MISATTRIBUTION.search(both):
        reasons.append("misattribution")
    reasons += [f"threshold_restated:{b}" for b in threshold_restatements(both, inp)]
    reasons += [f"direction_contradicted:{b}" for b in direction_contradictions(both, inp)]
    if reasons:
        return Validation(None, None, tuple(dict.fromkeys(reasons)))
    return Validation(headline, explanation, ())


def reason_codes(reasons: Sequence[str]) -> list[str]:
    """The codes without their details ("number_not_displayed:43" -> "number_not_displayed")."""
    return list(dict.fromkeys(r.split(":", 1)[0] for r in reasons))
