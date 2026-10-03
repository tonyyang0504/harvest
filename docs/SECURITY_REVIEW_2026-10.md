# Security review, 2026-10

An independent, adversarial review of harvest at `2eb03e3` (origin/main, 2026-10-01). It covered the service core,
the review gate, the sandbox, the runner, the HTTP client, the proxy route, the user-agent mode, storage, the job
queue, the web app, the MCP server, the headless agents and the plugin files.

The first pass (`cfacbf8`) fixed S1–S15 and left seven items open. The second pass, the same day, closed O1–O4,
O6 and O7, and documents O5 as residual risk (see "Second pass" and "Residual risk" below).

Each fixed finding has a regression test, written first. The first pass's tests are in
`tests/test_security_review.py`; 45 of its 48 tests fail on `2eb03e3`. The other three guard controls that already
held: every API route needs the token, job ids on per-project SQLite stores, and the helper imports a module may use.
The second pass's tests are in `tests/test_security_open.py` and `tests/test_browser_lane.py`. On `cfacbf8`, 17 of
the 18 that run off Linux fail; the 18th guards that operator verdicts are not re-fetched. The two bubblewrap tests
run on Linux only.

## Summary

| # | Severity | Area | Finding | Status |
|---|---|---|---|---|
| S1 | critical | sandbox | A scraper module could switch the sandbox audit hook off, and reach `os` functions under other names | fixed |
| S2 | high | sandbox | A scraper module could read harvest's secrets (admin token, MCP-lane API keys, every store) and other processes' environments | fixed |
| S3 | high | prompt injection | Build and repair agents had unscoped `Read` plus `WebFetch` | fixed |
| S4 | high | policy gates | Agents could lift a `forbids` terms verdict and open a lane that is `none` (login, paywall, challenge); the MCP tool allowlist relied on CLI settings alone | fixed |
| S5 | medium | review gate | The lint and the sha could see different bytes (TOCTOU) | fixed |
| S6 | medium | SSRF | The URL gate let through shared address space (100.64.0.0/10, a cloud metadata address) and NAT64/6to4 forms, and DNS rebinding between the gate and the connection | fixed (browser lane: O3) |
| S7 | medium | secrets | A project's `store_dsn` (Postgres password) was returned by the API, MCP and UI | fixed |
| S8 | medium | data integrity | A replace could delete rows that an overlapping walk of the same source had just refreshed | fixed |
| S9 | low | secrets | Agent processes inherited `HARVEST_ADMIN_TOKEN` and the alert hook settings | fixed |
| S10 | low | agents | A job parameter (`cli`) could import any `module:Class` | fixed |
| S11 | low | web | No CSP; links were not scheme-checked in the browser | fixed (defence in depth) |
| S12 | low | web | `/jobs/{id}` showed another project's job on a shared Postgres store | fixed |
| S13 | low | web | `runs` and `quarantine` limits were unbounded | fixed |
| S14 | low | data integrity | FX rates were not validated (zero, negative, NaN, infinite) | fixed |
| S15 | low | secrets | Job failure traces were not redacted | fixed |
| S16 | low | data export | CSV exports wrote scraped text that a spreadsheet evaluates as a formula (`=`, `+`, `-`, `@`, tab, CR); found by the UI end-to-end run | fixed (second pass) |
| O1 | medium | sandbox | The module shared an interpreter with the walk's HTTP client and proxy exits; no OS isolation | fixed (second pass) |
| O2 | medium | jobs | A lease that expired under a slow worker re-ran non-agent jobs; runs were serialised per host only | fixed (second pass) |
| O3 | low | SSRF | The browser lane could not pin DNS (Chromium resolves on its own) | fixed (second pass) |
| O4 | low | policy | A module could send `Cookie` or `Authorization` on direct requests | fixed (second pass) |
| O5 | low | prompt injection | The plugin's interactive agents keep unscoped `Read`; the plugin MCP server acts as the operator | residual risk, documented |
| O6 | low | prompt injection | An agent's `unknown` → `allowed` verdict was not re-verified against the cited page | fixed (second pass) |
| O7 | low | availability | Export built the whole file in memory; `probe_url(grep=…)` accepted any regex | fixed (second pass) |

