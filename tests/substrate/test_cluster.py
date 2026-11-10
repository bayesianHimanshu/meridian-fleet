"""Agent Substrate on the local kind cluster. Run with -m substrate (CI has no cluster).

Needs ate-system at the pinned release, `meridian substrate deploy` done (templates with
golden snapshots) and the curated DC-BASE cut. The proofs of docs/substrate-eval.md run
through the CLI (meridian substrate verify, conformance, canary); these tests are the
quick version of the first and third.
"""

import shutil
import subprocess

import pytest
import structlog

from meridian.agents.base import FindingDraft
from meridian.agents.execute import DraftsDocument
from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.sandbox import substrate_conformance as sc
from meridian.sandbox.substrate_deploy import registry_config_digest
from meridian.sandbox.substrate_executor import load_template
from meridian.sandbox.substrate_templates import CONFORMANCE
from meridian.sandbox.verify import trigger_for

pytestmark = pytest.mark.substrate


def _skip_reason() -> str | None:
    if shutil.which("kubectl") is None:
        return "kubectl is not on PATH"
    s = Settings().substrate
    done = subprocess.run(
        [s.kubectl, "ate", "--context", s.context, "get", "atespace", s.atespace],
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        None if done.returncode == 0 else "no Agent Substrate atespace (meridian substrate deploy)"
    )


@pytest.fixture(scope="module", autouse=True)
def _cluster() -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


def test_a_substrate_run_gives_the_in_process_drafts_and_leaves_no_actor() -> None:
    with Container.build(Settings()) as c:
        log = structlog.get_logger()
        executor = c.substrate_executor(log)
        inprocess = c.inprocess_executor(log)
        trigger = trigger_for(c.ledger, c.settings.study_id, "DC-BASE")
        there = executor.run("rbqm.kri_monitor", trigger, "DC-BASE")
        here = inprocess.run("rbqm.kri_monitor", trigger, "DC-BASE")
        executor.drain()
        version = c.agent_registry().get("rbqm.kri_monitor").agent.version

        def doc(drafts: tuple[FindingDraft, ...]) -> str:
            return DraftsDocument(
                agent_id="rbqm.kri_monitor", agent_version=version, drafts=drafts
            ).dumps()

        assert doc(there.drafts) == doc(here.drafts)
        assert there.substrate is not None
        assert there.substrate.actor_uid
        assert c.substrate_client().actors() == []


def test_a_zone_actor_reaches_nothing() -> None:
    with Container.build(Settings()) as c:
        s = c.settings.substrate
        client = c.substrate_client()
        executor = c.substrate_executor(structlog.get_logger(), client)
        probe = load_template(client, s.deploy_dir, CONFORMANCE, probe=True)
        result = sc.run_substrate_conformance(
            client=client,
            zone="rbqm",
            zone_template=executor.template_for_policy("rbqm"),
            probe_template=probe,
            placement=executor.placement,
            image_digest=registry_config_digest(probe.image),
            kubectl=s.kubectl,
            context=s.context,
        )
        assert result.passed, result.failures()
        assert client.actors() == []
