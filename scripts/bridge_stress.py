"""Run preclinical.translational_bridge once on the hand-written stress set (phase 5 step
3a; docs/translational-notes.md, "Stress set"): each case through the host's extract
writer and the agent, its actual result written back under the agent version. It tests
the specification: the cases and the agent have the same author, so agreement shows the
code follows the rules, not that the monitoring is what a clinical pharmacologist would
choose. Usage: uv run python scripts/bridge_stress.py"""

import json
import tempfile
from pathlib import Path

import structlog

from meridian.agents.base import AgentContext
from meridian.agents.preclinical.translational_bridge import VERSION, TranslationalBridge
from meridian.data.send import write_send_cut
from meridian.data.study_store import LayeredStudyReader, NoaelStageReader, SendStudyReader
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.preclinical.bridge_cases import T0, case_events, outcome
from meridian.preclinical.inputs import write_noael_extract
from meridian.preclinical.synth import STUDY, StudySpec, build

FIXTURES = Path("tests/fixtures/bridge_stress")
CUT = "TOX-STRESS"


def main() -> None:
    cfg = Settings().agents.translational_bridge
    agree = 0
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_send_cut(
            send_root=root / "send", study_id=STUDY, cut_id=CUT,
            tables=build(StudySpec(seed=1)), created_at=T0, sources=(),
        )  # fmt: skip
        send = SendStudyReader(root / "send" / STUDY, CUT)
        for path in sorted(FIXTURES.glob("*.json")):
            case = json.loads(path.read_text())
            events, trigger = case_events(case, CUT, STUDY)
            stage = write_noael_extract(events, trigger, CUT, root / "noael" / path.stem)
            reader = LayeredStudyReader(
                send, NoaelStageReader.at_cut_dir(stage, cut_id=CUT, study_id=STUDY)
            )
            ctx = AgentContext(
                study=reader, clock=FixedClock(T0), config=cfg, log=structlog.get_logger()
            )
            actual = outcome(list(TranslationalBridge().run(trigger, ctx)))
            ideal = case["ideal"][VERSION]
            same = actual == ideal
            agree += same
            rows.append((case["name"], same, ideal, actual))
            case.setdefault("actual", {})[VERSION] = actual
            path.write_text(json.dumps(case, indent=2) + "\n")
    for name, same, ideal, actual in rows:
        print(f"{'agree ' if same else 'DIFFER'} {name}")
        if not same:
            print(f"         ideal  {json.dumps(ideal)}")
            print(f"         actual {json.dumps(actual)}")
    print(f"{agree} of {len(rows)} cases as ideal (preclinical.translational_bridge {VERSION})")


if __name__ == "__main__":
    main()
