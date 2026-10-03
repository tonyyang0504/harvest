"""Run a scraper module in a child process: the review smoke fetch and every production walk.

Parent side (`run`): reads the module's bytes and checks them against the reviewed sha256, builds the walk's
`Http` (robots, politeness, the URL gate, the residential-proxy route and its credentials), spawns the child, and
serves the child's http calls over the pipe (`_serve_rpc`). It streams the child's JSON lines (one per page, then
an `end` line) and kills the child at the deadline, or when `stop()` says so (a lost source lock).

Child side (`main`): gets the module's bytes and context on stdin, never a route, a credential or a path into
harvest's state. Its `http` is `RpcHttp`, which forwards every call to the parent; the MCP lane does the same.
Scrubbed environment, a throwaway working directory, CPU and file-size rlimits, and an audit hook that refuses
process spawning, any network use, native loading, writes outside the working directory, and reads of harvest's
state, the operator's home and other processes' /proc entries.

Isolation (`isolation()`, HARVEST_SANDBOX=auto|bwrap|policy): under bubblewrap (Linux) the child also gets a
read-only filesystem except its scratch directory, harvest's state and the operator's home masked, its own network
namespace (nothing to talk to) and pid namespace. Where bubblewrap is unavailable the policy layer runs alone,
with a loud warning unless HARVEST_SANDBOX=policy accepts it.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

_BLOCKED_EVENTS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty", "pty.spawn",
                   "ctypes.dlopen", "ctypes.cdata", "os.kill", "os.killpg", "os.remove", "os.rmdir", "os.rename", "shutil.rmtree", "os.chmod",
                   "os.symlink", "os.link", "os.truncate", "socket.bind", "winreg")


# the child has no network of its own: every request goes through the parent's Http (RpcHttp)
_NETWORK_EVENTS = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg")


def _redact(text: str) -> str:
    from .proxy import redact
    return redact(text)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


# ------------------------------------------------------------------------------ parent
def child_env(extra: dict | None = None) -> dict:
    keep = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "HARVEST_ALLOW_PRIVATE", "HARVEST_USER_AGENT", "HARVEST_HOME", "HARVEST_MCP_CONFIG", "HARVEST_PSL_FILE")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    paths = [p for p in sys.path if p and os.path.isdir(p)]
    env["PYTHONPATH"] = os.pathsep.join(paths)
    # the child's HOME is a throwaway dir, so point Playwright at the parent's browsers explicitly
    # (Playwright's default cache: ~/.cache on Linux, ~/Library/Caches on macOS, %LOCALAPPDATA% on Windows)
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or next(
        (d for d in (os.path.expanduser("~/.cache/ms-playwright"), os.path.expanduser("~/Library/Caches/ms-playwright"),
                     os.path.join(os.environ.get("LOCALAPPDATA") or "/nonexistent", "ms-playwright")) if os.path.isdir(d)), "")
    if browsers and os.path.isdir(browsers):
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra or {})
    return env


def secret_paths() -> dict:
    """Paths a scraper module must never read, resolved in the parent and handed to the child in the job.
    hard: harvest's state (admin token, every project's store and modules, the MCP-lane config with its API keys,
    proxy health) and the proxy pool file; always refused. home: the operator's home directory (agent CLI
    credentials, SSH keys ...); refused except under the interpreter's own import roots, since a venv can live there."""
    from .project import home
    hard = [str(home())] + [os.environ[k] for k in ("HARVEST_PROXY_FILE", "HARVEST_MCP_CONFIG") if os.environ.get(k)]
    user_home = os.path.expanduser("~")
    return {"hard": [os.path.realpath(os.path.expanduser(x)) for x in hard],
            "home": os.path.realpath(user_home) if user_home and user_home != "/" else None}


# ------------------------------------------------------------------------------ isolation (bubblewrap)
_BWRAP_OK: dict[str, bool] = {}
_WARNED: list[bool] = []
POLICY_WARNING = ("harvest: WARNING: bubblewrap is not available, so scraper modules run under the policy sandbox only (AST lint, "
                  "audit hook, rlimits; no OS isolation). Install bubblewrap (Linux), or run workers in a container as described in "
                  "docs/harvest.md, 'Running workers in production'. Set HARVEST_SANDBOX=policy to accept this explicitly.")


