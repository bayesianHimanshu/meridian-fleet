"""Site milestones from blinded DM: each site's first and last subject.

The first subject at a site is the earliest value of the chosen start field (RFSTDTC by
default) over the site's subjects, and the last subject the latest value of the end field
(RFENDTC). A subject with an empty or unreadable value is left out of that milestone; a
site whose subjects all lack it has none. Only the date part of a date-time is used.

Shared by the eTMF generator, quality.tmf_completeness and the API's TMF view, so all
three agree on when a site started and ended. Nothing here is subject-level output: only
dates per site leave this module.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl


@dataclass(frozen=True, slots=True)
class SiteMilestones:
    site: str
    subjects: int
    first_subject: date | None
    last_subject: date | None


def parse_day(value: object) -> date | None:
    """A date from YYYY-MM-DD or a date-time starting with it; None if empty or unreadable."""
    text = "" if value is None else str(value).strip()
    if len(text) < 10 or (len(text) > 10 and text[10] != "T"):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def site_milestones(
    dm: pl.DataFrame, first_field: str = "RFSTDTC", last_field: str = "RFENDTC"
) -> dict[str, SiteMilestones]:
    """Per site id: how many subjects DM holds, and the first and last subject dates."""
    if dm.height == 0 or "SITEID" not in dm.columns:
        return {}
    columns = ["USUBJID", "SITEID", first_field, last_field]
    present = [c for c in columns if c in dm.columns]
    rows: list[dict[str, Any]] = dm.select(present).sort(present, maintain_order=True).to_dicts()
    out: dict[str, SiteMilestones] = {}
    for r in rows:
        site = str(r.get("SITEID") or "").strip()
        if not site:
            continue
        start = parse_day(r.get(first_field))
        end = parse_day(r.get(last_field))
        old = out.get(site, SiteMilestones(site, 0, None, None))
        out[site] = SiteMilestones(
            site=site,
            subjects=old.subjects + 1,
            first_subject=_min(old.first_subject, start),
            last_subject=_max(old.last_subject, end),
        )
    return dict(sorted(out.items()))


def trial_first_subject(milestones: dict[str, SiteMilestones]) -> date | None:
    days = [m.first_subject for m in milestones.values() if m.first_subject is not None]
    return min(days) if days else None


def _min(a: date | None, b: date | None) -> date | None:
    return b if a is None else a if b is None else min(a, b)


def _max(a: date | None, b: date | None) -> date | None:
    return b if a is None else a if b is None else max(a, b)
