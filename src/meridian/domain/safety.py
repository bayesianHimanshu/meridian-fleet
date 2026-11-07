"""The safety database's case table, and the normalisation SAE reconciliation compares by.

Shared by the host-side generator (meridian.data.safety_db) and the agent
(meridian.agents.safety.sae_reconciler), so both read the same column names and the same
idea of "the same term" and "the same day". Nothing here reads data.
"""

import re
from datetime import date, datetime

# Blinded table of the safety database, one row per case (blinded/safety_cases.parquet).
SAFETY_CASES = "safety_cases"
# Unblinded table: the treatment an expedited (SUSAR) case carries, by case id.
SAFETY_UNBLINDED = "safety_unblinded"

CASEID = "CASEID"
USUBJID = "USUBJID"
SITEID = "SITEID"
REPORTED_TERM = "REPORTED_TERM"  # the reporter's words
PREFERRED_TERM = "PREFERRED_TERM"  # MedDRA preferred term
ONSET = "ONSET"  # ISO 8601 date or date-time
OUTCOME = "OUTCOME"
CAUSALITY = "CAUSALITY"
AWARE_DTC = "AWARE_DTC"  # when the site became aware (ISO date-time, UTC)
RECEIPT_DTC = "RECEIPT_DTC"  # when the sponsor received the case (ISO date-time, UTC)
# Seriousness criteria, Y or N, the same criteria as SDTM AESDTH ... AESMIE.
CRITERIA: dict[str, str] = {
    "SDTH": "AESDTH",
    "SLIFE": "AESLIFE",
    "SHOSP": "AESHOSP",
    "SDISAB": "AESDISAB",
    "SCONG": "AESCONG",
    "SMIE": "AESMIE",
}
# The unblinded treatment column. Its name contains TRTA, which meridian.data.blinding
# forbids in blinded and ops areas, so it can only ever be written to unblinded/.
TREATMENT = "TRTA"

CASE_COLUMNS: tuple[str, ...] = (
    CASEID,
    USUBJID,
    SITEID,
    REPORTED_TERM,
    PREFERRED_TERM,
    ONSET,
    *CRITERIA,
    OUTCOME,
    CAUSALITY,
    AWARE_DTC,
    RECEIPT_DTC,
)

# The checks of safety.sae_reconciler, one per kind of discrepancy, and the site-level one.
SAE_MISSING = "sae_missing_in_safety_db"
CASE_WITHOUT_SAE = "case_without_clinical_sae"
ONSET_MISMATCH = "onset_mismatch"
OUTCOME_MISMATCH = "outcome_mismatch"
CAUSALITY_MISMATCH = "causality_mismatch"
LATE_REPORT = "late_sae_report"
LATE_SITE = "sae_late_reporting_site"
RECORD_CHECKS: tuple[str, ...] = (
    SAE_MISSING,
    CASE_WITHOUT_SAE,
    ONSET_MISMATCH,
    OUTCOME_MISMATCH,
    CAUSALITY_MISMATCH,
    LATE_REPORT,
)
CHECKS: tuple[str, ...] = (*RECORD_CHECKS, LATE_SITE)
# Checks anchored on the clinical record (subject/AESEQ) and on the case (case id).
CLINICAL_ANCHORED = frozenset({SAE_MISSING, ONSET_MISMATCH, OUTCOME_MISMATCH, CAUSALITY_MISMATCH})
CASE_ANCHORED = frozenset({CASE_WITHOUT_SAE, LATE_REPORT})

_SPACE = re.compile(r"\s+")


def norm_text(value: str | None) -> str:
    """Upper case, surrounding whitespace removed, inner runs of whitespace as one space."""
    return _SPACE.sub(" ", (value or "").strip()).upper()


def onset_day(value: str | None) -> date | None:
    """The calendar day of an ISO 8601 date or date-time; None for a partial date
    (YYYY or YYYY-MM), which cannot be compared day by day."""
    text = (value or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_dtc(value: str) -> datetime:
    """An ISO 8601 date-time written by the generator (always with a UTC offset)."""
    return datetime.fromisoformat(value)


def clinical_key(usubjid: str, aeseq: int) -> str:
    """The record key of a clinical AE: subject and sequence number."""
    return f"{usubjid}/{aeseq}"


def check_id(check: str, record: str) -> str:
    """The dedup key's check part for one discrepancy: check name and record key."""
    return f"{check}:{record}"


def check_name(check: str) -> str:
    """The check name of a dedup key's check part (the part before the record key)."""
    return check.split(":", 1)[0]
