"""meridian.sandbox.substrate_deploy (kind and GKE deploy steps)."""


def test_wait_pool_waits_for_the_controller_to_create_the_deployment() -> None:
    """Wrap-up fresh-clone check: on a first deploy `rollout status` ran before the
    WorkerPool controller had created its Deployment and failed with NotFound."""
    from meridian.sandbox.substrate_deploy import DeployError, wait_pool

    calls: list[list[str]] = []
    missing = [2]

    def run(argv: list[str], **_: object) -> str:
        calls.append(argv)
        if argv[-2] == "get" and missing[0]:
            missing[0] -= 1
            raise DeployError('Error from server (NotFound): deployments.apps "p" not found')
        return ""

    wait_pool("kubectl", "kind-kind", "ns", "p", 30, run=run, sleep=lambda _: None)
    assert [("rollout" if "rollout" in c else c[-2]) for c in calls] == [
        "get",
        "get",
        "get",
        "rollout",
    ]


def test_a_deploy_without_a_labelled_node_says_how_to_label_one() -> None:
    import pytest

    from meridian.sandbox.substrate_deploy import DeployError, require_worker_node

    label = "meridian.dev/pool=substrate"
    with pytest.raises(DeployError, match=r"label node kind-control-plane meridian\.dev/pool"):
        require_worker_node("kubectl", "kind-kind", label, run=lambda argv: "")
    found = require_worker_node(
        "kubectl", "kind-kind", label, run=lambda argv: "node/kind-control-plane\n"
    )
    assert found == "node/kind-control-plane"
