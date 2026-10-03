"""A local filtering proxy for the headless browser: DNS pinning for the browser lane.

Chromium resolves host names itself, so the per-request URL gate in `Http.render` (which resolves in Python) and
the address Chromium finally connects to could differ (DNS rebinding). The browser is therefore pointed at this
proxy, bound to 127.0.0.1 inside harvest's own process: Chromium hands it the host name (CONNECT host:port for
https, an absolute URI for http), the proxy resolves it once, refuses any address that is not globally routable
(`http.public_ip`), and connects to the address it vetted. Loopback is not bypassed (`--proxy-bypass-list=<-loopback>`).

Not used when the walk goes through the residential-proxy route (that proxy resolves remotely) or when private
addresses are allowed (local testing).
"""

from __future__ import annotations

import ipaddress
import select
import socket
import socketserver
import threading
from typing import Callable
from urllib.parse import urlsplit


def _vetted_address(host: str, port: int, vet: Callable) -> tuple[str, int]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    bad = next((a for a in addrs if not vet(a)), None)
    if bad is not None or not addrs:
        raise PermissionError(f"non-public address {bad} for {host}")
    return str(addrs[0]), port


def _pipe(a: socket.socket, b: socket.socket, idle_s: float = 60.0) -> None:
    socks = [a, b]
    while True:
        r, _, x = select.select(socks, [], socks, idle_s)
        if x or not r:
            return
        for s in r:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            try:
                (b if s is a else a).sendall(data)
            except OSError:
                return


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        srv: FilteringProxy = self.server.owner  # type: ignore[attr-defined]
        try:
            line = self.rfile.readline(8192).decode("latin-1")
            method, target, version = line.split(" ", 2)
            headers = []
            while True:
                h = self.rfile.readline(8192)
                if h in (b"\r\n", b"\n", b""):
                    break
                headers.append(h)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                host = host.strip("[]")
                ip, prt = _vetted_address(host, int(port), srv.vet)
                up = socket.create_connection((ip, prt), timeout=srv.timeout)
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
                srv.connections.append((host, ip))
                _pipe(self.connection, up)
                up.close()
                return
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                raise PermissionError("only absolute http:// URIs and CONNECT")
            ip, prt = _vetted_address(u.hostname, u.port or 80, srv.vet)
            up = socket.create_connection((ip, prt), timeout=srv.timeout)
            srv.connections.append((u.hostname, ip))
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            kept = [h for h in headers if not h.lower().startswith((b"proxy-", b"connection:", b"keep-alive:"))]
            up.sendall(f"{method} {path} {version.strip()}\r\n".encode("latin-1") + b"".join(kept) + b"Connection: close\r\n\r\n")
            _pipe(self.connection, up)
            up.close()
        except PermissionError as exc:
            srv.refused.append(str(exc))
            self._deny(str(exc))
        except (OSError, ValueError) as exc:
            self._deny(f"proxy error: {exc.__class__.__name__}", status=b"502 Bad Gateway")

    def _deny(self, why: str, status: bytes = b"403 Forbidden") -> None:
        try:
            body = why.encode("utf-8", "replace")[:300]
            self.wfile.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
        except OSError:
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FilteringProxy:
    """`with FilteringProxy() as fp: ... fp.url` -> http://127.0.0.1:<port>. `vet(ip) -> bool` defaults to
    `http.public_ip`; `refused` and `connections` record what happened (tests, diagnostics)."""

    def __init__(self, vet: Callable | None = None, timeout: float = 20.0):
        from .http import public_ip
        self.vet = vet or public_ip
        self.timeout = timeout
        self.refused: list[str] = []
        self.connections: list[tuple[str, str]] = []
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.owner = self  # type: ignore[attr-defined]
        self._t = threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        self._t.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def __enter__(self) -> "FilteringProxy":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
