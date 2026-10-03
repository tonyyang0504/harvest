"""One walk per source at a time, across processes and hosts: runs, reviews and lane detection of a source hold a
database lock for as long as they work on it (db.SourceLock: a fenced lease row on SQLite, plus a session advisory
lock on Postgres). The file lock in runner.py only covers one host; a job whose queue lease expired under a slow
worker is claimed again, and without this lock the same source would be walked twice and the two walks' replaces
could undo each other.

`held()` renews the lease in a background thread every lease/3. A holder that stops renewing (a stalled process)
loses the source on SQLite once the lease runs out; it notices through `SourceLock.valid()` / `.lost` and stops
without writing (the runner checks before storing a page and before the replace).
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from contextlib import contextmanager

LEASE_S = float(os.environ.get("HARVEST_SOURCE_LOCK_LEASE_S", "120"))


class SourceBusy(RuntimeError):
    """Another worker is running, reviewing or detecting this source."""


def owner_id(purpose: str) -> str:
    return f"{purpose}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@contextmanager
def held(p, sid: str, purpose: str, lease_s: float | None = None, renew: bool = True):
    lease = float(lease_s or LEASE_S)
    lk = p.store.acquire_source_lock(p.name, sid, owner_id(purpose), lease, purpose)
    if lk is None:
        h = p.store.source_lock_holder(p.name, sid) or {}
        raise SourceBusy(f"source {sid} is busy: {h.get('purpose') or 'another walk'} since {h.get('acquired_at') or '?'} "
                         f"({h.get('owner') or 'another worker'}); try again when it ends")
    stop = threading.Event()

    def beat():
        while not stop.wait(max(0.2, lease / 3)):
            if not lk.renew():
                return
    t = threading.Thread(target=beat, daemon=True) if renew else None
    if t:
        t.start()
    try:
        yield lk
    finally:
        stop.set()
        if t:
            t.join(timeout=5)
        lk.release()
