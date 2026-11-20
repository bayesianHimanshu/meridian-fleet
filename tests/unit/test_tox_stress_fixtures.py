"""The tox stress fixtures (phase 5 step 1, 1.1.0 ideals in step 1b) are well formed: each
spec builds, and each ideal names a check pre-registered for its version and a known
endpoint form."""

import json
import re
from pathlib import Path

import pytest

from meridian.preclinical.synth import StudySpec, build

FIXTURES = sorted(Path("tests/fixtures/tox_stress").glob("*.json"))
ENDPOINT = r"(?:lb:[A-Z0-9]+|bw:(?:BW|GAIN)|om:[A-Z, ]+:(?:abs|rel)|mi:[A-Z, ]+:[A-Z, ()]+|death)"
IDEAL = {
    "1.0.0": re.compile(
        r"^(?:(?:dose_related_change|pairwise_change|dose_related_incidence|pairwise_incidence)"
        rf":{ENDPOINT}:[MF]:(?:main|recovery)|data_quality:[a-z_]+:.+)$"
    ),
    "1.1.0": re.compile(
        r"^(?:(?:dose_related_change|pairwise_change|dose_related_incidence|pairwise_incidence)"
        rf":{ENDPOINT}:[MF]:main"
        rf"|recovery_only_(?:change|incidence):{ENDPOINT}:[MF]:recovery"
        r"|organ_weight_secondary_to_bw:[MF]:(?:main|recovery):r[1-9]"
        r"|neoplasm:mi:[A-Z, ]+:[A-Z, ()]+:[MF]"
        r"|unscheduled_death:[A-Z0-9-]+"
        r"|data_quality:[a-z_]+:.+)$"
    ),
}


def test_there_are_the_registered_cases() -> None:
    assert len(FIXTURES) == 16  # 13 from 1.0.0, 3 added with the 1.1.0 rules


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_each_case_builds_and_names_known_checks(path: Path) -> None:
    case = json.loads(path.read_text())
    assert set(case) == {"case", "description", "spec", "ideal", "actual"}
    tables = build(StudySpec.model_validate(case["spec"]))
    assert {"dm", "tx", "ds", "bw", "lb", "om", "mi"} <= set(tables)
    assert "1.1.0" in case["ideal"]
    for version, ideal in case["ideal"].items():
        assert ideal == sorted(ideal)
        assert all(IDEAL[version].fullmatch(i) for i in ideal), (version, ideal)
