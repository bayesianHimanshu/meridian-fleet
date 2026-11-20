"""meridian-local-model-worker: send one local model job's requests from inside the sandbox.

    meridian-local-model-worker --job /stage/lm/job.json --base-url URL --timeout SECONDS

This runs only in the safety.local_model sandbox (policies/safety.local_model.yaml: GET
/api/tags and POST /api/chat on the Ollama host address, nothing else). It checks the job
folder against the host's inputs manifest, checks that Ollama serves the pinned digest,
sends each request with OllamaHttpClient and prints one LocalModelResults document on
stdout; logs go to stderr. The host built every request and validates every response. It
never imports study data or detection code.

Exit codes: 0 success (per-request errors, including a digest mismatch, are in the
document), 2 bad arguments, 3 data error.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import structlog
from pydantic import ValidationError

from meridian.data.inputs_manifest import InputsManifestError, verify_manifest
from meridian.ledger.hashing import canonical_json
from meridian.llm.local import OllamaHttpClient
from meridian.llm.local_job import LocalModelJob, run_local_job

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_DATA = 3


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"meridian-local-model-worker: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_BAD_ARGS)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="meridian-local-model-worker", description=__doc__.split("\n\n")[0])
    parser.add_argument("--job", required=True, type=Path, help="job.json in a staged folder")
    parser.add_argument("--base-url", required=True, help="the Ollama base URL")
    parser.add_argument("--timeout", required=True, type=float, help="seconds per request")
    return parser


def _logger() -> structlog.typing.FilteringBoundLogger:
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger()
    return logger


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log = _logger()
    try:
        verify_manifest(args.job.parent)
    except InputsManifestError as exc:
        log.error("local_model_worker.inputs_rejected", error=str(exc))
        return EXIT_DATA
    try:
        job = LocalModelJob.model_validate_json(args.job.read_bytes())
    except (OSError, ValidationError) as exc:
        log.error("local_model_worker.bad_input", error=f"{type(exc).__name__}: {exc}")
        return EXIT_DATA
    client = OllamaHttpClient(base_url=args.base_url, timeout_seconds=args.timeout)
    try:
        results = run_local_job(job, client)
    finally:
        client.close()
    sys.stdout.write(canonical_json(results.model_dump(mode="json")))
    sys.stdout.flush()
    errors = sum(r.error is not None for r in results.results)
    log.info("local_model_worker.done", requests=len(job.requests), errors=errors)
    return EXIT_OK


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()
