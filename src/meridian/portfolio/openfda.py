"""openFDA queries for portfolio.safety_signal_scout (phase 5 step 3a; docs/signal-scout.md):
the count queries, the label query, the records of what each returned, and the fetch
loop. The loop takes a get function, so it runs with httpx inside the portfolio_signal
sandbox and with recorded responses or a fake anywhere else; nothing here reads study
data. Count queries only on the event endpoint, no API key.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from meridian.platform.agent_settings import SignalScoutConfig

EVENT = "/drug/event.json"
LABEL = "/drug/label.json"
REACTION_FIELD = "patient.reaction.reactionmeddrapt.exact"
RESPONSES_TABLE = "openfda_responses"
# 1.0.1: an earlier scan's responses, staged for a rerun that reuses them.
PRIOR_TABLE = "prior_responses"

Get = Callable[[str, str], tuple[int, str]]  # (path, query string) -> (status, body)


@dataclass(frozen=True, slots=True)
class Response:
    """One request and what came back: the body exactly as received, its sha256 and
    openFDA's meta.last_updated (empty when the body has none)."""

    path: str
    query: str
    status: int
    body: str
    sha256: str
    last_updated: str

    @classmethod
    def of(cls, path: str, query: str, status: int, body: str) -> Response:
        return cls(
            path=path,
            query=query,
            status=status,
            body=body,
            sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            last_updated=_last_updated(body),
        )

    def json(self) -> dict[str, Any]:
        try:
            value = json.loads(self.body)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


def _last_updated(body: str) -> str:
    try:
        meta = json.loads(body).get("meta", {})
    except ValueError, AttributeError:
        return ""
    return str(meta.get("last_updated", "")) if isinstance(meta, dict) else ""


_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~")


def _encode(text: str) -> str:
    """Form encoding, as urllib.parse.quote_plus with safe="" (a test checks it): kept here
    so that meridian-worker's import closure, which reaches this module through the agent,
    stays free of every network module."""
    out = []
    for ch in text:
        if ch in _SAFE:
            out.append(ch)
        elif ch == " ":
            out.append("+")
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def _q(params: Sequence[tuple[str, str]]) -> str:
    return "&".join(f"{_encode(k)}={_encode(v)}" for k, v in params)


def product_search(cfg: SignalScoutConfig) -> str:
    return f'{cfg.product_field}:"{cfg.product_term}"'


def reactions_query(cfg: SignalScoutConfig) -> str:
    return _q(
        [("search", product_search(cfg)), ("count", REACTION_FIELD), ("limit", str(cfg.limit))]
    )


def drug_total_query(cfg: SignalScoutConfig) -> str:
    return _q([("search", product_search(cfg)), ("count", cfg.total_field)])


def all_total_query(cfg: SignalScoutConfig) -> str:
    return _q([("count", cfg.total_field)])


def term_total_query(cfg: SignalScoutConfig, term: str) -> str:
    return _q([("search", f'{REACTION_FIELD}:"{term}"'), ("count", cfg.total_field)])


def label_query(cfg: SignalScoutConfig) -> str:
    return _q([("search", f'openfda.generic_name.exact:"{cfg.product_term}"'), ("limit", "1")])


def counts(r: Response) -> list[tuple[str, int]]:
    """A count query's (term, count) pairs; empty for a 404 (openFDA's "no matches")."""
    results = r.json().get("results", [])
    if r.status != 200 or not isinstance(results, list):
        return []
    return [(str(x.get("term")), int(x.get("count", 0))) for x in results if isinstance(x, dict)]


def total(r: Response) -> int | None:
    """The sum of a count query's buckets (a report total), None when it did not answer."""
    if r.status == 404:
        return 0
    if r.status != 200:
        return None
    return sum(c for _, c in counts(r))


def throttled(r: Response) -> bool:
    """openFDA's throttling: HTTP 429, or HTTP 500 whose error details say "Too Many
    Requests" (what 1.0.0 received). Nothing else is retried (1.0.1)."""
    if r.status == 429:
        return True
    if r.status != 500:
        return False
    error = r.json().get("error")
    return isinstance(error, dict) and error.get("details") == "Too Many Requests"


def plan(cfg: SignalScoutConfig, reactions: Response) -> list[tuple[str, str]]:
    """The term-total queries, most reported term first (the reaction, drug, all-report and
    label queries come before them)."""
    terms = sorted(
        ((t, n) for t, n in counts(reactions) if n >= cfg.min_reports),
        key=lambda x: (-x[1], x[0]),
    )
    return [(EVENT, term_total_query(cfg, term)) for term, _ in terms]


