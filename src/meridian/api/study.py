"""Study-level views computed from a cut's blinded tables (never the unblinded area)."""

from meridian.api.models import Enrollment, SiteEnrollment
from meridian.data.derived import treated_subjects
from meridian.data.study_store import StudyReader


def _months(first: tuple[int, int], last: tuple[int, int]) -> list[tuple[int, int]]:
    out = []
    y, m = first
    while (y, m) <= last:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def enrollment(study: StudyReader) -> Enrollment:
    """Cumulative treated subjects by month of RFSTDTC, per site and in total.

    Only USUBJID, SITEID and the first-treatment date of treated subjects are read; the
    response holds counts only.
    """
    treated = treated_subjects(study.table("dm")).drop_nulls("RFSTDTC_DATE")
    starts: list[tuple[str, tuple[int, int]]] = [
        (str(site), (d.year, d.month))
        for site, d in treated.select("SITEID", "RFSTDTC_DATE").iter_rows()
    ]
    if not starts:
        return Enrollment(
            study_id=study.study_id, cut_id=study.cut_id, months=[], total=[], sites=[]
        )
    months = _months(min(m for _, m in starts), max(m for _, m in starts))
    sites = sorted({s for s, _ in starts})

    def cumulative(rows: list[tuple[int, int]]) -> list[int]:
        return [sum(1 for r in rows if r <= month) for month in months]

    return Enrollment(
        study_id=study.study_id,
        cut_id=study.cut_id,
        months=[f"{y:04d}-{m:02d}" for y, m in months],
        total=cumulative([m for _, m in starts]),
        sites=[
            SiteEnrollment(site_id=site, cumulative=cumulative([m for s, m in starts if s == site]))
            for site in sites
        ],
    )
