"""Projects: the spec a user gives (target, regions, record type, fields ...) and where its state lives.

Layout under `HARVEST_HOME` (default `~/.harvest`):
    projects/<name>/project.json     the spec
    projects/<name>/harvest.db       SQLite store (unless `store_dsn` points at Postgres)
    projects/<name>/sources/<id>.py  one scraper module per source (agent-written, review-gated)
    projects/<name>/fx.json          FX table {"base": "USD", "as_of": "...", "rates": {...}}
    projects/<name>/deploy/          generated cron / systemd snippets (never installed automatically)
    projects/<name>/agents/          headless agent job logs and hand-off files
"""

from __future__ import annotations

import importlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import regions, templates
from .db import Store, now_iso
from .normalize import FxTable

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
CADENCES = {"hourly": 1, "6h": 6, "12h": 12, "daily": 24, "weekly": 168, "monthly": 720, "manual": 0}


def home() -> Path:
    return Path(os.environ.get("HARVEST_HOME") or (Path.home() / ".harvest")).expanduser()


def cadence_hours(c: str | int | float | None) -> float:
    if c is None or c == "":
        return 24.0
    if isinstance(c, (int, float)):
        return float(c)
    s = str(c).strip().lower()
    if s in CADENCES:
        return float(CADENCES[s])
    m = re.fullmatch(r"(?:every\s*)?(\d+(?:\.\d+)?)\s*(h|hours?|d|days?|w|weeks?)", s)
    if not m:
        raise ValueError(f"cadence {c!r}: use hourly, 6h, 12h, daily, weekly, monthly, manual or '<n>h|d|w'")
    n = float(m.group(1))
    return n * {"h": 1, "d": 24, "w": 168}[m.group(2)[0]]


@dataclass
class Spec:
    name: str
    target: str
    record_type: str
    regions: list[str]
    region_codes: list[str] = field(default_factory=list)
    fields: list | dict = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    max_sources: int = 30
    cadence: str = "daily"
    report_currency: str = "USD"
    store_dsn: str | None = None
    prune_after_days: float = 14
    max_pages: int = 20
    time_budget_s: int = 900
    rate_s: float = 2.0
    proxy: dict = field(default_factory=dict)  # residential-proxy route: operator decision, caps, mode (see proxy.py)
    ua: dict = field(default_factory=dict)  # browser user-agent mode: operator decision (see uamode.py)
    created_at: str | None = None

    @classmethod
    def build(cls, **kw) -> "Spec":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(kw) - known
        if unknown:
            raise ValueError(f"unknown spec keys {sorted(unknown)}")
        s = cls(**kw)
        s.validate()
        return s

    def validate(self) -> None:
        if not NAME_RE.match(self.name or ""):
            raise ValueError("project name: lowercase letters, digits, '-' or '_', 2-63 characters")
        if not (self.target or "").strip():
            raise ValueError("target is required (what to collect, e.g. 'used cars')")
        self.record_type = templates.canonical_type(self.record_type)
        if isinstance(self.regions, str):
            self.regions = [r.strip() for r in re.split(r"[,;]", self.regions) if r.strip()]
        self.region_codes = regions.expand(self.regions)
        if isinstance(self.fields, str):
            self.fields = [f.strip() for f in self.fields.split(",") if f.strip()]
        templates.get(self.record_type, self.fields)  # validates extra fields
        if not self.languages:
            self.languages = regions.languages_for(self.region_codes)
        if isinstance(self.languages, str):
            self.languages = [x.strip() for x in self.languages.split(",") if x.strip()]
        self.max_sources = max(1, min(int(self.max_sources), 1000))
        cadence_hours(self.cadence)
        self.report_currency = (self.report_currency or "USD").upper()
        self.max_pages = max(1, min(int(self.max_pages), 10000))
        self.time_budget_s = max(10, int(self.time_budget_s))
        self.rate_s = max(0.0, float(self.rate_s))


