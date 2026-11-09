"""meridian-worker: run one agent on one staged cut and print its drafts.

    meridian-worker run --agent AGENT_ID --trigger /stage/meta/trigger.json --cut-dir /stage

This is what runs inside a sandbox. It reads a trigger (a DataCut event as JSON) and a
cut directory holding meta/ (the trigger and inputs_manifest.json) and the cut areas
its zone may read, checks every staged file against the host's manifest, builds the
agent from the packaged config, and prints one canonical JSON document {"agent_id",
"agent_version", "drafts"} on stdout. Logs go to stderr. It never opens the ledger, never
touches the network and never sees unblinded data or ground truth.

Exit codes: 0 success, 2 bad arguments, 3 data error, 4 agent error.
"""

import argparse
import os
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import polars as pl
import structlog
from pydantic import ValidationError

from meridian.agents.catalog import build_registry
from meridian.agents.execute import drafts_document, run_agent
from meridian.agents.registry import RegistrationError
from meridian.data.inputs_manifest import MANIFEST_FILE, InputsManifestError, verify_manifest
from meridian.data.stage_layout import META
from meridian.data.study_store import MissingTableError, ParquetStudyReader, StudyAccessError
from meridian.domain.events import DataCut, Event
from meridian.platform.agent_settings import SAFETY_TERMS_FILE, load_safety_terms
from meridian.platform.config import CONFIG_FILE_ENV, AgentsSettings

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_DATA = 3
EXIT_AGENT = 4
IMAGE_CONFIG = Path("/opt/meridian/config/default.toml")
DATA_ERRORS = (MissingTableError, StudyAccessError, OSError, pl.exceptions.PolarsError)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"meridian-worker: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_BAD_ARGS)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="meridian-worker", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one agent on one staged cut")
    run.add_argument("--agent", required=True, help="agent id, for example rbqm.csm")
    run.add_argument("--trigger", required=True, type=Path, help="DataCut event as JSON")
    run.add_argument("--cut-dir", required=True, type=Path, help="holds blinded/ and ops/")
    run.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"TOML config (default: ${CONFIG_FILE_ENV}, else {IMAGE_CONFIG})",
    )
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


def load_agents_settings(path: Path) -> AgentsSettings:
    """The [agents] table of the packaged config. Environment overrides do not apply."""
    with path.open("rb") as handle:
        agents = tomllib.load(handle)["agents"]
    # The safety terminology table sits next to this config file, whatever the working
    # directory of the sandbox is.
    reconciler = dict(agents.get("sae_reconciler", {}))
    if "terms" not in reconciler:
        terms = load_safety_terms(path.parent / SAFETY_TERMS_FILE)
        agents = {**agents, "sae_reconciler": {**reconciler, "terms": terms.model_dump()}}
    return AgentsSettings.model_validate(agents)


def _config_path(given: Path | None) -> Path:
    if given is not None:
        return given
    env = os.environ.get(CONFIG_FILE_ENV)
    return Path(env) if env else IMAGE_CONFIG


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log = _logger()
    try:
        registry = build_registry(load_agents_settings(_config_path(args.config)))
        registered = registry.get(args.agent)
    except KeyError:
        log.error("worker.unknown_agent", agent=args.agent)
        return EXIT_BAD_ARGS
    except (OSError, tomllib.TOMLDecodeError, ValidationError, RegistrationError) as exc:
        log.error("worker.bad_config", error=f"{type(exc).__name__}: {exc}")
        return EXIT_BAD_ARGS
    try:
        # Before any data is read: the staged files must be exactly the host's manifest,
        # which sits in meta/ (the sandbox cannot list the stage root itself).
        verify_manifest(args.cut_dir, at=args.cut_dir / META / MANIFEST_FILE)
    except InputsManifestError as exc:
        log.error("worker.inputs_rejected", error=str(exc))
        return EXIT_DATA
    try:
        trigger = Event.model_validate_json(args.trigger.read_bytes())
        if not isinstance(trigger.payload, DataCut):
            raise ValueError(f"trigger is a {trigger.payload.type}, not a data_cut")
        study = ParquetStudyReader.at_cut_dir(
            args.cut_dir, cut_id=trigger.payload.cut_id, study_id=trigger.payload.study_id
        )
    except (OSError, ValidationError, ValueError, StudyAccessError) as exc:
        log.error("worker.bad_input", error=f"{type(exc).__name__}: {exc}")
        return EXIT_DATA
    bound = log.bind(agent_id=args.agent, trigger_seq=trigger.seq, cut_id=study.cut_id)
    try:
        drafts = run_agent(registered, trigger, study, bound)
    except DATA_ERRORS as exc:
        bound.error("worker.data_error", error=f"{type(exc).__name__}: {exc}")
        return EXIT_DATA
    except Exception as exc:  # any other failure is the agent's
        bound.error("worker.agent_error", error=f"{type(exc).__name__}: {exc}")
        return EXIT_AGENT
    sys.stdout.write(drafts_document(registered, drafts))
    sys.stdout.flush()
    bound.info("worker.done", drafts=len(drafts))
    return EXIT_OK


def entry() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entry()
