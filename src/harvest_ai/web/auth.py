"""Auth for the web app and API. One admin token for now, behind an interface a multi-user
backend can replace later (`Principal` + `authenticate`).

Token source: `HARVEST_ADMIN_TOKEN`; when unset a random token is generated once and stored in
`$HARVEST_HOME/admin_token` (mode 0600) and printed at startup. Clients send
`Authorization: Bearer <token>` (the bundled frontend keeps it in localStorage).
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

from ..project import home


@dataclass
class Principal:
    name: str
    role: str  # "admin" for now; roles are where per-user permissions will hang


def admin_token() -> str:
    tok = os.environ.get("HARVEST_ADMIN_TOKEN")
    if tok:
        return tok
    path = home() / "admin_token"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def authenticate(request: Request) -> Principal:
    header = request.headers.get("authorization") or ""
    given = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-harvest-token", "")
    expected = request.app.state.admin_token
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="missing or wrong admin token", headers={"WWW-Authenticate": "Bearer"})
    return Principal(name="admin", role="admin")
