# harvest repair: source `{{source_id}}` of project `{{project}}`

The watchdog flagged this source (zero rows, degraded against its baseline, stuck or failing).
Diagnose the root cause. When a fix inside `{{module_path}}` is safe, make it and verify it. Otherwise report.

1. `harvest_list_sources` gives the row, the last runs and errors. `harvest_probe_url` opens the listing page politely.
2. Classify the root cause as one of: layout_change, endpoint_change, parser_bug, block, robots_disallow,
   terms_change, login_or_paywall, dead, rate_limited.
3. Fix only layout_change, endpoint_change or parser_bug, and only in `{{module_path}}`. Keep the contract
   `fetch(page, *, http, ctx)`. Then run `harvest_review_source` until it passes.
4. For block, robots, terms, login, paywall or dead, do **not** work around it. Report and recommend lane `none`.
5. Reply with one line: `{{source_id}}: <class> — fixed|not_fixed — review: <verdict>`.

Never log in, rotate identities, add cookies or proxies, or edit anything but the module.
{{extra}}
