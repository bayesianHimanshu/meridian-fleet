"""Attribution (phase 5 step 3b): the CLI records an operator, never a person; an
AttributionCorrection is recorded beside the event it corrects; and the audit reviewer's
1.3.0 check flags a person named on an event that did not come through the Fleet API."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.agents.quality.audit_trail_reviewer import VERSION, attribution, parse, review
from meridian.audit.extract import extract_rows, frame
from meridian.domain import audit as a
from meridian.domain.enums import DecisionAction, Role
from meridian.domain.events import (
    AttributionCorrection,
    HumanDecision,
    SignalScanRequested,
)
from meridian.domain.findings import Decision
from meridian.domain.identity import (
    NotAnOperatorError,
    is_operator,
    kind_of,
    operator_id,
    people_ids,
    require_operator,
)
from meridian.platform.agent_settings import AuditTrailReviewerConfig
from tests.api_support import T0, Fixture

PEOPLE = frozenset({"karan.mehta", "leela.nair"})


def _fixture(tmp_path: Path) -> Fixture:
    fx = Fixture(root=tmp_path, ledger_path=tmp_path / "ledger.sqlite3")
    with fx.ledger() as ledger:
        ledger.initialize()
    return fx


def _decision(actor: str) -> HumanDecision:
    return HumanDecision(
        decision=Decision(
            finding_id="PRE-0001",
            action=DecisionAction.ACCEPTED,
            actor=actor,
            role=Role.MEDICAL_MONITOR,
            reason="Read the evidence; accepted as recorded.",
        )
    )


def _scan(by: str) -> SignalScanRequested:
    return SignalScanRequested(study_id="MRD-201", scan_id="SCAN-20261119T211657Z", requested_by=by)


def _hits(fx: Fixture) -> list[str]:
    rows = extract_rows(
        fx.events,
        from_seq=1,
        through_seq=fx.events[-1].seq,
        operational_cuts=(),
        people=PEOPLE,
        agents={"portfolio.safety_signal_scout"},
    )
    return [h.record for h in attribution(parse(frame(rows)))]


def test_operator_ids() -> None:
    assert operator_id("himanshu") == "operator:himanshu"
    assert operator_id("Some User") == "operator:some_user"
    assert is_operator(operator_id())
    assert not is_operator("karan.mehta")
    assert kind_of("karan.mehta", PEOPLE) == "person"
    assert kind_of("operator:x", PEOPLE) == "operator"
    assert kind_of("rbqm.csm", PEOPLE, {"rbqm.csm"}) == "agent"
    assert kind_of("meridian.signals", PEOPLE) == "system"


def test_the_cli_refuses_a_person_or_anything_but_an_operator() -> None:
    assert require_operator("operator:himanshu", PEOPLE) == "operator:himanshu"
    with pytest.raises(NotAnOperatorError, match="is a person"):
        require_operator("karan.mehta", PEOPLE)
    with pytest.raises(NotAnOperatorError, match="not an operator"):
        require_operator("meridian.signals", PEOPLE)


def test_people_ids_are_config_people() -> None:
    people = people_ids(Path("config/people.toml"))
    assert {"karan.mehta", "leela.nair"} <= people
    assert not any(is_operator(p) for p in people)


def test_an_api_decision_passes_and_a_cli_scan_naming_a_person_is_flagged(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    fx.append(_decision("karan.mehta"), actor="karan.mehta")  # 1: as the API writes it
    fx.append(_scan("karan.mehta"), actor="meridian.signals")  # 2: step 3a's mistake
    fx.append(_scan("operator:himanshu"), actor="operator:himanshu")  # 3: as now
    fx.append(_decision("karan.mehta"), actor="rbqm.csm")  # 4: a decision not from the API
    fx.append(_scan("meridian.signals"), actor="karan.mehta")  # 5: a person as a CLI actor
    assert _hits(fx) == ["2", "4", "5"]


def test_a_correction_is_evidence_but_the_occurrence_stays(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    fx.append(_scan("karan.mehta"), actor="meridian.signals")
    fx.append(
        AttributionCorrection(
            corrects_seq=1,
            field="requested_by",
            recorded_value="karan.mehta",
            true_value="operator:himanshu",
            explanation="Started from the CLI by the coding agent, not by the medical monitor.",
            corrected_by="operator:himanshu",
        ),
        actor="operator:himanshu",
    )
    rows = extract_rows(fx.events, from_seq=1, through_seq=2, operational_cuts=(), people=PEOPLE)
    assert rows[1][a.CORRECTS_SEQ] == 1
    (hit,) = attribution(parse(frame(rows)))
    assert hit.record == "1"
    assert "seq 2" in hit.summary
    corrected = next(e for e in hit.evidence if e.statistic == "attribution:corrected")
    assert corrected.value == 1.0
    assert hit.actor is None  # the person named did nothing: nobody is under review


def test_the_correction_needs_an_explanation() -> None:
    with pytest.raises(ValidationError):
        AttributionCorrection(
            corrects_seq=1,
            field="requested_by",
            recorded_value="karan.mehta",
            true_value="operator:x",
            explanation="oops",
            corrected_by="operator:x",
        )


def test_an_older_extract_without_kinds_is_not_judged(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    fx.append(_scan("karan.mehta"), actor="meridian.signals")
    rows = extract_rows(fx.events, from_seq=1, through_seq=1, operational_cuts=())
    table = frame(rows).drop(a.ENVELOPE_KIND, a.NAMED)
    assert attribution(parse(table)) == []


def test_the_check_is_high_and_in_the_review(tmp_path: Path) -> None:
    assert VERSION == "1.3.0"
    fx = _fixture(tmp_path)
    fx.append(_scan("karan.mehta"), actor="meridian.signals")
    rows = extract_rows(fx.events, from_seq=1, through_seq=1, operational_cuts=(), people=PEOPLE)
    hits = [
        h for h in review(frame(rows), 1, 1, AuditTrailReviewerConfig(), T0)
        if h.check == a.PERSON_OUTSIDE_API
    ]  # fmt: skip
    assert [h.record for h in hits] == ["1"]


def test_the_scan_cli_refuses_a_person_before_recording_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from meridian.cli import app

    path = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("MERIDIAN_LEDGER_PATH", str(path))
    runner = CliRunner()
    assert runner.invoke(app, ["ledger", "init"]).exit_code == 0
    refused = runner.invoke(app, ["signals", "scan", "--by", "karan.mehta"])
    assert refused.exit_code == 1
    assert "is a person" in refused.output
    assert not runner.invoke(app, ["ledger", "show"]).stdout.strip()


def test_correct_attribution_from_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from typer.testing import CliRunner

    from meridian.cli import app

    path = tmp_path / "cli.sqlite3"
    monkeypatch.setenv("MERIDIAN_LEDGER_PATH", str(path))
    fx = Fixture(root=tmp_path, ledger_path=path)
    with fx.ledger() as ledger:
        ledger.initialize()
    fx.append(_scan("karan.mehta"), actor="meridian.signals")
    runner = CliRunner()
    base = ["ledger", "correct-attribution", "--seq", "1", "--field", "requested_by"]
    why = ["--explanation", "Started from the CLI by the coding agent, not by him."]
    person = runner.invoke(app, [*base, "--true", "leela.nair", *why])
    assert person.exit_code == 1
    done = runner.invoke(app, [*base, "--true", "operator:himanshu", *why])
    assert done.exit_code == 0, done.output
    (line,) = runner.invoke(app, ["ledger", "show", "--since", "1"]).stdout.splitlines()
    event = json.loads(line)
    assert event["actor"].startswith("operator:")
    assert event["payload"]["recorded_value"] == "karan.mehta"
    assert event["payload"]["true_value"] == "operator:himanshu"