def bwrap_usable() -> bool:
    """bubblewrap is installed and can create its namespaces here (unprivileged user namespaces may be disabled)."""
    exe = shutil.which("bwrap")
    if not exe:
        return False
    if exe not in _BWRAP_OK:
        try:
            r = subprocess.run([exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-all", "--die-with-parent", "true"],
                               capture_output=True, timeout=20)
            _BWRAP_OK[exe] = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _BWRAP_OK[exe] = False
    return _BWRAP_OK[exe]


def isolation() -> str:
    """`bwrap` or `policy`. HARVEST_SANDBOX: auto (default: bwrap when usable, else policy with a loud warning),
    bwrap (required: refuse to run without it), policy (accepted explicitly, no warning)."""
    want = (os.environ.get("HARVEST_SANDBOX") or "auto").lower()
    if want not in ("auto", "bwrap", "policy"):
        raise RuntimeError("HARVEST_SANDBOX: auto | bwrap | policy")
    if want == "policy":
        return "policy"
    if bwrap_usable():
        return "bwrap"
    if want == "bwrap":
        raise RuntimeError("HARVEST_SANDBOX=bwrap but bubblewrap is not installed or cannot create namespaces on this host")
    if not _WARNED:
        _WARNED.append(True)
        sys.stderr.write(POLICY_WARNING + "\n")
        sys.stderr.flush()
    return "policy"


def bwrap_argv(work: str, deny: dict) -> list[str]:
    """The child's view of the machine: everything read-only, a private /tmp and /dev, a fresh /proc (own pid
    namespace), its own network namespace (loopback only, nothing listening), the operator's home replaced by an
    empty tmpfs, the interpreter's own roots (venv, site-packages, harvest's source) bound back read-only wherever a
    mask hid them, then harvest's state and the secret files masked (last, so they win over any root that contains
    them), and one writable scratch directory."""
    a = [shutil.which("bwrap") or "bwrap", "--die-with-parent", "--new-session", "--unshare-all",
         "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    masked = ["/tmp"]
    h = deny.get("home")
    if h and os.path.isdir(h) and h != "/tmp":
        a += ["--tmpfs", h]
        masked.append(h)
    for root in _import_roots():
        if any(_under(root, m) and root != m for m in masked):
            a += ["--ro-bind", root, root]
    for d in deny.get("hard") or []:
        if os.path.isdir(d):
            a += ["--tmpfs", d]
        elif os.path.exists(d):
            a += ["--ro-bind", "/dev/null", d]
    a += ["--bind", work, work, "--chdir", work]
    return a


def _import_roots() -> list[str]:
    """Directories the child's interpreter needs: its prefixes (a venv holds bin/python and site-packages) and the
    parent's sys.path (the child's PYTHONPATH), outermost first so nested ones are not bound twice."""
    roots = sorted({os.path.realpath(x) for x in (sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(sys.executable),
                                                  *[q for q in sys.path if q]) if x and os.path.isdir(x)}, key=len)
    out: list[str] = []
    for r in roots:
        if not any(_under(r, o) for o in out):
            out.append(r)
    return out


# ------------------------------------------------------------------------------ the parent's side of the http RPC
RPC_METHODS = ("request", "get", "get_text", "get_json", "post_json", "render", "allowed")


def _rpc_value(v):
    from .http import Response
    if isinstance(v, Response):
        return {"__response__": {"url": v.url, "status": v.status, "headers": v.headers, "text": v.text, "elapsed": v.elapsed,
                                 "via_proxy": v.via_proxy}}
    return v


def _serve_rpc(http, msg: dict) -> dict:
    """One call from the module, executed by the parent's Http (robots, politeness, the URL gate, the proxy route and
    its credentials all stay here). MCP-lane calls go to harvest's own client. Errors come back as errors."""
    out: dict = {"type": "rpc_result", "id": msg.get("id")}
    try:
        method, args, kw = msg.get("method"), list(msg.get("args") or []), dict(msg.get("kwargs") or {})
        if method in RPC_METHODS:
            out["value"] = _rpc_value(getattr(http, method)(*args, **kw))
        elif method in ("mcp_call_tool", "mcp_list_tools"):
            from . import mcp_client
            out["value"] = (mcp_client.call_tool if method == "mcp_call_tool" else mcp_client.list_tools)(*args, **kw)
        else:
            out["error"] = f"PermissionError: {method!r} is not part of the module contract"
    except Exception as exc:
        out["error"] = _redact(f"{exc.__class__.__name__}: {str(exc)[:300]}")
    out["stats"] = dict(http.stats)
    return out


def child_job(job: dict, work: str, data: bytes) -> dict:
    """What the child receives: the module's bytes and its context. Never the proxy route (exits, credentials), never
    a path into harvest's state."""
    import base64
    keep = {k: v for k, v in job.items() if k not in ("proxy", "module_path")}
    return {**keep, "module_b64": base64.b64encode(data).decode("ascii"), "module_name": os.path.basename(job.get("module_path") or "module.py"),
            "workdir": work, "deny_read": secret_paths()}


# ------------------------------------------------------------------ scratch directories of killed parents
OWNER_SUFFIX = ".owner"


def _mark_owner(work: str) -> None:
    """A sibling file (outside the child's view) names the parent process that owns the scratch directory."""
    try:
        with open(work + OWNER_SUFFIX, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {_hostname()}\n")
    except OSError:
        pass


def _unmark_owner(work: str) -> None:
    try:
        os.remove(work + OWNER_SUFFIX)
    except OSError:
        pass


def _hostname() -> str:
    import socket
    return socket.gethostname()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_stale(tmp: str | None = None, unmarked_age_s: float = 86400) -> list[str]:
    """Remove scratch directories whose parent died without cleaning up (kill -9, OOM, a unit stopped mid-walk: the
    2026-10 soak left one per kill). A directory is removed when its owner file names a process on this host that no
    longer exists, or when it has no owner file (an older harvest) and is older than a day. Only this user's."""
    import glob
    base = tmp or tempfile.gettempdir()
    host, uid, removed = _hostname(), os.getuid() if hasattr(os, "getuid") else None, []
    for d in glob.glob(os.path.join(base, "harvest-sbx-*")):
        if d.endswith(OWNER_SUFFIX) or os.path.islink(d) or not os.path.isdir(d):
            continue  # never follow a symlink someone planted in the shared temp dir
        try:
            st = os.lstat(d)
            if uid is not None and st.st_uid != uid:
                continue
            try:
                pid_s, _, h = open(d + OWNER_SUFFIX, encoding="utf-8").read().strip().partition(" ")
                stale = h == host and not _pid_alive(int(pid_s))
            except (OSError, ValueError):
                stale = time.time() - st.st_mtime > unmarked_age_s
        except OSError:
            continue
        if stale:
            shutil.rmtree(d, ignore_errors=True)
            _unmark_owner(d)
            removed.append(d)
    return removed


def run(job: dict, timeout_s: float, on_event: Callable[[dict], None] | None = None, stop: Callable[[], bool] | None = None) -> dict:
    """-> {"pages": [...page events...], "end": {...} | None, "timed_out": bool, "returncode", "stderr", "isolation"}"""
    from .http import Http
    from .proxy import Route
    mode = isolation()
    data = Path(job["module_path"]).read_bytes()
    if job.get("expected_sha") and sha256_bytes(data) != job["expected_sha"]:
        return {"pages": [], "end": {"type": "end", "stopped": "sha_mismatch", "complete": False, "pages": 0, "rows": 0,
                                     "error": f"module sha {sha256_bytes(data)[:12]} differs from the reviewed {job['expected_sha'][:12]}"},
                "timed_out": False, "stopped": False, "returncode": None, "stderr": "", "isolation": mode}
    hj = job.get("http") or {}
    http = Http(rate_s=float(hj.get("rate_s", 2.0)), timeout=float(hj.get("timeout", 20)), retries=int(hj.get("retries", 3)),
                url_budget_s=float(hj.get("url_budget_s", 90)), backoff_s=float(hj.get("backoff_s", 1.0)),
                route=Route.from_job(job.get("proxy")), ua_mode=hj.get("ua_mode", "own"),
                allow_session_headers=bool(hj.get("allow_session_headers")))
    work = tempfile.mkdtemp(prefix="harvest-sbx-")
    _mark_owner(work)
    cj = child_job(job, work, data)
    argv = [sys.executable, "-m", "harvest_ai.sandbox"]
    if mode == "bwrap":
        argv = bwrap_argv(work, cj["deny_read"]) + ["--", *argv]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=work, env=child_env({"HOME": work}), text=True, encoding="utf-8")
    q: queue.Queue = queue.Queue()
    rpcq: queue.Queue = queue.Queue()
    wlock = threading.Lock()

    def send(obj: dict) -> None:
        with wlock:
            try:
                proc.stdin.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def reader():
        for line in proc.stdout:
            if line.startswith('{"type": "rpc"'):
                rpcq.put(line)
            else:
                q.put(line)
        q.put(None)
        rpcq.put(None)

    def rpc_worker():
        while True:
            line = rpcq.get()
            if line is None:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            send(_serve_rpc(http, msg))
    err_chunks: list[str] = []

    def err_reader():
        for line in proc.stderr:
            err_chunks.append(line)
            del err_chunks[:-60]
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=err_reader, daemon=True).start()
    rpc_t = threading.Thread(target=rpc_worker, daemon=True)
    rpc_t.start()
    send(cj)
    deadline = time.monotonic() + timeout_s
    pages, end, timed_out, stopped = [], None, False, False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        if stop is not None and stop():  # e.g. the walk lost its source lock: kill the child, record nothing more
            stopped = True
            break
        try:
            line = q.get(timeout=min(left, 1.0))
        except queue.Empty:
            if on_event:
                on_event({"type": "tick"})
            continue
        if line is None:
            break
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        rep = http.proxy_report()
        if ev.get("type") == "page":
            if rep is not None:
                ev["proxy"] = {k: rep[k] for k in ("requests", "bytes", "budget_stop")}
            pages.append(ev)
        elif ev.get("type") == "end":
            # the parent's client is the one that did the requests: its counters, events and proxy usage are the record
            ev["http"] = {**http.stats, "ua_mode": http.ua_mode}
            ev["events"] = http.events[-25:]
            ev["proxy"] = rep
            end = ev
        if on_event:
            on_event(ev)
    if timed_out or stopped:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    rpcq.put(None)
    rpc_t.join(timeout=5)
    http.close()
    shutil.rmtree(work, ignore_errors=True)
    _unmark_owner(work)
    return {"pages": pages, "end": end, "timed_out": timed_out, "stopped": stopped, "returncode": proc.returncode,
            "stderr": _redact("".join(err_chunks)[-3000:]), "isolation": mode, "proxy_report": http.proxy_report()}


# ------------------------------------------------------------------------------ child
def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _under(p: str, root: str) -> bool:
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def read_denied(p: str, work: str, deny: dict, allow: list[str]) -> bool:
    """Is a read of `p` (a realpath) refused? Under the working directory: no. Another process's /proc entry (its
    environment can hold the admin token and the proxy URL): yes. Under a hard secret root: yes. Under the
    operator's home: yes, unless under one of the interpreter's import roots."""
    if _under(p, work):
        return False
    if p.startswith("/proc/"):
        first = p.split("/")[2]
        return first.isdigit() and first != str(os.getpid())
    if any(_under(p, d) for d in deny.get("hard") or []):
        return True
    h = deny.get("home")
    return bool(h) and _under(p, h) and not any(_under(p, a) for a in allow)


def _install_guard(workdir: str, deny: dict | None = None) -> None:
    work = os.path.realpath(workdir)
    deny = deny or {}
    allow = sorted({os.path.realpath(x) for x in (sys.prefix, sys.base_prefix, sys.exec_prefix, *[q for q in sys.path if q])
                    if x and os.path.isdir(x)})
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers:
        allow.append(os.path.realpath(browsers))
    wflags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def hook(event: str, args: tuple) -> None:
        # the child never needs a trusted section: rendering and the MCP lane run in the parent (RpcHttp)
        if event.startswith(_BLOCKED_EVENTS) or event.startswith(_NETWORK_EVENTS):
            raise PermissionError(f"harvest sandbox: {event} is not allowed in scraper modules")
        if event in ("open", "os.listdir", "os.scandir") and args:
            path = args[0]
            if not isinstance(path, (str, bytes, os.PathLike)):
                return
            p = os.path.realpath(os.fsdecode(path))
            if event == "open":
                mode, flags = (list(args[1:]) + [None, None])[:2]
                writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & wflags)
                if writing and not (_under(p, work) or p == os.devnull):
                    raise PermissionError(f"harvest sandbox: writing {p} is not allowed")
            if read_denied(p, work, deny, allow):
                raise PermissionError(f"harvest sandbox: reading {p} is not allowed")
    sys.addaudithook(hook)


