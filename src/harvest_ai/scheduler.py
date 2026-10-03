"""Scheduling: which sources are due, plus cron and systemd snippets that call `harvest run --due`.

The snippets are written to `<project>/deploy/` and returned; nothing is installed automatically.
A timer ticks hourly; `--due` decides per source from its cadence and its last run, so one timer
serves every cadence.
"""

from __future__ import annotations

import datetime as dt
import os
import shlex
import shutil
import sys

from .project import Project, cadence_hours, home

SKEW = dt.timedelta(minutes=5)


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts)
    except ValueError:
        return None


def is_due(p: Project, src: dict, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(dt.timezone.utc)
    hours = src.get("cadence_hours")
    hours = float(hours) if hours not in (None, "") else cadence_hours(p.spec.cadence)
    if hours <= 0:
        return False  # manual
    # a run stamped in the future (a worker whose clock ran ahead, then was stepped back) is ignored: before this, it kept
    # every source "not due" for as long as the clock had been ahead (soak 2026-10: +2 h -> no collection for ~2 h)
    last = p.store.last_finished_run(p.name, src["id"], before=(now + SKEW).isoformat())
    if not last:
        return True
    started = _parse(last.get("started_at"))
    if started is None:
        return True
    return now - started >= dt.timedelta(hours=hours) - dt.timedelta(minutes=5)


def next_due(p: Project, src: dict) -> str | None:
    hours = src.get("cadence_hours")
    hours = float(hours) if hours not in (None, "") else cadence_hours(p.spec.cadence)
    if hours <= 0:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    last = p.store.last_finished_run(p.name, src["id"], before=(now + SKEW).isoformat())
    started = _parse(last.get("started_at")) if last else None
    if started is None:
        return "now"
    return (started + dt.timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _harvest_cmd() -> str:
    exe = shutil.which("harvest")
    return exe or f"{sys.executable} -m harvest_ai.cli"


def generate(p: Project, *, kind: str = "both", user: str | None = None, cadence: str | None = None) -> dict:
    """Write deploy snippets. kind: cron | systemd | both. `cadence` updates the project default."""
    if kind not in ("cron", "systemd", "both"):
        raise ValueError("kind: cron | systemd | both")
    if cadence:
        cadence_hours(cadence)
        p.spec.cadence = cadence
        p.save()
    cmd = _harvest_cmd()
    hh = shlex.quote(str(home()))
    log = shlex.quote(str(p.root / "deploy" / "harvest.log"))
    p.deploy_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    if kind in ("cron", "both"):
        cron = (f"# harvest: {p.name} — hourly tick; each source runs when its cadence ({p.spec.cadence}) is due\n"
                f"7 * * * * HARVEST_HOME={hh} {cmd} run {p.name} --due >> {log} 2>&1\n"
                f"37 * * * * HARVEST_HOME={hh} {cmd} watchdog {p.name} >> {log} 2>&1\n")
        path = p.deploy_dir / "harvest.cron"
        path.write_text(cron, encoding="utf-8")
        files["cron"] = {"path": str(path), "content": cron, "install": f"crontab -l | cat - {path} | crontab -"}
    if kind in ("systemd", "both"):
        u = user or os.environ.get("USER") or "harvest"
        service = f"""[Unit]
Description=harvest: run due sources of {p.name}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={u}
Environment=HARVEST_HOME={home()}
ExecStart={cmd} run {p.name} --due
ExecStartPost=-{cmd} watchdog {p.name}
TimeoutStartSec={max(3600, p.spec.time_budget_s * 4)}
Nice=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={home()}
"""
        timer = f"""[Unit]
Description=harvest: hourly tick for {p.name}

[Timer]
OnCalendar=hourly
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
"""
        sp, tp = p.deploy_dir / f"harvest-{p.name}.service", p.deploy_dir / f"harvest-{p.name}.timer"
        sp.write_text(service, encoding="utf-8")
        tp.write_text(timer, encoding="utf-8")
        files["systemd"] = {"service": str(sp), "timer": str(tp), "service_content": service, "timer_content": timer,
                            "install": f"sudo cp {sp} {tp} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now harvest-{p.name}.timer"}
    due = [{"source_id": s["id"], "next_due": next_due(p, s)} for s in p.store.list_sources(p.name, enabled=True)]
    return {"project": p.name, "cadence": p.spec.cadence, "files": files, "sources": due,
            "note": "Snippets only; nothing was installed. `harvest daemon` is the in-process alternative (used by docker compose)."}
