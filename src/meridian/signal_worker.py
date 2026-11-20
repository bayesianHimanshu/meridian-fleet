"""meridian-signal-worker: one portfolio.safety_signal_scout run inside its sandbox.

    meridian-signal-worker run --agent portfolio.safety_signal_scout \\
        --trigger /stage/meta/trigger.json --cut-dir /stage

This runs only under policies/portfolio_signal.yaml (phase 5 step 3a): egress to
api.fda.gov:443 for GET /drug/event.json and /drug/label.json, and nothing staged but the
trigger and, for a replay, the run's recorded responses in /stage/signal. It checks the
stage against the host's inputs manifest; fetches the pre-registered openFDA queries
(meridian.portfolio.openfda) unless responses are staged; runs the agent on them, which is
pure; and prints one SignalDocument with the drafts and every response, bodies included,
so the host keeps them. It is kept apart from meridian-worker, whose import closure stays
free of any network module. No API key, no credential: openFDA's count queries need none.

Exit codes: 0 success, 2 bad arguments, 3 data error, 4 the agent failed.
"""

import argparse
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from pathlib import Path

import httpx
import polars as pl
import structlog
from pydantic import ValidationError

from meridian.agents.catalog import build_registry
from meridian.agents.execute import run_agent
from meridian.agents.portfolio.safety_signal_scout import AGENT_ID, responses_frame, responses_from
from meridian.agents.registry import RegistrationError
from meridian.data.inputs_manifest import MANIFEST_FILE, InputsManifestError, verify_manifest
from meridian.data.stage_layout import META, SIGNAL
from meridian.data.study_store import SignalStageReader, StudyAccessError
from meridian.domain.events import Event, SignalScanRequested
from meridian.llm.client import ssl_context
from meridian.platform.agent_settings import SignalScoutConfig
from meridian.portfolio import openfda as o
from meridian.portfolio.document import ResponseRecord, SignalDocument
from meridian.worker import _config_path, load_agents_settings

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_DATA = 3
EXIT_AGENT = 4
USER_AGENT = "meridian-fleet-signal-scout/1.0"  # a generic client name, no contact details
# The signal stage's two forms besides a live scan: this scan's own kept responses (a
# replay: nothing is fetched) or, for a rerun (1.0.1), an earlier scan's as prior_responses.


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"meridian-signal-worker: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_BAD_ARGS)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="meridian-signal-worker", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--agent", required=True)
    run.add_argument("--trigger", required=True, type=Path)
    run.add_argument("--cut-dir", required=True, type=Path)
    run.add_argument("--config", type=Path, default=None)
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


def http_get(cfg: SignalScoutConfig) -> tuple[httpx.Client, o.Get]:
    """GET base_url + path + query with certificate verification on (the sandbox proxy's
    CA through SSL_CERT_FILE, as the narrator), no proxy settings, no redirects."""
    client = httpx.Client(
        verify=ssl_context(),
        trust_env=False,
        follow_redirects=False,
        timeout=cfg.request_timeout_seconds,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )

    def get(path: str, query: str) -> tuple[int, str]:
        if path not in (o.EVENT, o.LABEL):
            raise ValueError(f"refusing to request {path}: only {o.EVENT} and {o.LABEL}")
        r = client.get(f"{cfg.base_url}{path}?{query}")
        return r.status_code, r.text

    return client, get


def execute(
    agent_id: str,
    trigger_file: Path,
    cut_dir: Path,
    config: Path,
    log: structlog.typing.FilteringBoundLogger,
    get: o.Get | None = None,
) -> tuple[int, str | None]:
    """One run: (exit code, the SignalDocument or None). get replaces the HTTP client (a
    test's fake); with responses staged under cut_dir/signal, nothing is fetched."""
    if agent_id != AGENT_ID:
        log.error("signal_worker.unknown_agent", agent=agent_id)
        return EXIT_BAD_ARGS, None
    try:
        registered = build_registry(load_agents_settings(config)).get(agent_id)
    except (OSError, tomllib.TOMLDecodeError, ValidationError, RegistrationError, KeyError) as exc:
        log.error("signal_worker.bad_config", error=f"{type(exc).__name__}: {exc}")
        return EXIT_BAD_ARGS, None
    cfg = registered.config
    assert isinstance(cfg, SignalScoutConfig)
    try:
        verify_manifest(cut_dir, at=cut_dir / META / MANIFEST_FILE)
        trigger = Event.model_validate_json(trigger_file.read_bytes())
        if not isinstance(trigger.payload, SignalScanRequested):
            raise ValueError(f"trigger is a {trigger.payload.type}, not a signal scan")
    except (InputsManifestError, OSError, ValidationError, ValueError) as exc:
        log.error("signal_worker.bad_input", error=f"{type(exc).__name__}: {exc}")
        return EXIT_DATA, None
    scan = trigger.payload.scan_id
    staged = cut_dir / SIGNAL / f"{o.RESPONSES_TABLE}.parquet"
    prior_file = cut_dir / SIGNAL / f"{o.PRIOR_TABLE}.parquet"
    with tempfile.TemporaryDirectory(prefix="signal-") as tmp:
        if staged.exists():
            reader = SignalStageReader.at_cut_dir(cut_dir, cut_id=scan, study_id=cfg.study_id)
            replayed = True
        else:
            client = None
            try:
                if get is None:
                    client, get = http_get(cfg)
                if prior_file.exists():
                    # 1.0.1: a rerun on an earlier scan's responses asks only what it lacks.
                    if trigger.payload.reuses_responses_of is None:
                        raise ValueError("earlier responses staged for a scan that reuses none")
                    prior = responses_from(pl.read_parquet(prior_file))
                    responses = o.top_up(get, cfg, prior)
                else:
                    responses = o.fetch(get, cfg)
            except (httpx.HTTPError, ValueError) as exc:
                log.error("signal_worker.fetch_failed", error=f"{type(exc).__name__}: {exc}")
                return EXIT_DATA, None
            finally:
                if client is not None:
                    client.close()
            (Path(tmp) / SIGNAL).mkdir()
            responses_frame(responses).write_parquet(
                Path(tmp) / SIGNAL / f"{o.RESPONSES_TABLE}.parquet"
            )
            reader = SignalStageReader.at_cut_dir(Path(tmp), cut_id=scan, study_id=cfg.study_id)
            replayed = False
            log.info("signal_worker.fetched", requests=len(responses))
        try:
            records = tuple(
                ResponseRecord.model_validate(r)
                for r in reader.table(o.RESPONSES_TABLE).iter_rows(named=True)
            )
            drafts = run_agent(registered, trigger, reader, log)
        except (StudyAccessError, ValueError, ValidationError) as exc:
            log.error("signal_worker.data_error", error=f"{type(exc).__name__}: {exc}")
            return EXIT_DATA, None
        except Exception as exc:  # any other failure is the agent's
            log.error("signal_worker.agent_error", error=f"{type(exc).__name__}: {exc}")
            return EXIT_AGENT, None
    doc = SignalDocument(
        agent_id=registered.agent.agent_id,
        agent_version=registered.agent.version,
        drafts=tuple(drafts),
        replayed=replayed,
        responses=records,
    )
    log.info("signal_worker.done", drafts=len(drafts), replayed=replayed)
    return EXIT_OK, doc.dumps()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log = _logger()
    code, document = execute(args.agent, args.trigger, args.cut_dir, _config_path(args.config), log)
    if document is not None:
        sys.stdout.write(document)
        sys.stdout.flush()
    return code


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()
