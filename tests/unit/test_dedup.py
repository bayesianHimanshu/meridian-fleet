import pytest

from meridian.domain.dedup import dedup_prefix, make_dedup_key, parse_dedup_key
from meridian.domain.enums import FindingKind


def test_format_round_trips() -> None:
    key = make_dedup_key(
        agent_id="rbqm.kri_monitor",
        agent_version="2.0.0",
        kind=FindingKind.KRI_BREACH,
        study_id="MRD-201",
        site_id="1004",
        cut_id="DC-BASE",
        check="data_entry_lag",
    )
    assert key == "rbqm.kri_monitor@2.0.0|kri_breach|MRD-201|1004|DC-BASE|data_entry_lag"
    parts = parse_dedup_key(key)
    assert parts.agent_id == "rbqm.kri_monitor"
    assert parts.agent_version == "2.0.0"
    assert parts.check == "data_entry_lag"
    assert key.startswith(dedup_prefix("rbqm.kri_monitor", "2.0.0"))


def test_study_level_and_no_check() -> None:
    key = make_dedup_key(
        agent_id="rbqm.csm",
        agent_version="2.0.0",
        kind=FindingKind.SITE_DATA_ANOMALY,
        study_id="MRD-201",
        site_id=None,
        cut_id="DC-X",
        check=None,
    )
    assert key.endswith("|STUDY|DC-X|-")


def test_legacy_keys_without_version_still_parse() -> None:
    parts = parse_dedup_key("rbqm.kri_monitor|kri_breach|MRD-201|1004|DC-BASE|data_entry_lag")
    assert parts.agent_id == "rbqm.kri_monitor"
    assert parts.agent_version is None
    assert parts.site == "1004"


def test_same_finding_differs_by_version() -> None:
    def key(version: str) -> str:
        return make_dedup_key(
            agent_id="rbqm.csm",
            agent_version=version,
            kind=FindingKind.SITE_DATA_ANOMALY,
            study_id="S",
            site_id="1001",
            cut_id="C",
            check=None,
        )

    assert key("1.0.0") != key("2.0.0")


@pytest.mark.parametrize(
    ("field", "value"), [("study_id", "A|B"), ("agent_version", "2@0"), ("agent_id", "")]
)
def test_rejects_bad_parts(field: str, value: str) -> None:
    kwargs: dict[str, str] = {"agent_id": "rbqm.x", "agent_version": "1", "study_id": "S"}
    kwargs[field] = value
    with pytest.raises(ValueError, match=r"contain|empty"):
        make_dedup_key(kind=FindingKind.KRI_BREACH, site_id=None, cut_id="C", check=None, **kwargs)


def test_rejects_malformed() -> None:
    with pytest.raises(ValueError, match="not a dedup key"):
        parse_dedup_key("a|b|c")
