"""meridian-worker probe: fixed containment checks inside a Substrate actor.

Substrate has no exec into an actor, so `meridian substrate conformance` creates actors
from the conformance template (deploy/substrate/templates/conformance.yaml: the zone
templates' image, pool, sandbox class and size, with this command) and asks them, over
HTTP, to try what a zone actor must not be able to do. The checks are a fixed list; a
request names one and gives at most a host and port or a path. It never runs agent code
and never reads staged data (none is sent to it).

    GET  /readyz               200 once listening
    GET  /whoami               the actor's own addresses, as it sees them
    POST /check {"check": ...} one of:
        tcp_connect {"host", "port"}   connect, send one HTTP HEAD request, read the
                                       first 1 KiB of the reply (5 s timeouts)
        write {"path"}                 create the file and write one line
        read {"path"}                  open and read up to 64 bytes
        resolve {"host"}               getaddrinfo

Every answer is {"check", "outcome", "errno", "message"}, outcome one of ok, refused,
timeout, unreachable, error. tcp_connect adds "connected" (connect() returned), "reply"
(the first line received, if any), "server" (its Server header) and "closed" (the peer
closed the connection). In an actor, connect() to any address succeeds locally: atunnel
accepts every TCP connection and tunnels it to atenet-egress, which applies the policy
(Substrate docs/guides/egress-traffic.md). So a refusal shows up as the gateway's reply
or a close, not as a failed connect; the host classifies it (meridian.sandbox.
substrate_conformance), and this probe only reports what it saw.
"""

import errno
import http.server
import json
import socket
from pathlib import Path

CONNECT_TIMEOUT = 5.0


def _errno_outcome(code: int | None) -> str:
    if code in (errno.ECONNREFUSED, errno.EACCES, errno.EPERM, errno.ECONNRESET):
        return "refused"
    if code in (errno.ETIMEDOUT,):
        return "timeout"
    if code in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return "unreachable"
    return "error"


def _exchange(host: str, port: int) -> dict[str, object]:
    out: dict[str, object] = {"connected": False, "reply": None, "server": None, "closed": False}
    with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as conn:
        out["connected"] = True
        conn.settimeout(CONNECT_TIMEOUT)
        conn.sendall(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        data = b""
        try:
            while len(data) < 1024:
                chunk = conn.recv(1024 - len(data))
                if not chunk:
                    out["closed"] = True
                    break
                data += chunk
        except TimeoutError:
            pass
        except OSError as exc:  # reset by the tunnel: the gateway refused
            out["closed"] = True
            out["reset"] = exc.errno
    text = data.decode("latin-1")
    lines = text.split("\r\n")
    out["reply"] = lines[0] if data else None
    for line in lines[1:]:
        if line.lower().startswith("server:"):
            out["server"] = line.split(":", 1)[1].strip()
    out["outcome"] = "ok"
    after = f"reset (errno {out['reset']})" if out.get("reset") else f"reply {out['reply']!r}"
    out["message"] = f"connect() to {host}:{port} returned; then {after}"
    return out


def check(request: dict[str, object]) -> dict[str, object]:
    name = str(request.get("check", ""))
    out: dict[str, object] = {"check": name, "errno": None}
    try:
        if name == "tcp_connect":
            host, port = str(request["host"]), int(str(request["port"]))
            out.update(_exchange(host, port))
        elif name == "write":
            path = Path(str(request["path"]))
            with path.open("w", encoding="utf-8") as handle:
                handle.write("conformance\n")
            out.update(outcome="ok", message=f"wrote {path}")
        elif name == "read":
            path = Path(str(request["path"]))
            with path.open("rb") as handle:
                handle.read(64)
            out.update(outcome="ok", message=f"read {path}")
        elif name == "resolve":
            host = str(request["host"])
            addresses = sorted({str(a[4][0]) for a in socket.getaddrinfo(host, None)})
            out.update(outcome="ok", message=" ".join(addresses))
        else:
            out.update(outcome="error", message=f"unknown check {name!r}")
    except TimeoutError as exc:
        out.update(outcome="timeout", errno=errno.ETIMEDOUT, message=str(exc) or "timed out")
    except socket.gaierror as exc:
        out.update(outcome="unreachable", errno=exc.errno, message=str(exc))
    except OSError as exc:
        out.update(outcome=_errno_outcome(exc.errno), errno=exc.errno, message=str(exc))
    except (KeyError, ValueError) as exc:
        out.update(outcome="error", message=f"bad request: {exc}")
    return out


def _addresses() -> list[str]:
    found: set[str] = set()
    for host in (socket.gethostname(), "localhost"):
        try:
            found |= {str(a[4][0]) for a in socket.getaddrinfo(host, None)}
        except OSError:
            continue
    return sorted(found)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "meridian-probe"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        """No access log."""

    def _reply(self, status: int, payload: object) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/readyz":
            self._reply(200, {"state": "probe"})
        elif self.path == "/whoami":
            self._reply(200, {"hostname": socket.gethostname(), "addresses": _addresses()})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/check":
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 4096:
            self._reply(400, {"error": "a small JSON body is required"})
            return
        try:
            request = json.loads(self.rfile.read(length))
        except ValueError:
            self._reply(400, {"error": "not JSON"})
            return
        if not isinstance(request, dict):
            self._reply(400, {"error": "not an object"})
            return
        self._reply(200, check(request))


def probe(*, port: int) -> int:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)  # noqa: S104
    print(f"meridian-worker: probe listening on port {port}", flush=True)
    server.serve_forever()
    return 0
