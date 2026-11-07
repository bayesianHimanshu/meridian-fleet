"""rbqm.narrator 1.0.0: a plain-language summary of one finding, checked before acceptance.

Detection stays deterministic; this agent only explains. Its input is the finding's kind,
site, severity, title, template summary and evidence list, nothing from the cut. Before
any call the serialized input passes a de-identification check. The model's reply is
accepted only if it passes validate(): JSON with a headline and an explanation, within
the length limits, naming the site, with no em dash, no subject id, and no number that is
not in the input.

Everything here is a pure function; the runner (meridian.narrator.runner) records the
outcome as a FindingNarrative event.
"""

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from meridian.domain.findings import Finding
from meridian.ledger.hashing import canonical_json
from meridian.llm.client import ModelRequest

AGENT_ID = "rbqm.narrator"
VERSION = "1.0.0"
# finding_raised events from these agents only.
SUBSCRIBES_TO_AGENTS = frozenset({"rbqm.kri_monitor", "rbqm.qtl_detector", "rbqm.csm"})

ALLOWED_FIELDS = frozenset({"kind", "site_id", "severity", "title", "summary", "evidence"})
ALLOWED_EVIDENCE_FIELDS = frozenset({"source", "selector", "statistic", "value", "threshold"})
FORBIDDEN_KEYS = frozenset({"USUBJID", "SRC_USUBJID"})
SUBJECT_ID = re.compile(r"(?<!\d)201-\d{4}-\d{3}(?!\d)")
# A key inside a selector string such as "USUBJID=201-1001-001;VSTESTCD=SYSBP".
_SELECTOR_KEY = re.compile(r"(?:^|[;,\s])([A-Za-z_][A-Za-z0-9_]*)=")

EM_DASH = chr(0x2014)  # written as a code point: the character never appears in source
MAX_HEADLINE = 120
MAX_EXPLANATION = 400
MAX_SENTENCES = 2


def narrator_input(finding: Finding) -> dict[str, Any]:
    """The only fields the model sees."""
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
                "value": e.value,
                "threshold": e.threshold,
            }
            for e in finding.evidence
        ],
    }


def _keys(obj: object) -> list[str]:
    if isinstance(obj, dict):
        out = []
        for key, value in obj.items():
            out.append(str(key))
            out += _keys(value)
        return out
    if isinstance(obj, list):
        return [k for item in obj for k in _keys(item)]
    return []


def _strings(obj: object) -> list[str]:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return [obj] if isinstance(obj, str) else []


def deid_problems(serialized: str) -> list[str]:
    """Why this serialized input may not be sent; empty when it may.

    Refused: a subject id pattern (201-NNNN-NNN) anywhere, a USUBJID or SRC_USUBJID key
    (in the JSON or inside a selector), or any field outside the allowed list.
    """
    problems: list[str] = []
    if SUBJECT_ID.search(serialized):
        problems.append("subject_id_pattern")
    try:
        data = json.loads(serialized)
    except ValueError:
        return [*problems, "input_not_json"]
    if not isinstance(data, dict):
        return [*problems, "input_not_an_object"]
    keys = set(_keys(data))
    for value in _strings(data):
        keys |= {m.group(1) for m in _SELECTOR_KEY.finditer(value)}
    problems += [f"forbidden_key:{k}" for k in sorted(keys) if k.upper() in FORBIDDEN_KEYS]
    problems += [f"field_not_allowed:{k}" for k in sorted(set(data) - ALLOWED_FIELDS)]
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list):
        problems.append("field_not_allowed:evidence")
        evidence = []
    extra = set()
    for item in evidence:
        if not isinstance(item, dict):
            problems.append("field_not_allowed:evidence[]")
            continue
        extra |= set(item) - ALLOWED_EVIDENCE_FIELDS
    problems += [f"field_not_allowed:evidence.{k}" for k in sorted(extra)]
    return problems


