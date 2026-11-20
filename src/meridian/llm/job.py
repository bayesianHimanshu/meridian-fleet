"""A narration job: the requests the host built, and what the model client made of each.

The host writes job.json (with an inputs manifest) into the folder it uploads to the
narrator's sandbox; meridian-narrator-worker verifies the manifest, sends each request
with the model client and prints one NarrationResults document on stdout. The host then
validates every response itself: the sandbox only carries the call.
"""

from pydantic import BaseModel, ConfigDict, Field

from meridian.llm.client import ModelClient, ModelClientError, ModelRequest, ModelResponse

JOB_DIR = "narr"
JOB_FILE = "job.json"


class NarrationJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    requests: tuple[ModelRequest, ...] = Field(min_length=1)


class CallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: ModelResponse | None = None
    error_kind: str | None = None
    error: str | None = None
    status: int | None = None


class NarrationResults(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    results: tuple[CallResult, ...]


def call(client: ModelClient, request: ModelRequest) -> CallResult:
    try:
        response = client.complete(request)
    except ModelClientError as exc:
        return CallResult(
            request_sha256=request.sha256(),
            error_kind=exc.kind,
            error=str(exc)[:500],
            status=exc.status,
        )
    return CallResult(request_sha256=request.sha256(), response=response)


def run_job(job: NarrationJob, client: ModelClient) -> NarrationResults:
    return NarrationResults(results=tuple(call(client, r) for r in job.requests))
