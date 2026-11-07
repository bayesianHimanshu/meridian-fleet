"""Against the real OpenShell gateway and the agent image. Run with -m openshell.

Skipped, with the reason, when the openshell CLI is not on PATH, no gateway answers, or
the agent image has not been built (meridian image build).
"""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.data.study_store import ParquetStudyReader
from meridian.platform.agent_settings import QtlDetectorConfig
from meridian.platform.config import AgentsSettings
from meridian.sandbox import image
from meridian.sandbox.executor import OpenShellExecutor, sandbox_name
from meridian.sandbox.openshell import SubprocessOpenShellClient
from meridian.sandbox.policies import policy_path
from tests.worker_support import cut_trigger, tiny_cut

pytestmark = pytest.mark.openshell
REPO = Path(__file__).resolve().parents[2]
AGENTS = AgentsSettings(qtl_detector=QtlDetectorConfig(discontinuation_limit=0.617))


def _skip_reason() -> str | None:
    if shutil.which("openshell") is None:
        return "openshell is not on PATH"
    try:
        status = subprocess.run(
            ["openshell", "status"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"openshell status failed: {exc}"
    if status.returncode != 0 or "Connected" not in status.stdout:
        return f"no OpenShell gateway answers: {(status.stdout + status.stderr).strip()[:200]}"
    try:
        image.inspect(image.default_image())
    except image.ImageError:
        return f"image {image.default_image()} not built; run meridian image build"
    return None


@pytest.fixture(scope="module", autouse=True)
def _openshell_available() -> None:
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)


@pytest.fixture(scope="module")
def client() -> SubprocessOpenShellClient:
    return SubprocessOpenShellClient()


@pytest.mark.parametrize("agent", ["rbqm.csm", "rbqm.kri_monitor", "rbqm.qtl_detector"])
def test_sandboxed_drafts_are_identical_to_in_process(
    tmp_path: Path, client: SubprocessOpenShellClient, agent: str
) -> None:
    cut = tiny_cut(tmp_path / "curated")
    registry = build_registry(AGENTS)
    ref = image.default_image()
    executor = OpenShellExecutor(
        client=client,
        registry=registry,
        study_root=cut.parent,
        image=ref,
        image_digest=image.inspect(ref).digest,
        policies_dir=REPO / "policies",
        exec_timeout_seconds=120,
        keep_failed=False,
        log=structlog.get_logger(),
    )
    # The image carries config/default.toml; the tiny cut is scored with the same agents.
    before = set(client.names())
    result = executor.run(agent, cut_trigger(), cut.name)
    registered = registry.get(agent)
    reader = ParquetStudyReader(cut.parent, cut.name)
    here = run_agent(registered, cut_trigger(), reader, structlog.get_logger())
    assert drafts_document(registered, result.drafts) == drafts_document(registered, here)
    assert result.policy_sha256 is not None
    assert set(client.names()) == before  # deleted


@pytest.fixture(scope="module")
def sandbox(client: SubprocessOpenShellClient) -> Iterator[str]:
    name = sandbox_name("rbqm.csm", 1)
    client.create(name, image.default_image(), policy_path("rbqm", REPO / "policies"), [])
    try:
        yield name
    finally:
        client.delete(name)


def _sh(client: SubprocessOpenShellClient, name: str, command: str) -> tuple[int, str]:
    out = client.exec(name, ["/usr/bin/bash", "-c", command], 30)
    return out.exit_code, (out.stdout + out.stderr).strip()


def test_control_writes_to_tmp_and_reads_the_runtime(
    client: SubprocessOpenShellClient, sandbox: str
) -> None:
    code, text = _sh(client, sandbox, "echo fine > /tmp/x && cat /tmp/x")
    assert (code, text) == (0, "fine")
    # Unix permissions would allow both refused operations below: /etc/passwd is world
    # readable and /var/tmp world writable. So what refuses them is Landlock.
    code, text = _sh(
        client,
        sandbox,
        '/usr/local/bin/python3.14 -c "import os; '
        "print(oct(os.stat('/etc/passwd').st_mode), oct(os.stat('/var/tmp').st_mode))\"",
    )
    assert (code, text) == (0, "0o100644 0o41777")


def test_reading_outside_the_allowed_paths_is_refused_by_landlock(
    client: SubprocessOpenShellClient, sandbox: str
) -> None:
    code, text = _sh(client, sandbox, "cat /etc/passwd")
    assert code != 0
    assert text == "cat: /etc/passwd: Permission denied"


def test_writing_outside_tmp_is_refused_by_landlock(
    client: SubprocessOpenShellClient, sandbox: str
) -> None:
    code, text = _sh(client, sandbox, "echo x > /var/tmp/evil.txt")
    assert code != 0
    assert "/var/tmp/evil.txt: Permission denied" in text


def test_outbound_https_is_refused_by_the_policy_network(
    client: SubprocessOpenShellClient, sandbox: str
) -> None:
    # By name: the sandbox's policy DNS resolves nothing.
    code, text = _sh(client, sandbox, "exec 3<>/dev/tcp/pypi.org/443")
    assert code != 0
    assert "Temporary failure in name resolution" in text
    # By address: the policy proxy refuses the TCP connection (logged by the sandbox as
    # OCSF NET:OPEN DENIED, reason transparent_tcp_policy_denied).
    code, text = _sh(client, sandbox, "exec 3<>/dev/tcp/151.101.0.223/443")
    assert code != 0
    assert "connect: Permission denied" in text
    # And a real HTTPS request from Python fails the same way.
    code, text = _sh(
        client,
        sandbox,
        '/usr/local/bin/python3.14 -c "import urllib.request; '
        "urllib.request.urlopen('https://pypi.org/simple/', timeout=10)\"",
    )
    assert code != 0
    assert "Temporary failure in name resolution" in text
