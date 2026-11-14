"""Agent Substrate on the local kind cluster. Run with -m substrate (CI has no cluster).

Needs ate-system at the pinned release, `meridian substrate deploy` done (templates with
golden snapshots) and the curated DC-BASE cut. The proofs of docs/substrate-eval.md run
through the CLI (meridian substrate verify, conformance, canary); these tests are the
quick version of the first and third.
"""

import json
import shutil
import subprocess

import pytest
import structlog

from meridian.agents.base import FindingDraft
from meridian.agents.execute import DraftsDocument
from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.sandbox import substrate_conformance as sc
from meridian.sandbox.substrate_deploy import image_config_digest
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
        assert c.dispatcher_client().actors() == []


def test_a_zone_actor_reaches_nothing() -> None:
    with Container.build(Settings()) as c:
        s = c.settings.substrate
        client = c.dispatcher_client()
        executor = c.substrate_executor(structlog.get_logger())
        probe = load_template(client, s.deploy_dir, CONFORMANCE, probe=True)
        result = sc.run_substrate_conformance(
            session=client.conformance,
            zone="rbqm",
            zone_template=executor.template_for_policy("rbqm"),
            probe_template=probe,
            placement=executor.placement,
            image_digest=image_config_digest(probe.image, s.target),
            kubectl=s.kubectl,
            context=s.context,
        )
        assert result.passed, result.failures()
        assert result.dispatcher is not None  # an actor must not reach the dispatcher either
        assert client.actors() == []


def test_the_laptop_has_no_substrate_identity_and_the_router_admits_only_the_dispatcher() -> None:
    from meridian.cli_substrate import access_principals
    from meridian.sandbox.dispatcher_deploy import principal, router_allow_list, spiffe_id

    settings = Settings()
    s = settings.substrate
    assert access_principals(settings) == [principal(s)]
    assert router_allow_list(s.kubectl, s.context) == [spiffe_id(s)]
    ate = [s.kubectl, "ate", "--context", s.context]
    policy = subprocess.run(
        [*ate, "get", "access-policy", "-a", s.atespace, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    members = [m for b in json.loads(policy.stdout)["bindings"] for m in b["members"]]
    assert members == [principal(s)]
    gone = subprocess.run(
        [s.kubectl, "--context", s.context, "-n", s.namespace, "get", "sa", "meridian-runner"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert gone.returncode != 0
    assert "NotFound" in gone.stderr
