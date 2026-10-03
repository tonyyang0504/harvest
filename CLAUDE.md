# harvest: notes for coding agents

Read `README.md` and `docs/harvest.md` first. The distribution is `harvest-ai`, the import package `harvest_ai`, the
command `harvest`.

## Gate commands (safe to run; CI runs the same)

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
git status / git diff
```

Personal permission allowlists belong in `.claude/settings.local.json` (git-ignored), not in a committed settings file.

## Rules

- The review gate (`review.py`) and the sandbox (`sandbox.py`) are the security boundary; changes need a security test.
- Never work around a bot challenge, login, captcha or paywall; never commit scraped data, credentials or proxy lists.
- Commits are signed off (`git commit -s`, DCO).