class Project:
    def __init__(self, spec: Spec, root: Path):
        self.spec = spec
        self.name = spec.name
        self.root = root
        self._store: Store | None = None

    # ------------------------------------------------------------------ paths
    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    def module_path(self, sid: str) -> Path:
        return self.sources_dir / f"{sid}.py"

    @property
    def deploy_dir(self) -> Path:
        return self.root / "deploy"

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def dsn(self) -> str:
        return self.spec.store_dsn or os.environ.get("HARVEST_STORE_DSN") or f"sqlite:///{self.root / 'harvest.db'}"

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.dsn)
        return self._store

    @property
    def template(self) -> dict:
        return templates.get(self.spec.record_type, self.spec.fields)

    # ------------------------------------------------------------------ fx
    def fx(self) -> FxTable:
        hook = os.environ.get("HARVEST_FX_PROVIDER")
        if hook:
            mod, _, fn = hook.partition(":")
            data = getattr(importlib.import_module(mod), fn)()
            return FxTable.from_json(data) if isinstance(data, dict) and "rates" in data else FxTable(data)
        for p in (self.root / "fx.json", Path(os.environ.get("HARVEST_FX_FILE", "/nonexistent"))):
            if p.is_file():
                return FxTable.from_json(json.loads(p.read_text(encoding="utf-8")))
        return FxTable()

    def set_fx(self, rates: dict, base: str = "USD", as_of: str | None = None) -> dict:
        import math
        import re
        if not re.fullmatch(r"[A-Za-z]{3}", str(base or "")):
            raise ValueError("base must be an ISO-4217 code")
        for k, v in (rates or {}).items():
            # a zero, negative or non-finite rate would turn every *_report amount (and the USD sanity bounds) into garbage
            if not re.fullmatch(r"[A-Za-z]{3}", str(k)) or isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                raise ValueError(f"FX rate {k!r}: {v!r} must be a 3-letter code with a finite rate > 0")
        t = FxTable(rates, base, as_of or now_iso())
        (self.root / "fx.json").write_text(json.dumps(t.to_dict(), indent=1), encoding="utf-8")
        return t.to_dict()

    # ------------------------------------------------------------------ persistence
    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for d in (self.sources_dir, self.deploy_dir, self.agents_dir):
            d.mkdir(exist_ok=True)
        (self.root / "project.json").write_text(json.dumps(asdict(self.spec), indent=1, ensure_ascii=False), encoding="utf-8")

    def to_dict(self) -> dict:
        """The spec for display (API, MCP, CLI). A store DSN's password never leaves project.json."""
        d = asdict(self.spec)
        if d.get("store_dsn"):
            d["store_dsn"] = redact_dsn(d["store_dsn"])
        d["root"] = str(self.root)
        d["store"] = "postgres" if self.dsn.startswith("postgres") else "sqlite"
        return d

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None


def redact_dsn(dsn: str) -> str:
    """`postgresql://user:pw@host/db` -> `postgresql://user:***@host/db`; also `password=...` key/value DSNs."""
    import re
    s = re.sub(r"(?i)^([a-z][a-z0-9+.-]*://[^:/@\s]*):[^@\s]*@", r"\1:***@", str(dsn))
    return re.sub(r"(?i)\b(password\s*=\s*)('[^']*'|\S+)", r"\1***", s)


def create(**kw) -> Project:
    spec = Spec.build(**kw)
    root = home() / "projects" / spec.name
    if (root / "project.json").exists():
        raise ValueError(f"project {spec.name!r} already exists")
    spec.created_at = now_iso()
    p = Project(spec, root)
    p.save()
    p.store  # noqa: B018 - create the tables
    return p


_OPEN: dict[str, Project] = {}


def load(name: str) -> Project:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"bad project name {name!r}")
    root = home() / "projects" / name
    key = str(root)
    if key in _OPEN:
        return _OPEN[key]
    f = root / "project.json"
    if not f.is_file():
        raise LookupError(f"no project {name!r} under {home()}")
    data = json.loads(f.read_text(encoding="utf-8"))
    spec = Spec(**{k: v for k, v in data.items() if k in Spec.__dataclass_fields__})
    p = _OPEN[key] = Project(spec, root)
    return p


def list_projects() -> list[dict]:
    out = []
    base = home() / "projects"
    for f in sorted(base.glob("*/project.json")) if base.exists() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"name": d["name"], "target": d.get("target"), "record_type": d.get("record_type"), "regions": d.get("regions"), "cadence": d.get("cadence")})
        except (ValueError, KeyError):
            continue
    return out


def forget_cache() -> None:
    for p in _OPEN.values():
        p.close()
    _OPEN.clear()
