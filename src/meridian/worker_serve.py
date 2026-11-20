"""meridian-worker serve: exactly one agent run over HTTP, inside a Substrate actor.

A Substrate actor is an HTTP server (docs/substrate-design.md). The actor's template runs

    meridian-worker serve --areas meta,blinded,ops

which, before it listens, builds the agent registry from the packaged config and warms up
(imports every agent and reads a tiny constant parquet file it writes itself). The
template's wakeup probe (GET /readyz) answers 200 only after that, so the golden snapshot
Substrate takes holds a warm process and no study data.

POST /run carries one run: the body is an uncompressed tar of the staged folders (the
same meta/ and zone areas OpenShell receives), with the headers

    X-Meridian-Agent            the agent id
    X-Meridian-Archive-Sha256   sha256 of the body

The server checks the body against its sha256, unpacks it into a fresh folder under /tmp
(only regular files named <area>/<file>, and only the areas given by --areas), runs the
same code as `meridian-worker run` and answers one JSON object:

    {"exit_code", "document", "archive_sha256", "inputs_manifest_sha256", "log_tail"}

document is the drafts document (null unless exit_code is 0) and inputs_manifest_sha256
the sha256 of the meta/inputs_manifest.json the worker verified. Then it deletes the
folder and refuses every later run (409): one actor, one run, nothing kept. The run's
log lines go only into log_tail, never to stdout or stderr (which Substrate keeps in the
worker's pod log); the server prints only that a run finished and its exit code.
"""

import hashlib
import http.server
import io
import json
import shutil
import sys
import tarfile
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path

import polars as pl
import structlog

from meridian.agents.catalog import build_registry
from meridian.data.stage_layout import META, STAGE_AREAS

AGENT_HEADER = "X-Meridian-Agent"
SHA_HEADER = "X-Meridian-Archive-Sha256"
LOG_TAIL = 4000
IDLE, BUSY, DONE = "idle", "busy", "done"


class ArchiveError(Exception):
    pass


def unpack(body: bytes, dest: Path, areas: Sequence[str]) -> None:
    """Write each <area>/<file> member of the tar in body under dest. Anything else (a
    link, a device, a folder entry, a deeper path, an area not in areas) is refused."""
    allowed = set(areas)
    seen: set[str] = set()
    try:
        tar = tarfile.open(fileobj=io.BytesIO(body), mode="r:")  # noqa: SIM115
    except tarfile.TarError as exc:
        raise ArchiveError(f"not a tar archive: {exc}") from exc
    with tar:
        for member in tar:
            parts = member.name.split("/")
            if not member.isreg() or len(parts) != 2 or member.name in seen:
                raise ArchiveError(f"refused archive member {member.name!r}")
            area, name = parts
            if area not in allowed or not name or name.startswith("."):
                raise ArchiveError(f"refused archive member {member.name!r}")
            seen.add(member.name)
            source = tar.extractfile(member)
            if source is None:
                raise ArchiveError(f"refused archive member {member.name!r}")
            (dest / area).mkdir(exist_ok=True)
            with (dest / area / name).open("xb") as out:
                shutil.copyfileobj(source, out)


def _buffer_logger(buffer: io.StringIO) -> structlog.typing.FilteringBoundLogger:
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger()
    return logger


def warm_up(config: Path) -> None:
    """Import every agent and exercise the parquet reader, on constants only."""
    from meridian.worker import load_agents_settings

    build_registry(load_agents_settings(config))
    with tempfile.TemporaryDirectory(prefix="meridian-warm-") as tmp:
        path = Path(tmp) / "warm.parquet"
        pl.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}).write_parquet(path)
        frame = pl.read_parquet(path).filter(pl.col("a") > 1)
        frame = frame.group_by("b", maintain_order=True).len()
        assert frame.height == 2


class _State:
    def __init__(self, areas: Sequence[str], config: Path, max_bytes: int) -> None:
        self.areas = tuple(areas)
        self.config = config
        self.max_bytes = max_bytes
        self.state = IDLE
        self.lock = threading.Lock()

    def claim(self) -> bool:
        with self.lock:
            if self.state != IDLE:
                return False
            self.state = BUSY
            return True

    def finish(self) -> None:
        with self.lock:
            self.state = DONE


def run_once(state: _State, agent_id: str, body: bytes, claimed_sha: str) -> dict[str, object]:
    """Check, unpack, run and clean up. The caller has claimed the one run."""
    from meridian.worker import execute

    actual = hashlib.sha256(body).hexdigest()
    if actual != claimed_sha:
        raise ArchiveError(f"archive sha256 is {actual}, the request says {claimed_sha}")
    buffer = io.StringIO()
    log = _buffer_logger(buffer)
    work = Path(tempfile.mkdtemp(prefix="meridian-run-"))
    try:
        unpack(body, work, state.areas)
        if not (work / META).is_dir():
            raise ArchiveError("the archive has no meta/ folder")
        code, document = execute(agent_id, work / META / "trigger.json", work, state.config, log)
        manifest_sha = (
            hashlib.sha256((work / META / "inputs_manifest.json").read_bytes()).hexdigest()
            if code == 0
            else None
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {
        "exit_code": code,
        "document": document,
        "archive_sha256": actual,
        "inputs_manifest_sha256": manifest_sha,
        "log_tail": buffer.getvalue()[-LOG_TAIL:],
    }


def _handler(state: _State) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "meridian-worker"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            """No access log: the pod log must not record requests."""

        def _reply(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/readyz":
                self._reply(200, {"state": state.state})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/run":
                self._reply(404, {"error": "not found"})
                return
            if not state.claim():
                self._reply(409, {"error": "this actor has already taken its one run"})
                return
            try:
                agent_id = self.headers.get(AGENT_HEADER, "")
                claimed = self.headers.get(SHA_HEADER, "")
                length = int(self.headers.get("Content-Length", "-1"))
                if not agent_id or not claimed or not 0 < length <= state.max_bytes:
                    self._reply(400, {"error": "agent, sha256 and a bounded body are required"})
                    return
                body = self.rfile.read(length)
                try:
                    result = run_once(state, agent_id, body, claimed)
                except ArchiveError as exc:
                    self._reply(400, {"error": str(exc)})
                    return
                del body
                self._reply(200, result)
                print(f"meridian-worker: run finished, exit {result['exit_code']}", flush=True)
            finally:
                state.finish()

    return Handler


def serve(*, port: int, areas: Sequence[str], config: Path, max_bytes: int) -> int:
    unknown = set(areas) - set(STAGE_AREAS)
    if META not in areas or unknown:
        print(f"meridian-worker: bad --areas {','.join(areas)}", file=sys.stderr)
        return 2
    warm_up(config)
    state = _State(areas, config, max_bytes)
    server = http.server.HTTPServer(("0.0.0.0", port), _handler(state))  # noqa: S104
    print(f"meridian-worker: serving one run on port {port}", flush=True)
    server.serve_forever()
    return 0