class _Caller:
    """Requests in order, pausing between them, retrying throttling after each backoff,
    never more than max_requests attempts. Every attempt is kept."""

    def __init__(self, get: Get, cfg: SignalScoutConfig, sleep: Callable[[float], None]) -> None:
        self.get, self.cfg, self.sleep = get, cfg, sleep
        self.out: list[Response] = []
        self.start = 0  # responses that were not requested here (a rerun's earlier ones)

    def full(self) -> bool:
        return len(self.out) - self.start >= self.cfg.max_requests

    def call(self, path: str, query: str) -> Response:
        waits = iter(self.cfg.retry_backoff_seconds)
        pause = self.cfg.request_interval_seconds
        while True:
            if self.out:
                self.sleep(pause)
            status, body = self.get(path, query)
            r = Response.of(path, query, status, body)
            self.out.append(r)
            if not throttled(r) or self.full():
                return r
            wait = next(waits, None)
            if wait is None:
                return r
            pause = wait


def fetch(
    get: Get,
    cfg: SignalScoutConfig,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Response]:
    """The pre-registered plan: the reaction terms, the drug's and all reports, the label,
    then one total for each term with at least min_reports reports with the drug, most
    reported first, never more than max_requests requests (retries included), pausing
    between them; since 1.0.1 retrying openFDA's throttling."""
    c = _Caller(get, cfg, sleep)
    reactions = c.call(EVENT, reactions_query(cfg))
    c.call(EVENT, drug_total_query(cfg))
    c.call(EVENT, all_total_query(cfg))
    c.call(LABEL, label_query(cfg))
    for path, query in plan(cfg, reactions):
        if c.full():
            break
        c.call(path, query)
    return c.out


class DataDateChangedError(ValueError):
    pass


def top_up(
    get: Get,
    cfg: SignalScoutConfig,
    prior: Sequence[Response],
    sleep: Callable[[float], None] = time.sleep,
) -> list[Response]:
    """A rerun on an earlier scan's responses (1.0.1): the earlier responses, then a request
    (with retries) for each query of the plan the earlier scan has no 200 answer for. Every
    new answer must carry the earlier data date; otherwise DataDateChangedError."""
    answered = {(r.path, r.query): r for r in prior if r.status == 200}
    base = [(EVENT, reactions_query(cfg)), (EVENT, drug_total_query(cfg))]
    base += [(EVENT, all_total_query(cfg)), (LABEL, label_query(cfg))]
    reactions = answered.get(base[0])
    if reactions is None:
        raise ValueError("the earlier scan has no answer to the reaction query")
    date = reactions.last_updated
    c = _Caller(get, cfg, sleep)
    c.out = list(prior)
    c.start = len(prior)
    for path, query in [*base, *plan(cfg, reactions)]:
        if (path, query) in answered:
            continue
        if c.full():
            break
        r = c.call(path, query)
        if r.status == 200 and path == EVENT and r.last_updated != date:
            raise DataDateChangedError(
                f"openFDA's data date is {r.last_updated}, the earlier scan's {date}: a full "
                "scan is needed"
            )
    return c.out


@dataclass(frozen=True, slots=True)
class Label:
    set_id: str
    version: str
    effective_time: str
    text: str  # adverse reactions, warnings and precautions, boxed warning: lower case
    most_common: tuple[str, ...]  # section 6's "most common adverse reactions"


_MOST_COMMON = re.compile(r"most common adverse reactions \([^)]*\) were ([^.]*)\.")
LABEL_SECTIONS = ("boxed_warning", "warnings_and_cautions", "adverse_reactions")


def normalise(text: str) -> str:
    """Lower case, punctuation as spaces, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def label_of(r: Response) -> Label | None:
    results = r.json().get("results", [])
    if r.status != 200 or not results:
        return None
    doc = results[0]
    raw = " ".join(
        " ".join(doc.get(section, [])) for section in LABEL_SECTIONS if section in doc
    ).lower()
    m = _MOST_COMMON.search(" ".join(raw.split()))
    common: tuple[str, ...] = ()
    if m:
        parts = re.split(r",\s*|\s+and\s+", m.group(1))
        # An Oxford comma leaves "and x" after the split: the "and" is not part of x.
        common = tuple(p.strip().removeprefix("and ").strip() for p in parts if p.strip())
    return Label(
        set_id=str(doc.get("set_id", "")),
        version=str(doc.get("version", "")),
        effective_time=str(doc.get("effective_time", "")),
        text=normalise(raw),
        most_common=common,
    )


def listed(
    term: str,
    label: Label,
    spellings: Sequence[tuple[str, str]] = (),
    synonyms: Mapping[str, Sequence[str]] | None = None,
) -> bool:
    """The pre-registered rule: the term, lower-cased, as a phrase in the label's text
    (punctuation as spaces). No MedDRA hierarchy. 1.0.1: the term also after the spelling
    rules (British to American), and the label's own synonyms for it."""
    phrases = {normalise(term)}
    american = normalise(term)
    for british, us in spellings:
        american = american.replace(british, us)
    phrases.add(american)
    phrases |= {normalise(x) for x in (synonyms or {}).get(term.upper(), ())}
    text = f" {label.text} "
    return any(p and f" {p} " in text for p in phrases)
