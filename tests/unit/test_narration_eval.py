"""The narrator validator evaluation: seeded mutations and metrics on a tiny labelled set."""

from typing import Any

import pytest

from meridian.agents.rbqm import narrator as v1_1
from meridian.agents.rbqm import narrator_v1_0 as v1_0
from meridian.agents.rbqm.describe import ratio_evidence
from meridian.agents.rbqm.narrator_eval import (
    MUTATIONS,
    LabelledRecord,
    Narrative,
    evaluate,
    mutate,
    mutations_of,
    validator_1_0,
    validator_1_1,
)
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.findings import Evidence, Finding

FINDING = Finding(
    finding_id="RBQM-0001",
    kind=FindingKind.UNDER_REPORTING,
    agent_id="rbqm.kri_monitor",
    agent_version="2.2.0",
    study_id="MRD-201",
    site_id="1005",
    severity=Severity.HIGH,
    title="Site 1005: fewer adverse events than expected",
    summary="Site 1005 has 27 adverse events; the rest of the study implies 70.2.",
    evidence=(
        Evidence(source="x", selector="SITEID=1005", statistic="ae_rate:observed", value=27),
        Evidence(source="x", selector="SITEID=1005", statistic="ae_rate:expected", value=70.226),
        Evidence(
            source="x",
            selector="SITEID=1005",
            statistic="ae_rate:p_adjusted",
            value=4.349e-08,
            threshold=0.01,
        ),
        Evidence(source="x", selector="SITEID=1005", statistic="ae_rate:phi", value=1.27),
        ratio_evidence("x", "SITEID=1005", "ae_rate", 27, 70.226),
    ),
    visibility=Visibility.BLINDED,
    visible_to=frozenset({Role.CLINICAL_OPS_LEAD}),
    score=1.0,
)
SITES = ("1001", "1005", "1016")


def _record(headline: str, explanation: str, **changes: Any) -> LabelledRecord:
    base: dict[str, Any] = {
        "id": "RBQM-0001/real_good",
        "finding_id": "RBQM-0001",
        "cut": "DC-BASE",
        "origin": "real_good",
        "label": "good",
        "expected_reasons": (),
        "labelled_by": "assistant",
        "note": "test",
        "narrative": Narrative(headline=headline, explanation=explanation),
        "input_1_0": v1_0.narrator_input(FINDING),
        "input_1_1": v1_1.narrator_input(FINDING),
    }
    return LabelledRecord.model_validate(base | changes)


GOOD = _record(
    "Site 1005: fewer adverse events than expected",
    "Site 1005 recorded fewer adverse events than expected: 27 observed against 70.2 "
    "expected, Bonferroni-adjusted p = 4.3e-08.",
)


def test_the_good_record_passes_both_validators() -> None:
    assert validator_1_0(GOOD) == ()
    assert validator_1_1(GOOD) == ()


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_mutations_are_deterministic_per_seed(mutation: str) -> None:
    a = mutate(GOOD, mutation, 7, SITES)
    b = mutate(GOOD, mutation, 7, SITES)
    assert a == b
    assert a is not None
    assert a.label == "bad"
    assert a.origin == "mutated"
    assert a.narrative != GOOD.narrative


def test_a_different_seed_can_give_a_different_mutation() -> None:
    texts = {
        m.narrative.explanation
        for seed in range(20)
        if (m := mutate(GOOD, "wrong_number", seed, SITES)) is not None
    }
    assert len(texts) > 1


def test_each_mutation_does_what_it_says() -> None:
    by = {m.mutation: m for m in mutations_of([GOOD], 7, SITES)}
    assert set(by) == set(MUTATIONS)
    assert "more" in by["flip_direction"].narrative.explanation
    assert "1005" not in by["swap_site"].narrative.headline
    assert "p = 0" in by["zero_p"].narrative.explanation
    assert by["supporting_headline"].narrative.headline.startswith("Site 1005: ")
    assert by["wrong_number"].narrative.explanation != GOOD.narrative.explanation


def test_a_mutation_that_does_not_apply_is_skipped() -> None:
    no_p = _record("Site 1005: fewer adverse events", "Site 1005 recorded 27 adverse events.")
    assert mutate(no_p, "zero_p", 7, SITES) is None
    study = _record(
        "Study-wide", "27 events.", input_1_1=v1_1.narrator_input(FINDING) | {"site_id": None}
    )
    assert mutate(study, "swap_site", 7, SITES) is None


def test_metrics_on_a_tiny_hand_built_set() -> None:
    bad_ratio = _record(
        "Site 1005: fewer adverse events than expected",
        "Site 1005 recorded 27 adverse events, half the 70.2 expected.",
        id="b1",
        origin="real_bad",
        label="bad",
        expected_reasons=("unbacked_ratio",),
    )
    bad_zero = _record(
        "Site 1005: fewer adverse events than expected",
        "Site 1005 recorded 27 adverse events, adjusted p = 0.",
        id="b2",
        origin="mutated",
        mutation="zero_p",
        label="bad",
        expected_reasons=("zero_p_value",),
        labelled_by="generator",
    )
    rounded = _record(
        "Site 1005: fewer adverse events than expected",
        "Site 1005 recorded 27 adverse events against about 70 expected.",
        id="g2",
    )
    records = [GOOD, rounded, bad_ratio, bad_zero]

    new = evaluate(records, validator_1_1)
    assert (new.recall.hits, new.recall.total) == (2, 2)
    assert new.recall_by_origin["real_bad"].value == 1.0
    assert new.recall_by_mutation["zero_p"].value == 1.0
    assert [v.record.id for v in new.false_rejections] == ["g2"]  # 70 is not a display
    assert new.expected_reason["rejected for the expected reason"] == 2

    old = evaluate(records, validator_1_0)
    # 1.0.0 has no ratio rule, so "half" passes; "p = 0" is rejected only because no
    # evidence value here is 0; "about 70" passes its tolerance match.
    assert old.false_rejections == []
    assert old.recall_by_origin["real_bad"].hits == 0
    assert old.expected_reason["accepted"] == 1
