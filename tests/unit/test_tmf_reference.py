"""The expected TMF artifacts (config/tmf_expected.toml) and the agent's config."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.domain.enums import Role
from meridian.domain.tmf import norm_name
from meridian.platform.agent_settings import TmfCompletenessConfig
from meridian.platform.config import Settings
from meridian.platform.tmf_reference import ExpectedArtifact, TmfReference, load_tmf_reference
from meridian.worker import load_agents_settings

REPO = Path(__file__).resolve().parents[2]
EXPECTED = REPO / "config" / "tmf_expected.toml"
# The zones of the TMF Reference Model, as named in CDISC TMF Controlled Terminology
# 2024-09-27 (its "TMF-RM Zones" codelist and zone codelists).
ZONES = {
    "Trial Management",
    "Central Trial Documents",
    "Regulatory",
    "IRB or IEC and Other Approvals",
    "Site Management",
    "IP and Trial Supplies",
    "Safety Reporting",
    "Central and Local Testing",
    "Third Parties",
    "Data Management",
    "Statistics",
}


def test_the_reference_loads_and_every_artifact_has_level_milestone_and_timing() -> None:
    ref = load_tmf_reference(EXPECTED)
    assert 15 <= len(ref.artifacts) <= 20
    for a in ref.artifacts:
        assert a.level in ("trial", "site")
        assert a.milestone
        assert a.file_within_days > 0
        assert a.zone in ZONES
    levels = [a.level for a in ref.artifacts]
    assert levels.count("trial") == 7
    assert levels.count("site") == 9
    # The two records the approval check reads.
    dated = {a.key for a in ref.artifacts if a.dated_before == "site_first_subject"}
    assert dated == {"irb_iec_decision", "form_fda_1572"}
    # One report per visit type.
    assert {ref.for_visit(v).key for v in ("initiation", "routine", "close_out")} == {  # type: ignore[union-attr]
        "initiation_report",
        "monitoring_visit_report",
        "close_out_report",
    }


def test_the_reference_records_its_source_and_uses_no_numbers() -> None:
    ref = load_tmf_reference(EXPECTED)
    assert ref.reference.publication == "CDISC TMF Controlled Terminology, 2024-09-27"
    assert ref.reference.numbers.startswith("not used")
    text = EXPECTED.read_text()
    # No zone.section.artifact number such as 05.04.01 anywhere in the file.
    assert re.search(r"\b\d{2}\.\d{2}\.\d{2}\b", text) is None


def test_names_match_after_case_and_whitespace_are_normalised() -> None:
    ref = load_tmf_reference(EXPECTED)
    assert ref.by_name()["FORM FDA 1572"].key == "form_fda_1572"
    assert ref.by_name()[norm_name("  irb or iec   Decision ")].key == "irb_iec_decision"


def test_a_trial_artifact_cannot_have_a_site_milestone() -> None:
    with pytest.raises(ValidationError):
        ExpectedArtifact(
            key="x",
            name="X",
            zone="Regulatory",
            level="trial",
            milestone="site_activation",
            file_within_days=1,
            essential=True,
        )
    ref = load_tmf_reference(EXPECTED)
    with pytest.raises(ValidationError):
        TmfReference(
            reference=ref.reference, rules=ref.rules, artifacts=(*ref.artifacts, ref.artifacts[0])
        )


def test_the_agent_config_reads_the_reference_and_the_dm_fields() -> None:
    cfg = Settings().agents.tmf_completeness
    assert cfg.first_subject_field == "RFSTDTC"
    assert cfg.last_subject_field == "RFENDTC"
    assert set(cfg.visible_to) == {
        Role.CLINICAL_OPS_LEAD,
        Role.MEDICAL_MONITOR,
        Role.QA_AUDITOR,
        Role.PORTFOLIO_LEAD,
    }
    assert cfg.reference == load_tmf_reference(EXPECTED)
    with pytest.raises(ValidationError):
        TmfCompletenessConfig(first_subject_field="DMDTC")  # type: ignore[arg-type]


def test_the_worker_loads_the_reference_next_to_its_config() -> None:
    agents = load_agents_settings(REPO / "config" / "default.toml")
    assert agents.tmf_completeness.reference == load_tmf_reference(EXPECTED)
