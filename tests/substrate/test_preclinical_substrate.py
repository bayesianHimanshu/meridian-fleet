"""The preclinical zone on kind (phase 5 step 1): a run of preclinical.tox_study_monitor in
an Agent Substrate actor on MRD-101-TOX-01 gives the in-process drafts byte for byte, the
actor receives only meta/ and send/, and no actor is left. Run with -m substrate."""

import pytest
import structlog

from meridian.agents.base import FindingDraft
from meridian.agents.execute import DraftsDocument
from meridian.data.send import send_study_events
from meridian.domain.events import SendStudyLoaded
from meridian.platform.config import Settings
from meridian.platform.container import Container
from tests.substrate.test_cluster import _skip_reason

pytestmark = pytest.mark.substrate
AGENT = "preclinical.tox_study_monitor"


@pytest.fixture(scope="module", autouse=True)
def _cluster() -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


def test_the_tox_monitor_in_substrate_gives_the_in_process_drafts() -> None:
    with Container.build(Settings()) as c:
        p = c.settings.preclinical
        loads = [
            e
            for e in send_study_events(c.ledger, p.study_id)
            if isinstance(e.payload, SendStudyLoaded) and e.payload.cut_id == p.cut_id
        ]
        if not loads:
            pytest.skip("MRD-101-TOX-01 is not ingested (meridian preclinical ingest)")
        trigger = loads[-1]
        log = structlog.get_logger()
        executor = c.substrate_executor(log)
        there = executor.run(AGENT, trigger, p.cut_id)
        here = c.inprocess_executor(log).run(AGENT, trigger, p.cut_id)
        executor.drain()
        version = c.agent_registry().get(AGENT).agent.version

        def doc(drafts: tuple[FindingDraft, ...]) -> str:
            return DraftsDocument(agent_id=AGENT, agent_version=version, drafts=drafts).dumps()

        assert doc(there.drafts) == doc(here.drafts)
        assert there.drafts
        assert there.substrate is not None
        assert there.substrate.template.startswith("preclinical-")
        assert there.substrate.sandbox_class == "gvisor"
        assert c.dispatcher_client().actors() == []