def _limits(cpu_s: int | None) -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - not on Windows
        return
    for lim, val in ((getattr(resource, "RLIMIT_CPU", None), cpu_s), (getattr(resource, "RLIMIT_FSIZE", None), 64 * 1024 * 1024)):
        if lim is None or not val:
            continue
        try:
            soft, hard = resource.getrlimit(lim)
            v = int(val) if hard == resource.RLIM_INFINITY else min(int(val), hard)
            resource.setrlimit(lim, (v, hard))
        except (ValueError, OSError):
            pass


def _key(row: dict) -> str:
    k = row.get("source_id") or row.get("url")
    return str(k) if k else json.dumps(row, sort_keys=True, default=str)


def walk(fetch, *, http, ctx: dict, max_pages: int, max_rows: int, emit: Callable[[dict], None]) -> dict:
    """Paginate a module. Complete means the walk reached the natural end (explicit done, an empty
    page, or two pages with nothing new) with no page errors and no failed/blocked HTTP requests."""
    seen: set[str] = set()
    dry, page_errors, total, pages = 0, 0, 0, 0
    stopped = "max_pages"
    bad_http = ("errors", "blocked", "gated", "robots_denied")
    start_stats = dict(http.stats)
    for page in range(1, max_pages + 1):
        before = {k: http.stats.get(k, 0) for k in bad_http}
        pages = page
        err, done = None, False
        try:
            res = fetch(page, http=http, ctx=ctx)
        except Exception as exc:
            from .proxy import redact
            res, err = [], redact(f"{exc.__class__.__name__}: {str(exc)[:300]}")
        if isinstance(res, dict):
            done = bool(res.get("done"))
            res = res.get("rows") or []
        if not isinstance(res, list):
            err = err or f"fetch returned {type(res).__name__}, expected list"
            res = []
        rows = [r for r in res if isinstance(r, dict)]
        fresh = []
        for r in rows:
            k = _key(r)
            if k in seen:
                continue
            seen.add(k)
            fresh.append(r)
        room = max_rows - total
        fresh = fresh[:room]
        total += len(fresh)
        http_failed = any(http.stats.get(k, 0) > before[k] for k in bad_http)
        ev = {"type": "page", "page": page, "rows": fresh, "raw_count": len(rows), "error": err, "http_failed": http_failed}
        rep = http.proxy_report() if hasattr(http, "proxy_report") else None
        if rep is not None:
            ev["proxy"] = {k: rep[k] for k in ("requests", "bytes", "budget_stop")}
        emit(ev)
        if err:
            page_errors += 1
            if page_errors >= 2:
                stopped = "errors"
                break
            continue
        if done:
            stopped = "done"
            break
        if not rows:
            stopped = "fetch_failed" if http_failed else "empty_page"
            break
        if not fresh:
            dry += 1
            if dry >= 2:
                stopped = "repeating"
                break
        else:
            dry = 0
        if total >= max_rows:
            stopped = "max_rows"
            break
    http_bad = sum(http.stats.get(k, 0) - start_stats.get(k, 0) for k in bad_http)
    complete = stopped in ("done", "empty_page", "repeating") and page_errors == 0 and http_bad == 0
    return {"stopped": stopped, "complete": complete, "pages": pages, "rows": total, "page_errors": page_errors, "http_failures": http_bad}


