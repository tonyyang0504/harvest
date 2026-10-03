# harvest: the guide

harvest turns a *target* and *regions* into a maintained dataset. Agents do the open-ended work:
finding the sites and writing the scrapers. harvest does everything that must be deterministic:
the evidence rules, domain dedup, lane and policy detection, the review gate, polite fetching,
normalisation, storage, scheduling and health checks.

## Architecture

```
                     +--------------- harvest_ai.service (the core API) --------------+
 MCP server  ------> | projects  census  lanes  scaffold  review  runner  export ...  | <------ web app (FastAPI + SPA)
 CLI / daemon -----> |                                                                | <------ Claude Code plugin (via MCP)
                     +--------+------------+-----------+-----------+-------------------+
                              |            |           |           |
                         regions.py    domains.py   http.py     normalize.py      db.py (SQLite | Postgres)
                         templates.py  robots.py    extract.py  sandbox.py        scheduler.py  watchdog.py  agents.py
```

Every entry point is a thin wrapper over `harvest_ai.service`, so the same function and the same
rules run whether a call comes from an MCP client, the CLI, the web API or the scheduler.

| Module | Role |
|---|---|
| `regions.py` | ISO-3166 alpha-2 table (name, currency, languages), named groups (EU, GCC, CAUCASUS, LATAM …), aliases and country names |
| `domains.py` | Registrable domains that respect the public-suffix list (built-in subset; the full PSL via `HARVEST_PSL_FILE`) |
| `templates.py` | Record-schema templates: fields, types, units, currency and period rules, bounds, dedup keys, category conflicts |
| `project.py` | Project spec and on-disk layout |
| `census.py` | Plan (region × angle × language), evidence-checked candidates, domain dedup and merge, gap report and dry streak |
| `lanes.py`, `mcp_client.py` | Lane detection, the robots and terms policy, the optional generic MCP-server lane |
| `http.py`, `robots.py` | The polite client and an RFC 9309 robots parser |
| `extract.py` | A dependency-free HTML DOM, JSON-LD, Next/Nuxt data, `window.*` state, feeds, sitemaps |
| `scaffold.py` | The scraper contract and one starting template per lane |
| `review.py`, `sandbox.py` | The review gate and the sandboxed child process that runs every module |
| `runner.py` | Walks, completeness, streaming normalise and store, the never-wipe rule, run locks |
| `normalize.py` | Locale-aware parsing, currency and FX, units, periods, dates, enums, bounds, quarantine, fingerprints |
| `db.py` | Storage with sources, reviews, runs, records, quarantine, alerts, jobs, census_rounds and repairs tables |
| `scheduler.py`, `watchdog.py`, `agents.py` | Autonomy: due logic, cron and systemd snippets, health findings and alerts, headless agent jobs |

### Project layout (`$HARVEST_HOME`, default `~/.harvest`)

```
projects/<name>/project.json      the spec
projects/<name>/harvest.db        SQLite store (unless store_dsn / HARVEST_STORE_DSN points at Postgres)
projects/<name>/sources/<id>.py   one scraper module per source
projects/<name>/fx.json           FX table
projects/<name>/deploy/           generated cron / systemd snippets
projects/<name>/agents/           agent job logs, the MCP config handed to agents
projects/<name>/exports/          default export location
projects/<name>/alerts.jsonl      alert log
```

## 1. Project spec

| Key | Meaning |
|---|---|
| `name` | `[a-z0-9_-]`, 2–63 characters |
| `target` | What to collect, in plain words ("used cars", "rental apartments") |
| `regions` | ISO alpha-2 codes, country names or groups; `GLOBAL` for international sites |
| `record_type` | `vehicles`, `real_estate_sale`, `real_estate_rent`, `rentals`, `jobs`, `products`, `events`, `businesses`, `generic` (aliases: cars, rent, ecommerce, leads …) |
| `fields` | Extra fields wanted (strings unless typed: `{"visa": {"type": "boolean"}}`) |
| `languages` | Defaults to the regions' languages; drives local-language census queries |
| `max_sources`, `max_pages`, `time_budget_s`, `rate_s` | Budgets: sources per project, pages per walk, seconds per walk, seconds between requests to one host |
| `cadence` | `hourly`, `6h`, `12h`, `daily`, `weekly`, `monthly`, `manual` or `<n>h/d/w`; a source may override it |
| `report_currency` | Every money field gets `<field>_report` in this currency when the FX table has both rates |
| `prune_after_days` | Rows unseen for this long are pruned after walks that were not complete |

### Record templates

Each template defines its fields with types (`string`, `text`, `integer`, `number`, `money`,
`currency`, `area`, `distance`, `weight`, `date`, `datetime`, `url`, `enum`, `boolean`, `list`,
`period`, `country`, `object`), required flags and canonical units: area in m², distance in km and
weight in kg. A template also carries:

- **Currency rules.** Every `money` field names its `currency_field`. The currency comes from an ISO code or a symbol in the
  text. Region-ambiguous symbols (`$`, `¥`, `kr`, `Rs`) resolve to the region's currency; if there is no symbol, the region's currency is used.
- **Period rules.** `real_estate_rent` normalises `price` per `rent_period` into `price_per_month`. `rentals` gives `price_per_day`, and
  `jobs` gives `salary_min_annual` and `salary_max_annual`. A period quoted inside the price text ("950 €/mês") is picked up.
- **Sanity bounds.** Numeric `min`/`max` limits, conditional bounds (`area` of an apartment ≤ 2,000 m²) and USD bounds applied after FX
  (a vehicle priced under 100 USD is quarantined).
- **Dedup keys** across sources (vehicles: VIN, then make+model+year+mileage+price; products: GTIN, then brand+SKU …).
- **Category conflicts.** A sale ad inside a rent category, or a rent-to-own ad, goes to quarantine and is never relabelled.

`harvest templates <type>` prints a template in full.

## 2. Census

`harvest_census_plan` gives, for each region, its languages, currency, ccTLD and search queries for
every **angle**:

- classifieds
- marketplaces
- vertical portals
- aggregators (their own listings only)
- official and government sources (registries, statistics, open data, tenders)
- operators (dealer groups, agencies, chains, employers)
- local-language queries in every script
- API platforms

The census agent searches and **verifies**, then records candidates:

```json
{"url": "https://kolesa.kz/cars/", "name": "Kolesa", "regions": ["KZ"], "angle": "classifieds",
 "evidence": [{"url": "https://kolesa.kz/cars/", "observation": "page 1: 20 used-car ads with KZT prices"}]}
```

Rules enforced by `harvest_census_add`:

- Evidence must include a URL on the candidate's own registrable domain, with an observation.
- Candidates are deduplicated by registrable domain (`m.kolesa.kz` = `kolesa.kz`; `example.co.uk` is one site).
  A repeat merges regions, angles and evidence. Shared hosts are keyed by owner instead: the path owner on
  `raw.githubusercontent.com/<owner>/<repo>`, `github.com/<owner>`, `buttondown.com/<list>`, `medium.com/<user>` …, and the
  subdomain on `substack.com`, `wordpress.com`, `ghost.io` …, so two newsletters or datasets never merge.
