# Contributing to harvest

Thank you for helping. Useful contributions: normalisation for more locales, currencies and units; record templates;
lane detection and policy fixes; sandbox hardening; docs; bug reports with a reproducible public page.

## Development setup

```bash
git clone https://github.com/tonyyang0504/harvest && cd harvest
uv venv -p 3.12 && uv pip install -e ".[dev]"
```

Optional: `uv pip install -e ".[dev,browser]" && .venv/bin/playwright install chromium` for the browser lane and the
web UI end-to-end tests; `bubblewrap` on Linux for the module sandbox; a Postgres DSN in `HARVEST_TEST_PG_DSN` to run
the suite against Postgres.

## The gates (CI runs the same)

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
HARVEST_SANDBOX=bwrap .venv/bin/pytest -q      # Linux with bubblewrap: every module under the sandbox
```

## Rules

- The review gate and the sandbox are the security boundary: any change to `review.py`, `sandbox.py`, `http.py`
  (address checks) or `agents.py` (tool allowlists) comes with a test in `tests/test_security_*.py`.
- Never commit scraped data, credentials, proxy lists or recorded pages that contain personal data. Test sites are
  local fixtures (`tests/localsite.py`, `tests/uisite.py`).
- Collection stays polite and honest: no change may work around a bot challenge, login, captcha or paywall.
- Add a line to `CHANGELOG.md` under "Unreleased" for anything user-visible.

## Developer Certificate of Origin (DCO)

Every commit must be signed off, certifying the [Developer Certificate of Origin 1.1](https://developercertificate.org/):
you wrote the change or have the right to submit it under the project's licence (Apache-2.0). Use `git commit -s`,
which adds `Signed-off-by: Your Name <you@example.com>`; a GitHub `noreply` address is fine. Forgot?
`git commit --amend -s` or `git rebase --signoff main`.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Security issues go through
[SECURITY.md](SECURITY.md), never a public issue.
