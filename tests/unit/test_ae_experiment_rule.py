"""The pre-registered selection rule in scripts/ae_experiment.py."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ae_experiment.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ae_experiment", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ae_experiment"] = module
    spec.loader.exec_module(module)
    return module


ae = _module()
CLEAN = ("DC-BASE", *(f"DC-NULL-S{i}" for i in range(1, 6)))
DEV = ("DC-RBQM-CORE", "S0P2", "S0P4", "S0P7")


def result(variant: str, phi: float, fp: int, hits: int) -> object:
    return ae.VariantResult(
        variant=variant,
        phi_base=phi,
        ae_fp=dict.fromkeys(CLEAN, fp),
        detector_fp=dict.fromkeys(CLEAN, fp),
        detected={c: i < hits for i, c in enumerate(DEV)},
        target_z=dict.fromkeys(DEV),
    )


def test_most_detections_among_eligible_wins() -> None:
    results = [
        result("a", 8.0, 0, 0),
        result("b", 9.0, 0, 2),
        result("c", 5.0, 0, 1),
        result("e", 4.0, 1, 4),  # not eligible: one AE false positive
        result("d", 7.0, 0, 1),
    ]
    assert ae.choose(results)[0] == "b"


def test_ties_go_to_lower_phi_then_simplicity() -> None:
    results = [result("a", 8, 0, 0), result("b", 9, 0, 2), result("d", 7, 0, 2)]
    assert ae.choose(results)[0] == "d"
    results = [result("a", 8, 0, 0), result("d", 7, 0, 2), result("c", 7, 0, 2)]
    assert ae.choose(results)[0] == "c"


def test_a_is_kept_when_nothing_beats_it() -> None:
    results = [result("a", 8, 0, 1), result("b", 6, 0, 1), result("e", 6, 1, 3)]
    variant, reason = ae.choose(results)
    assert variant == "a"
    assert "known limitation" in reason