- `mirror_of` candidates are rejected.
- Regions must belong to the project.
- `max_sources` caps the registry.

**Budget and continuation.** Once `max_sources` collectable sources are registered, more verified candidates are
*deferred* with their evidence, not dropped. `harvest census-resume <p> --max-sources N` does three things
(also available as `harvest_census_resume` or `POST /api/projects/{p}/census/resume`):

- raises the budget;
- re-adds the deferred candidates;
- returns a continuation brief: the covered cells, the known domains and the empty cells.

With `--agent`, it also queues a census agent that searches only the remaining empty cells; when the re-added
deferred candidates already fill the new budget, the agent is skipped with the reason (it could only defer what it
finds). Under `HARVEST_JOB_MODE=inline` the CLI waits for that agent (`--wait` does so in queue mode). Blocked and
lane-`none` sources never count against the budget.

A source is a **maintained listing** (board, directory, register, search page, feed or API). The census and audit
briefs exclude one-off articles, listicles, PDFs, press releases and API documentation, and a dead domain is not
recorded as `blocked`. Plan queries are written per record type (employers for jobs, organisers for events,
installers and registers for businesses …).

Each call is a **round** (`angle:classifieds:KZ`, `critic:r2`, `audit:r1`). `harvest_census_gaps` returns the
region × angle matrix, the empty cells and the **dry streak**. The critic loops until two rounds in a row add
nothing. Then an **independent auditor** agent tries to prove the census incomplete and records the terms
verdicts it read itself.

## 3. Lanes and policy

`harvest_detect_lane` probes each source through the polite client, and the first match wins:

