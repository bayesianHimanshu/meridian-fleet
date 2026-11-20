"""Where a local model job runs: the safety.local_model sandbox, or in process for tests.

Both local model agents (safety.term_adjudicator and safety.local_narrator) build their
requests on the host, hand one LocalModelJob per pass to an executor, and validate every
result on the host. The executor only carries the calls.
"""

from dataclasses import dataclass
from typing import Protocol

from meridian.llm.local import LocalModelClient
from meridian.llm.local_job import LocalCallResult, LocalModelJob, run_local_job

INPROCESS = "inprocess"


@dataclass(frozen=True, slots=True)
class LocalBatch:
    results: tuple[LocalCallResult, ...]
    served_digest: str | None
    executor: str
    sandbox: str | None = None
    policy_sha256: str | None = None


class LocalModelExecutor(Protocol):
    @property
    def name(self) -> str: ...

    def call(self, job: LocalModelJob, timeout_seconds: float) -> LocalBatch:
        """Send the job; one result per request, in order. Raise if the batch failed."""
        ...

    def drain(self) -> None: ...


class InProcessLocalModelExecutor:
    """Calls the client in this process. Tests and CI use it with FakeLocalModelClient;
    real calls go through the safety.local_model sandbox."""

    name = INPROCESS

    def __init__(self, client: LocalModelClient) -> None:
        self._client = client

    def call(self, job: LocalModelJob, timeout_seconds: float) -> LocalBatch:
        results = run_local_job(job, self._client)
        return LocalBatch(results.results, results.served_digest, self.name)

    def drain(self) -> None:
        """Nothing runs in the background in process."""