class RpcHttp:
    """The `http` a module gets in the child: the same contract (get_text, get_json, post_json, render, get, request,
    allowed), executed by the parent's Http over the stdin/stdout pipe. The child holds no client, no route and no
    credentials, and needs no network of its own."""

    def __init__(self, ua_mode: str = "own"):
        self.stats: dict = {}
        self.events: list = []
        self.ua_mode = ua_mode
        self._n = 0
        self._lock = threading.Lock()

    def _call(self, method: str, *args, **kwargs):
        with self._lock:
            self._n += 1
            _emit({"type": "rpc", "id": self._n, "method": method, "args": list(args), "kwargs": kwargs})
            line = sys.stdin.readline()
        if not line:
            raise RuntimeError("harvest sandbox: the parent closed the http channel")
        msg = json.loads(line)
        self.stats = msg.get("stats") or self.stats
        if msg.get("error"):
            raise RuntimeError(msg["error"])
        v = msg.get("value")
        if isinstance(v, dict) and "__response__" in v:
            from .http import Response
            return Response(**v["__response__"])
        return v

    def request(self, method: str, url: str, **kw):
        return self._call("request", method, url, **kw)

    def get(self, url: str, **kw):
        return self._call("get", url, **kw)

    def get_text(self, url: str, **kw):
        return self._call("get_text", url, **kw)

    def get_json(self, url: str, **kw):
        return self._call("get_json", url, **kw)

    def post_json(self, url: str, body, **kw):
        return self._call("post_json", url, body, **kw)

    def render(self, url: str, wait_ms: int = 1500):
        return self._call("render", url, wait_ms)

    def allowed(self, url: str) -> bool:
        return bool(self._call("allowed", url))

    def proxy_report(self):
        return None  # the parent attaches the route's usage to every event

    def close(self) -> None:
        pass


