"""meridian-narrator-worker: send one narration job's requests from inside the sandbox.

    meridian-narrator-worker --job /tmp/narr/job.json --timeout SECONDS

This runs only in rbqm.narrator's sandbox (policies/rbqm.narrator.yaml, with the
meridian-anthropic provider attached). It checks the job folder against the host's
inputs manifest, sends each request with AnthropicHttpClient and prints one
NarrationResults document on stdout; logs go to stderr. The host built every request and
validates every response. It is kept apart from meridian-worker, whose import closure
stays free of any network module, and it never imports study data or detection code.

Exit codes: 0 success (per-request errors are in the document), 2 bad arguments,
3 data error.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import structlog
from pydantic import ValidationError

from meridian.data.inputs_manifest import InputsManifestError, verify_manifest
from meridian.ledger.hashing import canonical_json
from meridian.llm.client import AnthropicHttpClient
from meridian.llm.job import NarrationJob, run_job

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_DATA = 3


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"meridian-narrator-worker: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_BAD_ARGS)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="meridian-narrator-worker", description=__doc__.split("\n\n")[0])
    parser.add_argument("--job", required=True, type=Path, help="job.json in a staged folder")
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
        log.error("narrator_worker.inputs_rejected", error=str(exc))
        return EXIT_DATA
    try:
        job = NarrationJob.model_validate_json(args.job.read_bytes())
    except (OSError, ValidationError) as exc:
        log.error("narrator_worker.bad_input", error=f"{type(exc).__name__}: {exc}")
        return EXIT_DATA
    client = AnthropicHttpClient(timeout_seconds=args.timeout)
    try:
        results = run_job(job, client)
    finally:
        client.close()
    sys.stdout.write(canonical_json(results.model_dump(mode="json")))
    sys.stdout.flush()
    errors = sum(r.error is not None for r in results.results)
    log.info("narrator_worker.done", requests=len(job.requests), errors=errors)
    return EXIT_OK


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()
