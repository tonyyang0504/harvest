"""A tiny forward (absolute-URI) and CONNECT proxy for tests, with Basic proxy auth.

It records the local port of every upstream connection it opens (`out_ports`), so a local target server can
tell a proxied connection from a direct one by the peer port, the way a real site tells a residential exit
from a datacenter IP.
"""

from __future__ import annotations

import base64
import http.client
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class FakeProxy:
    def __init__(self, user: str = "puser1", password: str = "s3cret-Pw9"):
        self.user, self.password = user, password
        self.hits: list[tuple[str, str]] = []
        self.users: list[str] = []
        self.out_ports: set[int] = set()
        self.refuse: int | None = None  # answer every request with this status (402 / 407) as a dead exit would
        self.server: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    @property
    def line(self) -> str:
        return f"127.0.0.1:{self.port}:{self.user}:{self.password}"

    def is_proxied(self, peer: tuple) -> bool:
        return bool(peer) and peer[1] in self.out_ports

    def start(self) -> FakeProxy:
        fp = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _auth(self) -> bool:
                got = self.headers.get("Proxy-Authorization") or ""
                ok = False
                if got.lower().startswith("basic "):
                    user, _, pw = base64.b64decode(got[6:]).decode().partition(":")
                    base = user if user == fp.user else user.rsplit("-", 1)[0]  # accept a -CC country suffix
                    ok = base == fp.user and pw == fp.password
                    if ok:
                        fp.users.append(user)
                if fp.refuse:
                    self.send_response(fp.refuse)
                    self.send_header("x-webshare-reason", "bandwidthlimit")
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return False
                if not ok:
                    self.send_response(407)
                    self.send_header("Proxy-Authenticate", 'Basic realm="fake"')
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return False
                return True

            def do_CONNECT(self):
                fp.hits.append(("CONNECT", self.path))
                if not self._auth():
                    return
                host, _, port = self.path.rpartition(":")
                up = socket.create_connection((host, int(port)), timeout=10)
                fp.out_ports.add(up.getsockname()[1])
                self.send_response(200, "Connection established")
                self.end_headers()
                a, b = self.connection, up
                try:
                    while True:
                        r, _, _ = select.select([a, b], [], [], 10)
                        if not r:
                            break
                        done = False
                        for s in r:
                            data = s.recv(65536)
                            if not data:
                                done = True
                                break
                            (b if s is a else a).sendall(data)
                        if done:
                            break
                finally:
                    up.close()
                self.close_connection = True

            def _forward(self, method: str):
                fp.hits.append((method, self.path))
                if not self._auth():
                    return
                u = urlsplit(self.path)
                conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10)
                conn.connect()
                fp.out_ports.add(conn.sock.getsockname()[1])
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else None
                hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("proxy-authorization", "proxy-connection", "connection", "host")}
                try:
                    conn.request(method, (u.path or "/") + ("?" + u.query if u.query else ""), body, hdrs)
                    r = conn.getresponse()
                    data = r.read()
                    status, headers = r.status, r.getheaders()
                except (OSError, http.client.HTTPException):
                    status, headers, data = 502, [], b"bad gateway"
                finally:
                    conn.close()
                self.send_response(status)
                for k, v in headers:
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._forward("GET")

            def do_POST(self):
                self._forward("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
