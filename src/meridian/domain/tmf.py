"""The synthetic eTMF index: table and column names, and the checks of quality.tmf_completeness.

Shared by the host-side generator (meridian.tmf.generate), the fault injector and the agent
(meridian.agents.quality.tmf_completeness), so all read the same names. Nothing here
reads data.

The eTMF holds metadata only, no documents. Three blinded tables per cut, staged into
/stage/tmf for the quality zone only:

* etmf_index: one row per filed document version.
* monitoring_visits: the monitoring visit schedule (SDTM has no monitoring visits).
* etmf_export: one row, the date the index was exported (the "as of" date).

Every value is a string as an export would hold it, so a bad value can exist and becomes a
tmf_data_quality finding instead of a failed cut. Dates are ISO 8601 (YYYY-MM-DD).
"""

ETMF_INDEX = "etmf_index"
MONITORING_VISITS = "monitoring_visits"
ETMF_EXPORT = "etmf_export"
TABLES: tuple[str, ...] = (ETMF_INDEX, MONITORING_VISITS, ETMF_EXPORT)

# etmf_index
DOCUMENT_ID = "DOCUMENT_ID"  # opaque
ARTIFACT = "ARTIFACT"  # the TMF Reference Model artifact name, as indexed
LEVEL = "LEVEL"  # trial or site
SITE = "SITE"  # the site it is filed under (site level); empty for trial level
# The site the document itself names (read from its face at indexing, for example the
# site number on a Form FDA 1572); empty for trial level. A real index would not always
# hold this; here it is what makes a misfiled document visible (docs/tmf-notes.md).
DOC_SITE = "DOC_SITE"
VERSION = "VERSION"  # 1, 2, ...
DOCUMENT_DATE = "DOCUMENT_DATE"
FILED_DATE = "FILED_DATE"
QC_STATUS = "QC_STATUS"  # passed, pending or rejected
INDEX_COLUMNS: tuple[str, ...] = (
    DOCUMENT_ID,
    ARTIFACT,
    LEVEL,
    SITE,
    DOC_SITE,
    VERSION,
    DOCUMENT_DATE,
    FILED_DATE,
    QC_STATUS,
)
QC_PASSED = "passed"
QC_PENDING = "pending"
QC_REJECTED = "rejected"
QC_STATUSES: tuple[str, ...] = (QC_PASSED, QC_PENDING, QC_REJECTED)
TRIAL = "trial"
SITE_LEVEL = "site"
LEVELS: tuple[str, ...] = (TRIAL, SITE_LEVEL)

# monitoring_visits
VISIT_ID = "VISIT_ID"  # opaque
VISIT_TYPE = "VISIT_TYPE"  # initiation, routine or close_out
VISIT_DATE = "VISIT_DATE"
VISIT_COLUMNS: tuple[str, ...] = (VISIT_ID, SITE, VISIT_TYPE, VISIT_DATE)
INITIATION = "initiation"
ROUTINE = "routine"
CLOSE_OUT = "close_out"
VISIT_TYPES: tuple[str, ...] = (INITIATION, ROUTINE, CLOSE_OUT)

# etmf_export
EXPORT_DATE = "EXPORT_DATE"
EXPORT_COLUMNS: tuple[str, ...] = (EXPORT_DATE,)

# The checks, one per kind of discrepancy, plus data quality.
MISSING_ARTIFACT = "tmf_missing_artifact"
LATE_FILING = "tmf_late_filing"
APPROVAL_AFTER_FIRST_SUBJECT = "tmf_approval_after_first_subject"
MISFILED = "tmf_misfiled"
MONITORING_REPORT_MISSING = "tmf_monitoring_report_missing"
QC_OVERDUE = "tmf_qc_overdue"
DATA_QUALITY = "tmf_data_quality"
CHECKS: tuple[str, ...] = (
    MISSING_ARTIFACT,
    LATE_FILING,
    APPROVAL_AFTER_FIRST_SUBJECT,
    MISFILED,
    MONITORING_REPORT_MISSING,
    QC_OVERDUE,
    DATA_QUALITY,
)
# The record of a trial-level artifact in a finding's record key.
TRIAL_RECORD = "TRIAL"


def norm_name(value: str) -> str:
    """An artifact name as matched: upper case, whitespace collapsed."""
    return " ".join(value.split()).upper()


def check_id(check: str, record: str) -> str:
    """The dedup key's check part: "<check>:<record>"."""
    return f"{check}:{record}"


def check_name(check_part: str) -> str:
    return check_part.split(":", 1)[0]
