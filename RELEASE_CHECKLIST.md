# Release checklist

harvest-ai is published to PyPI from GitHub Actions (`.github/workflows/release.yml`) when a tag `vX.Y.Z` is pushed,
through **trusted publishing** (OIDC; no token is stored).

## One-time setup (operator)

1. **GitHub environment**: repository settings → Environments → `pypi`; add yourself as a required reviewer and
   restrict it to tags `v*`.
2. **PyPI pending trusted publisher**: pypi.org → Your account → Publishing → Add a new pending publisher → GitHub:
   project `harvest-ai`, owner `tonyyang0504`, repository `harvest`, workflow `release.yml`, environment `pypi`.

## Release

1. CI green on `main` (ruff, tests on 3.11/3.12, Postgres, browser, UI end-to-end, bubblewrap sandbox, CodeQL, secret
   scan).
2. Same version in `pyproject.toml`, `src/harvest_ai/__init__.py` and `.claude-plugin/plugin.json` (the workflow checks
   the tag against all three); `CHANGELOG.md` "Unreleased" moved under the version.
3. `git tag -s v0.1.0 -m "harvest-ai 0.1.0" && git push origin v0.1.0`, then approve the `pypi` deployment.
4. On a clean machine: `pipx install harvest-ai` (or `uvx --from harvest-ai harvest templates`) lists the templates, and
   `harvest-mcp` starts over stdio.
5. Optional: build and push the container image (`docker build -t <registry>/harvest-ai:0.1.0 .`); no registry is
   configured in this repository.