def main() -> None:
    sys.dont_write_bytecode = True
    job = json.loads(sys.stdin.readline())
    import base64
    import encodings.idna  # noqa: F401
    import types

    from . import extract, mcp_client  # noqa: F401  (import everything the module may need before the guard goes up)
    from .http import Response  # noqa: F401
    from .proxy import redact

    data = base64.b64decode(job["module_b64"])
    sha = hashlib.sha256(data).hexdigest()
    if job.get("expected_sha") and sha != job["expected_sha"]:
        _emit({"type": "end", "stopped": "sha_mismatch", "complete": False, "pages": 0, "rows": 0,
               "error": f"module sha {sha[:12]} differs from the reviewed {job['expected_sha'][:12]}"})
        return
    _limits(job.get("cpu_limit_s"))
    http = RpcHttp(ua_mode=(job.get("http") or {}).get("ua_mode", "own"))
    # the MCP lane goes through the parent too: the child never spawns a server or opens a connection
    mcp_client.call_tool = lambda server, tool, args=None, timeout=120.0: http._call("mcp_call_tool", server, tool, args, timeout=timeout)
    mcp_client.list_tools = lambda server, timeout=60.0: http._call("mcp_list_tools", server, timeout=timeout)
    name = "harvest_source_" + str(job.get("source", {}).get("id", "x"))
    code = compile(data, job.get("module_name") or "module.py", "exec")
    _install_guard(job["workdir"], job.get("deny_read"))
    mod = types.ModuleType(name)
    mod.__file__ = job.get("module_name") or "module.py"
    try:
        exec(code, mod.__dict__)  # the bytes the parent verified against the reviewed sha
        fetch = mod.fetch
    except Exception as exc:
        _emit({"type": "end", "stopped": "import_error", "complete": False, "pages": 0, "rows": 0, "error": redact(f"{exc.__class__.__name__}: {str(exc)[:400]}")})
        return
    ctx = {"source": job.get("source") or {}, "project": job.get("project") or {}, "fields": job.get("fields") or {},
           "region": job.get("region"), "currency": job.get("currency"), "state": {}}
    res = walk(fetch, http=http, ctx=ctx, max_pages=int(job.get("max_pages", 1)), max_rows=int(job.get("max_rows", 100000)), emit=_emit)
    if res.get("error"):
        res["error"] = redact(res["error"])
    _emit({"type": "end", **res})


if __name__ == "__main__":
    main()