| Lane | Signal |
|---|---|
| `mcp` | A configured MCP server serves the domain (declared in `domains`, or its lookup tool names it) |
| `openapi` | An OpenAPI/Swagger document linked from the page or at a well-known path |
| `json_api` | The source URL answers JSON records with no key or login (the site's own list API, Shopify `products.json`, WooCommerce Store API, WordPress REST, a JSON file). Builders record it when they build on such an endpoint |
| `feed` | The source URL is itself a feed, or a `<link rel=alternate>` RSS / Atom / JSON Feed that carries this page's inventory: the feed of the listing path (Shopify `/collections/x.atom`) or one whose items the page links (≥ 30 %). A site-wide blog feed is not inventory (`signals.feed_unrelated`) |
| `embedded_json` | `__NEXT_DATA__`, Nuxt data, `window.__INITIAL_STATE__`-style JSON, or JSON-LD item types. For `businesses`, JSON-LD business entities count only when at least two are not the site describing itself |
| `sitemap` | A sitemap listing pages (from robots.txt or `/sitemap.xml`) when nothing richer exists |
| `html` | Server-rendered content |
| `browser` | A JavaScript shell. Needs Playwright (`pip install 'harvest[browser]'`) and is always limited to page 1. Chromium runs in the parent and connects through harvest's local filtering proxy (`egress.py`), which resolves each host once and connects only to a public address it vetted (DNS pinning) |
| `none` | robots.txt disallows the path (or is unreachable: 5xx, timeout, connection error); the terms forbid automated collection; a login, captcha, paywall or block (including a bot-wall interstitial served with HTTP 200, e.g. Imperva/Incapsula) |

Terms are found from the page's links (terms, conditions, AGB, termos, условия, 利用規約, 이용약관 …), or from the
site's home page when the source URL is a JSON endpoint or a feed. They are classified by prohibition patterns in
English, German, Dutch, Spanish, Portuguese, French, Italian, Polish, Turkish, Russian, Indonesian/Malay, Arabic,
Japanese, Chinese and Korean.

- The heuristic returns `forbids`, `no_clause` or `unknown`. A terms page that is a PDF or other binary document,
  or an unrendered JavaScript shell, is `unknown`: it was not read, so nothing was found.
- robots.txt follows RFC 9309: every group naming harvest's product token (exact match), or else every `*` group,
  applies, merged (many sites repeat `User-agent: *` further down the file).
- An agent or operator records a reviewed verdict with the terms URL through `harvest_record_policy`.
- A `forbids` verdict or a robots disallow always forces lane `none`, and a lane can never be made more permissive than that.

**harvest never logs in, solves captchas, gets past paywalls or uses mirrors.** A block is recorded as a finding.

### Optional MCP-server lane

Any MCP server can serve as an API lane, including platform API servers you already use. Configure it in
`$HARVEST_HOME/mcp_servers.json` (or `HARVEST_MCP_CONFIG`):

```json
{"servers": {"cars-api": {"command": "uvx", "args": ["some-cars-mcp"], "env": {"API_KEY": "…"},
                          "domains": ["cars.example"], "lookup": {"tool": "search_platforms", "arg": "query"}}}}
```

A module on the `mcp` lane calls `harvest_ai.mcp_client.call_tool(server, tool, args)`. Without a config the lane is skipped.

## 4. Scraper contract and the review gate

```python
def fetch(page: int, *, http, ctx) -> list[dict]:   # or {"rows": [...], "done": True} on the last page
```

- `http` is the only way out to the network. It provides `get_text`, `get_json`, `post_json` and `render`,
  and returns `None` on a block, a robots denial or an error.
- `ctx` holds the source row, the project, the template fields, the region and its currency, and a `state`
  dict that persists across the pages of one walk.
- Rows are keyed by template field names. Raw strings are fine; normalisation parses them.
- `field_map` on the source (`{"price": "offer.amount", "currency": "=EUR"}`) maps raw keys without code changes.

`harvest_template_scraper` writes a starting module for the lane. The **review gate** (`harvest_review_source`)
is mandatory before a source can be enabled:

1. **Lint.**
   - The module must compile and define `fetch(page, *, http, ctx)`.
   - An AST policy scan forbids `subprocess`, `os.system/popen/exec*/spawn*/fork`, `eval/exec/compile/__import__`,
     `importlib`, `ctypes`, raw `socket`, and direct HTTP clients (`requests`, `httpx`, `urllib.request` …).
   - Dunder escapes, `open`, `getattr` and `sys` are also forbidden, as are `os` under another name and the layers
     under it (`posix`, private `_…` modules), string-driven attribute access (`operator`, `str.format` fields such
     as `{0.x}`), file APIs other than `open` (`io`, `pathlib` …), relative imports, and every `harvest` module except
     `harvest_ai.extract` and `harvest_ai.mcp_client`.
   - ruff checks syntax and undefined names when it is installed.
2. **Smoke.** A page-1 fetch runs in a child process (the same one every production walk uses):
   - a hard timeout;
   - the module's bytes, checked against the sha256 by the parent and again by the child, arrive on stdin; the
     child never opens the module file or anything else under `$HARVEST_HOME`;
   - **no network of its own.** The module's `http` (and `harvest_ai.mcp_client`) is a stub that forwards each call
     over the pipe to the parent, whose `Http` does the request with robots, politeness, the URL gate, the
     session-header rule and the proxy route. The child never holds a proxy exit or credential;
   - a scrubbed environment, a throwaway working directory, CPU and file-size rlimits;
   - an audit hook that refuses process spawning, any socket use, native loading, writes outside the working
     directory, and reads of harvest's state (`$HARVEST_HOME`), the proxy pool file, the operator's home outside the
     interpreter's import roots, and other processes' `/proc` entries;
   - **bubblewrap** where available (Linux): a read-only filesystem except the scratch directory, `$HARVEST_HOME`,
     the secret files and the operator's home masked, its own network namespace (nothing to reach) and pid
     namespace. `HARVEST_SANDBOX=auto` (default) uses it when usable and otherwise runs the policy layer alone with a
     loud warning; `bwrap` requires it; `policy` accepts the policy layer explicitly. The review report and each
     run's `http_stats` record the `isolation` used.
   - The lint, the sha and the sandbox all see the same bytes: the module is read once per review.
3. **Schema.** The rows are normalised against the template. At least 80 % must pass. The report shows field
   coverage, quarantine reasons, parse warnings and samples.

The verdict is stored with the module's sha256. `harvest_enable_source` refuses unless the lane is not `none`,
robots allow, the terms are `allowed`/`no_clause` and the current sha has a passing review. The runner will not
run a module whose sha has changed; the source becomes `review_stale` until it is reviewed again.

## 5. Runner

- Each walk runs in the sandbox child, which is killed at `time_budget_s + 30 s`.
- Sources on one registrable domain run one after another; different domains run in parallel (`HARVEST_WORKERS`).
- A project lock file prevents overlapping runs.
- **Politeness:**
  - one in-flight request per host;
  - `rate_s` between requests, raised to the site's Crawl-delay (up to 60 s);
  - retries on 429, 5xx and network errors with backoff;
  - a 429 honours Retry-After and doubles the host's delay for the rest of the run;
  - a per-URL budget of attempts and seconds;
  - `Cookie`, `Authorization` and `Proxy-Authorization` are refused (harvest never logs in) unless the operator
    recorded a decision for that source, for a public API that wants a published key:
    `harvest session-headers enable <p> --source ID --reason "…"` or
    `POST /api/projects/{p}/sources/{id}/session-headers`. Agents have no tool for it;
  - 401/403/451 and challenge pages are never retried;
  - a URL gate refuses non-http(s) URLs, embedded credentials and any address that is not globally routable
    (including 100.64.0.0/10 and IPv6 forms that embed a private IPv4 address); direct connections re-check the
    address they actually connect to, so a DNS answer that changes after the gate (rebinding) is refused too;
  - the residential-proxy route (next section) is the only thing that ever retries a blocked request, and only an
    IP-level block, for sources the operator switched it on for.
- **Pagination.** A walk is **complete** only when it reached the natural end with no page errors and no failed or
  blocked requests. The natural end is an explicit `done`, an empty page, or two pages with nothing new.
  `max_pages`, `max_rows`, the time budget and errors make a walk incomplete.
- **Streaming.** Rows are normalised and upserted as pages arrive. Bad rows go to quarantine with reasons. The run
  row carries a heartbeat.

### Residential-proxy route

Many market leaders refuse datacenter IPs outright (plain 403, reset connections). For those, and only those,
an operator can switch on a route through rotating residential proxies (a pool you provide).

**The pool.** `HARVEST_PROXY_FILE` points at a file with one exit per line: `host:port:user:pass`, `host:port` or
`http://user:pass@host:port` (blank lines and `#` comments are skipped, malformed lines ignored). Or set
`HARVEST_PROXY_URL` to one gateway URL. The pool is a credential:

- it is read in place, only by the parent process, never copied, printed, logged or stored;
- the sandbox child gets only the few exits its walk may use, inside the job on stdin (never in its environment);
- everywhere else an exit is an id (`x` + 10 hex characters of a hash);
- every log line, event, error, exception, stderr tail and alert passes `proxy.redact`, which removes
  `user:pass@`, `host:port:user:pass`, (Proxy-)Authorization values and any credential string of the loaded pool;
- scraper modules may not touch private attributes (`http._route`), which the review lint enforces;
- headless agents never inherit `HARVEST_PROXY_*` and have no tool that switches the route on.

**The gate.** A request goes through the route only when all three hold:

| | Rule | Where |
|---|---|---|
| (a) | The project or the source is switched on by an operator decision with a reason and a date (`harvest proxy enable`). A source-level decision (on or off) wins over the project's. | `project.json` → `proxy` (with history); source → `lane_detail.proxy` (like the reviewed terms verdict in `lane_detail.policy`) |
| (b) | robots.txt allows the source and its terms verdict is `allowed`/`no_clause`. Lane detection may also run with an unread (`unknown`) verdict so the proxied probe can read the terms; `forbids`, or a lane `none` because of a login, paywall, captcha or challenge, stops it. | `proxy.eligible` |
| (c) | The direct request failed with an IP-level block: a 401/403 whose page is not a challenge, login or paywall; a connection reset/refused/dropped; or a 429 that persisted through the back-off. | `Http.request` / `Http.render` |

The route is never used for:

- captcha and bot-challenge pages: Cloudflare (`cf-mitigated`, `cf-chl`, "Just a moment…"), DataDome
  (`x-datadome`, `captcha-delivery.com`), PerimeterX (`px-captcha`, `_pxhd`), Imperva/Incapsula, Kasada,
  AWS WAF, DDoS-Guard, Sucuri, reCAPTCHA/hCaptcha/Turnstile. These stay a block and lane `none`;
- login walls, HTTP auth (401 + `WWW-Authenticate`) and paywalls (402, "subscribe to continue" …);
- any request that carries a session (`Cookie` or `Authorization` header);
- a host where a proxied response turned out to be one of the above. That host is direct-only for the rest of
  the walk.

**How a walk uses it.** Every request goes direct first. On an IP-level block the same request is retried
through the route, under the same per-host politeness (one request at a time, `rate_s`, Crawl-delay, 429
escalation). Once a host answered through the route, the rest of that walk's requests to it go through the route.
A robots.txt that is itself IP-blocked is read through the route, so the real robots rules apply (never "403 = allow
all"). The browser lane does the same with Chromium's proxy setting; images, media and fonts are not loaded through
the proxy.

**Exits.** `mode` is `sticky` (default) or `rotate`:

- `sticky` gives one stable session per `project:source` (a hash into the pool), plus two spares used only if it fails;
- `rotate` gives six healthy exits, used round-robin per request.

`country: auto` (default) pins the session to the source's region with the Webshare username suffix `-CC`;
`off` disables that, and a 2-letter code forces one country. Before an exit is handed out it is probed once per 10
minutes (`HARVEST_PROXY_PROBE_URL`, default `http://example.com/`). An exit that fails the probe, or refuses
mid-walk (402 bandwidthlimit, 407, unreachable), is cooled down for 15 minutes (`HARVEST_PROXY_COOLDOWN_S`).
The cool-downs are kept in `$HARVEST_HOME/proxy_health.json` (ids only).

**Budgets.** Per project and UTC day there is a byte cap (`daily_bytes`, default 50 MB) and a request cap
(`daily_requests`, default 1000), accounted per source in the `proxy_usage` table.

- Each walk (run, review or detection) reserves its share of what is left: the remainder divided by the proxied
  walks of the run that have not started yet. The unused part is released when it ends.
- The child books usage as pages arrive, so a killed walk is still counted.
- A request through the route starts only with at least 64 KiB of allowance left (`HARVEST_PROXY_HEADROOM`). A
  response whose announced size would overrun the allowance is not read. The cap therefore holds to within one read
  chunk, and only for chunked responses.
- When the day's cap is reached, the route stops: an IP-blocked request is then a block again (as without a proxy).
  One `proxy_budget` alert per project per day goes to the alerts table, `alerts.jsonl`, `HARVEST_ALERT_HOOK` and
  `HARVEST_ALERT_WEBHOOK`.
- Requests count top-level fetches: a browser render counts as one, and its sub-requests count in bytes.

**Surfaces.**

- CLI: `harvest proxy status [project] [--check N]`, `harvest proxy enable <project> [--source ID] --reason "…"
  [--daily-mb N] [--daily-requests N] [--mode sticky|rotate] [--country auto|off|CC]`,
  `harvest proxy disable <project> [--source ID] [--reason …]`. `run`, `review` and `detect-lane` take `--no-proxy`.
- MCP: `harvest_proxy_status`, `harvest_proxy_enable`, `harvest_proxy_disable`; `harvest_run(use_proxy=false)`.
- Web: the project overview has a "Residential proxy" panel (switch, today's bytes and requests against the caps,
  7-day usage per source), and the sources table has a per-source Proxy column with on/off toggles. Switching on
  asks for the reason.
- API: `GET /api/proxy`, `GET /api/projects/{p}/proxy`, and `POST /api/projects/{p}/proxy`
  (`{"enabled", "reason", "source_id", "daily_bytes", "daily_requests", "mode", "country"}`).
- Status: `harvest status` has a `proxy` block (decision, caps, today's usage, 7-day usage per source), and each
  source shows `via_proxy` and `proxy_today`. A run's `http_stats` carries `ip_blocked`, `proxy_fallbacks`,
  `proxy_requests`, `proxy_ok`, `proxy_bytes`, `proxy_budget_stop` and a `proxy` summary. Lane detection records
  `lane_detail.via_proxy`, `signals.proxy_trigger` and, for a direct IP block without the route, `ip_block`.

**Live check (prod, 2026-09-27).** This ran from a datacenter IP against the real 20,000-exit pool, read in place,
with harvest's honest user agent, caps of 5–8 MB and 50–80 requests per project, and 1–2 pages per source.

| Site | Direct from the datacenter | Through the route | Outcome |
|---|---|---|---|
| myauto.ge, turbo.az, idealista.pt | 403 Cloudflare challenge / captcha, DataDome | never tried | lane `none`: a challenge is never worked around |
| imovirtual.com, olx.pt, standvirtual.com | CloudFront 403 | one request each, same 403 | the block follows the user agent (a browser UA gets 200 even from the datacenter); `proxy_no_help`, lane `none` |
| olx.kz, olx.pl, olx.ro, otomoto.pl | CloudFront 403 for any UA | one request each, same 403 from in-country residential exits | not an IP-only block; `proxy_no_help` |
| donedeal.ie | 403 for any UA | 200, lane `embedded_json` (`via_proxy`) | skipped: the terms (help centre) sit behind a challenge, so the verdict stays `unknown` and runs are refused |
| yad2.co.il | Radware 403 for any UA | 200, lane `browser` (`via_proxy`) | skipped: the terms page renders into a challenge |
| kufar.by (cars) | 403 for any UA | 200, lane `embedded_json` | terms read (no automated-access clause; no public redistribution), reviewed through the route (29/30 rows valid) and run: **29 rows stored, 1 quarantined**. Page 2 was refused by robots (`*cursor=*`) even through the route |

Proxied usage across the check was 31 requests and about 2.5 MB. That includes the kufar.by run (1 request, 195 KB wire),
its review (194 KB), and one 0.8 MB browser render on yad2. A scan of all harvest state found none of the pool's
credential strings. The check found and fixed three things: a block that survives a residential exit now stops the
route for that host, a large proxied page that mentions a captcha widget counts as content, and the browser lane
waits for a client-side redirect before reading the page.

`use_proxy` is no longer a tuning knob: `harvest tune` / `harvest_update_source` refuse it and point to
`harvest proxy enable`, because the route needs a recorded decision.

### Browser user-agent mode

Some sites refuse any non-browser user agent (the live check found CloudFront rules on imovirtual.com, olx.pt and
standvirtual.com that answer 403 to harvest's UA and 200 to a browser UA, from any IP). For those an operator can
switch on the browser user-agent mode, with the same recorded-decision rule as the proxy route, decided separately
from it (the two can be combined).

- **Decision.** `harvest ua enable <project> [--source ID] --reason "…"` (and `disable`); stored with its date
  and history in `project.json` → `ua` or the source's `lane_detail.ua`. A source decision wins. MCP
  `harvest_ua_status|enable|disable`; API `GET/POST /api/projects/{p}/ua`; a panel and per-source toggles in
  the web app. `tune` / `harvest_update_source` refuse it, and agents have no tool for it.
- **Gates.** robots allow and the terms verdict is `allowed`/`no_clause` (lane detection may run with an unread
  verdict so the terms can be read; `forbids` stops it); never for a source that is lane `none` because of a login,
  paywall, captcha or challenge.
- **What changes.** Only the header set: a current stable desktop Chrome UA (`Chrome/<major>.0.0.0`, the host's
  platform; `HARVEST_BROWSER_UA` overrides) with matching `Accept` and `Accept-Language: en-US,en;q=0.9`. The
  browser lane sends the launched Chromium build's own Chrome UA (without the "Headless" marker).
- **What does not.** No fingerprint spoofing, no navigator.webdriver hiding, no captcha or challenge solving, no
  login or paywall work-arounds. A challenge page still stops the source (lane `none`).
- **robots.txt** is evaluated for harvest's own token *and* for `*`; the stricter rule (and the longer
  Crawl-delay) wins.
- **Recorded.** Runs carry `ua_mode` (`own` | `browser`, a column and in `http_stats`), reviews and lane
  detection too; `harvest status` shows the decision and each source's mode. `run`, `review` and `detect-lane`
  take `--own-ua` to force harvest's UA.

**Live re-check (prod, 2026-09-27)** of the three CloudFront user-agent blocks with the browser UA on, harvest's
own terms gate first, no proxy, 1–2 pages:

| Site | Browser UA | Terms | Outcome |
|---|---|---|---|
| imovirtual.com | 200, lane `embedded_json` | **forbids**: the rendered T&C (clientes individuais) prohibit using the content without written consent and "agregar e processar dados … disponíveis no Site" | skipped; recorded as a reviewed `forbids` verdict (lane `none`) |
| standvirtual.com | 200, lane `embedded_json` | **unknown**: the T&C (Salesforce help centre) would not render from the host | skipped; its sister site's T&C forbid aggregation |
| olx.pt | still 403 with the browser UA | unknown | not a UA-only block; lane `none` |

No rows were collected, as the gates require. The check exposed a heuristic gap: the unrendered JavaScript help-centre
pages had been classified `no_clause`. A terms page that is a JavaScript shell now classifies as `unknown`.

## 6. Normalisation and cleaning

See `normalize.py` for the full rules. The main ones:

- **Numbers.** Space, dot, comma and apostrophe grouping, and Indian grouping (`12,34,567`) are handled.
  "Single separator + 3 digits = grouping" (`1.872` → 1872; `82,32` → 82.32).
- **Multipliers.** k, m/mn/million, bn/mrd, тыс/млн/млрд, lakh and crore are supported. Compound CJK amounts
  work too: `1억 2천만`, `3億5000万円`, `3.5万`.
- **Money.** ISO codes and about 50 symbols and local words (₸, ₾, тг, лари, R$, zł, Kč, 円, 원 …) are
  recognised. FX comes from `fx.json` (`harvest fx`), `HARVEST_FX_FILE` or a provider hook
  `HARVEST_FX_PROVIDER=module:function`. No rates are built in: a missing rate leaves `_report` empty
  rather than guessing.
- **Units.** Area: m², sqft, 坪, 평, ha, acre, сотка, кв.м. Marla, perch and kanal are recognised but not converted,
  because definitions vary by region. Distance: km, miles, "150 тыс. км", 万公里. Weight: kg, lb, g, t.
- **Other fields.**
  - Dates cover ISO formats, epochs, dd.mm.yyyy (mm/dd for the US), CJK dates, month names in English, German,
    Dutch, French, Spanish, Portuguese, Italian, Polish and Indonesian/Malay ('Tue, 14 Oct 2026, 18:30',
    '15 października 2026'), a year inferred for 'Thu 16 Oct', and relative words. A wall-clock time without an
    offset is local time in the row's country when that country has one time zone; a date alone stays midnight UTC.
  - Numbers also take Arabic-Indic digits and separators and Indonesian/Malay `juta`/`jt` and `ribu`/`rb`; periods
    take Dutch, Polish, Indonesian/Malay and Arabic words and slash units (`zł/h`, `S$/mo`, never `€/m²`). A row
    with no amount gets no default period.
  - Enums take multilingual synonyms (Бензин → petrol, Автомат → automatic).
  - URLs are made absolute.
  - Text is stripped of tags and bounded.
- **Dedup.**
  - Within a source, rows dedup on `(source, source_id or url)`.
  - Across sources, rows share a template fingerprint. Nothing is deleted; `distinct=true` on queries and
    exports keeps the most recent row per group.
- **Price basis.** Real-estate templates carry `price_basis` (`total`, `per_area`, `per_person`), read from the price
  text when the scraper does not set it ('From RM799 / Seat / Month' → per_person, '$35.00 PSF' → per_area): the
  price and `price_per_month` are then per seat or per area unit, not the rent of the whole space. A currency glued
  to the amount ('RM799', 'Rp5.000.000', 'USD1200') is split before parsing.
- **Country.** A source that covers several regions reads each row's country from the row (its country field, else
  a city the gazetteer places in exactly one of them); it is never assumed to be the first region. Without one,
  the country stays empty and the default currency is used only if all the regions share it.
- **Quarantine.** Rows go to the `quarantine` table with reasons when a required field is missing, a sanity
  bound is violated, there is a category conflict, the row's country is outside the project's regions (unless the
  project includes `GLOBAL`), the city lies in another country than the one stated (per the gazetteer), or an event
  has already ended (template `expires`).
- **Event dedup** compares `starts_at` by its date, so a date-only and a timed listing of one event share a fingerprint.

### CSV and spreadsheet formulas

Scraped text is attacker-controlled. A CSV cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return is
evaluated as a formula when the file is opened in a spreadsheet: `=HYPERLINK(…)`, `@SUM(…)`. CSV exports therefore
prefix such text cells with a single quote (the OWASP CSV-injection defence). Numbers are written as numbers, and
JSONL and Parquet are written unchanged. To get the values exactly as stored, opt out explicitly:
`harvest export <p> --format csv --raw`, `harvest_export(raw=true)` or `GET …/export?format=csv&raw=true`. Only
open a raw CSV in a spreadsheet if you trust every source.

### Locations

`city` values are canonicalised per country (`geo.py`), e.g. Lisboa → Lisbon and Тбилиси / თბილისი → Tbilisi.
The site's wording is kept in `extra.city_raw`.

- **Built-in gazetteer:** capitals and large cities of the markets collected most often.
- **Your own gazetteers:** set `HARVEST_GAZETTEER` to one or more files separated by `:`. Each file is either
  JSON (`{"PT": {"Lisbon": ["Lisboa"]}}`) or a GeoNames dump (`cities500.txt`, `cities15000.txt` or `allCountries.txt`
  from geonames.org, CC BY 4.0). A GeoNames `name` becomes the canonical form, and its `asciiname` and
  `alternatenames` become aliases.
- **Matching** ignores case, accents, punctuation and prefixes such as "г." or "city of", then tries the name without
  a parenthetical qualifier or postcode and its first comma or semicolon part ('Utrecht (NDW)', 'London EC2A 4NE').
- The built-in table covers the 2026-10 trial markets too (NL, IE, SG, MY, ID, more of IN, PL, DE, GB, AE, SA).

## 7. Storage

SQLite (WAL) is the default. Set `store_dsn` or `HARVEST_STORE_DSN=postgresql://…` to use Postgres
(`pip install 'harvest[postgres]'`). Every table carries `project`, so one database can hold many projects.

**Never-wipe rule.** Rows are upserted during the walk. At the end:

- If the walk was **complete** and returned at least **60 %** of the source's baseline, the source's rows unseen
  in this run are deleted (a replace), and the baseline becomes this count. "Unseen" means last seen before the run
  started, so rows refreshed by an overlapping walk of the same source survive.
- Otherwise nothing is replaced, and only rows unseen for `prune_after_days` are pruned.

A run is `degraded` when it stored under half of what the source normally yields: the baseline of its last
complete walk, or, for a source whose walks never complete (a page cap), the median of its last five healthy runs.
It is never the row count of the table, which grows with every upsert walk. A run that stored nothing because a
page failed (for example a feed that does not parse) is `error`, not `empty`.

The `runs` table records the trigger, heartbeat, status (`ok`, `partial`, `degraded`, `empty`, `error`,
`timeout`, `stuck`), stop reason, pages, rows fetched, stored and quarantined, completeness, write mode,
rows pruned, error and HTTP stats.

## 8. Autonomy

- **Scheduling.**
  - `harvest schedule <p>` writes `deploy/harvest.cron` and `deploy/harvest-<p>.{service,timer}`, but never installs them.
  - Both tick hourly and run `harvest run <p> --due`; each source runs when its cadence is due.
  - `harvest daemon` is the in-process equivalent for every project, used by docker compose.
- **Watchdog.**
  - `harvest watchdog <p>` finds `zero`, `degraded`, `stuck` (closes the run), `stale`, `error_streak` and `review_stale` sources
    (a module edited since its passing review is found by the check itself, not only by the next run).
  - It also reports `clock_skew` when a run or heartbeat is stamped more than 5 minutes in the future (a worker
    clock ran ahead). Scheduling and the stale check ignore such runs, so a clock that was ahead and then stepped
    back does not pause collection.
  - Alerts go to the table, to `alerts.jsonl`, to `HARVEST_ALERT_HOOK=module:function` and to `HARVEST_ALERT_WEBHOOK`
    (JSON POST). Every alert goes through the same path, including the runner's `degraded` and `review_stale` and
    the proxy `proxy_budget`.
  - One alert per incident. A (source, kind) finding is not re-alerted while it lasts (24 h re-alert window), but
    once a healthy run (`ok`/`partial`) has ended the incident, the next one is alerted at once. `stuck` is
    alerted once per run.
  - Delivery is at least once. Each channel is tried on its own, a webhook answer of 300 or more is a failure, and
    an alert that did not reach every channel is retried on later checks for 24 h. Payloads carry the alert `id`
    for deduplication.
  - Retention: finished runs, quarantine entries, alerts and finished jobs older than `HARVEST_HISTORY_DAYS`
    (default 90; `0` keeps everything) are deleted by the check.
- **Repair agents.**
  - These are **off by default**. They need `--dispatch` and `HARVEST_REPAIR_DISPATCH=on`.
  - At most `HARVEST_REPAIR_BUDGET` (2) run per check, with a 72 h cooldown per source.
  - Each gets an explicit `--allowedTools` list that can edit only that source's module and call the review; it cannot enable anything.
  - The edit changes the sha, so the source cannot run until a new review passes.

### Job queue and workers

**One walk per source.** A run, a review and a lane detection of one source never overlap, across processes and
hosts. Each holds a database lock on the source while it works: a fenced lease row on SQLite, renewed every
lease/3 (`HARVEST_SOURCE_LOCK_LEASE_S`, default 120 s), and a session advisory lock on Postgres. A worker that finds
the source taken reports it `busy` (a run) or fails the job with `SourceBusy` (a review or detection). A walk that
loses its lock (it stalled past its lease on SQLite and another worker took the source) stores nothing more, never
replaces or prunes, and ends `superseded`. A crashed worker's source frees itself when its lease runs out (SQLite)
or its connection closes (Postgres).

Long work goes through a durable queue in the `jobs` table: agent census, audit, build and repair jobs, lane
detection, reviews, runs and the watchdog.

- **Claiming.** A worker (`harvest worker`) **claims** a job with an atomic conditional UPDATE, so exactly one
  worker wins. It gets a **lease** (`--lease`, default 120 s) and renews it every lease/3 while the job runs.
- **Crashes.** If a worker dies (crash, `kill -9`, host restart), its lease expires. The job returns to `queued` and
  the next worker takes it (`attempts` + 1). Once `max_attempts` is used up the job is `lost`. Agent jobs are never
  replayed automatically.
- **Lost leases.** A worker that lost its lease cannot record an outcome, and its running agent CLI is killed. A run
  job stops its walk the same way: nothing more is stored and nothing is replaced (`superseded`, `lease_lost`).
- **Clocks.** On Postgres, leases are written and compared on the database server's clock, so workers on several
  hosts, or a worker whose clock was stepped, agree on when a lease has expired. On SQLite (one host) the host
  clock is used.
- **Resilience.** The worker loop never exits on an error of its own (a database that is down, a full disk): it
  logs `worker_error`, backs off up to 60 s and continues; one project's broken store does not stop the others.
  A dead Postgres session is replaced before the next statement; a read is retried once, a write is not
  replayed. At start a worker removes scratch directories (`harvest-sbx-*`) left behind by a parent that was killed.
- **Modes.** `HARVEST_JOB_MODE=queue` is the default: the web app, the API and `start_job` enqueue. `inline` runs
  jobs in the submitting process, through the same claim/lease path; use it with `harvest web --inline-jobs` for
  development. `?wait=true` or `wait=True` runs a job synchronously.
- **Scheduling.** `harvest worker --schedule 300` also runs due sources and the watchdog every 300 s. Docker compose
  and `harvest daemon` use this. Run as many workers as you like; see `deploy/systemd/harvest-worker.service`.

### Headless agents

`harvest agent <project> census|audit|build|repair [source_id]`, the web app's "Run census agent" and "Build"
buttons, and the watchdog all go through `agents.run_job`. It launches the configured agent CLI:

- `claude -p <brief> --allowedTools <explicit list> --mcp-config <harvest server> --strict-mcp-config --output-format json`
- `HARVEST_AGENT_MODEL` sets the model.

Permission-bypass flags are refused. What an agent can do is limited in harvest itself, not only by the CLI's flags:

- Build and repair agents read only the project's `sources/` directory (`Read(//…/sources/**)`) and start there.
  Census and audit agents have no file tools.
- The harvest MCP server an agent gets runs with `HARVEST_AGENT_KIND` and refuses every tool outside that kind's
  list (`agents.MCP_ALLOWED`), even when the operator's own Claude Code settings pre-approve `mcp__harvest__*`.
- Through `harvest_record_policy` an agent can make a source stricter but cannot lift a `forbids` verdict, open
  a lane that is `none` or undetected, or cite terms off the source's own site. An agent's `allowed` or
  `no_clause` verdict is recorded only after harvest re-reads the cited page itself. The page must be fetchable,
  harvest's classifier must find no prohibition in it, and the quoted clause (required for `allowed`) must appear
  in it. Otherwise the verdict stays where it was. Those are operator decisions (CLI,
  web app); verdicts record who made them (`lane_detail.policy.by`).
- The agent's environment never carries `HARVEST_PROXY_*`, `HARVEST_ADMIN_TOKEN` or the alert hook settings. A
  custom agent CLI (`module:Class`) comes from `HARVEST_AGENT_CLI` only, never from a job parameter.

Settings:

- `HARVEST_AGENT_CLI` (`claude`, or `package.module:Class` for another CLI; subclass `harvest_ai.agents.AgentCli`)
- `HARVEST_AGENT_BIN`
- `HARVEST_AGENT_MODEL`
- `HARVEST_AGENT_TIMEOUT_S`
- `HARVEST_AGENT_MAX_TURNS`

The briefs live in `src/harvest/prompts/`. The plugin agents load the same briefs through `harvest_agent_brief`,
so there is a single source of instructions.

## 9. Interfaces

### MCP tools

`harvest_templates`, `harvest_new_project`, `harvest_list_projects`, `harvest_census_plan`, `harvest_census_add`,
`harvest_census_gaps`, `harvest_list_sources`, `harvest_detect_lane`, `harvest_record_policy`, `harvest_probe_url`,
`harvest_template_scraper`, `harvest_review_source`, `harvest_enable_source`, `harvest_update_source`, `harvest_run`,
`harvest_status`, `harvest_quarantine`, `harvest_query`, `harvest_export`, `harvest_schedule`, `harvest_watchdog`,
`harvest_set_fx`, `harvest_agent_brief`, `harvest_jobs`, `harvest_census_resume`, `harvest_proxy_status`,
`harvest_proxy_enable`, `harvest_proxy_disable`, `harvest_ua_status`, `harvest_ua_enable`, `harvest_ua_disable`.

Run the server with `harvest-mcp` (stdio) or `harvest mcp --http --port 8091`.

### CLI

`harvest templates | new | projects | census-plan | census-add | gaps | sources | reject | detect-lane | policy | probe |
scaffold | review | enable | disable | tune | run [--due] | runs | quarantine | query | export | schedule | watchdog |
fx | agent | census-resume | proxy status|enable|disable | ua status|enable|disable | worker | jobs | daemon | web | mcp`. Every command prints JSON; `harvest <cmd> -h` shows its options.

### Web app and API

`harvest web --host 127.0.0.1 --port 8080`. The frontend lets you create a project, read its census plan and
coverage, start census and audit agents, watch pipeline progress, detect lanes, scaffold, build (agent), review,
approve and reject sources, record proxy and user-agent decisions, browse, filter and export data, write the
schedule and run the watchdog.

- Auth is a single admin token (`HARVEST_ADMIN_TOKEN`, or one generated into `$HARVEST_HOME/admin_token`), sent as
  `Authorization: Bearer …`. Every `/api` route except `/api/health` requires it. A token that stops working sends
  every open tab back to the sign-in page; signing out in one tab signs out the others.
- The frontend is served with a Content-Security-Policy (no inline script) and renders only http(s) links.
  Project views redact a store DSN's password.
- `web/auth.py` isolates this behind `Principal`/`authenticate`, so users and roles can be added later.
- The OpenAPI document is at `/docs`.
- Tabs: overview (counts, pipeline, actions, the last schedule/watchdog result, proxy and UA panels, runs, alerts),
  census (plan per region, region × angle coverage, deferred candidates, queries), sources, data, runs,
  quarantine, jobs. The overview, census, sources, runs and jobs tabs refresh every 4 s without moving the
  keyboard focus or closing what you opened.
- Scraped text is hostile input: every value is rendered as text, long values are clipped (full text in the
  tooltip), and API errors never include server paths.
- `tests/test_ui_e2e.py` drives the whole journey in a real headless Chromium against `harvest web` and fixture
  sites (a paginated board with a broken page, a bot challenge, a plain 403), plus reloads mid-job, two tabs,
  keyboard-only use, a 390 px viewport, Arabic/CJK and XSS data; any console error fails it (CI job `ui-e2e`).

| Method | Path | |
|---|---|---|
| GET | `/api/health` | no auth |
| GET | `/api/templates`, `/api/templates/{type}` | record templates |
| GET, POST | `/api/projects` | list and create (target, regions, record_type, fields …) |
| GET | `/api/projects/{p}` | status: counts, lanes, runs, alerts, jobs |
| GET | `/api/projects/{p}/census/plan`, `/census/gaps` | plan and coverage |
| POST | `/api/projects/{p}/census/run` | start a census or audit agent job |
| POST | `/api/projects/{p}/census/candidates` | add candidates with evidence |
| POST | `/api/projects/{p}/census/resume` | raise the budget, re-add deferred candidates, optionally queue a census agent |
| GET | `/api/projects/{p}/sources`, `/sources/{id}` | registry, one source in detail |
| PATCH | `/api/projects/{p}/sources/{id}` | tune (field_map, max_pages, cadence …) |
| POST | `/api/projects/{p}/sources/detect`, `/sources/{id}/detect` | lane detection job (`?wait=true` to block) |
| POST | `/api/projects/{p}/sources/{id}/policy` | record a terms verdict or lane override |
| POST | `/api/projects/{p}/sources/{id}/scaffold` | write the lane template |
| POST | `/api/projects/{p}/sources/{id}/build` | start a scraper-build agent job |
| POST | `/api/projects/{p}/sources/{id}/review` | review gate job |
| POST | `/api/projects/{p}/sources/{id}/approve`, `/disable`, `/reject` | approve (409 with reasons when refused), disable, reject |
| POST | `/api/projects/{p}/run` | collection job (`{"source_ids", "due", "wait", "use_proxy"}`) |
| GET | `/api/proxy` | the residential-proxy pool: configured, size, cooling exits (`?check=N` probes N exits) |
| GET, POST | `/api/projects/{p}/ua` | browser user-agent decision; switch on/off (`{"enabled", "reason", "source_id"}`) |
| GET, POST | `/api/projects/{p}/proxy` | proxy decision, caps and usage; switch on/off (`{"enabled", "reason", "source_id", …}`) |
| POST | `/api/projects/{p}/schedule`, `/watchdog`, `/fx` | schedule snippets (`kind`: cron, systemd or both), health check, FX table |
| GET | `/api/projects/{p}/runs`, `/quarantine`, `/jobs`, `/jobs/{id}` | history |
| GET | `/api/projects/{p}/records` | query (`filters` JSON, `fields`, `order_by`, `desc`, `limit`, `offset`, `distinct`) |
| GET | `/api/projects/{p}/export?format=csv\|jsonl\|parquet` | file download (written streaming, one query page at a time; `raw=true`: see "CSV and spreadsheet formulas") |
| POST | `/api/projects/{p}/sources/{id}/session-headers` | operator decision allowing Cookie/Authorization for one source (`{"enabled", "reason"}`) |

### Claude Code plugin

```bash
claude plugin marketplace add /path/to/harvest
claude plugin install harvest@harvest
```

The plugin provides:

- the `harvest` skill;
- the `harvest-census`, `harvest-auditor`, `harvest-builder` and `harvest-repair` agents;
- the `/harvest <target> in <regions>` command;
- the `harvest` MCP server (`uv run --project ${CLAUDE_PLUGIN_ROOT} harvest-mcp`).

## 10. Worked examples

### Used cars, KZ + GE

```bash
harvest new used-cars --target "used cars" --regions KZ,GE --record-type vehicles --cadence daily
harvest fx used-cars '{"KZT": 480, "GEL": 2.7}'
harvest agent used-cars census          # classifieds, marketplaces, vertical portals ... in kk, ru, ka, en
harvest agent used-cars audit           # independent adversarial pass + terms verdicts
harvest gaps used-cars                  # matrix KZ/GE x angles, dry streak >= 2
harvest detect-lane used-cars           # lanes for every candidate; forbids/login/robots -> none
harvest sources used-cars               # review the list
harvest agent used-cars build <id>      # per source: template -> fetch() -> review until pass
harvest enable used-cars <id>
harvest run used-cars
harvest query used-cars --filters '{"price_report": {"lte": 15000}, "fuel_type": "diesel"}' --order-by price --asc
harvest export used-cars --format parquet --distinct
harvest schedule used-cars              # hourly tick; daily cadence decides
```

- Prices such as "12 500 000 ₸" and "25 000 ₾" become `price` + `currency` + `price_report` (USD).
- "85 тыс. км" becomes `mileage` 85000 (km). "Бензин" / "ბენზინი" become `petrol`; "Автомат" becomes `automatic`.
- VIN duplicates across sites share a fingerprint.

### Rental apartments, PT + ES

```bash
harvest new rent-iberia --target "rental apartments" --regions PT,ES --record-type real_estate_rent --report-currency EUR
harvest agent rent-iberia census        # portals, classifieds property sections, agencies, official housing data, in pt/es/ca/eu/gl
harvest detect-lane rent-iberia
harvest agent rent-iberia build <id> ...
harvest run rent-iberia
harvest query rent-iberia --filters '{"city": {"contains": "lisboa"}, "price_per_month": {"lte": 1200}}'
```

- "950 €/mês" becomes `price` 950 with `rent_period` month; "300 €/semana" becomes `price_per_month` 1300.
- "82,5 m²" becomes 82.5 m².
- "Apartamento T2 para venda" in a rent category is quarantined as a sale listing, not relabelled.

Both flows run end to end in the test suite against a local HTTP server:
`tests/test_mcp_cli_e2e.py::test_end_to_end_demo_used_cars` and `test_end_to_end_demo_rentals_pt_es`.

## 11. Operations

- **Docker.**
  - `docker compose up -d` starts the web app on `127.0.0.1:8080` and a worker running `harvest daemon`.
  - `--profile postgres` adds Postgres; point `HARVEST_STORE_DSN` at it.
  - Put `HARVEST_ADMIN_TOKEN` (and `POSTGRES_PASSWORD`) in `.env`.
  - Agent jobs need an agent CLI inside the image; extend the Dockerfile to install and authenticate it.
- **systemd.** Examples live in `deploy/systemd/`: `harvest-web.service`, `harvest-worker.service`, and
  `harvest-tick.service` + `.timer` (an hourly `harvest daemon --once`).

### Running workers in production

Workers run agent-written code, so give them the strongest isolation the platform has.

- **On a Linux host or VM (strongest).** `apt install bubblewrap` and run `deploy/systemd/harvest-worker.service`. It
  sets `HARVEST_SANDBOX=bwrap`, so modules never run without bubblewrap. On Ubuntu 24.04+ unprivileged user
  namespaces are restricted per binary: load `deploy/apparmor/bwrap` (the comment in it has the two commands).
  Do not add `RestrictNamespaces=` to the unit.
- **In a container.** Docker's default profile does not let bubblewrap create its namespaces, and the options that
  would allow it (seccomp and AppArmor unconfined) weaken the container more than they help. So in a container, the
  container is the OS boundary and the module sandbox is the policy layer. `docker-compose.yml`'s worker sets
  `HARVEST_SANDBOX=policy` and is hardened:
  - read-only root;
  - tmpfs `/tmp` and home;
  - all capabilities dropped and `no-new-privileges`;
  - pid and memory limits;
  - only the data volume writable.

  Keep the web app and the worker in separate containers, and do not mount anything else into the worker. A
  sandboxed container runtime (gVisor `runsc`, Kata) adds a kernel boundary; with it, the same compose file works
  unchanged.
- **Either way.** The proxy pool, the admin token and the agent CLI's credentials belong to the parent process.
  The module child never receives them, and its audit hook refuses to read `$HARVEST_HOME`.
- **Environment.**

  | Variable | Purpose |
  |---|---|
  | `HARVEST_HOME` | state directory |
  | `HARVEST_STORE_DSN` | store DSN |
  | `HARVEST_USER_AGENT` | put a contact URL in it |
  | `HARVEST_WORKERS` | parallel domains per run |
  | `HARVEST_HTTP_TIMEOUT`, `HARVEST_URL_BUDGET_S`, `HARVEST_BACKOFF_S` | HTTP budgets |
  | `HARVEST_REVIEW_TIMEOUT_S` | review smoke-fetch timeout |
  | `HARVEST_BROWSER_UA` | override the browser-mode user-agent string |
  | `HARVEST_PROXY_FILE`, `HARVEST_PROXY_URL` | residential-proxy pool (a credential: read in place, never logged) |
  | `HARVEST_PROXY_DAILY_BYTES`, `HARVEST_PROXY_DAILY_REQUESTS` | default daily caps per project |
  | `HARVEST_PROXY_MODE`, `HARVEST_PROXY_COUNTRY` | default exit mode (sticky/rotate) and country (auto/off/CC) |
  | `HARVEST_PROXY_HEADROOM`, `HARVEST_PROXY_COOLDOWN_S`, `HARVEST_PROXY_PROBE_URL`, `HARVEST_PROXY_HEALTHCHECK` | route tuning |
  | `HARVEST_FX_FILE`, `HARVEST_FX_PROVIDER` | FX table sources |
  | `HARVEST_PSL_FILE` | full public-suffix list |
  | `HARVEST_MCP_CONFIG` | MCP-server lane config |
  | `HARVEST_ALERT_HOOK`, `HARVEST_ALERT_WEBHOOK` | alert delivery |
  | `HARVEST_HISTORY_DAYS` | retention of finished runs, quarantine, alerts and finished jobs (default 90; 0 = keep) |
  | `HARVEST_REPAIR_DISPATCH`, `HARVEST_REPAIR_BUDGET` | repair agents |
  | `HARVEST_AGENT_*` | agent CLI settings |
  | `HARVEST_ADMIN_TOKEN` | web auth |
  | `HARVEST_SANDBOX` | `auto` (default), `bwrap` (require bubblewrap) or `policy` (accept the policy layer alone) |
  | `HARVEST_SOURCE_LOCK_LEASE_S` | lease of the per-source lock on SQLite (default 120) |
  | `HARVEST_GREP_TIMEOUT_S` | time limit for `probe_url(grep=…)` (default 3; patterns ≤ 300 characters) |
  | `HARVEST_ALLOW_PRIVATE=1` | local testing only: lets the client reach private addresses |

## Known limits

- The terms classifier is a keyword heuristic. Treat `no_clause` as "nothing found", and have the auditor record
  a reviewed verdict for important sources.
- The built-in public-suffix subset covers common registries; set `HARVEST_PSL_FILE` for the full list.
- Without bubblewrap (macOS, containers) the module sandbox is the policy layer only; see "Running workers in
  production".
- The browser lane needs Playwright installed and stays page-1 only.
- One project has one record type. Collect sale and rent real estate as two projects.
- Postgres is supported through the same SQL. The test suite runs on SQLite; set a DSN to exercise Postgres.
- Datacenter egress is blocked by many market-leading portals (the 2026-09-25 pilot from a VPS saw 403 on
  myauto.ge, turbo.az, idealista.pt, imovirtual.com, olx.pt …). harvest records them as lane `none`. An operator
  may switch on the residential-proxy route for an IP-level block (see "Residential-proxy route"). A bot
  challenge, login or paywall is never worked around; covering those needs an official API or data agreement.
- City canonicalisation covers the built-in gazetteer; supply GeoNames for full coverage (unknown names stay as cleaned text).
- Browser-lane sources are page 1 only and never trigger a replace: rows are upserted and pruned by age.

## Live pilot (2026-09-25)

Real census, audit and builder agents (`claude -p` through `HARVEST_AGENT_BIN`) ran on "used cars, GE+AZ" and
"apartments for rent, PT". Every defect found got a fix and a regression test: blocked sites recorded outside the budget,
honest saturation, the Playwright trust flag, `Edit(//abs)` permission rules, `--tools` restriction,
transcript logging, reviewed verdicts surviving re-detection, Organization JSON-LD and tiny feeds no longer
winning a lane, the source-URL correction, the budget ignoring lane-none sources, lost jobs, unmapped
enum values kept, page-1 browser walks never replacing, and compose/healthcheck issues.

## Live trials (2026-10-01)

Five more verticals (jobs DE/PL/NL, tech events GB/IE, solar installers AE/SA, used phones IN/ID, office rent SG/MY)
ran end to end with real agents. The results, per-project numbers and the defects fixed are in
[TRIALS_2026-10.md](TRIALS_2026-10.md).
