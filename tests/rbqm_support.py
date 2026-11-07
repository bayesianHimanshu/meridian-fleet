"""A small, homogeneous synthetic study in pilot SDTM shape, for fault and agent tests.

Every site is drawn from the same distributions, so a clean fixture should not trip any
agent. Built with a seeded numpy Generator, then curated and ops-synthesized with the
real step 2 code, so tables have exactly the production schema.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from meridian.data.cdisc_pilot import curate
from meridian.data.cut_writer import write_cut
from meridian.data.ops_synth import OpsSynthParams, synthesize

PILOT = "CDISCPILOT01"
STUDY = "MRD-201"
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
VISIT_DAYS = 14
N_VISITS = 10


def _pilot_frames(
    n_sites: int, per_site: int, seed: int, small_site: int | None, sizes: list[int] | None = None
) -> tuple[dict[str, pl.DataFrame], dict[str, str]]:
    rng = np.random.default_rng(seed)
    dm_rows: list[dict[str, object]] = []
    ae_rows: list[dict[str, object]] = []
    vs_rows: list[dict[str, object]] = []
    ds_rows: list[dict[str, object]] = []
    sv_rows: list[dict[str, object]] = []
    lb_rows: list[dict[str, object]] = []
    ex_rows: list[dict[str, object]] = []
    site_map: dict[str, str] = {}
    for s in range(n_sites):
        pilot_site = str(701 + s)
        site_map[pilot_site] = str(1001 + s)
        n = small_site if (small_site is not None and s == n_sites - 1) else per_site
        if sizes is not None:
            n = sizes[s]
        for k in range(n + 1):  # the last one is a screen failure
            usubjid = f"01-{pilot_site}-{1000 + k:04d}"
            failed = k == n
            start = date(2014, 1, 1) + timedelta(days=int(rng.integers(0, 60)))
            dm_rows.append(
                {
                    "STUDYID": PILOT,
                    "DOMAIN": "DM",
                    "USUBJID": usubjid,
                    "SUBJID": f"{1000 + k}",
                    "RFSTDTC": "" if failed else start.isoformat(),
                    "SITEID": pilot_site,
                    "AGE": float(rng.integers(55, 85)),
                    "ARMCD": "Scrnfail" if failed else ("Pbo" if k % 2 else "Xan_Hi"),
                    "ARM": "Screen Failure" if failed else ("Placebo" if k % 2 else "Xan High"),
                    "ACTARMCD": "Scrnfail" if failed else ("Pbo" if k % 2 else "Xan_Hi"),
                    "ACTARM": "Screen Failure" if failed else ("Placebo" if k % 2 else "Xan High"),
                }
            )
            visits = 1 if failed else N_VISITS
            for v in range(visits):
                day = start + timedelta(days=VISIT_DAYS * v - (7 if v == 0 else 0))
                sv_rows.append(
                    {
                        "USUBJID": usubjid,
                        "VISITNUM": float(v + 1),
                        "VISIT": f"VISIT {v + 1}",
                        "SVSTDTC": day.isoformat(),
                    }
                )
                if failed:
                    continue
                for test, mu, sd in (("SYSBP", 130, 15), ("DIABP", 80, 10), ("PULSE", 70, 10)):
                    value = float(np.rint(rng.normal(mu, sd)))
                    vs_rows.append(
                        {
                            "USUBJID": usubjid,
                            "VSSEQ": float(len(vs_rows) + 1),
                            "VSTESTCD": test,
                            "VSORRES": str(int(value)),
                            "VSSTRESC": str(int(value)),
                            "VSSTRESN": value,
                            "VISITNUM": float(v + 1),
                            "VSDTC": day.isoformat(),
                        }
                    )
            ds_rows.append(
                {
                    "USUBJID": usubjid,
                    "DSSEQ": 1.0,
                    "DSTERM": "SCREEN FAILURE" if failed else "PROTOCOL COMPLETED",
                    "DSDECOD": "SCREEN FAILURE" if failed else "COMPLETED",
                    "DSCAT": "DISPOSITION EVENT",
                    "DSSTDTC": start.isoformat(),
                }
            )
            if failed:
                continue
            if rng.random() < 0.4:
                ds_rows[-1]["DSDECOD"] = "ADVERSE EVENT"
                ds_rows[-1]["DSTERM"] = "ADVERSE EVENT"
            for a in range(int(rng.poisson(4))):
                ae_rows.append(
                    {
                        "USUBJID": usubjid,
                        "AESEQ": float(a + 1),
                        "AETERM": "HEADACHE",
                        "AESTDTC": (start + timedelta(days=int(rng.integers(0, 120)))).isoformat(),
                    }
                )
            lb_rows.append({"USUBJID": usubjid, "LBSEQ": 1.0, "LBTESTCD": "ALT", "LBDTC": ""})
            ex_rows.append(
                {
                    "USUBJID": usubjid,
                    "EXSEQ": 1.0,
                    "EXTRT": "PLACEBO",
                    "EXDOSE": 0.0,
                    "EXDOSU": "mg",
                    "EXDOSFRM": "PATCH",
                    "VISITNUM": 2.0,
                    "VISIT": "VISIT 2",
                    "EXSTDTC": start.isoformat(),
                    "EXENDTC": "",
                }
            )

    def frame(domain: str, rows: list[dict[str, object]]) -> pl.DataFrame:
        return pl.DataFrame([{"STUDYID": PILOT, "DOMAIN": domain, **r} for r in rows])

    frames = {
        "dm": pl.DataFrame(dm_rows),
        "ae": frame("AE", ae_rows),
        "vs": frame("VS", vs_rows),
        "lb": frame("LB", lb_rows),
        "ds": frame("DS", ds_rows),
        "sv": frame("SV", sv_rows),
        "ex": frame("EX", ex_rows),
    }
    return frames, site_map


def make_tables(
    n_sites: int = 6,
    per_site: int = 15,
    seed: int = 7,
    small_site: int | None = None,
    ops_seed: int = 11,
    sizes: list[int] | None = None,
) -> tuple[dict[str, pl.DataFrame], dict[str, pl.DataFrame], dict[str, pl.DataFrame]]:
    """(blinded, ops, unblinded) tables of a clean synthetic study.

    sizes, when given, sets treated subjects per site (and n_sites = len(sizes)).
    """
    if sizes is not None:
        n_sites = len(sizes)
    frames, site_map = _pilot_frames(n_sites, per_site, seed, small_site, sizes)
    curated = curate(frames, site_map, STUDY)
    blinded = dict(curated.blinded)
    ops = synthesize(blinded["dm"], blinded["sv"], ops_seed, OpsSynthParams()).tables
    return blinded, dict(ops), dict(curated.unblinded)


def write_tables_cut(
    root: Path,
    cut_id: str,
    blinded: dict[str, pl.DataFrame],
    ops: dict[str, pl.DataFrame],
    unblinded: dict[str, pl.DataFrame],
) -> Path:
    """Write a cut under root/MRD-201/cut_id and return the study directory."""
    write_cut(
        curated_root=root,
        study_id=STUDY,
        cut_id=cut_id,
        blinded=blinded,
        ops=ops,
        unblinded=unblinded,
        created_at=NOW,
        seed=11,
        seed_record={},
        sources=[],
    )
    return root / STUDY