Nothing critical, high or medium is open. Residual risks are listed at the end.

## Fixed

### S1 (critical): the audit hook could be switched off from a module

**Gap.** The sandbox child's audit hook steps aside while `trusted()` is true, so harvest's own browser and
MCP-lane helpers can spawn their processes. Any code could open the trusted section, including a scraper module:
the AST lint allowed imports from `harvest.*`. The lint also matched `os.<function>` by the literal name `os`, so
`os` imported under another name, or the platform module beneath it, passed the scan.

**Scenario.** A build agent is steered by a page it fetched, or an agent simply writes bad code. It produces a
module that opens a trusted section and starts a process. The review gate runs the module's page-1 smoke fetch
automatically, so the code would run before any person approves the source.

**Fix.**
- `trusted_section()` now accepts only registered callers: the code objects of `Http._render_once` and the
  MCP-lane `_run` (`http.trusted_caller`). Any other caller gets a `PermissionError`.
- The lint allows only `harvest.extract` and `harvest.mcp_client` from the harvest package (since 0.1.0 the package is `harvest_ai`: `harvest_ai.extract`, `harvest_ai.mcp_client`).
- It also refuses:
  - `os` under another name, and private or C-level modules (`_…`, `posix`, `nt`);
  - string-driven attribute access (`operator`, `str.format` fields such as `{0.x}` or `{0[k]}`, `string`), which
    defeats the private-attribute rule;
  - `inspect`, `gc`, `types`, threading modules and relative imports.

**Tests.** `test_lint_closes_review_gaps`, `test_module_cannot_open_a_trusted_section` and
`test_trusted_section_refuses_unregistered_callers`.

### S2 (high): the sandbox could read secrets

