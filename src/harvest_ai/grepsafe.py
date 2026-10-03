"""Bounded regex search for `probe_url(grep=...)`.

The pattern comes from an agent and the text from a website, so a backtracking-heavy pattern must not hang the
MCP server. The pattern is length-capped and compiled in the caller (syntax errors are reported as such), and the
matching runs in a separate process (spawned; this module imports nothing heavy) that is killed at the timeout.
"""

from __future__ import annotations

import os
import re

GREP_MAX_LEN = 300
GREP_MAX_MATCHES = 10000
GREP_TIMEOUT_S = float(os.environ.get("HARVEST_GREP_TIMEOUT_S", "3"))


def _worker(pattern: str, text: str, conn) -> None:
    total, spans = 0, []
    for m in re.finditer(pattern, text, re.I):
        total += 1
        if len(spans) < 8:
            spans.append((m.start(), m.end()))
        if total >= GREP_MAX_MATCHES:
            break
    conn.send((spans, total))
    conn.close()


def grep_spans(pattern: str, text: str, timeout_s: float | None = None) -> tuple[list[tuple[int, int]], int, bool]:
    """-> (first 8 match spans, match count up to GREP_MAX_MATCHES, capped?). ValueError on a too-long or invalid
    pattern, or when matching outlives the timeout."""
    import multiprocessing as mp
    if len(pattern) > GREP_MAX_LEN:
        raise ValueError(f"grep pattern longer than {GREP_MAX_LEN} characters")
    try:
        re.compile(pattern, re.I)
    except re.error as exc:
        raise ValueError(f"bad grep regex: {exc}") from None
    timeout = timeout_s or GREP_TIMEOUT_S
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_worker, args=(pattern, text, child), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(timeout):
            raise ValueError(f"grep pattern took longer than {timeout:g}s on this page; use a simpler (literal) pattern")
        spans, total = parent.recv()
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=5)
        parent.close()
    return spans, total, total >= GREP_MAX_MATCHES
