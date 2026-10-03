"""Query and export stored records (CSV, JSONL, Parquet when pyarrow is installed)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from . import templates
from .project import Project

META = ["source", "first_seen", "last_seen", "fingerprint"]
OPS = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _columns(p: Project) -> list[str]:
    return [*templates.output_columns(p.template), "extra"]


def query(p: Project, *, filters: dict | None = None, fields: list[str] | None = None, source_id: str | None = None, order_by: str | None = None,
          desc: bool = True, limit: int = 100, offset: int = 0, distinct: bool = False) -> dict:
    s = p.store
    cols = _columns(p)
    where, params = ["project = ?"], [p.name]
    if source_id:
        where.append("source_id = ?")
        params.append(source_id)
    for f, cond in (filters or {}).items():
        if f not in cols:
            raise ValueError(f"unknown field {f!r}; fields: {cols}")
        conds = cond if isinstance(cond, dict) else {"eq": cond}
        for op, val in conds.items():
            numeric = isinstance(val, (int, float)) and not isinstance(val, bool)
            expr = s.jx(f, numeric=numeric)
            if op in OPS:
                where.append(f"{expr} {OPS[op]} ?")
                params.append(val)
            elif op == "contains":
                where.append(f"LOWER({s.jx(f)}) LIKE ?")
                params.append(f"%{str(val).lower()}%")
            elif op == "in":
                vals = list(val or [])
                if not vals:
                    where.append("1 = 0")
                else:
                    where.append(f"{s.jx(f)} IN ({', '.join('?' for _ in vals)})")
                    params.extend(str(v) if s.pg else v for v in vals)
            elif op == "exists":
                where.append(f"{s.jx(f)} IS {'NOT ' if val else ''}NULL")
            else:
                raise ValueError(f"unknown operator {op!r}; use {sorted(OPS) + ['contains', 'in', 'exists']}")
    if distinct:  # one row per cross-source fingerprint (the most recently seen)
        where.append("(fingerprint IS NULL OR uid IN (SELECT r2.uid FROM records r2 WHERE r2.project = records.project AND r2.fingerprint = records.fingerprint "
                     "ORDER BY r2.last_seen DESC, r2.uid LIMIT 1))")
    order = "last_seen"
    if order_by:
        if order_by in META:
            order = order_by
        elif order_by in cols:
            num = any(t in (p.template["fields"].get(order_by, {}).get("type") or "number") for t in ("integer", "number", "money", "area", "distance", "weight"))
            order = s.jx(order_by, numeric=num)
        else:
            raise ValueError(f"cannot order by {order_by!r}")
    sql_where = " AND ".join(where)
    total = s.one(f"SELECT COUNT(*) AS n FROM records WHERE {sql_where}", params)["n"]
    rows = s.query(f"SELECT source_id, data, first_seen, last_seen, fingerprint FROM records WHERE {sql_where} "
                   f"ORDER BY {order} {'DESC' if desc else 'ASC'}, uid LIMIT ? OFFSET ?", [*params, int(limit), int(offset)])
    out = []
    for r in rows:
        d = r["data"] if isinstance(r["data"], dict) else json.loads(r["data"])
        d = {k: d.get(k) for k in fields} if fields else d
        out.append({**d, "source": r["source_id"], "first_seen": r["first_seen"], "last_seen": r["last_seen"], "fingerprint": r["fingerprint"]})
    return {"total": total, "count": len(out), "offset": offset, "rows": out}


def _iter_all(p: Project, **kw):
    offset = 0
    while True:
        page = query(p, limit=2000, offset=offset, **kw)
        yield from page["rows"]
        offset += page["count"]
        if page["count"] < 2000:
            break


FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(v: Any) -> Any:
    """OWASP CSV-injection defence: a text cell that starts with = + - @ (or a tab or carriage return) is prefixed with
    a single quote, so a spreadsheet shows it as text instead of evaluating it. Scraped values are attacker-controlled
    (`=HYPERLINK(...)`, `@SUM(...)`). Numbers are written as numbers. `raw=True` on an export turns this off."""
    return "'" + v if isinstance(v, str) and v.startswith(FORMULA_LEADS) else v


def _cell(v: Any) -> Any:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def _parquet_schema(p: Project, cols: list[str], **kw):
    """One pass over the rows to settle each column's type (bool, int64, float64 or string), so the second, writing
    pass can stream batches under a fixed schema. Memory stays at one query page."""
    import pyarrow as pa
    kinds: dict[str, set] = {c: set() for c in cols}
    for r in _iter_all(p, **kw):
        for c in cols:
            v = r.get(c)
            if v is not None:
                kinds[c].add("bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float" if isinstance(v, float) else "str")
    def typ(k: set):
        if k == {"bool"}:
            return pa.bool_(), bool
        if k == {"int"}:
            return pa.int64(), int
        if k and k <= {"int", "float"}:
            return pa.float64(), float
        return pa.string(), None
    types = {c: typ(kinds[c]) for c in cols}
    return pa.schema([(c, types[c][0]) for c in cols]), types


def export(p: Project, fmt: str = "csv", path: str | None = None, raw: bool = False, **kw) -> dict:
    """Write the records to a file, streaming: rows go out one query page (2000) at a time, never all in memory.
    CSV cells that a spreadsheet would evaluate are neutralised (`csv_safe`) unless `raw`; JSONL and Parquet are
    written unchanged."""
    fmt = fmt.lower()
    if fmt not in ("csv", "jsonl", "parquet"):
        raise ValueError("format: csv | jsonl | parquet")
    cols = [*templates.output_columns(p.template), *META]
    target = Path(path) if path else p.root / "exports" / f"{p.name}.{ 'parquet' if fmt == 'parquet' else fmt}"
    target.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    if fmt == "csv":
        with open(target, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[*cols, "extra"], extrasaction="ignore")
            w.writeheader()
            cell = _cell if raw else (lambda v: csv_safe(_cell(v)))
            for r in _iter_all(p, **kw):
                w.writerow({k: cell(r.get(k)) for k in [*cols, "extra"]})
                n += 1
    elif fmt == "jsonl":
        with open(target, "w", encoding="utf-8") as f:
            for r in _iter_all(p, **kw):
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
                n += 1
    else:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise RuntimeError("Parquet export needs pyarrow: pip install 'harvest[parquet]'") from exc
        allc = [*cols, "extra"]
        schema, types = _parquet_schema(p, allc, **kw)

        def conv(c, v):
            v = _cell(v)
            if v is None:
                return None
            cast = types[c][1]
            return cast(v) if cast else (v if isinstance(v, str) else str(v))
        batch: list[dict] = []
        with pq.ParquetWriter(target, schema) as wr:
            for r in _iter_all(p, **kw):
                batch.append({c: conv(c, r.get(c)) for c in allc})
                n += 1
                if len(batch) >= 2000:
                    wr.write_table(pa.Table.from_pylist(batch, schema=schema))
                    batch = []
            if batch or n == 0:
                wr.write_table(pa.Table.from_pylist(batch, schema=schema))
    return {"path": str(target), "format": fmt, "rows": n, **({"raw": bool(raw)} if fmt == "csv" else {})}


def to_csv_text(p: Project, raw: bool = False, **kw) -> str:
    cols = [*templates.output_columns(p.template), *META]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in _iter_all(p, **kw):
        w.writerow({k: (_cell(r.get(k)) if raw else csv_safe(_cell(r.get(k)))) for k in cols})
    return buf.getvalue()