**Gap.** The audit hook refused writes outside the working directory but allowed every read. `HARVEST_HOME` is
passed to the child, and it holds `admin_token`, `mcp_servers.json` (the MCP servers' API keys), every project's
store and modules, and `proxy_health.json`. On Linux, the parent's `/proc/<pid>/environ` can hold
`HARVEST_ADMIN_TOKEN` and `HARVEST_PROXY_URL`. A module can read through APIs other than `open`, and `http` is an
exfiltration channel.

**Fix.**
- The parent computes `sandbox.secret_paths()` and passes it to the child in the job.
- The audit hook now also checks reads and directory listings:
  - refused under `$HARVEST_HOME`, the proxy pool file and the MCP config (hard roots);
  - refused under the operator's home, except below the interpreter's import roots (a venv can live there);
  - refused for other processes' `/proc` entries.
- The MCP-lane config is loaded before the guard goes up (`mcp_client.preload`).

**Tests.** `test_sandbox_refuses_reads_of_harvest_state` and `test_read_policy_unit`.

### S3 (high): unscoped `Read` for agents that also browse

**Gap.** Build and repair agents had `Read` with no path rule, plus `WebFetch` and `harvest_probe_url`, and ran
with the project root as their working directory. The pages they read are written by third parties: listings,
reviews, terms pages. An injected instruction could get the agent to read local credentials (the admin token, the
proxy pool, the agent CLI's own login) and send them out in a fetched URL.

**Fix.**
- `Read` is now `Read(//<project>/sources/**)` (`agents.read_rule`).
- File agents start in `sources/`, so the CLI's implicit working-directory reads cover only modules.

**Tests.** `test_file_agents_read_only_the_sources_dir` and `test_agent_env_drops_operator_secrets` (working
directory).

### S4 (high): agents could loosen policy

**Gap.** `harvest_record_policy` is on the build and audit allowlists, which is by design: agents record terms
verdicts. Two holes:

1. **Lifting `forbids`.** The lane check looked at the new verdict, not the current one. A call with
   `terms_status="allowed"` and `lane="html"` turned a heuristic `forbids` source into an enable-able one.
2. **Opening a `none` lane.** A lane override was checked only against robots and terms. A source that is `none`
   because of a login wall, paywall or challenge could be set to `html`. That also clears the "lane none for a
   login/challenge" stop in the proxy and UA gates, because those read the lane reason.

Separately, the tool allowlist lived only in the CLI's `--allowedTools`. If the operator's user settings
pre-approve `mcp__harvest__*`, an agent could also call `harvest_enable_source`, `harvest_proxy_enable`,
`harvest_ua_enable` or `harvest_export(path=…)`.

**Fix.**
- Each agent's MCP config sets `HARVEST_AGENT_KIND`. The server refuses every tool outside
  `agents.MCP_ALLOWED[kind]` and runs `harvest_record_policy` as actor `agent:<kind>`.
- For agent actors, `lanes.record_policy` refuses to:
  - lift a `forbids` verdict;
  - set a lane other than `none` on a source whose lane is `none` or undetected;
  - accept an `allowed`/`no_clause` verdict whose terms URL is not on the source's registrable domain.
- Operators keep these overrides, because heuristic false positives happen. Every verdict now records `by`.

**Tests.** `test_mcp_server_enforces_the_agent_allowlist`, `test_agent_cannot_lift_forbids_or_open_a_none_lane`,
`test_agent_terms_verdict_must_cite_the_sources_own_site` and `test_mcp_record_policy_runs_as_the_agent`.

### S5 (medium): review TOCTOU

**Gap.** `review()` hashed the file, then `lint()` read it again, then the child checked the sha. A file swapped
between the hash and the lint, and swapped back before the child read it, would get a passing verdict for bytes the
lint never saw. The runner would then run those bytes, because the sha matches.

**Fix.**
- `review()` reads the module once.
- The sha (`sandbox.sha256_bytes`), the lint (`lint(path, data)`) and the ruff pass (on a temporary copy) all use
  those bytes, and the child refuses any other bytes through `expected_sha`.

**Test.** `test_review_lints_the_bytes_it_hashed`.

### S6 (medium): SSRF gaps

**Gap.** The gate refused `is_private`, `is_loopback` and similar flags. That let through:
- 100.64.0.0/10 (one cloud provider's metadata service is 100.100.100.200);
- NAT64 (`64:ff9b::/96`), 6to4 and Teredo forms of private IPv4 addresses.

The gate also resolved the name independently of the socket layer. A DNS answer that changed between the two
lookups (rebinding) reached loopback or metadata addresses. This affects every URL an agent or user supplies:
census candidates, `probe_url`, lane detection, modules and redirects.

**Fix.**
- `http.public_ip` requires `is_global` and judges embedded IPv4 forms by the embedded address.
- Direct clients use `_PublicOnlyBackend`. It resolves once at connect time, refuses any non-public answer and
  connects to the address it vetted. TLS still verifies the host name.
- Redirects still pass the request hook's gate.
- Proxied requests resolve at the proxy, so they never reach the host's own network.

**Tests.** `test_public_ip`, `test_gate_refuses_shared_address_space` and
`test_connection_is_pinned_against_dns_rebinding`.

### S7 (medium): store DSN password in API responses

**Gap.** `Project.to_dict()` returned the spec as stored, including `store_dsn`. It is used by `/api/projects/{p}`,
`harvest_status` and `harvest_new_project`.

**Fix.** `project.redact_dsn` masks the password in URL and key/value DSNs. Only `project.json` keeps it.

**Test.** `test_store_dsn_password_never_leaves_project_json`.

### S8 (medium): a replace could wipe an overlapping walk's rows

**Gap.** A complete walk deleted every row of the source whose `run_id` was not its own. Two walks of one source can
overlap: two worker hosts on one Postgres store (the run lock is a file on one host), or a lease that expired under
a slow worker (O2). The second walk's freshly refreshed rows were then deleted.

**Fix.** The replace deletes only rows last seen before this run started.

**Test.** `test_replace_spares_rows_an_overlapping_walk_refreshed`.

### S9 to S15 (low)

- **S9.** The agent environment drops `HARVEST_ADMIN_TOKEN`, `HARVEST_ALERT_WEBHOOK` and `HARVEST_ALERT_HOOK`, as
  well as `HARVEST_PROXY_*`. Test: `test_agent_env_drops_operator_secrets`.
- **S10.** `agents.get_cli` imports a `module:Class` only when it equals `HARVEST_AGENT_CLI`; job parameters may
  name built-in CLIs only. Test: `test_pluggable_agent_cli`.
- **S11.** The frontend gets a Content-Security-Policy with no inline script and no `javascript:` navigation, plus
  `nosniff`, `no-referrer` and `DENY` framing. Links go through `safeUrl`. Record URLs (`normalize.parse_url`) and
  source URLs (census) were already restricted to http(s) on the server, so this is defence in depth for the admin
  token in `localStorage`. Test: `test_frontend_has_a_csp_and_safe_links`.
- **S12.** `service.jobs(p, id)` returns a job only for its own project. Test: `test_job_ids_are_scoped_to_their_project`
  (it shows the leak only on a shared Postgres store, the CI `postgres` job).
- **S13.** `runs` and `quarantine` clamp `limit` to 1–1000. Test: `test_list_limits_are_clamped`.
- **S14.** `set_fx` requires 3-letter codes and finite rates above zero. Test: `test_fx_rates_are_validated`.
- **S15.** Job failure messages and traces pass `proxy.redact`.

## Checked, no issue found

- **Auth.** Every `/api` route except `/api/health` requires the token, compared in constant time. This is now
  pinned by `test_every_api_route_requires_the_token`.
- **CSRF.** The token travels in a header and there are no cookies, so CSRF does not apply. No CORS is configured.
- **Query injection.**
  - Filter fields are checked against the template's columns and `Store.jx` (`[A-Za-z0-9_]+`).
  - Values are bound parameters.
  - `order_by` is validated.
- **Export paths.** The web API never passes a path. The MCP and CLI `path` is the operator's choice, and agents
  can no longer call `harvest_export` (S4).
- **Proxy credentials.** They are redacted in logs, events, errors and alerts, are never in the child's
  environment, and agents have no proxy tool. The residual exposure inside the child is O1.
- **Never-wipe.** A partial, capped, browser (page 1) or below-60 % walk never replaces. Age pruning is per
  source.
- **Quarantine.** Raw rows are capped at 8 KB. Rows are quarantined, never relabelled.
- **Cross-source dedup.** Fingerprint collisions only group rows for `distinct=true`; nothing is deleted.
- **Job claiming.** The conditional UPDATE gives exactly one winner, and a stale worker cannot record an outcome.
- **UA and proxy decisions.** `tune` and `update_source` refuse them. They need a reason and a date, and their gates
  re-check robots, terms and the lane reason on every walk.

## Second pass: the open items

### O1 (medium): an OS boundary for module code

**Before.** The module ran in a child process, but inside the same interpreter as the walk's `Http` client, its
proxy route (exits and credentials) and harvest's MCP-lane client. The only boundary was Python-level: the lint and
the audit hook.

**Now.**
- **The child holds no client.** Its `http` is `sandbox.RpcHttp`, a stub that sends each call (`get_text`,
  `get_json`, `post_json`, `render`, `get`, `request`, `allowed`) as a JSON line to the parent. The parent's `Http`
  executes it under robots, politeness, the URL gate, the session-header rule (O4) and the proxy route, then sends
  the result back.
  - The MCP lane is forwarded the same way, and browser rendering happens in the parent.
  - The child receives the module's bytes and context (`sandbox.child_job`), never the route, a credential or a
    path under `$HARVEST_HOME`.
  - The parent checks the sha before it spawns the child, and the child checks it again.
- **No network in the child.** The audit hook refuses all socket use (connect, DNS, send), on top of the earlier
  rules.
- **bubblewrap** where available (`sandbox.bwrap_argv`):
  - the root filesystem is read-only, with a private `/tmp` and `/dev` and a fresh `/proc` (own pid namespace);
  - the child has its own network namespace;
  - the operator's home is an empty tmpfs, with the interpreter's own roots bound back read-only;
  - `$HARVEST_HOME`, the proxy pool file and the MCP config are masked last, so nothing re-exposes them;
  - one scratch directory is writable.
- **Choosing isolation.** `HARVEST_SANDBOX=auto|bwrap|policy`. `auto` falls back to the policy layer with a loud
  warning on stderr and in the review report's lint warnings; `bwrap` refuses to run without bubblewrap. Reviews and
  runs record the `isolation` used.
- **Production.** docs/harvest.md, "Running workers in production":
  - systemd workers with `HARVEST_SANDBOX=bwrap`, and an AppArmor profile (`deploy/apparmor/bwrap`) for Ubuntu
    24.04+;
  - a hardened compose worker (read-only root, tmpfs, no capabilities, `no-new-privileges`, pid and memory limits)
    that accepts the policy layer explicitly. Under Docker's default profile bubblewrap cannot create its
    namespaces: verified on a Linux cloud host, where uid maps and loopback setup are refused.

**Verification.** The whole suite passes with `HARVEST_SANDBOX=bwrap`: on a Linux cloud host (bubblewrap 0.9, root) and in
the new CI `sandbox` job (unprivileged runner user). The browser tests pass under bubblewrap on that host, and the
hardened worker container runs a walk.

**Tests.**
- `test_child_job_carries_no_route_and_no_state_paths`
- `test_the_child_has_no_network_of_its_own`
- `test_isolation_selection_and_the_loud_fallback`
- `test_review_reports_its_isolation`
- `test_bwrap_layer_hides_state_blocks_writes_and_network`: plain shell commands under the same bwrap argv, so the
  OS layer is tested without the Python hook
- `test_review_passes_under_bwrap`

### O2 (medium): one walk per source

**Before.** The project run lock was a file on one host, and nothing serialised reviews or lane detection. A job
whose queue lease expired under a slow worker was claimed and run again while the first copy continued.

**Now.**
- Runs, reviews and lane detection hold a per-source database lock for as long as they work (`locks.held`,
  `db.SourceLock`):
  - **SQLite:** a lease row with a fence, renewed every lease/3 (`HARVEST_SOURCE_LOCK_LEASE_S`, default 120 s);
  - **Postgres:** also a session advisory lock on a dedicated connection.
- A second worker finds the source `busy` (a run) or gets `SourceBusy` (a review or detection).
- A walk that loses its lock: the sandbox is killed through `sandbox.run(stop=…)`, no further page is stored, the
  replace and prune are skipped, and the run ends `superseded`.
- Reviews and detections check the lock before recording anything.
- A crashed worker's lock frees itself when its lease ends (SQLite) or its session closes (Postgres).

**Tests.**
- `test_source_lock_primitive` covers both backends: a stalled holder loses the source on SQLite but keeps it on
  Postgres, and a stale release never frees a new holder.
- `test_two_workers_and_a_stalled_job_lease_walk_a_source_once`: worker A's job lease expires mid-walk and worker B
  claims the job; B finds the source busy, and the site sees exactly one walk.
- `test_a_walk_that_loses_its_lock_stores_nothing_more_and_never_replaces`
- `test_review_and_detect_refuse_a_busy_source`
- `test_jobs.py::test_worker_crash_mid_job_is_recovered` still passes, with a short source-lock lease for the
  killed worker.

### O3 (low): DNS pinning for the browser

**Now.** Chromium runs in the parent and goes through `egress.FilteringProxy`, a proxy on 127.0.0.1 inside harvest's
process.
- Chromium hands it the host name (CONNECT for https, an absolute URI for http). The proxy resolves the name once,
  refuses any address that is not globally routable (`http.public_ip`), and connects to the address it vetted.
- Loopback is not bypassed (`--proxy-bypass-list=<-loopback>`).
- The filtering proxy is not used when the residential route is in use (that proxy resolves remotely) or when
  private addresses are allowed (local testing).

**Tests.**
- `test_egress_proxy_refuses_non_public_destinations`
- `test_egress_proxy_forwards_to_the_address_it_vetted`
- `test_browser_lane.py::test_browser_egress_is_pinned_against_dns_rebinding`: real Chromium, with the gate seeing a
  public address and the connection a loopback one. The site never sees the request.

### O4 (low): session headers

**Now.**
- `Http` refuses `Cookie`, `Authorization` and `Proxy-Authorization` (event `session_refused`, counted as gated)
  unless `allow_session_headers` is set.
- The flag comes only from an operator decision recorded on one source (`sessionhdr.py`):
  - CLI: `harvest session-headers enable|disable`;
  - API: `POST /api/projects/{p}/sources/{id}/session-headers`.
- The decision carries a reason, a date, who made it and a history. There is no MCP tool and no `tune` knob.
- Session requests still never go through the proxy route.

**Tests.** `test_http_refuses_session_headers_by_default` and `test_session_headers_need_an_operator_decision_per_source`.

### O6 (low): an agent's permissive verdict is re-read

**Now.** For agent actors, `lanes.verify_cited_terms` runs before an `allowed` or `no_clause` verdict is recorded:
- the cited page must be fetchable through the polite client (robots-checked);
- harvest's classifier must not find a prohibition in it or classify it as unreadable;
- the quoted clause, required for `allowed`, must appear in the page's visible text (case- and
  whitespace-insensitive).

Otherwise the call fails and the verdict stays where it was. The verification is stored with the verdict
(`lane_detail.policy.verified`). Operator verdicts are not re-fetched.

**Tests.** `test_agent_allowed_verdict_needs_the_quoted_clause_on_the_cited_page`, `test_operator_verdicts_are_not_refetched`
and the updated `test_agent_terms_verdict_must_cite_the_sources_own_site`.

### O7 (low): resource limits

**Now.**
- **Exports stream.** CSV and JSONL write one query page (2000 rows) at a time. Parquet makes one pass to settle a
  fixed schema, then writes batches.
- **`probe_url(grep=…)` is bounded** (`grepsafe.py`):
  - patterns are limited to 300 characters and compiled up front;
  - matching runs in a spawned process that is killed at `HARVEST_GREP_TIMEOUT_S` (3 s);
  - matches are counted up to 10,000.

**Tests.** `test_exports_stream`: CSV, JSONL and Parquet with 30,000 rows under a 20 MB peak, with the Parquet types
checked. `test_probe_grep_is_bounded`: a catastrophic pattern is cut off.

### S16 (low): CSV formula injection

**Gap.** A scraped value such as `=HYPERLINK("http://…","click")` or `@SUM(…)` was written to CSV as is, and a
spreadsheet opening the export would evaluate it. The UI end-to-end run found this.

**Now.**
- CSV export (`export.csv_safe`, also `to_csv_text`) prefixes a text cell that starts with `=`, `+`, `-`, `@`, a tab
  or a CR with a single quote, the OWASP defence. Numbers stay numbers.
- An explicit opt-out writes values unchanged: `--raw` (CLI), `raw=true` (MCP `harvest_export`, `GET …/export`). The
  export result records `raw`.
- JSONL and Parquet are unchanged.

**Tests.** `test_csv_export_neutralises_formula_cells` and `test_raw_csv_is_an_explicit_choice_on_every_surface`.

## Residual risk

- **O5, the plugin's interactive agents.**
  - `harvest-builder` and `harvest-repair` in the Claude Code plugin list `Read`. Plugin agent frontmatter names
    tools, not path rules, and `Edit` needs a prior `Read`, so `Read` cannot be scoped there.
  - The plugin's MCP server is shared by the user's whole session, so it cannot tell a subagent from the user and
    acts as the operator.
  - Mitigations: these agents run interactively, and Claude Code asks before reading outside the working directory.
    The headless agents, which run unattended, are fully scoped (S3, S4). For unattended work, use
    `harvest agent …` (headless), not the plugin agents.
- **Containers run the policy layer.** Under Docker's default profile bubblewrap cannot run. The container is then
  the OS boundary, and the module child shares the container with the worker, including the data volume. The audit
  hook refuses reads of `$HARVEST_HOME`, but that is a Python-level control. For bubblewrap isolation, run workers
  on a host or VM, or use a sandboxed container runtime (gVisor, Kata).
- **The policy layer alone (macOS, `HARVEST_SANDBOX=policy`)** is Python-level and has no OS boundary. Since O1,
  though, the child holds no client, no proxy credentials and no network.
- **Postgres advisory locks** follow the session. A worker that hangs but stays connected keeps its sources until
  its connection ends. That is safe (never two walks) but can delay a source; restart hung workers.
- **Agent verdicts.** O6 proves the quoted clause is on the cited page. It cannot prove the clause means what the
  agent says, so `allowed` verdicts on important sources still deserve an operator's look.