SYSTEM_PROMPT = """\
You explain one clinical trial monitoring finding to a clinical operations reader who \
is not a statistician. The finding comes as JSON: its kind, site_id, severity, title, a \
template summary, and the evidence values behind it.

Reply with one JSON object and nothing else (no code fences, no other text): \
{"headline": "...", "explanation": "..."}

Rules:
1. headline: one short line, at most 100 characters.
2. explanation: at most 2 sentences and at most 350 characters, in plain language.
3. If site_id is not null, name the site exactly as "Site <site_id>". If it is null, \
the finding is study-wide.
4. Use only numbers that appear in the finding: copy them from the summary or title, or \
round an evidence value to 2 or 3 significant digits. Do not compute new numbers, \
differences or ratios. A rate may be written as a percentage only if the summary does.
5. State what the finding states. No advice, next steps, causes or speculation.
6. Do not use the em dash character (U+2014); use a comma, colon or period instead.
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


MINUS = chr(0x2212)  # the Unicode minus sign, read like "-"
# Numbers in text: optional sign, digits with optional thousands commas, decimals, an
# exponent, and a percent sign. Not part of a word or of an id like 201-1001-001.
_NUMBER = re.compile(
    r"(?<![\w.])(?P<num>[-\u2212]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+\u2212]?\d+)?)"
    r"(?P<pct>%?)(?![\w])"
)
_SENTENCE_END = re.compile(r"[.!?](?:\s+|$)")


@dataclass(frozen=True, slots=True)
class NumberToken:
    text: str
    value: Decimal
    decimals: int  # digits after the point of the mantissa
    exponent: int | None  # for 3.35e-09
    percent: bool

    @property
    def significant(self) -> int:
        digits = self.text.lstrip("-" + MINUS).split("e")[0].split("E")[0].replace(",", "")
        digits = digits.replace(".", "").lstrip("0")
        return max(len(digits), 1)


def _token(num: str, pct: str) -> NumberToken | None:
    clean = num.replace(",", "").replace(MINUS, "-")
    mantissa, _, exp = clean.lower().partition("e")
    try:
        value = Decimal(clean)
    except InvalidOperation:
        return None
    decimals = len(mantissa.split(".")[1]) if "." in mantissa else 0
    return NumberToken(num, value, decimals, int(exp) if exp else None, bool(pct))


def numbers_in(text: str) -> list[NumberToken]:
    out = []
    for m in _NUMBER.finditer(text):
        token = _token(m["num"], m["pct"])
        if token is not None:
            out.append(token)
    return out


def _rounds_to(value: float, token: NumberToken) -> bool:
    """Does value, shown at the token's precision, read as the token (sign ignored)?"""
    target = abs(token.value)
    v = abs(Decimal(repr(value)))
    if token.exponent is not None:
        if v == 0:
            return target == 0
        exp = v.adjusted()
        if exp != token.exponent:
            return False
        q = Decimal(1).scaleb(exp - token.decimals)
    else:
        q = Decimal(1).scaleb(-token.decimals)
    for rounding in (ROUND_HALF_UP, ROUND_HALF_EVEN):
        if v.quantize(q, rounding=rounding) == target:
            # A value shown with one significant digit must be exact (0.57 shown as "1"
            # reads as a different number).
            return token.significant >= 2 or v == target
    return False


def _literal_numbers(inp: dict[str, Any]) -> set[tuple[Decimal, bool]]:
    """Every number written in the input's text (statistic names read _ as a space)."""
    tokens = [t for s in _strings(inp) for t in numbers_in(s.replace("_", " "))]
    return {(abs(t.value), t.percent) for t in tokens}


def allowed_number(token: NumberToken, inp: dict[str, Any]) -> bool:
    """In the input's text as written, or an evidence value at the token's precision."""
    if (abs(token.value), token.percent) in _literal_numbers(inp):
        return True
    values = [
        x
        for e in inp.get("evidence", [])
        for x in (e.get("value"), e.get("threshold"))
        if isinstance(x, (int, float))
    ]
    for v in values:
        if token.percent:
            if _rounds_to(float(v) * 100, token):
                return True
        elif _rounds_to(float(v), token):
            return True
    return False


def strip_code_fence(text: str) -> str:
    """One surrounding ```json ... ``` fence is tolerated; nothing else is."""
    s = text.strip()
    if s.startswith("```") and s.endswith("```") and s.count("```") == 2:
        s = s[3:-3]
        s = s.removeprefix("json").strip()
    return s


@dataclass(frozen=True, slots=True)
class Validation:
    headline: str | None
    explanation: str | None
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return not self.reasons and self.headline is not None


def sentence_count(text: str) -> int:
    return max(len([m for m in _SENTENCE_END.finditer(text.strip())]), 1)


def validate(text: str, inp: dict[str, Any], stop_reason: str | None = None) -> Validation:
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
    bad = [t.text + ("%" if t.percent else "") for t in numbers_in(masked)]
    bad = [
        label
        for label, t in zip(bad, numbers_in(masked), strict=True)
        if not allowed_number(t, inp)
    ]
    reasons += [f"number_not_in_evidence:{b}" for b in dict.fromkeys(bad)]
    if reasons:
        return Validation(None, None, tuple(reasons))
    return Validation(headline, explanation, ())
