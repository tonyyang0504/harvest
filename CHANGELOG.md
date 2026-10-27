# Changelog

All notable changes to harvest-ai are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.2] - 2026-10-27

### Removed
- robots.txt and website terms of service are no longer rules in harvest. The HTTP client no longer reads robots.txt
  before a request or a render (no `robots_denied` stat or event, no `respect_robots` / `max_crawl_delay` options, no
  Crawl-delay raising the host delay), and `robots.py` is gone. Lane detection no longer looks for or classifies terms
  pages, and neither robots.txt nor terms can make a lane `none`; robots.txt is read only for its `Sitemap:` lines.
- The terms-verdict tooling: MCP tool `harvest_record_policy`, CLI `harvest policy`, web `POST
  /api/projects/{p}/sources/{id}/policy`, and the auditor's and builder's instructions to read and record terms.

### Changed
- Enabling a source needs only a lane that is not `none` and a passing review of the current module sha.
- The residential-proxy route and the browser user-agent mode no longer require robots.txt or a terms verdict to
  allow collection. They still need the operator decision (reason and date), stay within the proxy budgets, are used
  only after an IP-level block (proxy), and are never used for a challenge, login, captcha or paywall.
- Lane overrides are a separate command: MCP `harvest_set_lane`, CLI `harvest lane <project> <id> <lane>`, web
  `POST /api/projects/{p}/sources/{id}/lane`. An agent still cannot open a source whose lane is `none` or undetected.
- The default user agent no longer claims "robots.txt honoured".
- Lane `none` still covers a login wall, captcha, paywall, bot challenge or block (including challenge pages served
  with HTTP 200), or no usable route.
- The `sources` table keeps its `robots_status` / `terms_*` columns so older databases open; nothing reads or writes them.

## [0.1.1] - 2026-10-05

### Changed
- README (also the PyPI page): install from PyPI; the "not published yet" notes are gone.

## [0.1.0] - 2026-10-04

First public release.

### Changed
- Distribution name `harvest-ai` (the PyPI name `harvest` belongs to an unrelated project) and import package
  `harvest_ai`, so both can be installed side by side. The `harvest` command stays; `harvest-ai` is an alias;
  `harvest-mcp` starts the MCP server. Scraper modules import `harvest_ai.extract` / `harvest_ai.mcp_client`; the review
  gate refuses the old `harvest` import name with a clear finding.
- Licence: Apache-2.0 (was all rights reserved).
- systemd examples no longer assume an install path.

### Added
- Project files: LICENSE and NOTICE, contributing guide with DCO sign-off, code of conduct, security policy, issue and
  pull request templates, release workflow (PyPI trusted publishing), CodeQL, secret scanning, Dependabot.
