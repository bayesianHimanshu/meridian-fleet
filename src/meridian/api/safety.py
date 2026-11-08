"""The SAE reconciliation view: counts and discrepancies of one cut, from findings the
caller may already see (Repository passes only those), with the two compared values."""

import re
from collections import Counter
from collections.abc import Mapping, Sequence

from meridian.api.models import CheckCount, ReconciliationItem, SiteCount
from meridian.domain import safety as s
from meridian.domain.findings import Evidence, Finding

_SUBJECT = re.compile(r"USUBJID=([^;|\s]+)")
PAIRS: dict[str, tuple[str, str, str, str]] = {
    # check: (left statistic, left label, right statistic, right label)
    s.SAE_MISSING: (
        "preferred_term:clinical",
        "Clinical database",
        "preferred_term:safety_db",
        "Safety database",
    ),
    s.CASE_WITHOUT_SAE: (
        "seriousness:clinical",
        "Clinical database",
        "seriousness:safety_db",
        "Safety database",
    ),
    s.ONSET_MISMATCH: ("onset:clinical", "Clinical onset", "onset:safety_db", "Case onset"),
    s.OUTCOME_MISMATCH: (
        "outcome:clinical",
        "Clinical outcome",
        "outcome:safety_db",
        "Case outcome",
    ),
    s.CAUSALITY_MISMATCH: (
        "causality:clinical",
        "Clinical causality",
        "causality:safety_db",
        "Case causality",
    ),
    s.LATE_REPORT: ("awareness:safety_db", "Site aware", "receipt:safety_db", "Sponsor received"),
    s.LATE_SITE: ("late_cases", "Late cases", "median_report_delay_hours", "Median delay"),
}


def _display(evidence: Sequence[Evidence], statistic: str) -> str:
    for e in evidence:
        if e.statistic == statistic:
            return e.display or f"{e.value:g}"
    return "n/a"


def subject_of(finding: Finding) -> str | None:
    for e in finding.evidence:
        m = _SUBJECT.search(e.selector)
        if m:
            return m.group(1)
    return None


def item(
    finding_id: str,
    check: str,
    finding: Finding,
    status: str,
    case_subjects: Mapping[str, str] | None = None,
) -> ReconciliationItem:
    """case_subjects (CASEID to USUBJID) gives the subject of a finding whose evidence names
    only its case."""
    name = s.check_name(check)
    subject = None if name == s.LATE_SITE else subject_of(finding)
    if subject is None and name != s.LATE_SITE and case_subjects:
        subject = case_subjects.get(case_of(finding) or "")
    left_stat, left_label, right_stat, right_label = PAIRS.get(name, ("", "", "", ""))
    return ReconciliationItem(
        finding_id=finding_id,
        check=name,
        site_id=finding.site_id,
        subject_id=subject,
        severity=finding.severity,
        status=status,  # type: ignore[arg-type]
        title=finding.title,
        left_label=left_label,
        left=_display(finding.evidence, left_stat),
        right_label=right_label,
        right=_display(finding.evidence, right_stat),
    )


_CASE = re.compile(r"CASEID=([^;|\s]+)")


def needs_case_subject(check: str, finding: Finding) -> bool:
    """A subject-level finding whose evidence names a case but no subject (a late report).
    The site-level check has no subject by design."""
    return (
        s.check_name(check) != s.LATE_SITE
        and subject_of(finding) is None
        and case_of(finding) is not None
    )


def case_of(finding: Finding) -> str | None:
    for e in finding.evidence:
        m = _CASE.search(e.selector)
        if m:
            return m.group(1)
    return None


def counts(items: Sequence[ReconciliationItem]) -> tuple[list[CheckCount], list[SiteCount]]:
    by_check = Counter(i.check for i in items)
    by_site = Counter(i.site_id for i in items if i.site_id is not None)
    return (
        [CheckCount(check=c, count=by_check.get(c, 0)) for c in s.CHECKS],
        [SiteCount(site_id=k, count=v) for k, v in sorted(by_site.items())],
    )
