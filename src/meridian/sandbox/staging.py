"""Stage exactly what an agent may see: <cut>/blinded, <cut>/ops and trigger.json.

The staged folder is called "cut" and is uploaded to SANDBOX_ROOT, so the worker sees
/tmp/cut/blinded, /tmp/cut/ops and /tmp/cut/trigger.json. It never holds unblinded/,
the cut's manifest (which lists the unblinded files) or any ground truth.
inputs_manifest.json (size and sha256 of every staged file) is written last.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path

from meridian.data.cut_writer import BLINDED_AREA, OPS_AREA
from meridian.data.inputs_manifest import MANIFEST_FILE, write_manifest
from meridian.domain.events import Event
from meridian.ledger.hashing import canonical_json

STAGE_NAME = "cut"
TRIGGER_FILE = "trigger.json"
AREAS = (BLINDED_AREA, OPS_AREA)
SANDBOX_ROOT = "/tmp"  # noqa: S108 (a path inside the sandbox, not on the host)
SANDBOX_CUT_DIR = f"{SANDBOX_ROOT}/{STAGE_NAME}"
SANDBOX_TRIGGER = f"{SANDBOX_CUT_DIR}/{TRIGGER_FILE}"


class StagingError(Exception):
    pass


def _copy_area(source: Path, target: Path, area: str) -> None:
    if not source.is_dir() or source.is_symlink():
        raise StagingError(f"{source} is not a directory")
    for entry in sorted(source.iterdir()):
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".parquet":
            raise StagingError(f"unexpected entry {entry} in {area}/")
        if (target / entry.name).exists():
            raise StagingError(f"{area}/{entry.name} would be staged twice")
        shutil.copyfile(entry, target / entry.name)


def stage_cut(
    cut_dir: Path, trigger: Event, dest_root: Path, extra_blinded: Sequence[Path] = ()
) -> Path:
    """Copy the blinded and ops parquet files and write the trigger. Returns the stage.

    extra_blinded: further blinded areas staged into blinded/ (a safety agent gets the
    blinded area of the cut's safety database snapshot; never an unblinded/ folder)."""
    stage = dest_root / STAGE_NAME
    stage.mkdir(parents=True, exist_ok=False)
    for area in AREAS:
        (stage / area).mkdir()
        _copy_area(cut_dir / area, stage / area, area)
    for extra in extra_blinded:
        if extra.name != BLINDED_AREA:
            raise StagingError(f"only a blinded area may be staged, not {extra}")
        _copy_area(extra, stage / BLINDED_AREA, BLINDED_AREA)
    (stage / TRIGGER_FILE).write_text(canonical_json(trigger.model_dump(mode="json")))
    write_manifest(stage)  # last, so it covers every other staged file
    assert_clean_stage(stage)
    return stage


def assert_clean_stage(stage: Path) -> None:
    """Raise unless the stage holds only blinded/*.parquet, ops/*.parquet, trigger.json
    and inputs_manifest.json."""
    allowed = {*AREAS, TRIGGER_FILE, MANIFEST_FILE}
    top = {p.name for p in stage.iterdir()}
    if top != allowed:
        raise StagingError(f"stage holds {sorted(top)}; only {sorted(allowed)}")
    for path in stage.rglob("*"):
        rel = path.relative_to(stage)
        if path.is_symlink():
            raise StagingError(f"symlink in stage: {rel}")
        if "unblinded" in rel.parts or "ground_truth" in rel.parts:
            raise StagingError(f"forbidden path in stage: {rel}")
        if path.is_dir():
            if len(rel.parts) != 1:
                raise StagingError(f"nested directory in stage: {rel}")
            continue
        if rel.as_posix() in (TRIGGER_FILE, MANIFEST_FILE):
            continue
        if len(rel.parts) != 2 or rel.parts[0] not in AREAS or path.suffix != ".parquet":
            raise StagingError(f"unexpected file in stage: {rel}")
