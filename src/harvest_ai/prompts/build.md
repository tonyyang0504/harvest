# harvest scraper build: source `{{source_id}}` of project `{{project}}`

You write ONE scraper module, `{{module_path}}`, that returns `{{record_type}}` records for {{target}}.
You may edit only that file.

## Steps
1. `harvest_list_sources(project="{{project}}")`: read this source's row (url, lane, lane_detail, terms, robots).
   If the lane is missing, call `harvest_detect_lane`. If the lane is `none`, stop and report why. Never work
   around robots.txt, terms, logins, captchas or paywalls.
1b. If `terms_status` is `unknown`, find the site's terms of use (footer links: terms, conditions, legal, termos,
   условия ...), read the clauses on automated access / scraping / data collection, and record your verdict with
   `harvest_record_policy` (`allowed` or `no_clause` with the terms URL and a clause of at most 25 words in the
   original language, or `forbids`). If it forbids collection, stop: the source stays lane `none`.
2. `harvest_template_scraper(project, source_id)` writes the starting module for the lane and returns the
   contract and the template fields. Read the module.
3. Inspect the site with `harvest_probe_url` (polite, robots-checked): page 1, page 2, the item markup or
   embedded JSON. Find the pagination parameter and a stable item id.
4. Implement `fetch(page, *, http, ctx)`:
   - Use only `http.get_text`, `http.get_json`, `http.post_json` or `http.render`, plus `harvest_ai.extract` helpers.
   - Return a list of dicts keyed by template field names. Raw strings are fine; the normaliser parses
     numbers, currencies, areas, distances, dates and periods.
   - Return `[]` for an empty or blocked page. Return `{"rows": [...], "done": True}` on the last page.
   - No sleeps, retries, logins, cookies, subprocess, file writes or other HTTP clients. The review rejects them.
   - `source_id` must be the site's stable item id. `url` must be the item's own page.
   - Pass the price as the site's text (`"From RM799 / Seat / Month"`) rather than a bare number, or set the
     template's basis field yourself (`price_basis`: per_person for a seat, desk or pax; per_area for psf or per m²).
     A per-seat or per-area figure is never the rent of the whole space.
   - Keep only {{target}} in {{regions}}: skip items for other countries, and items the site marks sold or
     out of stock unless the template has an availability field.
   If the detected lane is not the best one (e.g. a feed that only carries the 10 newest items of every category),
   build on the better public surface and record what you built on with
   `harvest_record_policy(project, source_id, lane=<lane>, reason=<why>)`. Lanes: `json_api` (a public JSON endpoint
   that needs no key or login: the site's own list API, Shopify `products.json`, WooCommerce Store API, WordPress
   REST), `openapi` (a documented API), `feed`, `sitemap`, `embedded_json` (data inside the HTML), `html`, `browser`.
5. `harvest_review_source(project, source_id)` runs lint, a sandboxed page-1 fetch and schema validation.
   Fix and re-run until the verdict is `pass`. Read `schema.quarantine_reasons` and `field_coverage`: map every
   field the site shows.
6. Reply with one line: `{{source_id}}: pass|fail — <rows> rows, <coverage summary>`. Do not enable the source.
   Enabling is the operator's decision.
{{extra}}
