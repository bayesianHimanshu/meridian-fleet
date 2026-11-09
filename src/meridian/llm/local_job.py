"""A local model job: the requests the host built, for one pinned model, and their results.

The host writes job.json (with an inputs manifest) into the folder it uploads to the
safety.local_model sandbox; meridian-local-model-worker verifies the manifest, checks
that Ollama serves the pinned digest, sends each request and prints one LocalModelResults
document on stdout. If the served digest differs, no request is sent and every result is
a digest_mismatch error. The host validates every response itself.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.llm.local import (
    LocalModelClient,
    LocalModelError,
    LocalModelRequest,
    LocalModelResponse,
    require_pinned,
)

JOB_DIR = "lm"
JOB_FILE = "job.json"
_SHA = r"^[0-9a-f]{64}$"


class LocalModelJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    pinned_digest: str = Field(pattern=_SHA)
    requests: tuple[LocalModelRequest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_model(self) -> LocalModelJob:
        if any(r.model != self.model for r in self.requests):
            raise ValueError("every request of a job names the job's model")
        return self


class LocalCallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_sha256: str = Field(pattern=_SHA)
    response: LocalModelResponse | None = None
    error_kind: str | None = None
    error: str | None = None
    status: int | None = None


class LocalModelResults(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    served_digest: str | None = None  # None if /api/tags could not be read
    results: tuple[LocalCallResult, ...]


def _error(request: LocalModelRequest, exc: LocalModelError) -> LocalCallResult:
    return LocalCallResult(
        request_sha256=request.sha256(),
        error_kind=exc.kind,
        error=str(exc)[:500],
        status=exc.status,
    )


def run_local_job(job: LocalModelJob, client: LocalModelClient) -> LocalModelResults:
    try:
        served = client.served_digest(job.model)
    except LocalModelError as exc:
        return LocalModelResults(results=tuple(_error(r, exc) for r in job.requests))
    try:
        require_pinned(job.model, served, job.pinned_digest)
    except LocalModelError as exc:
        return LocalModelResults(
            served_digest=served, results=tuple(_error(r, exc) for r in job.requests)
        )
    results = []
    for request in job.requests:
        try:
            results.append(
                LocalCallResult(request_sha256=request.sha256(), response=client.chat(request))
            )
        except LocalModelError as exc:
            results.append(_error(request, exc))
    return LocalModelResults(served_digest=served, results=tuple(results))
