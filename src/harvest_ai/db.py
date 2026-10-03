"""Storage: SQLite by default, Postgres when the DSN is `postgresql://...` (needs `psycopg`).

All tables carry a `project` column so one Postgres database can hold many projects. Records keep
their normalised fields as JSON in `data` plus the keys used for dedup and pruning.

Write policy (see `finalize_run`): rows are upserted as pages arrive; a source's rows that were not
seen in this run are deleted only after a *complete* walk that returned at least `REPLACE_MIN_FRACTION`
of the source's baseline. Otherwise nothing is replaced and rows unseen for `prune_after_days` are pruned.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterable

REPLACE_MIN_FRACTION = 0.6

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sources (
        project TEXT NOT NULL, id TEXT NOT NULL, name TEXT, url TEXT, domain TEXT, regions TEXT, angles TEXT, kind TEXT,
        evidence TEXT, status TEXT, lane TEXT, lane_detail TEXT, robots_status TEXT, terms_status TEXT, terms_url TEXT,
        terms_clause TEXT, enabled INTEGER DEFAULT 0, module_sha TEXT, reviewed_sha TEXT, review_verdict TEXT,
        field_map TEXT, max_pages INTEGER, time_budget_s INTEGER, cadence_hours REAL, rate_s REAL, use_proxy INTEGER DEFAULT 0,
        notes TEXT, baseline INTEGER, created_at TEXT, updated_at TEXT, PRIMARY KEY (project, id))""",
    """CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL, module_sha TEXT, verdict TEXT, report TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT NOT NULL, trigger_kind TEXT, started_at TEXT, heartbeat_at TEXT,
        finished_at TEXT, status TEXT, stopped TEXT, pages INTEGER DEFAULT 0, rows_fetched INTEGER DEFAULT 0, rows_stored INTEGER DEFAULT 0,
        rows_quarantined INTEGER DEFAULT 0, complete INTEGER DEFAULT 0, write_mode TEXT, pruned INTEGER DEFAULT 0, error TEXT,
        module_sha TEXT, http_stats TEXT)""",
    """CREATE TABLE IF NOT EXISTS records (
        project TEXT NOT NULL, uid TEXT NOT NULL, source_id TEXT NOT NULL, source_key TEXT, url TEXT, fingerprint TEXT,
        data TEXT, first_seen TEXT, last_seen TEXT, run_id TEXT, PRIMARY KEY (project, uid))""",
    "CREATE INDEX IF NOT EXISTS records_src ON records (project, source_id, last_seen)",
    "CREATE INDEX IF NOT EXISTS records_fp ON records (project, fingerprint)",
    """CREATE TABLE IF NOT EXISTS quarantine (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT, run_id TEXT, reasons TEXT, raw TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT, kind TEXT, message TEXT, created_at TEXT, delivered INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT, status TEXT, params TEXT, result TEXT, log_path TEXT,
        created_at TEXT, started_at TEXT, finished_at TEXT, pid INTEGER)""",
    """CREATE TABLE IF NOT EXISTS census_deferred (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, domain TEXT, url TEXT, candidate TEXT, reason TEXT, round TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS census_rounds (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, label TEXT, added INTEGER, merged INTEGER, rejected INTEGER, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS proxy_usage (
        project TEXT NOT NULL, day TEXT NOT NULL, source_id TEXT NOT NULL, requests INTEGER DEFAULT 0, bytes BIGINT DEFAULT 0,
        updated_at TEXT, PRIMARY KEY (project, day, source_id))""",
    """CREATE TABLE IF NOT EXISTS source_locks (
        project TEXT NOT NULL, source_id TEXT NOT NULL, owner TEXT, purpose TEXT, fence INTEGER DEFAULT 0, until TEXT, acquired_at TEXT,
        PRIMARY KEY (project, source_id))""",
    """CREATE TABLE IF NOT EXISTS repairs (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, source_id TEXT, dispatched_at TEXT, status TEXT, result TEXT)""",
]

JSON_COLS = {"candidate", "regions", "angles", "evidence", "lane_detail", "field_map", "report", "reasons", "raw", "data", "params", "result", "http_stats"}


