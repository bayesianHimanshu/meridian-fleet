"""Small hand-built SDTM-shaped frames for data tests. No XPT files involved."""

from pathlib import Path

import polars as pl

from meridian.data.cut_writer import ParquetWriter, SourceFile, write_parquet_deterministic
from meridian.data.ingest import IngestService
from meridian.data.ops_synth import OpsSynthParams
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import Clock

PILOT = "CDISCPILOT01"
# Deliberately not sorted, so numbering within site has to sort by the original id.
SUBJECTS = [
    ("01-703-1100", "1100", "703", "2014-02-01", "Xan_Hi", "Xanomeline High Dose"),
    ("01-701-1015", "1015", "701", "2014-01-02", "Pbo", "Placebo"),
    ("01-701-1002", "1002", "701", "", "Scrnfail", "Screen Failure"),
    ("01-703-1050", "1050", "703", "2014-03-05", "Pbo", "Placebo"),
    ("01-701-1023", "1023", "701", "2012-08-05", "Xan_Lo", "Xanomeline Low Dose"),
]


def dm() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "STUDYID": [PILOT] * len(SUBJECTS),
            "DOMAIN": ["DM"] * len(SUBJECTS),
            "USUBJID": [s[0] for s in SUBJECTS],
            "SUBJID": [s[1] for s in SUBJECTS],
            "RFSTDTC": [s[3] for s in SUBJECTS],
            "RFPENDTC": ["2014-07-02T11:45", "2014-07-01", "2013-01", "", "2013-02-18"],
            "SITEID": [s[2] for s in SUBJECTS],
            "AGE": [63.0, 64.0, 70.0, 71.0, 55.0],
            "SEX": ["F", "M", "F", "M", "F"],
            "ARMCD": [s[4] for s in SUBJECTS],
            "ARM": [s[5] for s in SUBJECTS],
            "ACTARMCD": [s[4] for s in SUBJECTS],
            "ACTARM": [s[5] for s in SUBJECTS],
        }
    )


def _rows(domain: str, rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame([{"STUDYID": PILOT, "DOMAIN": domain, **r} for r in rows])


def ae() -> pl.DataFrame:
    return _rows(
        "AE",
        [
            {"USUBJID": "01-701-1015", "AESEQ": 1.0, "AETERM": "HEADACHE", "AESTDTC": "2014-01-05"},
            {"USUBJID": "01-701-1015", "AESEQ": 2.0, "AETERM": "RASH", "AESTDTC": "2014-02"},
            {"USUBJID": "01-703-1100", "AESEQ": 1.0, "AETERM": "NAUSEA", "AESTDTC": "2014"},
            {"USUBJID": "01-703-1050", "AESEQ": 1.0, "AETERM": "COUGH", "AESTDTC": ""},
        ],
    )


def vs() -> pl.DataFrame:
    return _rows(
        "VS",
        [
            {
                "USUBJID": u,
                "VSSEQ": float(i),
                "VSTESTCD": "SYSBP",
                "VSSTRESN": 120.0 + i,
                "VSDTC": "2014-01-02",
            }
            for u in ("01-701-1015", "01-703-1100", "01-701-1023")
            for i in (1, 2)
        ],
    )


def lb() -> pl.DataFrame:
    return _rows(
        "LB",
        [
            {
                "USUBJID": "01-701-1015",
                "LBSEQ": 1.0,
                "LBTESTCD": "ALT",
                "LBDTC": "2014-01-02T08:30",
            },
            {"USUBJID": "01-703-1050", "LBSEQ": 1.0, "LBTESTCD": "AST", "LBDTC": "2014-03-05"},
        ],
    )


def ds() -> pl.DataFrame:
    return _rows(
        "DS",
        [
            {"USUBJID": u, "DSSEQ": 1.0, "DSDECOD": d, "DSSTDTC": "2014-01-01"}
            for u, d in [
                ("01-701-1015", "COMPLETED"),
                ("01-701-1002", "SCREEN FAILURE"),
                ("01-703-1100", "ADVERSE EVENT"),
                ("01-703-1050", "COMPLETED"),
                ("01-701-1023", "COMPLETED"),
            ]
        ],
    )


def sv() -> pl.DataFrame:
    visits = [(1.0, "SCREENING 1"), (2.0, "BASELINE"), (3.0, "WEEK 2")]
    rows: list[dict[str, object]] = []
    for usubjid, *_ in SUBJECTS:
        for num, name in visits if usubjid != "01-701-1002" else visits[:1]:
            rows.append(
                {
                    "USUBJID": usubjid,
                    "VISITNUM": num,
                    "VISIT": name,
                    "SVSTDTC": "2014-01-0" + str(int(num)),
                }
            )
    return _rows("SV", rows)


def ex() -> pl.DataFrame:
    return _rows(
        "EX",
        [
            {
                "USUBJID": u,
                "EXSEQ": 1.0,
                "EXTRT": t,
                "EXDOSE": d,
                "EXDOSU": "mg",
                "EXDOSFRM": "PATCH",
                "VISITNUM": 3.0,
                "VISIT": "BASELINE",
                "EXSTDTC": "2014-01-02",
                "EXENDTC": e,
            }
            for u, t, d, e in [
                ("01-701-1015", "PLACEBO", 0.0, "2014-02-01"),
                ("01-703-1100", "XANOMELINE", 81.0, ""),
                ("01-703-1050", "PLACEBO", 0.0, "2014-04-01"),
                ("01-701-1023", "XANOMELINE", 54.0, "2012-09-01"),
            ]
        ],
    )


def pilot_frames() -> dict[str, pl.DataFrame]:
    return {"dm": dm(), "ae": ae(), "vs": vs(), "lb": lb(), "ds": ds(), "sv": sv(), "ex": ex()}


SITE_MAP = {"701": "1001", "703": "1003"}
SOURCES = [
    SourceFile(name=f"{d}.xpt", url=f"https://example.test/{d}.xpt", sha256="0" * 64)
    for d in pilot_frames()
]


def make_ingest(
    root: Path,
    clock: Clock,
    ledger: SqliteLedger,
    writer: ParquetWriter = write_parquet_deterministic,
) -> IngestService:
    return IngestService(
        clock=clock,
        ledger=ledger,
        curated_root=root / "curated",
        sites_file=root / "sites.toml",
        study_id="MRD-201",
        ops_params=OpsSynthParams(),
        writer=writer,
    )
