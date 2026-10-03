"""The mandatory review gate a scraper module passes before its source can be enabled.

  1. lint    compile, the module contract (`fetch(page, *, http, ctx)`), an AST scan for code a
             scraper never needs (process spawning, os.system, eval/exec, dynamic imports, raw
             sockets, direct HTTP clients that would bypass robots and politeness, private
             attributes such as the http client's proxy route), and ruff's
             syntax / undefined-name rules when ruff is installed.
  2. smoke   a page-1 fetch in the sandbox child process with a hard timeout.
  3. schema  every row normalised against the project's record template; enough rows must pass
             (default 80 %) and every required field must be covered.
The verdict is stored with the module's sha256. The runner refuses a module whose current sha
differs from its last passing review, so any later edit needs a new review.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import sandbox, sessionhdr
from .normalize import Normalizer
from .project import Project

MIN_VALID_FRACTION = 0.8
REVIEW_TIMEOUT_S = int(os.environ.get("HARVEST_REVIEW_TIMEOUT_S", "180"))

FORBIDDEN_MODULES = {"subprocess", "pty", "ctypes", "cffi", "multiprocessing", "socket", "socketserver", "ssl", "asyncio.subprocess",
                     "importlib", "runpy", "code", "codeop", "shutil", "signal", "requests", "httpx", "urllib3", "aiohttp", "http.client",
                     "urllib.request", "pycurl", "paramiko", "ftplib", "smtplib", "telnetlib", "webbrowser", "pickle", "marshal", "builtins", "sys",
                     # the OS layer under `os` (same functions, other names) and string-driven attribute access, which defeats the
                     # private-attribute rule below
                     "posix", "nt", "operator", "inspect", "gc", "ctypes.util", "io", "pathlib", "glob", "tempfile", "mmap", "fcntl",
                     "resource", "threading", "_thread", "concurrent", "asyncio", "string", "types", "functools"}
# harvest-ai's own package (harvest_ai): a module may use the extraction helpers and the MCP-lane client, nothing else (harvest's internals
# include the audit hook's trusted-section switch and the proxy route)
HARVEST_ALLOWED = {"harvest_ai.extract", "harvest_ai.mcp_client"}
FORBIDDEN_CALLS = {("os", n) for n in ("system", "popen", "execv", "execve", "execl", "execlp", "execvp", "execvpe", "spawnv", "spawnl", "spawnve",
                                        "posix_spawn", "posix_spawnp", "fork", "forkpty", "kill", "killpg", "remove", "unlink", "rmdir", "removedirs",
                                        "rename", "chmod", "chown", "putenv", "setuid")}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "breakpoint", "globals", "vars", "open", "input", "getattr", "setattr", "delattr"}
FORBIDDEN_ATTRS = {"__subclasses__", "__globals__", "__builtins__", "__code__", "__class__", "__bases__", "__mro__", "f_globals", "f_locals", "gi_frame",
                   "__closure__", "cell_contents", "__dict__", "__self__", "__func__", "f_back", "tb_frame", "__reduce__", "__reduce_ex__"}


def _format_reaches_attrs(fmt: str) -> bool:
    import string as _string
    try:
        fields = [f for _, f, _, _ in _string.Formatter().parse(fmt) if f is not None]
    except ValueError:
        return True
    return any("." in f or "[" in f for f in fields)


def static_findings(source: str, filename: str = "<module>") -> list[str]:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    errors = []

    def mod_forbidden(name: str) -> bool:
        if not name:
            return False
        if name == "harvest" or name.startswith("harvest."):
            return True  # the old import name (harvest-ai's package is harvest_ai; PyPI's "harvest" is an unrelated project)
        if name == "harvest_ai" or name.startswith("harvest_ai."):
            return name not in HARVEST_ALLOWED
        if any(part.startswith("_") for part in name.split(".")):  # private / C-level modules (_posixsubprocess, _io ...)
            return True
        return name in FORBIDDEN_MODULES or any(name.startswith(m + ".") for m in FORBIDDEN_MODULES) or name.split(".")[0] in FORBIDDEN_MODULES

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if mod_forbidden(a.name):
                    errors.append(f"line {node.lineno}: imports {a.name}")
                elif a.name == "os" and a.asname and a.asname != "os":
                    errors.append(f"line {node.lineno}: imports os under another name ({a.asname})")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                errors.append(f"line {node.lineno}: relative import")
            elif mod == "harvest_ai":
                for a in node.names:
                    if f"harvest_ai.{a.name}" not in HARVEST_ALLOWED:
                        errors.append(f"line {node.lineno}: imports harvest_ai.{a.name}")
            elif mod_forbidden(mod):
                errors.append(f"line {node.lineno}: imports from {mod}")
            if mod == "os":
                for a in node.names:
                    if ("os", a.name) in FORBIDDEN_CALLS or a.name == "*":
                        errors.append(f"line {node.lineno}: imports os.{a.name}")
            if mod == "urllib":
                for a in node.names:
                    if a.name == "request":
                        errors.append(f"line {node.lineno}: imports urllib.request")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_NAMES:
                errors.append(f"line {node.lineno}: calls {fn.id}()")
            elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and (fn.value.id, fn.attr) in FORBIDDEN_CALLS:
                errors.append(f"line {node.lineno}: calls {fn.value.id}.{fn.attr}()")
            if isinstance(fn, ast.Attribute) and fn.attr in ("format", "format_map"):
                # str.format resolves `{0.attr}` / `{0[key]}` at run time, out of the AST scan's sight
                recv = fn.value
                fmt_ok = isinstance(recv, ast.Constant) and isinstance(recv.value, str) and not _format_reaches_attrs(recv.value)
                if not fmt_ok:
                    errors.append(f"line {node.lineno}: str.{fn.attr}() only with a literal format string without attribute or index fields")
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value:
                    errors.append(f"line {node.lineno}: shell=True")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                errors.append(f"line {node.lineno}: touches {node.attr}")
            elif node.attr.startswith("_") and not (node.attr.startswith("__") and node.attr.endswith("__")):
                # harvest's internals (the http client's proxy route, its clients) are not part of the module contract
                errors.append(f"line {node.lineno}: touches private attribute {node.attr}")
            if isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr in {n for _, n in FORBIDDEN_CALLS}:
                errors.append(f"line {node.lineno}: references os.{node.attr}")
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__import__"):
            errors.append(f"line {node.lineno}: references {node.id}")
    return sorted(set(errors), key=lambda e: (int(e.split(":")[0].split()[-1]) if e.startswith("line") else 0, e))


def contract_findings(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fetch"), None)
    if fn is None:
        return ["the module must define `def fetch(page, *, http, ctx)` at module level"]
    args = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    missing = [a for a in ("page", "http", "ctx") if a not in args and not fn.args.kwarg]
    return [f"fetch() must accept {', '.join(missing)}"] if missing else []


def _ruff(path: Path) -> tuple[list[str], list[str]]:
    exe = shutil.which("ruff")
    argv = [exe] if exe else [sys.executable, "-m", "ruff"]
    try:
        r = subprocess.run([*argv, "check", "--no-cache", "--isolated", "--select", "E9,F63,F7,F82", "--output-format", "json", str(path)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], [f"ruff not run: {exc.__class__.__name__}"]
    if r.returncode not in (0, 1) or not r.stdout.strip().startswith("["):
        return [], ["ruff is not installed; syntax/undefined-name lint skipped (pip install ruff)"]
    try:
        found = json.loads(r.stdout)
    except ValueError:
        return [], ["ruff output unreadable"]
    return [f"line {f['location']['row']}: {f['code']} {f['message']}" for f in found], []


def lint(path: Path, data: bytes | None = None) -> dict:
    """Lint a module. `data` is the exact bytes the caller hashed: the verdict must describe the bytes the sha binds,
    not a later re-read of a file an agent can still be editing."""
    try:
        data = path.read_bytes() if data is None else data
        source = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return {"ok": False, "errors": [f"cannot read {path}: {e}"], "warnings": []}
    errors, warnings = [], []
    try:
        compile(source, str(path), "exec")
    except SyntaxError as e:
        errors.append(f"does not compile: {e}")
    errors += static_findings(source, str(path))
    errors += contract_findings(source)
    if not errors:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="harvest-lint-") as td:
            copy = Path(td) / path.name
            copy.write_bytes(data)
            e2, w2 = _ruff(copy)
        errors += e2
        warnings += w2
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def job_for(p: Project, src: dict, sha: str | None, max_pages: int, max_rows: int = 100000, ua_mode: str = "own") -> dict:
    region = next((r for r in (src.get("regions") or []) if r != "GLOBAL"), None)
    from . import regions as _regions
    return {"module_path": str(p.module_path(src["id"])), "expected_sha": sha,
            "source": {k: src.get(k) for k in ("id", "name", "url", "domain", "regions", "lane", "field_map", "kind")},
            "project": {"name": p.name, "target": p.spec.target, "record_type": p.spec.record_type, "regions": p.spec.region_codes},
            "fields": {k: {kk: vv for kk, vv in v.items() if kk in ("type", "required", "unit")} for k, v in p.template["fields"].items()},
            "region": region, "currency": _regions.info(region)["currency"] if region else None,
            "max_pages": max_pages, "max_rows": max_rows,
            "cpu_limit_s": int(src.get("time_budget_s") or p.spec.time_budget_s) + 30,
            "http": {"rate_s": src.get("rate_s") if src.get("rate_s") is not None else p.spec.rate_s,
                     "timeout": float(os.environ.get("HARVEST_HTTP_TIMEOUT", "20")), "retries": 3,
                     "url_budget_s": float(os.environ.get("HARVEST_URL_BUDGET_S", "90")),
                     "backoff_s": float(os.environ.get("HARVEST_BACKOFF_S", "1.0")), "ua_mode": ua_mode,
                     "allow_session_headers": sessionhdr.allowed(src)}}


def normalizer_for(p: Project, src: dict) -> Normalizer:
    region = next((r for r in (src.get("regions") or []) if r != "GLOBAL"), None)
    return Normalizer(p.template, source_id=src["id"], base_url=src.get("url"), region=region, report_currency=p.spec.report_currency,
                      fx=p.fx(), field_map=src.get("field_map") or {}, regions_=list(src.get("regions") or []),
                      project_regions=list(p.spec.region_codes))


def validate_rows(p: Project, src: dict, rows: list[dict]) -> dict:
    norm = normalizer_for(p, src)
    ok, bad, warnings = [], [], {}
    for r in rows:
        rec, errs, warns = norm.normalize(r)
        if rec is None:
            bad.append({"errors": errs, "row": {k: (str(v)[:80] if not isinstance(v, (int, float)) else v) for k, v in list(r.items())[:12]}})
        else:
            ok.append(rec)
        for w in warns:
            key = w.split(":")[0]
            warnings[key] = warnings.get(key, 0) + 1
    n = len(rows)
    frac = len(ok) / n if n else 0.0
    coverage = {f: round(sum(1 for r in ok if r.get(f) not in (None, "", [], {})) / len(ok), 2) for f in p.template["fields"]} if ok else {}
    errors = []
    if n == 0:
        errors.append("page 1 returned no rows")
    elif frac < MIN_VALID_FRACTION:
        errors.append(f"only {len(ok)}/{n} rows ({frac:.0%}) pass the {p.spec.record_type} template (need {MIN_VALID_FRACTION:.0%})")
    reasons: dict[str, int] = {}
    for b in bad:
        for e in b["errors"]:
            reasons[e] = reasons.get(e, 0) + 1
    return {"ok": not errors, "errors": errors, "rows": n, "valid": len(ok), "valid_fraction": round(frac, 3), "field_coverage": coverage,
            "quarantine_reasons": reasons, "parse_warnings": warnings, "sample": ok[:3], "rejected_sample": bad[:3]}


def review(p: Project, sid: str, *, timeout_s: int | None = None, pages: int = 1, use_proxy: bool = True, use_browser_ua: bool = True) -> dict:
    """The gate, holding the source's lock (locks.py): never alongside a run or another review of the same source.
    Raises locks.SourceBusy when the source is taken."""
    from . import locks
    if not p.store.get_source(p.name, sid):
        raise LookupError(f"no source {sid}")
    with locks.held(p, sid, "review") as lk:
        return _review_locked(p, sid, lk, timeout_s=timeout_s, pages=pages, use_proxy=use_proxy, use_browser_ua=use_browser_ua)


def _review_locked(p: Project, sid: str, lk, *, timeout_s: int | None, pages: int, use_proxy: bool, use_browser_ua: bool) -> dict:
    src = p.store.get_source(p.name, sid)
    if not src:
        raise LookupError(f"no source {sid}")
    path = p.module_path(sid)
    if not path.is_file():
        raise FileNotFoundError(f"no module at {path}; call harvest_template_scraper first")
    data = path.read_bytes()  # read once: the sha, the lint and (via expected_sha) the sandbox all see these bytes
    sha = sandbox.sha256_bytes(data)
    report: dict = {"source_id": sid, "module": str(path), "module_sha": sha, "lane": src.get("lane")}
    report["lint"] = lint(path, data)
    if report["lint"]["ok"]:
        from . import proxy, uamode
        ua = uamode.mode_for(p, src, "review", use_browser_ua)
        report["ua_mode"] = ua
        res = proxy.sandboxed(p, src, job_for(p, src, sha, max_pages=max(1, min(pages, 3)), ua_mode=ua), timeout_s or REVIEW_TIMEOUT_S,
                              purpose="review", enabled=use_proxy, stop=lambda: lk.lost)
        rows = [r for pg in res["pages"] for r in pg["rows"]]
        page_errors = [pg["error"] for pg in res["pages"] if pg.get("error")]
        end = res["end"] or {}
        smoke_err = []
        if res["timed_out"]:
            smoke_err.append(f"page fetch timed out after {timeout_s or REVIEW_TIMEOUT_S}s")
        if end.get("stopped") in ("import_error", "sha_mismatch"):
            smoke_err.append(end.get("error") or end["stopped"])
        smoke_err += [f"fetch raised: {e}" for e in page_errors]
        if not res["end"] and not res["timed_out"]:
            smoke_err.append("sandbox exited without a result: " + (res["stderr"].strip().splitlines() or ["?"])[-1][:300])
        report["smoke"] = {"ok": not smoke_err and bool(rows), "errors": smoke_err or ([] if rows else ["page 1 returned no rows"]),
                           "rows": len(rows), "pages": len(res["pages"]), "http": end.get("http"), "http_events": (end.get("events") or [])[-8:],
                           "proxy": res.get("proxy"), "isolation": res.get("isolation")}
        if res.get("isolation") == "policy" and (os.environ.get("HARVEST_SANDBOX") or "auto").lower() != "policy":
            report["lint"].setdefault("warnings", []).append(sandbox.POLICY_WARNING)
        report["schema"] = validate_rows(p, src, rows) if rows else {"ok": False, "errors": ["no rows to validate"]}
    else:
        report["smoke"] = {"ok": False, "errors": ["skipped: lint failed"]}
        report["schema"] = {"ok": False, "errors": ["skipped: lint failed"]}
    verdict = "pass" if report["lint"]["ok"] and report["smoke"]["ok"] and report["schema"]["ok"] else "fail"
    if not lk.valid():
        raise RuntimeError("the review lost its source lock to another worker; nothing recorded")
    report["verdict"] = verdict
    rid = p.store.add_review(p.name, sid, sha, verdict, report)
    fields = {"module_sha": sha, "review_verdict": verdict}
    if verdict == "pass":
        fields["reviewed_sha"] = sha
        if src.get("status") not in ("enabled",):
            fields["status"] = "reviewed"
    else:
        if src.get("status") != "enabled":
            fields["status"] = "review_failed"
    p.store.upsert_source(p.name, sid, fields)
    report["review_id"] = rid
    return report


def enable(p: Project, sid: str, enabled: bool = True) -> dict:
    src = p.store.get_source(p.name, sid)
    if not src:
        raise LookupError(f"no source {sid}")
    if not enabled:
        p.store.upsert_source(p.name, sid, {"enabled": 0, "status": "disabled"})
        return {"id": sid, "enabled": False}
    problems = []
    if src.get("lane") in (None, "none"):
        problems.append(f"lane is {src.get('lane')}")
    if src.get("robots_status") != "allowed":
        problems.append(f"robots.txt status is {src.get('robots_status')}")
    if src.get("terms_status") not in ("allowed", "no_clause"):
        problems.append(f"terms status is {src.get('terms_status')}: read the terms and record a verdict (harvest_record_policy)")
    path = p.module_path(sid)
    cur_sha = sandbox.sha256_file(path) if path.is_file() else None
    if not src.get("reviewed_sha"):
        problems.append("no passing review")
    elif cur_sha != src.get("reviewed_sha"):
        problems.append("the module changed after its passing review; review it again")
    if problems:
        return {"id": sid, "enabled": False, "refused": problems}
    p.store.upsert_source(p.name, sid, {"enabled": 1, "status": "enabled"})
    return {"id": sid, "enabled": True, "module_sha": cur_sha}