def now_iso(delta_s: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_s)).isoformat(timespec="microseconds")


def _plus(iso: str, seconds: float) -> str:
    return (dt.datetime.fromisoformat(iso) + dt.timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class SourceLock:
    """One walk's hold on a source (run, review or lane detection): see `Store.acquire_source_lock`.
    SQLite: a lease row with a fence; the holder renews it, and every check compares owner + fence, so a holder that
    stalled past its lease and lost the row to another worker finds out and cannot write as if it still held it.
    Postgres: additionally a session advisory lock on a dedicated connection; nobody else can take the source while
    that connection lives, and the lock goes with the connection if the worker dies."""

    def __init__(self, store: "Store", project: str, sid: str, owner: str, fence: int, lease_s: float, pg_conn=None):
        self.store, self.project, self.sid, self.owner, self.fence, self.lease_s = store, project, sid, owner, fence, lease_s
        self._pg = pg_conn
        self.lost = False

    def renew(self) -> bool:
        n = self.store.execute("UPDATE source_locks SET until = ? WHERE project = ? AND source_id = ? AND owner = ? AND fence = ?",
                               (now_iso(self.lease_s), self.project, self.sid, self.owner, self.fence))
        ok = n == 1 and self._pg_alive()
        if not ok:
            self.lost = True
        return ok

    def _pg_alive(self) -> bool:
        if self._pg is None:
            return True
        try:
            row = self._pg.execute("SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND granted").fetchone()
            return bool(row and list(row.values() if isinstance(row, dict) else row)[0])
        except Exception:
            return False

    def valid(self) -> bool:
        if self.lost:
            return False
        r = self.store.one("SELECT until FROM source_locks WHERE project = ? AND source_id = ? AND owner = ? AND fence = ?",
                           (self.project, self.sid, self.owner, self.fence))
        ok = bool(r) and (self._pg is not None or str(r["until"]) >= now_iso()) and self._pg_alive()
        if not ok:
            self.lost = True
        return ok

    def release(self) -> None:
        try:
            self.store.execute("DELETE FROM source_locks WHERE project = ? AND source_id = ? AND owner = ? AND fence = ?",
                               (self.project, self.sid, self.owner, self.fence))
        finally:
            if self._pg is not None:
                try:
                    self._pg.close()  # ends the session: the advisory lock goes with it
                except Exception:
                    pass
                self._pg = None


class Store:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pg = dsn.startswith(("postgres://", "postgresql://"))
        self._lock = threading.RLock()
        if self.pg:
            try:
                import psycopg  # noqa: F401  (optional; connections are made by _pg_connect)
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("Postgres DSN given but psycopg is not installed: pip install 'harvest[postgres]'") from exc
            self.conn = self._pg_connect()
        else:
            path = dsn[len("sqlite:///"):] if dsn.startswith("sqlite:///") else dsn
            self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
        for stmt in SCHEMA:
            self.execute(stmt)
        self._ensure_columns("jobs", {"worker_id": "TEXT", "lease_until": "TEXT", "heartbeat_at": "TEXT",
                                      "attempts": "INTEGER DEFAULT 0", "max_attempts": "INTEGER DEFAULT 3"})
        self.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (status, created_at)")
        self._ensure_columns("runs", {"ua_mode": "TEXT"})
        if self._ensure_columns("alerts", {"dedup_key": "TEXT", "attempts": "INTEGER DEFAULT 0"}):
            # alerts written before delivery was tracked went out (or not) back then: never re-send them now
            self.execute("UPDATE alerts SET delivered = 1")
        self.execute("CREATE INDEX IF NOT EXISTS alerts_src ON alerts (project, source_id, kind, created_at)")
        self.execute("CREATE INDEX IF NOT EXISTS runs_src ON runs (project, source_id, started_at)")

    # ---------------------------------------------------------------- connection health (Postgres)
    reconnects = 0

    def _pg_connect(self):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)

    def _conn_dead(self) -> bool:
        return bool(self.pg and (self.conn.closed or getattr(self.conn, "broken", False)))

    def _ensure_conn(self) -> None:
        """A Postgres session that died (server restart, failover, idle-session kill) is replaced before the next
        statement. Without this one dead session made every later call of that Store fail until the process restarted."""
        if self._conn_dead():
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = self._pg_connect()
            self.reconnects += 1

    def _is_conn_error(self, exc: BaseException) -> bool:
        if not self.pg:
            return False
        try:
            import psycopg
        except ImportError:  # pragma: no cover
            return False
        return isinstance(exc, psycopg.OperationalError) and self._conn_dead()

    def clock_iso(self, delta_s: float = 0) -> str:
        """Now (+delta) on the store's clock. Postgres: the server's clock, so job leases written and compared by processes
        on different hosts, or by a process whose clock was stepped, agree (the soak saw a worker with a skewed clock have
        its job requeued by the web app's reaper and executed three times). SQLite: the host clock (one host by design)."""
        if not self.pg:
            return now_iso(delta_s)
        r = self.one("SELECT to_char((now() AT TIME ZONE 'UTC') + make_interval(secs => ?), 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"') AS t",
                     (float(delta_s),))
        return str(r["t"])

    def _ensure_columns(self, table: str, cols: dict[str, str]) -> list[str]:
        """Additive migrations for databases created by an older harvest. Returns the columns it added."""
        if self.pg:
            have = {r["column_name"] for r in self.query("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))}
        else:
            have = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
        added = []
        for name, decl in cols.items():
            if name not in have:
                self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(name)
        return added

    # ---------------------------------------------------------------- primitives
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql: str, params: Iterable = ()) -> int:
        # a write is never replayed on a new session: its outcome on the dead one is unknown. It fails this once; the
        # next call reconnects (_ensure_conn).
        with self._lock:
            self._ensure_conn()
            cur = self.conn.execute(self._sql(sql), tuple(params))
            return cur.rowcount if cur.rowcount is not None else 0

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            self._ensure_conn()
            try:
                cur = self.conn.execute(self._sql(sql), tuple(params))
                rows = cur.fetchall()
            except Exception as exc:
                if not self._is_conn_error(exc):
                    raise
                self._ensure_conn()  # a read is safe to repeat once on a fresh session
                cur = self.conn.execute(self._sql(sql), tuple(params))
                rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in list(d):
                if k in JSON_COLS and isinstance(d[k], str):
                    try:
                        d[k] = json.loads(d[k])
                    except ValueError:
                        pass
            out.append(d)
        return out

    def one(self, sql: str, params: Iterable = ()) -> dict | None:
        r = self.query(sql, params)
        return r[0] if r else None

    @contextmanager
    def transaction(self):
        with self._lock:
            self._ensure_conn()
            if self.pg:
                with self.conn.transaction():
                    yield
            else:
                self.conn.execute("BEGIN")
                try:
                    yield
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    def close(self) -> None:
        self.conn.close()

    def jx(self, field: str, numeric: bool = False) -> str:
        """SQL expression for a field inside records.data (field names are validated by callers)."""
        if not re.fullmatch(r"[A-Za-z0-9_]+", field):
            raise ValueError(f"bad field {field!r}")
        if self.pg:
            e = f"(data::jsonb->>'{field}')"
            return f"({e})::double precision" if numeric else e
        return f"json_extract(data, '$.{field}')"

    @staticmethod
    def _dump(v: Any) -> Any:
        return json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v

    # ---------------------------------------------------------------- sources
    SOURCE_COLS = ["name", "url", "domain", "regions", "angles", "kind", "evidence", "status", "lane", "lane_detail", "robots_status",
                   "terms_status", "terms_url", "terms_clause", "enabled", "module_sha", "reviewed_sha", "review_verdict", "field_map",
                   "max_pages", "time_budget_s", "cadence_hours", "rate_s", "use_proxy", "notes", "baseline"]

    def upsert_source(self, project: str, sid: str, fields: dict) -> None:
        bad = set(fields) - set(self.SOURCE_COLS)
        if bad:
            raise ValueError(f"unknown source fields {sorted(bad)}")
        now = now_iso()
        cur = self.get_source(project, sid)
        if cur is None:
            cols = ["project", "id", "created_at", "updated_at", *fields]
            vals = [project, sid, now, now, *[self._dump(v) for v in fields.values()]]
            self.execute(f"INSERT INTO sources ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals)
        elif fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.execute(f"UPDATE sources SET {sets}, updated_at = ? WHERE project = ? AND id = ?",
                         [*[self._dump(v) for v in fields.values()], now, project, sid])

    def get_source(self, project: str, sid: str) -> dict | None:
        return self.one("SELECT * FROM sources WHERE project = ? AND id = ?", (project, sid))

    def list_sources(self, project: str, status: str | None = None, enabled: bool | None = None) -> list[dict]:
        sql, p = "SELECT * FROM sources WHERE project = ?", [project]
        if status:
            sql += " AND status = ?"
            p.append(status)
        if enabled is not None:
            sql += " AND enabled = ?"
            p.append(1 if enabled else 0)
        return self.query(sql + " ORDER BY created_at, id", p)

    def source_by_domain(self, project: str, domain: str) -> dict | None:
        return self.one("SELECT * FROM sources WHERE project = ? AND domain = ?", (project, domain))

    # ---------------------------------------------------------------- reviews
    def add_review(self, project: str, sid: str, sha: str, verdict: str, report: dict) -> str:
        rid = new_id()
        self.execute("INSERT INTO reviews (id, project, source_id, module_sha, verdict, report, created_at) VALUES (?,?,?,?,?,?,?)",
                     (rid, project, sid, sha, verdict, self._dump(report), now_iso()))
        return rid

    def latest_review(self, project: str, sid: str) -> dict | None:
        return self.one("SELECT * FROM reviews WHERE project = ? AND source_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (project, sid))

    # ---------------------------------------------------------------- runs
    def start_run(self, project: str, sid: str, trigger: str, module_sha: str | None) -> str:
        rid = new_id()
        now = now_iso()
        self.execute("INSERT INTO runs (id, project, source_id, trigger_kind, started_at, heartbeat_at, status, module_sha) VALUES (?,?,?,?,?,?,?,?)",
                     (rid, project, sid, trigger, now, now, "running", module_sha))
        return rid

    def heartbeat(self, run_id: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE runs SET heartbeat_at = ?{', ' + sets if sets else ''} WHERE id = ?",
                     [now_iso(), *[self._dump(v) for v in fields.values()], run_id])

    def get_run(self, run_id: str) -> dict | None:
        return self.one("SELECT * FROM runs WHERE id = ?", (run_id,))

    def list_runs(self, project: str, source_id: str | None = None, limit: int = 50) -> list[dict]:
        sql, p = "SELECT * FROM runs WHERE project = ?", [project]
        if source_id:
            sql += " AND source_id = ?"
            p.append(source_id)
        return self.query(sql + " ORDER BY started_at DESC, id DESC LIMIT ?", [*p, int(limit)])

    def last_finished_run(self, project: str, sid: str, before: str | None = None) -> dict | None:
        """The latest finished run; with `before`, only runs that started before that time (a run stamped by a clock that
        ran ahead must not decide when the source is due next)."""
        sql, p = "SELECT * FROM runs WHERE project = ? AND source_id = ? AND status != 'running'", [project, sid]
        if before:
            sql += " AND started_at <= ?"
            p.append(before)
        return self.one(sql + " ORDER BY started_at DESC, id DESC LIMIT 1", p)

    def recent_yield(self, project: str, sid: str, exclude_run_id: str | None = None, n: int = 5) -> int | None:
        """The median rows stored by the source's last `n` healthy runs: what a run of this source normally yields. This
        is the reference for `degraded` when the source has no replace baseline (its walks never complete)."""
        rows = self.query("SELECT rows_stored FROM runs WHERE project = ? AND source_id = ? AND id != ? AND status IN ('ok', 'partial') "
                          "AND rows_stored > 0 ORDER BY started_at DESC, id DESC LIMIT ?", (project, sid, exclude_run_id or "", int(n)))
        vals = sorted(int(r["rows_stored"]) for r in rows)
        return vals[len(vals) // 2] if vals else None

    def good_run_since(self, project: str, sid: str, since: str) -> bool:
        """A healthy run (ok / partial) finished after `since`: the end of an incident."""
        return bool(self.one("SELECT id FROM runs WHERE project = ? AND source_id = ? AND status IN ('ok', 'partial') AND finished_at > ? LIMIT 1",
                             (project, sid, since)))

    # ---------------------------------------------------------------- records
    def upsert_records(self, project: str, sid: str, run_id: str, rows: list[tuple[str, dict, str | None]]) -> int:
        """rows: (uid, record, fingerprint)."""
        now = now_iso()
        n = 0
        with self.transaction():
            for uid, rec, fp in rows:
                self.conn.execute(self._sql(
                    "INSERT INTO records (project, uid, source_id, source_key, url, fingerprint, data, first_seen, last_seen, run_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (project, uid) DO UPDATE SET data = excluded.data, url = excluded.url, "
                    "fingerprint = excluded.fingerprint, last_seen = excluded.last_seen, run_id = excluded.run_id"),
                    (project, uid, sid, str(rec.get("source_id") or ""), rec.get("url"), fp, json.dumps(rec, ensure_ascii=False, default=str), now, now, run_id))
                n += 1
        return n

    def count_records(self, project: str, sid: str | None = None) -> int:
        sql, p = "SELECT COUNT(*) AS n FROM records WHERE project = ?", [project]
        if sid:
            sql += " AND source_id = ?"
            p.append(sid)
        return int(self.one(sql, p)["n"])

    def finalize_run(self, project: str, sid: str, run_id: str, *, complete: bool, stored: int, prune_after_days: float) -> dict:
        """Apply the never-wipe rule. Returns {write_mode, pruned, baseline}."""
        src = self.get_source(project, sid) or {}
        baseline = src.get("baseline")
        if not baseline:
            baseline = self.one("SELECT COUNT(*) AS n FROM records WHERE project = ? AND source_id = ? AND run_id != ?", (project, sid, run_id))["n"]
        if complete and stored > 0 and stored >= REPLACE_MIN_FRACTION * (baseline or 0):
            # "unseen in this run" means last seen before this run started: a row an overlapping walk of the same source
            # refreshed meanwhile (a second worker host, a lease that expired under a slow worker) is not wiped
            run = self.one("SELECT started_at FROM runs WHERE id = ?", (run_id,)) or {}
            started = run.get("started_at") or now_iso()
            pruned = self.execute("DELETE FROM records WHERE project = ? AND source_id = ? AND run_id != ? AND last_seen < ?",
                                  (project, sid, run_id, started))
            self.upsert_source(project, sid, {"baseline": stored})
            return {"write_mode": "replace", "pruned": pruned, "baseline": stored}
        cutoff = now_iso(-prune_after_days * 86400)
        pruned = self.execute("DELETE FROM records WHERE project = ? AND source_id = ? AND last_seen < ?", (project, sid, cutoff))
        return {"write_mode": "upsert", "pruned": pruned, "baseline": baseline or None}

    def add_quarantine(self, project: str, sid: str, run_id: str | None, reasons: list[str], raw: Any) -> None:
        blob = json.dumps(raw, ensure_ascii=False, default=str)
        if len(blob) > 8000:
            blob = json.dumps({"_truncated": blob[:8000]})
        why = json.dumps(reasons, ensure_ascii=False)
        # the same bad row seen again (every run of a source re-reads it) is one quarantine entry, refreshed with the
        # latest run and time, so the quarantine and its count do not grow with every run
        qid = "q" + hashlib.sha256("\x1f".join((project, sid or "", why, blob)).encode("utf-8")).hexdigest()[:24]
        self.execute("INSERT INTO quarantine (id, project, source_id, run_id, reasons, raw, created_at) VALUES (?,?,?,?,?,?,?) "
                     "ON CONFLICT (id) DO UPDATE SET run_id = excluded.run_id, created_at = excluded.created_at",
                     (qid, project, sid, run_id, why, blob, now_iso()))

    def list_quarantine(self, project: str, source_id: str | None = None, limit: int = 100) -> list[dict]:
        sql, p = "SELECT * FROM quarantine WHERE project = ?", [project]
        if source_id:
            sql += " AND source_id = ?"
            p.append(source_id)
        return self.query(sql + " ORDER BY created_at DESC LIMIT ?", [*p, int(limit)])

    # ---------------------------------------------------------------- alerts / jobs / census rounds / repairs
    def add_alert(self, project: str, sid: str | None, kind: str, message: str, key: str | None = None, created_at: str | None = None) -> str:
        aid = new_id()
        self.execute("INSERT INTO alerts (id, project, source_id, kind, message, created_at, dedup_key, delivered, attempts) VALUES (?,?,?,?,?,?,?,0,0)",
                     (aid, project, sid, kind, message, created_at or now_iso(), key))
        return aid

    def last_alert(self, project: str, sid: str | None, kind: str) -> dict | None:
        return self.one("SELECT * FROM alerts WHERE project = ? AND COALESCE(source_id, '') = ? AND kind = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                        (project, sid or "", kind))

    def alert_key_seen(self, project: str, sid: str | None, kind: str, key: str, since: str) -> bool:
        return bool(self.one("SELECT id FROM alerts WHERE project = ? AND COALESCE(source_id, '') = ? AND kind = ? AND dedup_key = ? AND created_at > ? LIMIT 1",
                             (project, sid or "", kind, key, since)))

    def undelivered_alerts(self, project: str, since: str, max_attempts: int = 20) -> list[dict]:
        return self.query("SELECT * FROM alerts WHERE project = ? AND COALESCE(delivered, 0) = 0 AND created_at > ? AND COALESCE(attempts, 0) < ? "
                          "ORDER BY created_at, id", (project, since, int(max_attempts)))

    def mark_alerts(self, ids: list[str], delivered: bool) -> None:
        for aid in ids:
            self.execute("UPDATE alerts SET delivered = ?, attempts = COALESCE(attempts, 0) + 1 WHERE id = ?", (1 if delivered else 0, aid))

    def prune_history(self, project: str, before: str) -> dict:
        """Retention: finished runs, quarantine entries, alerts and finished jobs older than `before`. Records, sources and
        reviews are never touched here (records have their own never-wipe and prune rules)."""
        out = {}
        for table, col, extra in (("runs", "started_at", " AND status != 'running'"), ("quarantine", "created_at", ""),
                                  ("alerts", "created_at", ""), ("jobs", "created_at", " AND status NOT IN ('queued', 'running')")):
            out[table] = self.execute(f"DELETE FROM {table} WHERE project = ? AND {col} < ?{extra}", (project, before))
        return out

    def list_alerts(self, project: str, limit: int = 100) -> list[dict]:
        return self.query("SELECT * FROM alerts WHERE project = ? ORDER BY created_at DESC LIMIT ?", (project, int(limit)))

    def add_job(self, project: str, kind: str, params: dict, max_attempts: int = 3) -> str:
        jid = new_id()
        self.execute("INSERT INTO jobs (id, project, kind, status, params, created_at, attempts, max_attempts) VALUES (?,?,?,?,?,?,?,?)",
                     (jid, project, kind, "queued", self._dump(params), now_iso(), 0, int(max_attempts)))
        return jid

    # ---------------------------------------------------------------- durable queue (claim / lease / heartbeat)
    def claim_job(self, project: str, worker_id: str, lease_s: float, *, job_id: str | None = None, kinds: list[str] | None = None) -> dict | None:
        """Atomically take the oldest queued job (or one whose lease expired). The conditional UPDATE is the
        lock: of several workers racing for one row exactly one sees rowcount 1. Times come from the store clock."""
        now = self.clock_iso()
        lease_until = _plus(now, lease_s)
        sql = ("SELECT id, kind FROM jobs WHERE project = ? AND (status = 'queued' OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?))"
               " AND COALESCE(attempts, 0) < COALESCE(max_attempts, 3)")
        params: list = [project, now]
        if job_id:
            sql += " AND id = ?"
            params.append(job_id)
        for cand in self.query(sql + " ORDER BY created_at, id LIMIT 20", params):
            if kinds and not any(cand["kind"] == k or cand["kind"].startswith(k + ":") for k in kinds):
                continue
            n = self.execute("UPDATE jobs SET status = 'running', worker_id = ?, lease_until = ?, heartbeat_at = ?, attempts = COALESCE(attempts, 0) + 1, "
                             "started_at = COALESCE(started_at, ?), finished_at = NULL WHERE id = ? AND (status = 'queued' OR "
                             "(status = 'running' AND lease_until IS NOT NULL AND lease_until < ?))",
                             (worker_id, lease_until, now, now, cand["id"], now))
            if n == 1:
                return self.get_job(cand["id"])
        return None

    def renew_job(self, jid: str, worker_id: str, lease_s: float) -> bool:
        now = self.clock_iso()
        return self.execute("UPDATE jobs SET lease_until = ?, heartbeat_at = ? WHERE id = ? AND worker_id = ? AND status = 'running'",
                            (_plus(now, lease_s), now, jid, worker_id)) == 1

    def finish_job(self, jid: str, worker_id: str, status: str, result: dict) -> bool:
        """Record the outcome, only if this worker still owns the job (a stale worker cannot overwrite a re-run)."""
        return self.execute("UPDATE jobs SET status = ?, result = ?, finished_at = ?, lease_until = NULL WHERE id = ? AND worker_id = ? AND status = 'running'",
                            (status, self._dump(result), now_iso(), jid, worker_id)) == 1

    def reap_jobs(self, project: str) -> dict:
        """Expired leases: back to the queue while attempts remain, else 'lost'. Compared on the store clock."""
        now = self.clock_iso()
        lost = self.execute("UPDATE jobs SET status = 'lost', finished_at = ?, result = ? WHERE project = ? AND status = 'running' AND lease_until IS NOT NULL "
                            "AND lease_until < ? AND COALESCE(attempts, 0) >= COALESCE(max_attempts, 3)",
                            (now, json.dumps({"error": "lease expired; no attempts left (worker died or hung)"}), project, now))
        requeued = self.execute("UPDATE jobs SET status = 'queued', worker_id = NULL, lease_until = NULL WHERE project = ? AND status = 'running' "
                                "AND lease_until IS NOT NULL AND lease_until < ?", (project, now))
        return {"lost": lost, "requeued": requeued}

    # ---------------------------------------------------------------- per-source locks (run / review / detect)
    @staticmethod
    def _lock_key(project: str, sid: str) -> int:
        import hashlib
        return int.from_bytes(hashlib.sha256(f"harvest:{project}:{sid}".encode()).digest()[:8], "big", signed=True)

    def acquire_source_lock(self, project: str, sid: str, owner: str, lease_s: float, purpose: str = "") -> SourceLock | None:
        """Take the source if nobody holds it (or the holder's lease expired, SQLite). None when it is busy."""
        now = now_iso()
        pg_conn = None
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            pg_conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
            got = pg_conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (self._lock_key(project, sid),)).fetchone()["ok"]
            if not got:
                pg_conn.close()
                return None
            # we hold the advisory lock, so any row left behind is from a holder whose session ended: take it over
            cond = ""
        else:
            cond = " WHERE source_locks.until < ?"
        params = [project, sid, owner, purpose, now_iso(lease_s), now] + ([now] if not self.pg else [])
        n = self.execute("INSERT INTO source_locks (project, source_id, owner, purpose, fence, until, acquired_at) VALUES (?,?,?,?,1,?,?) "
                         "ON CONFLICT (project, source_id) DO UPDATE SET owner = excluded.owner, purpose = excluded.purpose, "
                         "fence = source_locks.fence + 1, until = excluded.until, acquired_at = excluded.acquired_at" + cond, params)
        row = self.one("SELECT fence FROM source_locks WHERE project = ? AND source_id = ? AND owner = ?", (project, sid, owner))
        if (not self.pg and n != 1) or not row:
            if pg_conn is not None:
                pg_conn.close()
            return None
        return SourceLock(self, project, sid, owner, int(row["fence"]), lease_s, pg_conn)

    def source_lock_holder(self, project: str, sid: str) -> dict | None:
        return self.one("SELECT owner, purpose, fence, until, acquired_at FROM source_locks WHERE project = ? AND source_id = ?", (project, sid))

    # ---------------------------------------------------------------- census continuation
    def defer_candidate(self, project: str, domain: str, url: str, candidate: dict, reason: str, round_label: str) -> None:
        if self.one("SELECT id FROM census_deferred WHERE project = ? AND domain = ?", (project, domain)):
            return
        self.execute("INSERT INTO census_deferred (id, project, domain, url, candidate, reason, round, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (new_id(), project, domain, url, self._dump(candidate), reason, round_label, now_iso()))

    def deferred_candidates(self, project: str) -> list[dict]:
        return self.query("SELECT * FROM census_deferred WHERE project = ? ORDER BY created_at, id", (project,))

    def drop_deferred(self, project: str, domain: str) -> None:
        self.execute("DELETE FROM census_deferred WHERE project = ? AND domain = ?", (project, domain))

    def update_job(self, jid: str, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*[self._dump(v) for v in fields.values()], jid])

    def get_job(self, jid: str) -> dict | None:
        return self.one("SELECT * FROM jobs WHERE id = ?", (jid,))

    def list_jobs(self, project: str, limit: int = 50) -> list[dict]:
        return self.query("SELECT * FROM jobs WHERE project = ? ORDER BY created_at DESC LIMIT ?", (project, int(limit)))

    def add_census_round(self, project: str, label: str, added: int, merged: int, rejected: int) -> None:
        self.execute("INSERT INTO census_rounds (id, project, label, added, merged, rejected, created_at) VALUES (?,?,?,?,?,?,?)",
                     (new_id(), project, label, added, merged, rejected, now_iso()))

    def census_rounds(self, project: str) -> list[dict]:
        return self.query("SELECT * FROM census_rounds WHERE project = ? ORDER BY created_at, id", (project,))

    def add_repair(self, project: str, sid: str, status: str, result: dict | None = None) -> str:
        rid = new_id()
        self.execute("INSERT INTO repairs (id, project, source_id, dispatched_at, status, result) VALUES (?,?,?,?,?,?)",
                     (rid, project, sid, now_iso(), status, self._dump(result or {})))
        return rid

    # ---------------------------------------------------------------- residential-proxy usage (per project, UTC day, source)
    def add_proxy_usage(self, project: str, day: str, sid: str, requests: int, nbytes: int) -> None:
        self.execute("INSERT INTO proxy_usage (project, day, source_id, requests, bytes, updated_at) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT (project, day, source_id) DO UPDATE SET requests = proxy_usage.requests + excluded.requests, "
                     "bytes = proxy_usage.bytes + excluded.bytes, updated_at = excluded.updated_at",
                     (project, day, sid, int(requests), int(nbytes), now_iso()))

    def proxy_usage_total(self, project: str, day: str) -> dict:
        r = self.one("SELECT COALESCE(SUM(requests), 0) AS requests, COALESCE(SUM(bytes), 0) AS bytes FROM proxy_usage WHERE project = ? AND day = ?",
                     (project, day)) or {}
        return {"requests": int(r.get("requests") or 0), "bytes": int(r.get("bytes") or 0)}

    def proxy_usage(self, project: str, since: str | None = None) -> list[dict]:
        sql, p = "SELECT day, source_id, requests, bytes, updated_at FROM proxy_usage WHERE project = ?", [project]
        if since:
            sql += " AND day >= ?"
            p.append(since)
        return [{**r, "requests": int(r["requests"] or 0), "bytes": int(r["bytes"] or 0)} for r in self.query(sql + " ORDER BY day DESC, source_id", p)]

    def last_repair(self, project: str, sid: str) -> dict | None:
        return self.one("SELECT * FROM repairs WHERE project = ? AND source_id = ? ORDER BY dispatched_at DESC LIMIT 1", (project, sid))
