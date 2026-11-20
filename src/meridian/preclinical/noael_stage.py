"""The staged NOAEL area's layout (phase 5 step 2), shared by the host that writes it
(meridian.preclinical.inputs) and the worker that reads it. Imports nothing of the ledger.
"""

from pathlib import Path

import polars as pl

from meridian.data.stage_layout import NOAEL

NOAEL_TABLE = "noael_decisions"
CONTEXT_TABLE = "noael_context"
# Phase 5 step 3a, for preclinical.translational_bridge: the findings the current decisions
# cite, and the study's current dose proposal.
BRIDGE_FINDINGS = "bridge_findings"
BRIDGE_PROPOSAL = "bridge_proposal"


def noael_dir(noael_root: Path, trigger_seq: int) -> Path:
    """<noael_root>/<trigger seq>: the stage-like folder holding noael/."""
    return noael_root / str(trigger_seq)


def read_noael_context(stage: Path) -> tuple[str, str]:
    """(study id, SEND cut id) from a staged noael/ area."""
    ctx = pl.read_parquet(stage / NOAEL / f"{CONTEXT_TABLE}.parquet")
    return str(ctx["study_id"][0]), str(ctx["send_cut_id"][0])
