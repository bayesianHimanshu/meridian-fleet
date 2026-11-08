"""A small synthetic study with serious AEs, for the safety database and reconciler tests.

The rbqm fixture's blinded tables, with an AE table in full SDTM shape: per site a few
serious AEs (AESER = Y) and many non-serious ones, so the evaluation set's augmentation
and every fault type have somewhere to land. No real data.
"""

from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import structlog

from meridian.agents.base import AgentContext, FindingDraft
from meridian.agents.safety.sae_reconciler import SaeReconciler
from meridian.data.study_store import LayeredStudyReader, ParquetStudyReader
from meridian.domain import safety as s
from meridian.faults.reader import FullCut, FullCutReader
from meridian.platform.agent_settings import SaeReconcilerConfig
from meridian.platform.clock import FixedClock
from meridian.safetydb.generate import split, write_snapshot
from tests.agent_support import trigger
from tests.rbqm_support import NOW, STUDY, make_tables, write_tables_cut

TERMS = ("SYNCOPE", "FALL", "PNEUMONIA", "ATRIAL FIBRILLATION", "DIZZINESS", "RASH", "NAUSEA")
OUTCOMES = ("RECOVERED/RESOLVED", "RECOVERING/RESOLVING", "NOT RECOVERED/NOT RESOLVED")
RELATIONS = ("NONE", "REMOTE", "POSSIBLE", "PROBABLE")


def sdtm_ae(dm: pl.DataFrame, seed: int = 5, serious_share: float = 0.12) -> pl.DataFrame:
    """Three to five AEs per treated subject, distinct preferred terms per subject."""
    rng = np.random.default_rng(seed)
    rows: list[dict[str, Any]] = []
    treated = dm.filter(pl.col("RFSTDTC").fill_null("") != "").sort("USUBJID")
    for usubjid, start in treated.select("USUBJID", "RFSTDTC").iter_rows():
        n = int(rng.integers(3, 6))
        terms = rng.choice(TERMS, size=n, replace=False)
        for i, term in enumerate(terms, start=1):
            day = np.datetime64(start[:10]) + np.timedelta64(int(rng.integers(1, 150)), "D")
            serious = bool(rng.random() < serious_share)
            rows.append(
                {
                    "STUDYID": STUDY,
                    "DOMAIN": "AE",
                    "USUBJID": usubjid,
                    "AESEQ": i,
                    "AETERM": str(term),
                    "AEDECOD": str(term),
                    "AESER": "Y" if serious else "N",
                    "AESDTH": "N",
                    "AESLIFE": "N",
                    "AESHOSP": "Y" if serious else "N",
                    "AESDISAB": "N",
                    "AESCONG": "N",
                    "AEOUT": str(rng.choice(OUTCOMES)),
                    "AEREL": str(rng.choice(RELATIONS)),
                    "AESTDTC": str(day),
                }
            )
    return pl.DataFrame(rows).sort(["USUBJID", "AESEQ"])


def base_cut(root: Path, cut_id: str = "DC-BASE", **kwargs: Any) -> FullCut:
    """Write the synthetic base cut and read it back whole (unblinded included)."""
    blinded, ops, unblinded = make_tables(**kwargs)
    blinded = {**blinded, "ae": sdtm_ae(blinded["dm"])}
    study = write_tables_cut(root / "curated", cut_id, blinded, ops, unblinded)
    return FullCutReader(study / cut_id).read()


def reconcile(
    ae: pl.DataFrame,
    cases: pl.DataFrame,
    dm: pl.DataFrame,
    cfg: SaeReconcilerConfig | None = None,
    cut_id: str = "DC-T",
) -> list[FindingDraft]:
    """Run the reconciler on in-memory tables (the export is split as ingestion would)."""
    blinded, _ = split(cases)

    class _Reader:
        study_id = STUDY

        def __init__(self) -> None:
            self.cut_id = cut_id
            self._t = {"ae": ae, "dm": dm, s.SAFETY_CASES: blinded}

        def table(self, name: str) -> pl.DataFrame:
            return self._t[name]

        def tables(self) -> list[str]:
            return sorted(self._t)

    ctx = AgentContext(
        study=_Reader(),
        clock=FixedClock(NOW),
        config=cfg or SaeReconcilerConfig(),
        log=structlog.get_logger(),
    )
    return list(SaeReconciler().run(trigger(cut_id), ctx))


def checks(drafts: list[FindingDraft]) -> set[tuple[str, str]]:
    """(check, record key) of each draft, from its dedup key."""
    out = set()
    for d in drafts:
        name, _, record = d.dedup_key.split("|")[5].partition(":")
        out.add((name, record))
    return out


def snapshot_reader(root: Path, cut: FullCut, cases: pl.DataFrame) -> LayeredStudyReader:
    """Write a safety snapshot for cut under root/safety and return the agent's reader."""
    write_snapshot(
        safety_root=root / "safety",
        study_id=STUDY,
        cut=cut,
        cases=cases,
        harmless={},
        seed=1,
        clock=FixedClock(NOW),
    )
    return LayeredStudyReader(
        ParquetStudyReader(root / "curated" / STUDY, cut.manifest.cut_id),
        ParquetStudyReader(root / "safety" / STUDY, cut.manifest.cut_id),
    )
