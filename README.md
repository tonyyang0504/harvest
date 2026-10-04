# harvest

**harvest-ai** on PyPI (the command is `harvest`; `harvest-ai` is an alias; the Python package is `harvest_ai`).

Give it a **target** and **regions**, for example "used cars" in KZ and GE, or "rental apartments"
in PT and ES. harvest then does the rest:

1. **Census.** Agents find every website in those regions that carries the target. They search
   each discovery angle in every local language. A completeness critic and an adversarial audit
   check the result. Every source needs evidence.
2. **Lanes.** harvest decides how each source may be collected: an API through a configured MCP
   server, OpenAPI, a feed or sitemap, embedded JSON, plain HTML, or a headless browser. If robots.txt
   or the terms forbid collection, or the site needs a login, a captcha or a paywall, the lane is
   `none`.
3. **Scrapers.** An agent writes one `fetch(page, *, http, ctx)` module per source from a template.
   A scripted **review gate** checks it: compile and AST policy lint, a sandboxed page-1 fetch and
   schema validation. The verdict is bound to the module's sha256.
4. **Collection.** harvest fetches politely: one request at a time per host, rate limits, backoff
   that escalates on 429, a per-URL budget and robots.txt. It tracks pagination and completeness.
5. **Normalisation.** Numbers are parsed per locale (万 / 만 / lakh / crore included). Currencies
   are detected and converted with FX. Units and rent or salary periods are converted. Duplicates
   are removed within and across sources, sanity bounds are applied, and failing rows go to a
   quarantine table.
6. **Storage.** SQLite by default; Postgres via a DSN. A partial fetch never wipes a source's data.
7. **Autonomy.** A cron or systemd schedule, or `harvest daemon`, runs `harvest run --due`. A watchdog
   raises alerts. Repair agents can be dispatched; this is off by default and uses an explicit tool
   allowlist.

The same core is exposed four ways:

| Form | Entry |
|---|---|
| MCP server | `harvest-mcp` (stdio) or `harvest mcp --http` |
| CLI + scheduler | `harvest …`, `harvest daemon` |
| Web app + API | `harvest web` (FastAPI + a small frontend) |
| Claude Code plugin | `.claude-plugin/`, skill `harvest`, agents, `/harvest` command |

## Quick start

```bash
pipx install "harvest-ai[web,parquet]"     # or: uv tool install "harvest-ai[web,parquet]"
# from source instead: git clone https://github.com/tonyyang0504/harvest && cd harvest
#   && uv venv && uv pip install -e ".[web,parquet]" && source .venv/bin/activate
harvest new used-cars --target "used cars" --regions KZ,GE --record-type vehicles
harvest census-plan used-cars              # angles and queries per region and language
harvest agent used-cars census             # headless census agent (claude -p, explicit tool allowlist)
harvest detect-lane used-cars              # lanes for every candidate
harvest agent used-cars build kolesa_kz    # agent writes sources/kolesa_kz.py
harvest review used-cars kolesa_kz         # the gate: lint + sandboxed fetch + schema
harvest enable used-cars kolesa_kz
harvest run used-cars
harvest query used-cars --filters '{"price_report": {"lte": 15000}}'
harvest export used-cars --format parquet
harvest schedule used-cars                 # cron + systemd snippets (not installed)
harvest web                                # http://127.0.0.1:8080, admin token printed at start
```

Read [docs/harvest.md](docs/harvest.md) for the full guide: architecture, record templates, the
scraper contract, the policies, configuration, the web API and two end-to-end examples.

Agent steps (`harvest agent ...`) run a coding-agent CLI headless (Claude Code by default, `HARVEST_AGENT_CLI` for
another) with an explicit tool allowlist; everything else works without one.

## Limits (honest list)

- **Collection is only as allowed as the site says.** harvest reads robots.txt and looks for terms that forbid
  automated collection with a keyword heuristic; it does not give legal advice. Review the verdicts for sources that
  matter, and respect each site's terms and local law (personal data in particular).
- **No circumvention.** A bot challenge, a login, a captcha or a paywall makes the lane `none`; harvest never works
  around them. Many large portals block datacenter IPs; an operator may opt in to a residential-proxy route for plain
  IP blocks only, per source, with a recorded reason.
- **Agent-written scrapers.** Modules pass a review gate and run in a sandbox (bubblewrap on Linux; a policy layer
  elsewhere), but they are still code written by a model: review what you enable.
- **Coverage.** The census finds what search and the agents find; completeness checks reduce, not remove, gaps. The
  browser lane is page 1 only. One project has one record type.
- **Normalisation** covers the locales, currencies, units and date formats in the test suite; unknown formats are
  kept as cleaned text or quarantined, not guessed.

## Development

```bash
uv venv -p 3.12 && uv pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md) (DCO sign-off), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md),
[SECURITY.md](SECURITY.md) and [CHANGELOG.md](CHANGELOG.md).

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
